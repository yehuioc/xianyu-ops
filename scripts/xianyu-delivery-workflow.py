"""Reusable digital-product preparation, publication, binding and reconciliation."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from console.delivery_workflow import Backend, DeliveryWorkflow, WorkflowStop
from console.paths import DEFAULT_ACCOUNT
from console.store import Store


def main():
    parser = argparse.ArgumentParser(description="数字商品交付流程：准备、确认发布并绑定、状态与远端回读")
    parser.add_argument("action", choices=["status", "prepare", "finish", "verify"])
    parser.add_argument("slug")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--values", type=Path, help="可选发布字段 JSON：title、description、price_cents、quantity、images")
    parser.add_argument("--preview-id")
    parser.add_argument("--digest")
    parser.add_argument("--delivery-sha256")
    args = parser.parse_args()
    if args.action == "finish" and not all((args.preview_id, args.digest, args.delivery_sha256)):
        parser.error("finish 需要 prepare 返回的 --preview-id、--digest 和 --delivery-sha256")
    backend = Backend(args.account, args.port)
    try:
        backend.check()
        workflow = DeliveryWorkflow(Store(), backend, args.account, args.slug)
        if args.action == "prepare":
            values = json.loads(args.values.read_text(encoding="utf-8-sig")) if args.values else None
            result = workflow.prepare(values)
        elif args.action == "finish":
            result = workflow.finish(args.preview_id, args.digest, args.delivery_sha256)
        else:
            result = getattr(workflow, args.action)()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result["state"] == "needs_attention" else 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"state": "stopped", "message": str(exc), "job_id": getattr(exc, "job_id", None)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
