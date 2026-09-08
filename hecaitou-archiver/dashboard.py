#!/usr/bin/env python3
"""Start the local scheduler and reading page."""
import sys

if __name__ == "__main__":
    try:
        from hecaitou_archiver.dashboard import main
        sys.exit(main())
    except ImportError as exc:
        print(f"缺少运行依赖，请用当前 Python 安装 requirements.txt：{exc}", file=sys.stderr)
        sys.exit(1)
