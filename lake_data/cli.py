"""显示原始湖仓资料、规范袋摘要、范围下钻与可重算袋差分。"""
import argparse
import json
import pathlib
import sys

from .io import load
from .domain import summarize
from .reconcile import reconcile, canonical_json


def _inspect(path):
    v, digest = load(path)
    return {
        'source_sha256': digest,
        'summary': summarize(v, pathlib.Path(path).parent),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog='lake_data.cli')
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect', help='显示原字段概况')
    inspect.add_argument('file')
    inspect.add_argument('--out', help='把完整 JSON 报告写到该文件')
    bag = sub.add_parser('bag', help='规范袋摘要、范围下钻与有序袋差分')
    bag.add_argument('file')
    bag.add_argument('--keep', action='store_true',
                     help='保留封存段与工作目录，便于逐段复核')
    bag.add_argument('--out', help='把完整 JSON 报告写到该文件')
    args = parser.parse_args(argv)
    try:
        if args.command == 'inspect':
            text = json.dumps(_inspect(args.file), ensure_ascii=False,
                              indent=2, allow_nan=False)
        else:
            text = canonical_json(reconcile(args.file, keep=args.keep))
    except (OSError, ValueError) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.out:
        with open(args.out, 'w', encoding='utf8') as f:
            f.write(text + '\n')
    else:
        print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
