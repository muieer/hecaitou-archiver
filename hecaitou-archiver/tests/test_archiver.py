import contextlib
from datetime import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from hecaitou_archiver import cli


PROJECT = Path(__file__).resolve().parents[1]
TITLE = '测试：文章 / A?'
BODY = '''<p>第一段，保留中文 &amp; 标点。</p><p>第二段<br>明确换行</p>
<blockquote><p>引用文字</p></blockquote>
<ol start="3"><li>有序一</li><li>有序二<ul><li>嵌套条目</li></ul></li></ol>
<p><a href="/reference">文字链接</a>和<strong>强调</strong>。</p>
<div><a href="https://images.example/pic.png"><img src="https://images.example/pic.png" alt="图片说明不保留"></a></div>
<script>脚本杂项</script><style>样式杂项</style><p>末段。</p>'''


def entry(identity="1", title=TITLE, published="2026-09-05T11:00:00+08:00"):
    return {"id": {"$t": f"tag:blogger.com,1999:blog-123.post-{identity}"},
            "title": {"$t": title}, "published": {"$t": published},
            "updated": {"$t": "2099-01-01T00:00:00Z"},
            "link": [{"rel": "alternate", "type": "text/html", "href": "https://www.hecaitou.com/2026/09/test.html"}]}


def feed(item=None):
    return json.dumps({"feed": {"entry": [item or entry()]}}, ensure_ascii=False).encode()


def page(identity="1", title=TITLE, body=BODY):
    return f'''<html><head><title>站点标题</title></head><body><nav>顶部导航</nav>
<div class="post"><h3 class="post-title">{title}</h3>
<div class="post-body" id="post-body-{identity}">{body}</div>
<div class="post-footer">作者与页脚</div></div><aside>侧栏</aside>
<div class="comments">评论区</div></body></html>'''.encode()


@contextlib.contextmanager
def timezone(name):
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


class SilentLog:
    def emit(self, *args, **kwargs):
        pass


class UnitTests(unittest.TestCase):
    def test_published_not_updated(self):
        with timezone("Asia/Shanghai"):
            self.assertEqual(cli.parse_latest(feed()).local_date, "2026-09-05")

    def test_timezone_conversion_and_naive(self):
        with timezone("Asia/Shanghai"):
            self.assertEqual(cli.parse_latest(feed(entry(published="2026-09-04T16:30:00Z"))).local_date, "2026-09-05")
            self.assertEqual(cli.parse_latest(feed(entry(published="2026-09-05T01:00:00"))).local_date, "2026-09-05")
        with timezone("America/Los_Angeles"):
            self.assertEqual(cli.parse_latest(feed(entry(published="2026-09-05T01:00:00+08:00"))).local_date, "2026-09-04")
            # DST uses the publication instant's offset, not today's fixed offset.
            self.assertEqual(cli.parse_latest(feed(entry(published="2026-01-05T07:30:00Z"))).local_date, "2026-01-04")

    def test_invalid_metadata_and_no_fallback(self):
        bad_items = []
        for key in ("published", "id", "title", "link"):
            item = entry()
            del item[key]
            bad_items.append(feed(item))
        bad_items += [b"not json", b'{"feed":{"entry":[]}}',
                      json.dumps({"feed": {"entry": [entry(), entry("2")]}}).encode(),
                      feed(entry(published="invalid")), feed(entry(title=""))]
        item = entry()
        item["link"][0]["href"] = "https://elsewhere.example/article"
        bad_items.append(feed(item))
        for payload in bad_items:
            with self.subTest(payload=payload), self.assertRaises(cli.ArchiveError) as caught:
                cli.parse_latest(payload)
            self.assertEqual(caught.exception.code, "parse_error")

    def test_markdown_structure_and_exclusions(self):
        md = cli.article_markdown(page(), cli.parse_latest(feed()))
        self.assertTrue(md.startswith(f"# {TITLE}\n\n"))
        for expected in ["第一段，保留中文 & 标点。", "\n\n第二段  \n明确换行", "> 引用文字",
                         "3. 有序一", "4. 有序二", "嵌套条目",
                         "[文字链接](https://www.hecaitou.com/reference)", "**强调**", "末段。"]:
            self.assertIn(expected, md)
        for excluded in ["![", "images.example", "图片说明不保留", "顶部导航", "侧栏", "评论区", "作者与页脚", "脚本杂项", "样式杂项"]:
            self.assertNotIn(excluded, md)
        self.assertLess(md.index("第一段"), md.index("第二段"))
        self.assertLess(md.index("第二段"), md.index("引用文字"))

    def test_missing_wrong_empty_article_rejected(self):
        for payload in [b"<html>maintenance</html>", page(identity="2"), page(title="wrong"), page(body="<img src='x'>")]:
            with self.subTest(payload=payload), self.assertRaises(cli.ArchiveError):
                cli.article_markdown(payload, cli.parse_latest(feed()))

    def test_safe_titles(self):
        for original in [' /:*?"<>|\\ ', '.', '中文' * 200, '\x00\n危险', 'a. ']:
            safe = cli.safe_title(original)
            self.assertTrue(safe)
            self.assertLessEqual(len(safe.encode()), 180)
            self.assertFalse(set(safe) & set('<>:"/\\|?*\x00\n'))
            self.assertFalse(safe.endswith(('.', ' ')))

    def test_failed_write_never_commits(self):
        with tempfile.TemporaryDirectory() as directory, timezone("Asia/Shanghai"):
            root = Path(directory)
            with cli.root_lock(root, 1) as state, patch.object(cli, "write_synced", side_effect=OSError("磁盘已满")):
                with self.assertRaises(OSError):
                    cli.commit_archive(root, state, cli.parse_latest(feed()), "# Test\n", SilentLog())
            self.assertFalse(list(root.glob("*/正文.md")))
            self.assertFalse(list((state / "staging").glob("txn-*")))

    def test_failure_after_commit_preserves_valid_archive(self):
        with tempfile.TemporaryDirectory() as directory, timezone("Asia/Shanghai"):
            root = Path(directory)
            article = cli.parse_latest(feed())
            real_sync = cli.fsync_directory

            def fail_root(path):
                if path == root:
                    raise OSError("fsync failure after rename")
                real_sync(path)

            with cli.root_lock(root, 1) as state, patch.object(cli, "fsync_directory", side_effect=fail_root):
                with self.assertRaises(OSError):
                    cli.commit_archive(root, state, article, "# Complete\n", SilentLog())
            self.assertIsNotNone(cli.existing_archive(root, article))

    def test_started_date_is_frozen_across_midnight(self):
        with tempfile.TemporaryDirectory() as directory, timezone("Asia/Shanghai"):
            root = Path(directory)
            started = datetime.fromisoformat("2026-09-05T23:59:59+08:00")
            result = dict(system_date="2026-09-05")
            # Publication is next day; simulated run start remains Sep 5 even if request finishes Sep 6.
            with patch.object(cli, "fetch", return_value=feed(entry(published="2026-09-06T00:00:01+08:00"))):
                cli.execute(root, 1, 1, started, result, SilentLog())
            self.assertEqual(result["status"], "skipped_not_today")


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = {"feed": feed(), "article": page(), "counts": {}, "fail": {}, "delay": {}, "truncate": {}}
        shared = cls.shared

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                route = self.path.split("?", 1)[0].strip("/")
                shared["counts"][route] = shared["counts"].get(route, 0) + 1
                count = shared["counts"][route]
                time.sleep(shared["delay"].get(route, 0))
                try:
                    if count <= shared["fail"].get(route, 0):
                        self.send_error(503, "test failure")
                        return
                    payload = shared.get(route, b"unknown")
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload) + shared["truncate"].get(route, 0)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "输出 根目录"
        self.shared.update(feed=feed(), article=page(), counts={}, fail={}, delay={}, truncate={})
        self.env = dict(os.environ, TZ="Asia/Shanghai",
                        ARCHIVER_TEST_SERVER=f"http://127.0.0.1:{self.server.server_port}")
        self.env.pop("ARCHIVER_TEST_FAULT", None)
        self.env.pop("ARCHIVER_TEST_LM", None)

    def command(self, *extra):
        return [sys.executable, str(PROJECT / "tests/worker.py"), "--output-dir", str(self.root), *extra]

    def invoke(self, *extra, fault=None):
        env = dict(self.env)
        if fault:
            env["ARCHIVER_TEST_FAULT"] = fault
        proc = subprocess.run(self.command(*extra), env=env, cwd=self.temp.name, capture_output=True, text=True, timeout=15)
        if fault:
            return proc
        result = json.loads(proc.stdout)  # Rejects stray logs and multiple JSON objects.
        self.assertEqual(set(result), {"status", "title", "published_date", "system_date", "source_url", "output_path", "error", "archive_status", "analysis"})
        self.assertEqual(proc.returncode == 0, result["status"] != "failed")
        return result

    def test_save_repeat_and_first_version(self):
        first = self.invoke()
        self.assertEqual(first["status"], "saved")
        path = Path(first["output_path"])
        self.assertTrue(path.is_absolute())
        initial = path.read_bytes()
        self.shared["article"] = page(body="<p>更新正文不得覆盖首次版本</p>")
        second = self.invoke()
        self.assertEqual(second["status"], "skipped_existing")
        self.assertEqual(second["output_path"], first["output_path"])
        self.assertEqual(path.read_bytes(), initial)
        self.assertEqual(self.shared["counts"], {"feed": 2, "article": 1})
        self.assertEqual(len(list(self.root.glob("*/正文.md"))), 1)
        self.assertFalse(list(self.root.rglob("*.png")))
        logs = list((self.root / "logs").glob("*.jsonl"))
        self.assertEqual(len(logs), 2)
        events = [json.loads(line) for log in logs for line in log.read_text().splitlines()]
        self.assertIn("date_checked", {e["event"] for e in events})
        start = next(e for e in events if e["event"] == "run_started")
        self.assertEqual(start["utc_offset"], "+0800")

    def test_yesterday_tomorrow_skip_no_history_no_page_request(self):
        for published in ["2026-09-04T23:59:00+08:00", "2026-09-06T00:00:00+08:00"]:
            self.shared["feed"] = feed(entry(published=published))
            result = self.invoke()
            self.assertEqual(result["status"], "skipped_not_today")
            self.assertIsNone(result["output_path"])
        self.assertNotIn("article", self.shared["counts"])
        self.assertFalse(list(self.root.glob("*/正文.md")))

    def test_same_day_newest_and_title_collision(self):
        first = self.invoke()
        self.shared["feed"] = feed(entry("2", published="2026-09-05T12:00:00+08:00"))
        self.shared["article"] = page(identity="2")
        second = self.invoke()
        self.assertEqual(second["status"], "saved")
        self.assertNotEqual(second["output_path"], first["output_path"])
        self.assertEqual(len(list(self.root.glob("*/正文.md"))), 2)
        self.assertEqual(self.invoke()["status"], "skipped_existing")

    def test_existing_untracked_folder_not_overwritten(self):
        with timezone("Asia/Shanghai"):
            folder = self.root / ("2026-09-05 " + cli.safe_title(TITLE))
        folder.mkdir(parents=True)
        (folder / "正文.md").write_text("用户原有内容")
        result = self.invoke()
        self.assertEqual(result["status"], "saved")
        self.assertNotEqual(Path(result["output_path"]).parent, folder)
        self.assertEqual((folder / "正文.md").read_text(), "用户原有内容")

    def test_network_retries_once_then_success(self):
        self.shared["fail"] = {"feed": 1, "article": 1}
        self.assertEqual(self.invoke()["status"], "saved")
        self.assertEqual(self.shared["counts"], {"feed": 2, "article": 2})

    def test_network_retries_once_then_fails(self):
        self.shared["fail"] = {"feed": 20}
        result = self.invoke()
        self.assertEqual(result["error"]["code"], "network_error")
        self.assertEqual(self.shared["counts"], {"feed": 2})
        self.assertFalse(list(self.root.glob("*/正文.md")))

    def test_failed_article_request_never_marks_success(self):
        self.shared["fail"] = {"article": 20}
        self.assertEqual(self.invoke()["error"]["code"], "network_error")
        self.assertEqual(self.shared["counts"], {"feed": 1, "article": 2})
        self.assertFalse(list(self.root.glob("*/正文.md")))
        self.shared["fail"] = {}
        self.assertEqual(self.invoke()["status"], "saved")

    def test_timeout_and_retry_are_bounded(self):
        self.shared["delay"] = {"feed": 0.7}
        start = time.monotonic()
        result = self.invoke("--timeout", "0.3")
        self.assertEqual(result["error"]["code"], "network_error")
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(self.shared["counts"], {"feed": 2})
        time.sleep(0.75)  # Let test server handlers finish before resetting shared fixtures.

    def test_truncated_http_response_retries_and_fails(self):
        self.shared["truncate"] = {"feed": 10}
        self.assertEqual(self.invoke()["error"]["code"], "network_error")
        self.assertEqual(self.shared["counts"], {"feed": 2})

    def test_parse_failure_does_not_try_another_article(self):
        self.shared["article"] = b"<html>login or unavailable</html>"
        self.assertEqual(self.invoke()["error"]["code"], "parse_error")
        self.assertEqual(self.shared["counts"], {"feed": 1, "article": 1})
        self.assertFalse(list(self.root.glob("*/正文.md")))

    def test_corrupt_archive_reported_not_overwritten(self):
        result = self.invoke()
        output = Path(result["output_path"])
        output.write_text("人为改坏的文件")
        second = self.invoke()
        self.assertEqual(second["error"]["code"], "archive_damaged")
        self.assertEqual(output.read_text(), "人为改坏的文件")
        self.assertEqual(self.shared["counts"]["article"], 1)

    def test_six_concurrent_processes_save_once(self):
        processes = [subprocess.Popen(self.command(), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(6)]
        results = []
        for proc in processes:
            stdout, stderr = proc.communicate(timeout=15)
            self.assertEqual(proc.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual([r["status"] for r in results].count("saved"), 1)
        self.assertEqual([r["status"] for r in results].count("skipped_existing"), 5)
        self.assertEqual(len({r["output_path"] for r in results}), 1)
        self.assertEqual(self.shared["counts"]["article"], 1)

    def test_kill_during_write_then_retry(self):
        proc = self.invoke(fault="during_write")
        self.assertEqual(proc.returncode, -9)
        self.assertFalse(list(self.root.glob("*/正文.md")))
        self.assertTrue(list((self.root / cli.STATE_DIR / "staging").glob("txn-*")))
        self.assertEqual(self.invoke()["status"], "saved")
        self.assertFalse(list((self.root / cli.STATE_DIR / "staging").glob("txn-*")))

    def test_kill_after_commit_then_skip(self):
        self.assertEqual(self.invoke(fault="after_commit").returncode, -9)
        self.assertEqual(self.invoke()["status"], "skipped_existing")
        self.assertEqual(len(list(self.root.glob("*/正文.md"))), 1)

    def test_lock_wait_timeout(self):
        self.root.mkdir()
        with cli.root_lock(self.root, 1):
            self.assertEqual(self.invoke("--lock-timeout", "0.05")["error"]["code"], "lock_timeout")
        self.assertFalse(self.shared["counts"])

    def test_different_output_root_is_independent(self):
        first = self.invoke()
        self.root = Path(self.temp.name) / "第二个目录"
        second = self.invoke()
        self.assertEqual(second["status"], "saved")
        self.assertNotEqual(first["output_path"], second["output_path"])

    def test_relative_path_resolves_against_caller(self):
        self.root = Path("相对目录")
        result = self.invoke()
        self.assertTrue(Path(result["output_path"]).is_relative_to(Path(self.temp.name).resolve()))
        self.assertTrue((Path(self.temp.name) / "相对目录" / "logs").is_dir())

    def test_real_entry_argument_errors_and_help(self):
        base = [sys.executable, str(PROJECT / "archive.py")]
        for args in [[], ["--unknown"], ["--output-dir", ""],
                     ["--output-dir", str(self.root), "--timeout", "nan"],
                     ["--output-dir", str(self.root), "--timeout", "-1"],
                     ["--output-dir", str(self.root), "--lock-timeout", "inf"]]:
            proc = subprocess.run(base + args, capture_output=True, text=True, cwd=self.temp.name)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(json.loads(proc.stdout)["error"]["code"], "argument_error")
            self.assertTrue(proc.stderr)
        proc = subprocess.run(base + ["--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--output-dir", proc.stdout)

    def test_unwritable_root_and_logs_report_json(self):
        self.root.parent.mkdir(exist_ok=True)
        self.root.write_text("这不是目录")
        self.assertEqual(self.invoke()["error"]["code"], "filesystem_error")
        self.root.unlink()
        self.root.mkdir()
        (self.root / "logs").write_text("占用日志目录名")
        self.assertEqual(self.invoke()["error"]["code"], "filesystem_error")
        (self.root / "logs").unlink()
        self.root.chmod(0o555)
        try:
            if os.geteuid() != 0:
                self.assertEqual(self.invoke()["error"]["code"], "filesystem_error")
        finally:
            self.root.chmod(0o755)


if __name__ == "__main__":
    unittest.main(verbosity=2)
