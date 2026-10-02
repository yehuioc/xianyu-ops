#!/usr/bin/env python3
"""Own console entry. Run: python scripts/xianyu-console.py serve."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from console.paths import DEFAULT_ACCOUNT
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="闲鱼自有后台")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8090)
    for name in ("start", "stop", "status"):
        lifecycle = sub.add_parser(name)
        lifecycle.add_argument("--port", type=int, default=8090)
        if name == "start":
            lifecycle.add_argument("--take-over-legacy", action="store_true")
    sub.add_parser("migrate")
    collect = sub.add_parser("collect")
    collect.add_argument("--account", default=DEFAULT_ACCOUNT)
    collect.add_argument("--item-id", action="append")
    collect.add_argument("--scheduled", action="store_true")
    collect.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    if args.command in {"start", "stop", "status"}:
        from console import runtime
        try:
            function = getattr(runtime, args.command)
            result = function(args.port, **({"take_over_legacy": args.take_over_legacy} if args.command == "start" else {}))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except (ValueError, OSError) as exc:
            print(json.dumps({"state": "blocked", "message": str(exc)}, ensure_ascii=False))
            return 2
    if args.command == "collect":
        from console.runtime import collect_via_server
        try:
            result = collect_via_server(args.account, args.item_id, port=args.port, scheduled=args.scheduled)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "complete" else 2
        except (ValueError, OSError) as exc:
            print(json.dumps({"status": "blocked", "message": str(exc)}, ensure_ascii=False))
            return 2
    lock = None
    if args.command == "serve":
        from console.runtime import server_lock
        try:
            lock = server_lock()
        except ValueError as exc:
            print(json.dumps({"state": "blocked", "message": str(exc)}, ensure_ascii=False))
            return 2
    from console.service import ConsoleService
    service = ConsoleService()
    if args.command == "serve":
        import uvicorn
        from console.app import create_app
        uvicorn.run(create_app(service), host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
    elif args.command == "migrate":
        print(json.dumps(service.import_result, ensure_ascii=False, indent=2))
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
