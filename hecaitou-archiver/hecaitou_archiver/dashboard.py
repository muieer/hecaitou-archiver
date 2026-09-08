"""Loopback-only HTTP UI and durable daily scheduler. Browser lifetime is irrelevant."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import bleach
import markdown

from .analysis import strict_json, validate_result
from .credentials import DEFAULT_KEY_FILE
from .cli import ArchiveError, STATE_DIR, positive_seconds

STATIC = Path(__file__).with_name("static")


def iso(timestamp):
    return datetime.fromtimestamp(timestamp).astimezone().isoformat()


def validate_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("每日时间必须为 HH:mm，例如 14:30")
    return value


def next_due(now, daily_time, last_auto_date=None):
    """Strictly future local time; skip nonexistent DST minutes and already-attempted dates."""
    hour, minute = map(int, validate_time(daily_time).split(":"))
    for offset in range(370):
        date = datetime.fromtimestamp(now).date() + timedelta(days=offset)
        if last_auto_date and date.isoformat() <= last_auto_date:
            continue
        candidate = datetime(date.year, date.month, date.day, hour, minute, fold=0).timestamp()
        if candidate > now and datetime.fromtimestamp(candidate).strftime("%H:%M") == daily_time:
            return candidate
    raise ValueError("系统时钟与上次调度日期相差过大，请检查系统时间")


@contextmanager
def service_lock(root):
    state = root / STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    with (state / "dashboard.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("此存档目录已有管理服务运行，请打开已有页面")
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_pipeline(root, key_file, analysis_timeout):
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "archive.py"),
               "--output-dir", str(root), "--ark-key-file", str(key_file),
               "--analysis-timeout", str(analysis_timeout), "--lock-timeout", "0.1"]
    try:
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                 timeout=180 + 4 * analysis_timeout)
        result = strict_json(process.stdout)
        if not isinstance(result, dict) or result.get("status") not in {
                "saved", "skipped_existing", "skipped_not_today", "failed"}:
            raise ValueError("存档程序未返回有效运行结果")
        if (process.returncode == 0) != (result["status"] != "failed"):
            raise ValueError("存档程序退出码与结果不一致")
        return result
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": {"code": "pipeline_timeout", "message": "本次执行超过总时限，正文已保存的部分会保留"}}
    except (OSError, ValueError) as exc:
        return {"status": "failed", "error": {"code": "pipeline_error", "message": str(exc)}}


class Controller:
    def __init__(self, root, runner, clock=time.time):
        self.root, self.runner, self.clock = root, runner, clock
        state = root / STATE_DIR
        state.mkdir(parents=True, exist_ok=True)
        self.db_path = state / "dashboard.sqlite3"
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.worker = None
        self.scheduler = None
        self.active_id = None
        self.fault = None
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, daily_time TEXT NOT NULL, last_auto_date TEXT)")
            db.execute("INSERT OR IGNORE INTO config VALUES (1, 0, '14:30', NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, trigger TEXT, started REAL, finished REAL, status TEXT, message TEXT, result TEXT)")
            db.execute("UPDATE runs SET finished=?, status='failed', message='管理服务上次中断，未确认执行结果；可查看文章或再次执行' WHERE status='running'", (clock(),))
        config = self.config()
        self.due = next_due(clock(), config["time"], config["last_auto_date"]) if config["enabled"] else None
        self.last_tick = clock()

    def db(self):
        # SQLite's transaction context commits/rolls back; close connection after use.
        @contextmanager
        def connection():
            db = sqlite3.connect(self.db_path, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()
        return connection()

    def config(self):
        with self.db() as db:
            row = db.execute("SELECT * FROM config WHERE id=1").fetchone()
        validate_time(row["daily_time"])
        return {"enabled": bool(row["enabled"]), "time": row["daily_time"], "last_auto_date": row["last_auto_date"]}

    def configure(self, patch):
        if not isinstance(patch, dict) or not patch or set(patch) - {"enabled", "time"}:
            raise ValueError("只接受 enabled 和 time 配置")
        if "enabled" in patch and type(patch["enabled"]) is not bool:
            raise ValueError("调度开关必须为布尔值")
        if "time" in patch:
            validate_time(patch["time"])
        with self.lock:
            config = self.config()
            updated = dict(config, **patch)
            due = next_due(self.clock(), updated["time"], config["last_auto_date"]) if updated["enabled"] else None
            with self.db() as db:
                db.execute("UPDATE config SET enabled=?, daily_time=? WHERE id=1", (int(updated["enabled"]), updated["time"]))
            self.due, self.last_tick, self.fault = due, self.clock(), None
        return self.snapshot()

    def _record(self, trigger, status, message, now, result=None):
        with self.db() as db:
            cursor = db.execute("INSERT INTO runs(trigger,started,finished,status,message,result) VALUES(?,?,?,?,?,?)",
                                (trigger, now, None if status == "running" else now, status, message,
                                 None if result is None else json.dumps(result, ensure_ascii=False)))
            return cursor.lastrowid

    def start_run(self, trigger="manual"):
        with self.lock:
            if self.stop.is_set():
                raise RuntimeError("服务正在停止")
            if self.active_id is not None:
                if trigger == "scheduled":
                    self._record(trigger, "skipped", "已有任务执行中，本次自动调度跳过", self.clock())
                return False
            run_id = self._record(trigger, "running", "正在检查文章并生成分析", self.clock())
            self.active_id = run_id
            self.worker = threading.Thread(target=self._work, args=(run_id,), name="article-runner")
            try:
                self.worker.start()
            except BaseException:
                self.active_id = None
                with self.db() as db:
                    db.execute("UPDATE runs SET status='failed', finished=?, message='执行线程启动失败' WHERE id=?", (self.clock(), run_id))
                raise
            return True

    def _work(self, run_id):
        try:
            result = self.runner()
            status = result.get("status")
            error = result.get("error") or {}
            if status == "saved":
                outcome, message = "saved", "成功产生结果"
            elif status == "skipped_existing":
                outcome, message = "skipped", "正文与分析均已存在，正常跳过"
            elif status == "skipped_not_today":
                outcome, message = "skipped", "最新文章不是当天发布，正常跳过"
            elif error.get("code") == "lock_timeout":
                outcome, message = "skipped", "另一个存档任务正在执行，本次跳过"
            else:
                outcome, message = "failed", error.get("message", "执行失败，请查看运行日志")
        except Exception as exc:
            outcome, message, result = "failed", str(exc), None
        with self.lock:
            try:
                with self.db() as db:
                    db.execute("UPDATE runs SET finished=?,status=?,message=?,result=? WHERE id=?",
                               (self.clock(), outcome, message, json.dumps(result, ensure_ascii=False), run_id))
                    db.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 100)")
            except Exception as exc:
                self.fault = f"无法保存最近执行记录：{exc}"
                logging.exception(self.fault)
            finally:
                self.active_id = None

    def tick(self, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            gap = now - self.last_tick
            self.last_tick = now
            config = self.config()
            if not config["enabled"]:
                self.due = None
                return
            if self.due is None or gap < 0:
                self.due = next_due(now, config["time"], config["last_auto_date"])
            if now < self.due:
                return
            due = self.due
            date = datetime.fromtimestamp(due).date().isoformat()
            if date == config["last_auto_date"]:
                self.due = next_due(now, config["time"], date)
                return
            with self.db() as db:
                # Persist before dispatch: a restart must not repeat today's automatic attempt.
                db.execute("UPDATE config SET last_auto_date=? WHERE id=1", (date,))
            self.due = next_due(now, config["time"], date)
            if gap > 60 or now - due >= 60:
                self._record("scheduled", "skipped", "错过调度时刻（休眠或服务停顿），不补跑", now)
            else:
                self.start_run("scheduled")

    def start_scheduler(self):
        def loop():
            while not self.stop.wait(1):
                try:
                    self.tick()
                except Exception as exc:
                    with self.lock:
                        self.fault = f"调度异常：{exc}"
                    logging.exception("Scheduler failed")
        self.scheduler = threading.Thread(target=loop, name="daily-scheduler", daemon=True)
        self.scheduler.start()

    def snapshot(self):
        with self.lock:
            config = self.config()
            with self.db() as db:
                row = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
                current = db.execute("SELECT * FROM runs WHERE id=?", (self.active_id,)).fetchone() if self.active_id else None
            def convert(row):
                if row is None:
                    return None
                data = dict(row)
                data["started"] = iso(data["started"])
                data["finished"] = iso(data["finished"]) if data["finished"] else None
                data["result"] = json.loads(data["result"]) if data["result"] else None
                return data
            return {"config": {"enabled": config["enabled"], "time": config["time"]},
                    "timezone": datetime.now().astimezone().tzname(),
                    "utc_offset": datetime.now().astimezone().strftime("%z"),
                    "next_run": iso(self.due) if self.due and config["enabled"] else None,
                    "running": current is not None, "current_run": convert(current),
                    "last_run": convert(row), "scheduler_error": self.fault, "server_time": iso(self.clock())}

    def close(self):
        self.stop.set()
        if self.scheduler:
            self.scheduler.join(3)
        if self.worker and self.worker.is_alive():
            self.worker.join()  # The subprocess runner has its own finite total timeout.


def render_body(text):
    body = text.split("\n", 1)[1] if text.startswith("# ") and "\n" in text else text
    return bleach.clean(markdown.markdown(body, extensions=["fenced_code", "tables", "sane_lists"]),
                        tags={"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "del",
                              "blockquote", "ul", "ol", "li", "pre", "code", "a", "table", "thead", "tbody", "tr", "th", "td"},
                        attributes={"a": ["href", "title"], "ol": ["start"]},
                        protocols={"http", "https", "mailto"}, strip=True)


def latest_article(root, snapshot):
    candidates, warnings = [], []
    for folder in root.iterdir():
        if not folder.is_dir() or folder.is_symlink() or folder.name in (STATE_DIR, "logs"):
            continue
        record = folder / ".archive.json"
        if not record.exists():
            continue
        try:
            data = strict_json(record.read_text(encoding="utf-8"))
            published = datetime.fromisoformat(data["published"])
            if not isinstance(data["title"], str) or not isinstance(data["source_url"], str):
                raise ValueError("元数据格式异常")
            candidates.append((published.timestamp(), folder, data))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            warnings.append(f"无法读取存档 {folder.name}：{exc}")
    if not candidates:
        return {"article": None, "warnings": warnings}
    _, folder, data = max(candidates, key=lambda item: (item[0], item[1].name))
    source = data["source_url"]
    if urlsplit(source).scheme not in ("https", "http"):
        source = None
    article = {"title": data["title"], "published_date": data.get("published_date"), "source_url": source,
               "html": None, "body_error": None, "analysis": None, "analysis_state": "pending", "analysis_message": "分析尚未生成", "model": None}
    try:
        body_path = folder / "正文.md"
        if body_path.is_symlink():
            raise ValueError("正文不能为软链接")
        raw = body_path.read_bytes()
        article["html"] = render_body(raw.decode("utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        article["body_error"] = str(exc)
    try:
        analysis_path = folder / "分析.json"
        if analysis_path.is_symlink():
            raise ValueError("分析不能为软链接")
        if analysis_path.exists():
            article.update(analysis=validate_result(strict_json(analysis_path.read_text(encoding="utf-8"))),
                           analysis_state="ready", analysis_message=None)
            # Model metadata is optional; it must not block reading the saved result.
            try:
                metadata = strict_json((folder / ".analysis.json").read_text(encoding="utf-8"))
                if isinstance(metadata, dict) and isinstance(metadata.get("model"), str):
                    article["model"] = metadata["model"]
            except (OSError, ValueError, ArchiveError):
                pass
        else:
            for run in (snapshot.get("current_run"), snapshot.get("last_run")):
                result = run.get("result") if run else None
                if result and result.get("source_url") == data["source_url"] and run["status"] == "failed":
                    article.update(analysis_state="failed", analysis_message=run["message"])
            if snapshot.get("running"):
                article.update(analysis_state="pending", analysis_message="任务执行中，分析生成后会自动显示")
    except ArchiveError as exc:
        article.update(analysis_state="failed", analysis_message=str(exc))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        article.update(analysis_state="failed", analysis_message=f"无法读取分析文件：{exc}")
    return {"article": article, "warnings": warnings}


def make_server(controller, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logging.info("http %s", fmt % args)

        def send(self, code, payload, content_type="application/json; charset=utf-8"):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if content_type.startswith("application/json") else payload
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'none'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(raw)

        def check_local(self, mutation=False):
            host = self.headers.get("Host", "")
            if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
                raise PermissionError("仅允许本地页面访问")
            if mutation and (self.headers.get("X-Local-Client") != "1" or
                             self.headers.get("Origin", f"http://{host}") != f"http://{host}"):
                raise PermissionError("请从本地管理页面提交操作")

        def do_GET(self):
            try:
                self.check_local()
                path = urlsplit(self.path).path
                if path == "/api/state":
                    snapshot = controller.snapshot()
                    self.send(200, dict(snapshot, **latest_article(controller.root, snapshot)))
                elif path in ("/", "/app.js", "/style.css"):
                    filename, mime = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}[path]
                    self.send(200, (STATIC / filename).read_bytes(), mime + "; charset=utf-8")
                else:
                    self.send(404, {"error": "页面不存在"})
            except PermissionError as exc:
                self.send(403, {"error": str(exc)})
            except Exception as exc:
                logging.exception("GET failed")
                self.send(500, {"error": str(exc)})

        def do_POST(self):
            try:
                self.check_local(mutation=True)
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    raise ValueError("请求必须为 JSON")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError("请求大小无效")
                data = strict_json(self.rfile.read(length).decode("utf-8"))
                if self.path == "/api/config":
                    self.send(200, controller.configure(data))
                elif self.path == "/api/run":
                    if data != {}:
                        raise ValueError("立即执行不接受额外参数")
                    if controller.start_run():
                        self.send(202, controller.snapshot())
                    else:
                        self.send(409, {"error": "已有任务执行中，请等待本次完成"})
                else:
                    self.send(404, {"error": "接口不存在"})
            except PermissionError as exc:
                self.send(403, {"error": str(exc)})
            except (ValueError, UnicodeError) as exc:
                self.send(400, {"error": str(exc)})
            except Exception as exc:
                logging.exception("POST failed")
                self.send(500, {"error": str(exc)})

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地文章管理页面与每日调度，分析使用火山方舟云端模型")
    parser.add_argument("--output-dir", required=True, help="与存档工具相同的保存根目录；相对路径基于当前工作目录")
    parser.add_argument("--port", type=int, default=8765, help="本地页面端口，默认 8765")
    parser.add_argument("--ark-key-file", type=Path, default=DEFAULT_KEY_FILE, help="Ark API Key txt 文件")
    parser.add_argument("--analysis-timeout", type=positive_seconds, default=300)
    args = parser.parse_args(argv)
    if not args.output_dir.strip() or not 1 <= args.port <= 65535:
        parser.error("保存目录不能为空，端口须在 1–65535 之间")
    root = Path(args.output_dir).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
        with service_lock(root):
            logging.basicConfig(filename=str(root / STATE_DIR / "dashboard.log"), level=logging.INFO,
                                format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
            controller = Controller(root, lambda: run_pipeline(root, args.ark_key_file, args.analysis_timeout))
            try:
                server = make_server(controller, args.port)
                controller.start_scheduler()
                def stop_service(signum, frame):
                    raise KeyboardInterrupt()
                signal.signal(signal.SIGTERM, stop_service)
                print(f"本地页面：http://127.0.0.1:{args.port}\n关闭浏览器不影响调度；退出此服务后调度停止。", flush=True)
                try:
                    server.serve_forever(poll_interval=0.5)
                except KeyboardInterrupt:
                    print("正在停止服务；若任务仍在执行，将等待本次完成。", flush=True)
                finally:
                    server.server_close()
            finally:
                controller.close()
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
