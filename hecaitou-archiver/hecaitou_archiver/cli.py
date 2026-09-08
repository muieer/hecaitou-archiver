"""Single-shot article archive and cloud Ark analysis; no interactive prompts."""
import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter


FEED_URL = "https://www.hecaitou.com/feeds/posts/default?alt=json&max-results=1&orderby=published"
SITE_HOSTS = {"www.hecaitou.com", "hecaitou.com"}
STATE_DIR = ".hecaitou-state"
RECORD_NAME = ".archive.json"


class ArchiveError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ArchiveError("argument_error", message)


def positive_seconds(value):
    try:
        result = float(value)
        if not math.isfinite(result) or not 0 < result <= 600:
            raise ValueError()
        return result
    except ValueError:
        raise argparse.ArgumentTypeError("必须是大于 0 且不超过 600 的有限秒数")


class RunLog:
    """Each process writes its own UTF-8 JSONL file; write failures propagate."""
    def __init__(self, root, started):
        logdir = root / "logs"
        logdir.mkdir(exist_ok=True)
        name = started.strftime("%Y%m%dT%H%M%S.%f") + "-" + uuid.uuid4().hex + ".jsonl"
        self.file = (logdir / name).open("x", encoding="utf-8")

    def emit(self, event, **fields):
        self.file.write(json.dumps(dict(time=datetime.now().astimezone().isoformat(),
                                        event=event, **fields), ensure_ascii=False) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()


def fetch(url, timeout, log):
    """Two attempts max. A child process bounds DNS, TLS, redirects, and slow bodies."""
    for attempt in (1, 2):
        log.emit("request", url=url, attempt=attempt, timeout_seconds=timeout)
        error_code = "network_error"
        try:
            worker = Path(__file__).with_name("_http.py").resolve()
            proc = subprocess.run([sys.executable, str(worker), url, str(timeout)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            if proc.returncode == 0:
                log.emit("request_success", url=url, attempt=attempt, bytes=len(proc.stdout))
                return proc.stdout
            reason = proc.stderr.decode("utf-8", errors="replace").strip() or f"请求子进程退出：{proc.returncode}"
            if proc.returncode == 3:
                error_code = "response_too_large"
        except subprocess.TimeoutExpired:
            # subprocess.run kills and reaps the worker before raising.
            reason = f"请求超过 {timeout:g} 秒总时限（包含 DNS、连接、重定向和读取）"
        except OSError as exc:
            reason = f"{type(exc).__name__}: {exc}"
        log.emit("request_failed", url=url, attempt=attempt, reason=reason, retry=attempt == 1)
        if attempt == 2:
            raise ArchiveError(error_code, f"请求失败（已尝试 2 次）：{url}；{reason}")


@dataclass(frozen=True)
class Article:
    identity: str
    title: str
    published: datetime
    source_url: str

    @property
    def local_date(self):
        # astimezone() consults the OS zone for the publication instant, including DST.
        # A timezone-free timestamp is interpreted as local time by Python.
        return self.published.astimezone().date().isoformat()


def parse_latest(payload):
    try:
        data = json.loads(payload)
        entries = data["feed"]["entry"]
        if not isinstance(entries, list) or len(entries) != 1:
            raise ValueError("订阅源没有唯一的最新文章，拒绝猜测或改抓其他文章")
        item = entries[0]
        identity = item["id"]["$t"]
        title = item["title"]["$t"]
        published = item["published"]["$t"]  # Never use 'updated'.
        links = [link["href"] for link in item["link"]
                 if link.get("rel") == "alternate" and link.get("type") == "text/html"]
        if not isinstance(identity, str) or not re.fullmatch(r"tag:blogger.com,1999:blog-\d+\.post-\d+", identity):
            raise ValueError("文章 ID 缺失或格式不受支持")
        if not isinstance(title, str) or not title.strip() or "\n" in title or "\r" in title:
            raise ValueError("文章标题缺失或包含异常换行")
        if len(links) != 1:
            raise ValueError("文章链接缺失或不唯一")
        parsed = urllib.parse.urlsplit(links[0])
        if parsed.scheme != "https" or parsed.hostname not in SITE_HOSTS or parsed.username or parsed.port not in (None, 443):
            raise ValueError("文章链接不属于目标网站 HTTPS 地址")
        return Article(identity, title, datetime.fromisoformat(published.replace("Z", "+00:00")), links[0])
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ArchiveError("parse_error", f"最新文章元数据解析失败：{exc}") from exc


class BodyConverter(MarkdownConverter):
    def convert_img(self, el, text, parent_tags):
        return ""


def article_markdown(payload, article):
    soup = BeautifulSoup(payload, "html.parser", from_encoding="utf-8")
    post_id = article.identity.rsplit("-", 1)[1]
    bodies = soup.select(f".post-body[id='post-body-{post_id}']")
    if len(bodies) != 1:
        raise ArchiveError("parse_error", "文章页缺少与文章 ID 对应的唯一正文，网站结构可能已变化")
    body = bodies[0]
    post = body.find_parent(class_="post")
    title_node = post.select_one(".post-title") if post else None
    if title_node is None or title_node.get_text(strip=True) != article.title.strip():
        raise ArchiveError("parse_error", "文章页标题与订阅源不一致，请稍后重新调用")
    for node in body.select("script, style, noscript, template, img, picture, svg, canvas, iframe, video, audio, nav, aside, form, .comments, .post-footer"):
        node.decompose()
    for link in list(body.find_all("a")):
        if not link.get_text(strip=True):
            link.decompose()  # Also removes anchors that used to wrap only an image.
            continue
        href = urllib.parse.urljoin(article.source_url, link.get("href", ""))
        if urllib.parse.urlsplit(href).scheme not in {"http", "https", "mailto"}:
            link.unwrap()
        else:
            link["href"] = href
    if not body.get_text(strip=True):
        raise ArchiveError("parse_error", "去除图片后正文为空，未创建存档")
    markdown = BodyConverter(heading_style="ATX", bullets="-", wrap=False,
                             newline_style="spaces").convert_soup(body).strip()
    if not markdown:
        raise ArchiveError("parse_error", "正文 Markdown 转换结果为空")
    return f"# {article.title}\n\n{markdown}\n"


@contextmanager
def root_lock(root, timeout):
    state = root / STATE_DIR
    state.mkdir(exist_ok=True)
    # Never unlink this file: every process must lock the same inode.
    with (state / "archive.lock").open("a+b") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise ArchiveError("lock_timeout", "等待同一保存目录的其他运行结束超时，可稍后重试")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            yield state
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def existing_archive(root, article):
    for folder in root.iterdir():
        if folder.name in {"logs", STATE_DIR} or not folder.is_dir() or folder.is_symlink():
            continue
        record_path = folder / RECORD_NAME
        if not record_path.exists():
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError("元数据不是对象")
            if record.get("identity") != article.identity:
                continue
            output = folder / "正文.md"
            if record.get("schema_version") != 1 or record.get("status") != "complete" or output.is_symlink():
                raise ValueError("不完整或不受支持的存档记录")
            content = output.read_bytes()
            if not content or hashlib.sha256(content).hexdigest() != record.get("sha256"):
                raise ValueError("正文缺失、为空或校验值不一致")
            return output
        except (ValueError, OSError) as exc:
            raise ArchiveError("archive_damaged", f"无法验证已有存档 {folder}：{exc}；请先检查或移出损坏目录") from exc
    return None


def safe_title(title):
    name = unicodedata.normalize("NFC", title)
    name = "".join("_" if c in '<>:"/\\|?*' or unicodedata.category(c).startswith("C") else c for c in name)
    name = name.strip().rstrip(". ") or "无标题"
    return name.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(". ") or "无标题"


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_synced(path, content):
    with path.open("xb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def commit_archive(root, state, article, markdown, log):
    staging = state / "staging"
    staging.mkdir(exist_ok=True)
    # Only our private transaction directories are disposable; never article folders.
    for old in staging.glob("txn-*"):
        if old.is_dir() and not old.is_symlink():
            shutil.rmtree(old)
            log.emit("recovered_interrupted_transaction", path=str(old))
    temp = Path(tempfile.mkdtemp(prefix="txn-", dir=staging))
    try:
        content = markdown.encode("utf-8")
        write_synced(temp / "正文.md", content)
        record = dict(schema_version=1, status="complete", identity=article.identity,
                      title=article.title, source_url=article.source_url,
                      published=article.published.isoformat(), published_date=article.local_date,
                      saved_at=datetime.now().astimezone().isoformat(),
                      sha256=hashlib.sha256(content).hexdigest())
        write_synced(temp / RECORD_NAME, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        fsync_directory(temp)
        base = f"{article.local_date} {safe_title(article.title)}"
        destination = root / base
        suffix = hashlib.sha256(article.identity.encode()).hexdigest()[:12]
        counter = 0
        while os.path.lexists(destination):
            counter += 1
            destination = root / f"{base} [{suffix}{'-' + str(counter) if counter > 1 else ''}]"
        # Same filesystem and same-root lock: body and success record become visible together.
        os.rename(temp, destination)
        fsync_directory(root)
        fsync_directory(staging)
        return destination / "正文.md"
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def execute(root, timeout, lock_timeout, started, result, log, key_file=None, analysis_timeout=300):
    with root_lock(root, lock_timeout) as state:
        # Fetch after acquiring the lock so queued calls check the current latest post.
        article = parse_latest(fetch(FEED_URL, timeout, log))
        result.update(title=article.title, published_date=article.local_date,
                      source_url=article.source_url)
        log.emit("article_checked", identity=article.identity, title=article.title,
                 source_url=article.source_url, published=article.published.isoformat(),
                 published_date=article.local_date)
        today = article.local_date == result["system_date"]
        log.emit("date_checked", is_today=today, system_date=result["system_date"],
                 published_date=article.local_date)
        if not today:
            result.update(status="skipped_not_today", archive_status="skipped_not_today")
            result.setdefault("analysis", {})["status"] = "skipped_not_today"
            log.emit("skipped", reason="最新文章发布日期不是运行开始时的系统日期")
            return
        existing = existing_archive(root, article)
        if existing:
            result.update(status="skipped_existing", archive_status="skipped_existing", output_path=str(existing))
            log.emit("skipped", reason="文章已成功保存且完整性验证通过", output_path=str(existing))
            output = existing
        else:
            markdown = article_markdown(fetch(article.source_url, timeout, log), article)
            output = commit_archive(root, state, article, markdown, log)
            result.update(status="saved", archive_status="saved", output_path=str(output))
            log.emit("saved", output_path=str(output))
        from .analysis import analyze_archive
        analyze_archive(output, key_file, analysis_timeout, result, log)
        if result["analysis"]["status"] == "saved":
            result["status"] = "saved"


def main(argv=None):
    started = datetime.now().astimezone()
    result = dict(status="failed", title=None, published_date=None,
                  system_date=started.date().isoformat(), source_url=None,
                  output_path=None, error=None, archive_status="not_started",
                  analysis=dict(status="not_started", output_path=None, model=None))
    log = None
    exit_code = 0
    try:
        from .credentials import DEFAULT_KEY_FILE
        parser = Parser(description="保存当天最新文章并用 火山方舟云端模型生成七项分析。stdout 仅输出一个 JSON 对象。")
        parser.add_argument("--output-dir", required=True, help="保存根目录；自动创建，支持 ~；相对路径基于调用时工作目录")
        parser.add_argument("--timeout", type=positive_seconds, default=20.0, help="单次请求超时秒数，失败重试一次（默认 20，最大 600）")
        parser.add_argument("--lock-timeout", type=positive_seconds, default=120.0, help="等待同目录运行锁的秒数（默认 120，最大 600）")
        parser.add_argument("--ark-key-file", type=Path, default=DEFAULT_KEY_FILE, help="Ark API Key 的 txt 文件；默认使用工具目录 secrets/ark_api_key.txt，相对路径基于调用目录")
        # Old already-running dashboard processes may still pass this flag. It never selects a provider.
        parser.add_argument("--lm-url", help=argparse.SUPPRESS)
        parser.add_argument("--analysis-timeout", type=positive_seconds, default=300.0, help="每次模型生成总超时秒数（默认 300，最大 600），网络失败最多尝试两次")
        args = parser.parse_args(argv)
        if not args.output_dir.strip():
            raise ArchiveError("argument_error", "--output-dir 不得为空")
        root = Path(args.output_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        log = RunLog(root, started)
        log.emit("run_started", started_at=started.isoformat(), system_date=result["system_date"],
                 timezone_name=started.tzname(), utc_offset=started.strftime("%z"),
                 tz_environment=os.environ.get("TZ"), output_dir=str(root))
        result["archive_status"] = "running"
        execute(root, args.timeout, args.lock_timeout, started, result, log, args.ark_key_file, args.analysis_timeout)
    except (ArchiveError, OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
        code = exc.code if isinstance(exc, ArchiveError) else (
            "interrupted" if isinstance(exc, KeyboardInterrupt) else "filesystem_error")
        result.update(status="failed", error=dict(code=code, message=str(exc) or "运行被中断"))
        exit_code = 2 if code == "argument_error" else 130 if code == "interrupted" else 1
    except Exception as exc:
        result.update(status="failed", error=dict(code="internal_error", message=f"{type(exc).__name__}: {exc}"))
        exit_code = 1
    finally:
        if result["archive_status"] == "running":
            result["archive_status"] = "failed"
        if log:
            try:
                log.emit("run_finished", **result)
                log.close()
            except OSError as exc:
                result.update(status="failed", error=dict(code="log_error", message=f"日志写入失败：{exc}"))
                exit_code = 1
    if result["error"]:
        print(json.dumps(result["error"], ensure_ascii=False), file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False))
    return exit_code
