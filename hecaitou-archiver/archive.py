#!/usr/bin/env python3
"""Stable entry point; imports resolve relative to this file, not the working directory."""
import json
import sys
from datetime import datetime

if __name__ == "__main__":
    try:
        from hecaitou_archiver.cli import main
    except ImportError as exc:
        message = "依赖或运行环境不完整，请按 README 安装依赖（macOS/Linux，Python 3.9+）：" + str(exc)
        print(message, file=sys.stderr)
        print(json.dumps(dict(status="failed", title=None, published_date=None,
                              system_date=datetime.now().astimezone().date().isoformat(),
                              source_url=None, output_path=None, archive_status="not_started",
                              analysis=dict(status="not_started", output_path=None, model=None),
                              error=dict(code="dependency_error", message=message)), ensure_ascii=False))
        sys.exit(1)
    sys.exit(main())
