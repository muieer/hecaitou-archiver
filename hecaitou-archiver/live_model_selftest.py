#!/usr/bin/env python3
"""Focused Ark live test: read the newest existing article, never fetch the website."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

from hecaitou_archiver import analysis
from hecaitou_archiver.cli import RunLog
from hecaitou_archiver.credentials import DEFAULT_KEY_FILE

PROJECT = Path(__file__).resolve().parent


def latest_article(root):
    candidates = []
    for path in root.glob('*/正文.md'):
        record = json.loads(path.with_name('.archive.json').read_text(encoding='utf-8'))
        published = datetime.fromisoformat(record['published'].replace('Z', '+00:00')).astimezone()
        candidates.append((published, str(path), path))
    if not candidates:
        raise ValueError('保存目录中没有已有正文，不访问网站补抓')
    return max(candidates)[2].resolve()


def main(argv=None):
    parser = argparse.ArgumentParser(description='仅测试云端分析，读取最新已有正文，结果写入测试目录')
    parser.add_argument('--articles-dir', type=Path, default=PROJECT.parent / '文章存档')
    parser.add_argument('--ark-key-file', type=Path, default=DEFAULT_KEY_FILE)
    args = parser.parse_args(argv)
    root = PROJECT / 'test-results' / 'cloud-analysis'
    root.mkdir(parents=True, exist_ok=True)
    log = RunLog(root, datetime.now().astimezone())
    report = {'run_at': datetime.now().astimezone().isoformat(), 'model': analysis.DEFAULT_MODEL, 'passed': False}
    try:
        source = latest_article(args.articles_dir.expanduser().resolve())
        raw = source.read_bytes()
        if not raw or len(raw) > analysis.MAX_INPUT_BYTES:
            raise ValueError('正文为空或超过 1 MiB')
        report.update(source_path=str(source), source_sha256=hashlib.sha256(raw).hexdigest())
        value = analysis.generate(raw.decode('utf-8'), {'id': analysis.DEFAULT_MODEL},
                                  analysis.DEFAULT_API_URL, 300, log, args.ark_key_file)
        if source.read_bytes() != raw:
            raise ValueError('测试期间正文发生变化')
        output = root / '分析.json'
        output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        report.update(passed=True, output_path=str(output), source_unchanged=True,
                      exact_seven_strings=set(value) == set(analysis.FIELDS), website_requests=0)
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        log.close()
        (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
