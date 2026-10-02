#!/usr/bin/env python3
"""Inspect or initialize product reply coverage. Never sends customer messages."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from console.store import Store
from console.support import configure_all, coverage

parser = argparse.ArgumentParser(description="配置商品咨询回复和服务需求收集")
parser.add_argument("--apply", action="store_true", help="补齐缺少配置，保留已编辑文案；重启后台加载新商品")
parser.add_argument("--account", default="demo-account")
args = parser.parse_args()
result = configure_all(Store(), args.account) if args.apply else coverage(Store(), args.account)
print(json.dumps(result, ensure_ascii=False, indent=2))
