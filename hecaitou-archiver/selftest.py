#!/usr/bin/env python3
"""Run repeatable offline acceptance tests and retain results beside the code."""
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import unittest


def main():
    project = Path(__file__).resolve().parent
    output = project / "test-results"
    output.mkdir(exist_ok=True)
    started = time.monotonic()
    suite = unittest.defaultTestLoader.discover(str(project / "tests"))
    with (output / "unit-tests.txt").open("w", encoding="utf-8") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    summary = dict(run_at=datetime.now().astimezone().isoformat(),
                   tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   skipped=len(result.skipped), passed=result.wasSuccessful(),
                   elapsed_seconds=round(time.monotonic() - started, 3),
                   details_path=str(output / "unit-tests.txt"))
    content = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    (output / "summary.json").write_text(content, encoding="utf-8")
    print(content, end="")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
