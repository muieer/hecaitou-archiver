"""Subprocess harness only: routes production requests to a local HTTP test server."""
import os
from pathlib import Path
import signal
import sys
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hecaitou_archiver import cli
from hecaitou_archiver import analysis

base = os.environ["ARCHIVER_TEST_SERVER"]
cli.FEED_URL = base + "/feed"
real_fetch = cli.fetch


def local_fetch(url, timeout, log):
    return real_fetch(base + "/article" if url.startswith("https://www.hecaitou.com/") else url, timeout, log)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromisoformat("2026-09-05T12:30:00+08:00").astimezone(tz)


cli.fetch = local_fetch
cli.datetime = FixedDatetime
if not os.environ.get("ARCHIVER_TEST_LM"):
    # Existing archive regression tests isolate their scope; pipeline tests use real local HTTP.
    def archive_only(output, base_url, timeout, result, log):
        result["analysis"].update(status=result["archive_status"], model="archive-regression-stub")

    analysis.analyze_archive = archive_only
fault = os.environ.get("ARCHIVER_TEST_FAULT")
if fault == "during_write":
    real_write = cli.write_synced

    def crash_write(path, content):
        real_write(path, content[:10])
        os.kill(os.getpid(), signal.SIGKILL)

    cli.write_synced = crash_write
elif fault == "after_commit":
    real_rename = cli.os.rename

    def crash_rename(source, destination):
        real_rename(source, destination)
        os.kill(os.getpid(), signal.SIGKILL)

    cli.os.rename = crash_rename
elif fault == "analysis_before_commit":
    real_replace = analysis.os.replace

    def crash_replace(source, destination):
        real_replace(source, destination)
        if Path(destination).name == analysis.RECORD_NAME:
            os.kill(os.getpid(), signal.SIGKILL)

    analysis.os.replace = crash_replace
elif fault == "analysis_after_commit":
    real_link = analysis.os.link

    def crash_link(source, destination):
        real_link(source, destination)
        if Path(destination).name == analysis.OUTPUT_NAME:
            os.kill(os.getpid(), signal.SIGKILL)

    analysis.os.link = crash_link

sys.exit(cli.main())
