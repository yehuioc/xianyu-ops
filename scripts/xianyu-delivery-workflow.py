"""Reusable digital-product preparation, publication, binding and reconciliation."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from console.delivery_workflow import Backend, DeliveryWorkflow, BatchWorkflow, WorkflowStop
from console.paths import DEFAULT_ACCOUNT
from console.store import Store


def main():
    parser = argparse.ArgumentParser(description="数字商品交付流程：准备、确认发布并绑定、状态与远端回读")
    parser.add_argument("action", choices=["status", "prepare", "finish", "verify", "research",
        "batch-plan", "batch-prepare", "batch-finish", "batch-status", "batch-verify"])
    parser.add_argument("slugs", nargs="*")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--values", type=Path, help="发布字段 JSON；批量时按商品标识分组，字段为 title、description、price_cents、quantity、images")
    parser.add_argument("--preview-id")
    parser.add_argument("--digest")
    parser.add_argument("--delivery-sha256")
    parser.add_argument("--batch-id")
    parser.add_argument("--batch-digest")
    parser.add_argument("--price-cents", type=int, default=99, help="资料批次试售价：59、99 或 199 分")
    parser.add_argument("--keyword", help="research 的单次公开挂牌查询词")
    parser.add_argument("--no-wait", action="store_true", help="批次到达发布间隔时返回，不在本命令中等待")
    args = parser.parse_args()
    if args.action == "finish" and not all((args.preview_id, args.digest, args.delivery_sha256)):
        parser.error("finish 需要 prepare 返回的 --preview-id、--digest 和 --delivery-sha256")
    if args.action in {"status", "prepare", "finish", "verify"} and len(args.slugs) != 1:
        parser.error("单商品操作需要一个商品标识")
    if args.action.startswith("batch-") and args.action != "batch-plan" and not args.batch_id:
        parser.error("批次接续需要 --batch-id")
    if args.action == "batch-finish" and not args.batch_digest:
        parser.error("批量发布需要核对后的 --batch-digest")
    if args.action == "research" and not args.keyword:
        parser.error("research 需要 --keyword")
    backend = Backend(args.account, args.port)
    try:
        values = json.loads(args.values.read_text(encoding="utf-8-sig")) if args.values else None
        if args.action != "batch-plan":
            backend.check()
        if args.action == "research":
            result = backend.request("POST", "/api/commerce/search", {"keyword": args.keyword})
        elif args.action.startswith("batch-"):
            batch = BatchWorkflow(Store(), backend, args.account)
            operation = args.action.removeprefix("batch-")
            if operation == "plan":
                result = batch.plan(args.slugs, values, price_cents=args.price_cents)
            elif operation == "prepare":
                result = batch.prepare(args.batch_id)
            elif operation == "finish":
                result = batch.finish(args.batch_id, args.batch_digest)
                while result["state"] == "waiting_between_listings" and not args.no_wait:
                    print(result["message"], file=sys.stderr, flush=True)
                    time.sleep(min(60, result["wait_seconds"]))
                    result = batch.finish(args.batch_id, args.batch_digest)
            else:
                result = batch.status(args.batch_id, verify=operation == "verify")
        else:
            workflow = DeliveryWorkflow(Store(), backend, args.account, args.slugs[0])
            if args.action == "prepare":
                result = workflow.prepare(values)
            elif args.action == "finish":
                result = workflow.finish(args.preview_id, args.digest, args.delivery_sha256)
            else:
                result = getattr(workflow, args.action)()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("state") == "needs_attention" or result.get("status") == "unavailable" else 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"state": "stopped", "message": str(exc), "job_id": getattr(exc, "job_id", None)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
