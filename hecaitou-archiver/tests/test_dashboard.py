from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from hecaitou_archiver import dashboard as d
from hecaitou_archiver.analysis import FIELDS
from test_archiver import timezone


def timestamp(value):
    return datetime.fromisoformat(value).timestamp()


class FakeClock:
    def __init__(self, value="2026-09-08T12:00:00+08:00"):
        self.value = timestamp(value)

    def __call__(self):
        return self.value


def archive(root, title, published, analysis=True):
    folder = root / (published[:10] + " " + title)
    folder.mkdir()
    raw = (f"# {title}\n\n正文第一段。\n\n第二段与[链接](https://example.com)。\n").encode()
    digest = hashlib.sha256(raw).hexdigest()
    metadata = {"title": title, "published": published, "published_date": published[:10],
                "status": "complete", "sha256": digest, "source_url": f"https://www.hecaitou.com/{title}.html"}
    (folder / ".archive.json").write_text(json.dumps(metadata))
    (folder / "正文.md").write_bytes(raw)
    if analysis:
        data = {key: title if key == "what" else "无" for key in FIELDS}
        result = json.dumps(data, ensure_ascii=False).encode()
        (folder / "分析.json").write_bytes(result)
        (folder / ".analysis.json").write_text(json.dumps({"schema_version": 1, "model": "fixture",
            "source_sha256": digest, "sha256": hashlib.sha256(result).hexdigest()}))
    return folder, metadata


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.zone = timezone("Asia/Shanghai")
        self.zone.__enter__()
        self.addCleanup(self.zone.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = FakeClock()
        self.calls = []
        def runner():
            self.calls.append(1)
            return {"status": "saved", "error": None}
        self.control = d.Controller(self.root, runner, self.clock)
        self.addCleanup(self.control.close)

    def wait(self):
        if self.control.worker:
            self.control.worker.join(2)
            self.assertFalse(self.control.worker.is_alive())

    def test_defaults_disabled_and_validation(self):
        self.assertFalse(self.control.snapshot()["config"]["enabled"])
        for change in [{"time": "24:00"}, {"time": "9:00"}, {"time": "12:30:00"}, {"enabled": "false"}, {}, {"extra": 1}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.control.configure(change)

    def test_future_and_past_times_no_immediate_run(self):
        self.control.configure({"enabled": True, "time": "11:00"})
        self.assertEqual(self.control.snapshot()["next_run"], "2026-09-09T11:00:00+08:00")
        self.assertEqual(self.calls, [])
        self.control.configure({"time": "13:07"})
        self.assertEqual(self.control.snapshot()["next_run"], "2026-09-08T13:07:00+08:00")

    def test_automatic_once_per_day_and_time_change_no_second_auto(self):
        self.control.configure({"enabled": True, "time": "12:01"})
        self.clock.value += 59
        self.control.tick()
        self.clock.value += 1
        self.control.tick()
        self.wait()
        self.control.tick()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.control.snapshot()["last_run"]["trigger"], "scheduled")
        self.control.configure({"time": "12:02"})
        self.assertEqual(self.control.snapshot()["next_run"], "2026-09-09T12:02:00+08:00")

    def test_resume_from_sleep_does_not_catch_up(self):
        self.control.configure({"enabled": True, "time": "12:01"})
        self.clock.value += 3600
        self.control.tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.control.snapshot()["last_run"]["status"], "skipped")
        self.assertEqual(self.control.snapshot()["next_run"], "2026-09-09T12:01:00+08:00")

    def test_restart_preserves_config_and_skips_past_due(self):
        self.control.configure({"enabled": True, "time": "12:01"})
        self.control.close()
        self.clock.value += 120
        other = d.Controller(self.root, lambda: self.fail("Must not run on restart"), self.clock)
        self.addCleanup(other.close)
        self.assertEqual(other.snapshot()["config"], {"enabled": True, "time": "12:01"})
        self.assertEqual(other.snapshot()["next_run"], "2026-09-09T12:01:00+08:00")

    def test_manual_does_not_change_schedule_and_can_run_when_disabled(self):
        before = self.control.snapshot()
        self.assertTrue(self.control.start_run())
        self.wait()
        after = self.control.snapshot()
        self.assertEqual(before["config"], after["config"])
        self.assertEqual(before["next_run"], after["next_run"])
        self.assertEqual(after["last_run"]["status"], "saved")

    def test_stop_schedule_does_not_cancel_running_job_and_rejects_duplicate(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.control.runner = lambda: (release.wait(2), {"status": "saved"})[1]
        self.control.configure({"enabled": True})
        self.assertTrue(self.control.start_run())
        self.assertFalse(self.control.start_run())
        self.control.configure({"enabled": False})
        self.assertTrue(self.control.snapshot()["running"])
        self.assertFalse(self.control.snapshot()["config"]["enabled"])
        release.set()
        self.wait()
        self.assertEqual(self.control.snapshot()["last_run"]["status"], "saved")

    def test_busy_automatic_attempt_skips_and_is_not_repeated(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.control.runner = lambda: (release.wait(2), {"status": "saved"})[1]
        self.control.configure({"enabled": True, "time": "12:01"})
        self.control.start_run()
        self.clock.value += 59
        self.control.tick()
        self.clock.value += 1
        self.control.tick()
        self.assertEqual(self.control.snapshot()["last_run"]["status"], "skipped")
        self.assertTrue(self.control.snapshot()["running"])
        release.set()
        self.wait()
        self.assertEqual(self.control.config()["last_auto_date"], "2026-09-08")

    def test_failure_and_normal_skip_are_distinct_and_leave_schedule_on(self):
        self.control.configure({"enabled": True})
        for result, expected in [({"status": "skipped_not_today"}, "skipped"),
                                 ({"status": "skipped_existing"}, "skipped"),
                                 ({"status": "failed", "error": {"code": "lock_timeout", "message": "busy"}}, "skipped"),
                                 ({"status": "failed", "error": {"code": "lm_timeout", "message": "模型超时"}}, "failed")]:
            self.control.runner = lambda result=result: result
            self.control.start_run()
            self.wait()
            self.assertEqual(self.control.snapshot()["last_run"]["status"], expected)
            self.assertTrue(self.control.snapshot()["config"]["enabled"])

    def test_interrupted_run_marked_unknown_failure_after_restart(self):
        self.control._record("manual", "running", "pending", self.clock())
        self.control.close()
        other = d.Controller(self.root, lambda: {}, self.clock)
        self.addCleanup(other.close)
        self.assertFalse(other.snapshot()["running"])
        self.assertEqual(other.snapshot()["last_run"]["status"], "failed")

    def test_service_singleton(self):
        with d.service_lock(self.root):
            with self.assertRaises(RuntimeError):
                with d.service_lock(self.root):
                    pass

    def test_spring_dst_missing_minute_and_fall_once(self):
        with timezone("America/New_York"):
            spring = timestamp("2026-03-08T00:00:00-05:00")
            self.assertEqual(d.iso(d.next_due(spring, "02:30")), "2026-03-09T02:30:00-04:00")
            fall = timestamp("2026-11-01T00:00:00-04:00")
            first = d.next_due(fall, "01:30")
            self.assertEqual(d.iso(first), "2026-11-01T01:30:00-04:00")
            self.assertEqual(d.iso(d.next_due(first + 1, "01:30", "2026-11-01")), "2026-11-02T01:30:00-05:00")

    def test_pipeline_invokes_existing_entry_and_bounded_timeout(self):
        completed = type("Completed", (), {"stdout": '{"status":"skipped_existing"}', "returncode": 0})()
        with patch.object(d.subprocess, "run", return_value=completed) as run:
            self.assertEqual(d.run_pipeline(self.root, "http://127.0.0.1:2051", 300)["status"], "skipped_existing")
        command = run.call_args.args[0]
        self.assertTrue(command[1].endswith("/archive.py"))
        self.assertIn(str(self.root), command)
        self.assertEqual(run.call_args.kwargs["timeout"], 1380)


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_empty_state(self):
        self.assertIsNone(d.latest_article(self.root, {})["article"])

    def test_latest_by_publication_not_folder_mtime_or_today(self):
        archive(self.root, "今天早些", "2026-09-08T09:00:00+08:00")
        archive(self.root, "今天最新", "2026-09-08T15:00:00+08:00")
        archive(self.root, "昨天后写入", "2026-09-07T16:00:00+08:00")
        article = d.latest_article(self.root, {})["article"]
        self.assertEqual(article["title"], "今天最新")
        self.assertEqual(article["analysis"]["what"], "今天最新")
        self.assertIn("<p>正文第一段。", article["html"])
        self.assertNotIn("<h1>", article["html"])

    def test_new_article_missing_analysis_does_not_show_old_analysis(self):
        archive(self.root, "旧文章", "2026-09-07T16:00:00+08:00")
        _, record = archive(self.root, "新文章", "2026-09-08T16:00:00+08:00", analysis=False)
        snapshot = {"last_run": {"status": "failed", "message": "模型不可用", "result": {"source_url": record["source_url"]}}}
        article = d.latest_article(self.root, snapshot)["article"]
        self.assertEqual(article["title"], "新文章")
        self.assertIsNone(article["analysis"])
        self.assertEqual(article["analysis_state"], "failed")
        self.assertEqual(article["analysis_message"], "模型不可用")

    def test_corrupted_analysis_and_body_are_reported(self):
        folder, _ = archive(self.root, "文章", "2026-09-08T16:00:00+08:00")
        (folder / "分析.json").write_text("{}")
        article = d.latest_article(self.root, {})["article"]
        self.assertEqual(article["analysis_state"], "failed")
        self.assertIsNotNone(article["html"])
        (folder / "正文.md").write_text("篡改正文")
        changed = d.latest_article(self.root, {})["article"]
        self.assertIsNone(changed["body_error"])
        self.assertIn("篡改正文", changed["html"])
        self.assertEqual(changed["analysis_state"], "failed")
        self.assertIsNone(changed["analysis"])

    def test_edited_body_displays_saved_analysis_without_hash_warning(self):
        folder, _ = archive(self.root, "文章", "2026-09-08T16:00:00+08:00")
        (folder / "正文.md").write_text("修改后的正文", encoding="utf-8")
        article = d.latest_article(self.root, {})["article"]
        self.assertIsNone(article["body_error"])
        self.assertIn("修改后的正文", article["html"])
        self.assertEqual(article["analysis_state"], "ready")
        self.assertEqual(article["analysis"]["what"], "文章")

    def test_analysis_metadata_is_optional_and_does_not_gate_display(self):
        folder, _ = archive(self.root, "文章", "2026-09-08T16:00:00+08:00")
        metadata = folder / ".analysis.json"
        for value in (None, "invalid json", "{}"):
            with self.subTest(metadata=value):
                if value is None:
                    metadata.unlink()
                else:
                    metadata.write_text(value)
                article = d.latest_article(self.root, {})["article"]
                self.assertEqual(article["analysis_state"], "ready")
                self.assertEqual(article["analysis"]["what"], "文章")

    def test_rendering_removes_scripts_images_and_unsafe_links(self):
        html = d.render_body('# 标题\n\n<script>alert(1)</script><img src="https://x" onerror="bad()"><a href="javascript:bad()">文字</a>\n\n> 引用\n\n- 条目')
        for excluded in ("<script", "<img", "onerror", "javascript:"):
            self.assertNotIn(excluded, html)
        self.assertIn("blockquote", html)
        self.assertIn("<li>", html)


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.control = d.Controller(Path(self.temp.name), lambda: {"status": "skipped_not_today"})
        self.addCleanup(self.control.close)
        self.server = d.make_server(self.control, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        def cleanup():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
        self.addCleanup(cleanup)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, value=None, extra_headers=None):
        headers = {"Content-Type": "application/json", "X-Local-Client": "1"}
        headers.update(extra_headers or {})
        request = urllib.request.Request(self.base + path, headers=headers,
            data=None if value is None else json.dumps(value).encode())
        try:
            result = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            result = exc
        with result:
            return result.status, result.read(), result.headers

    def test_static_page_and_state(self):
        for path in ("/", "/app.js", "/style.css"):
            status, body, headers = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, raw, _ = self.request("/api/state")
        state = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertFalse(state["config"]["enabled"])
        self.assertIsNone(state["article"])

    def test_config_and_manual_run(self):
        status, raw, _ = self.request("/api/config", {"time": "18:42", "enabled": True})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["config"], {"enabled": True, "time": "18:42"})
        status, _, _ = self.request("/api/run", {})
        self.assertEqual(status, 202)
        self.control.worker.join(2)
        state = json.loads(self.request("/api/state")[1])
        self.assertEqual(state["last_run"]["status"], "skipped")
        self.assertTrue(state["config"]["enabled"])

    def test_invalid_config_origin_and_paths(self):
        self.assertEqual(self.request("/api/config", {"time": "25:00"})[0], 400)
        self.assertEqual(self.request("/api/run", {}, {"Origin": "http://other.example"})[0], 403)
        self.assertEqual(self.request("/api/run", {}, {"X-Local-Client": ""})[0], 403)
        self.assertEqual(self.request("/api/state", extra_headers={"Host": "other.example"})[0], 403)
        self.assertEqual(self.request("/../../README.md")[0], 404)

    def test_duplicate_manual_request_conflict(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.control.runner = lambda: (release.wait(2), {"status": "saved"})[1]
        self.assertEqual(self.request("/api/run", {})[0], 202)
        self.assertEqual(self.request("/api/run", {})[0], 409)
        release.set()


if __name__ == "__main__":
    unittest.main(verbosity=2)
