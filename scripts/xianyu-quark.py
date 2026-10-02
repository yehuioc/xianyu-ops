"""Project-local Quark CLI workflow; credentials never become command arguments here."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from console import quark
from console.paths import DEFAULT_ACCOUNT
from console.store import Store


def main():
    parser = argparse.ArgumentParser(description="闲鱼交付包的夸克上传、分享与核对")
    parser.add_argument("action", choices=["install", "status", "login", "prepare", "reconcile", "bind", "audit", "adopt-share"])
    parser.add_argument("slug", nargs="?")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--code", action="store_true", help="以隐藏输入方式读取一次性授权码")
    parser.add_argument("--url", help="分享结果不确定时核对现有链接")
    args = parser.parse_args()
    store, cli = Store(), quark.QuarkCLI()
    if not store.get("account", args.account):
        parser.error("未找到闲鱼账号")
    if args.action == "install":
        result = cli.install()
    elif args.action == "status":
        result = quark.connection(store, cli=cli)
    elif args.action == "login":
        with quark.operation_lock(store):
            cli.install()
            cli.run("login", *(["--token", getpass.getpass("一次性授权码：")] if args.code else []), timeout=180)
            result = quark.connection(store, cli=cli)
    elif args.action == "audit":
        result = quark.audit_links(store, args.account, cli=cli)
    else:
        if not args.slug:
            parser.error("此操作需要商品标识")
        if args.action == "bind":
            from console.store import product_key
            record = store.get("quark_delivery", product_key(args.account, args.slug))
            if not record:
                parser.error("请先准备并核对夸克交付包")
            from urllib.parse import urlencode, quote
            url = "http://127.0.0.1:8090/api/commerce/" + quote(args.slug, safe="") + "/quark/bind?" + urlencode({"account": args.account})
            req = urllib.request.Request(url, data=json.dumps({"sha256":record["sha256"], "acknowledgment":"enable_this_verified_delivery"}).encode(), headers={"Content-Type":"application/json"})
            with urllib.request.urlopen(req, timeout=15) as response:
                result = json.load(response)
        elif args.action == "adopt-share":
            if not args.url:
                parser.error("需要 --url 提供该文件的已有分享链接")
            result = quark.adopt_share(store, args.account, args.slug, args.url, cli=cli)
        else:
            result = quark.prepare(store, args.account, args.slug, cli=cli, reconcile_only=args.action == "reconcile")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
