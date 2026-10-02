"""Source-aware descriptive analysis for a small listing experiment."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .paths import OPS
from .store import CHINA, now


def moment(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=CHINA)
    except ValueError:
        return None


def count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def snapshot_history(account: str, item_id: str, directory: Path | None = None) -> list[dict]:
    history = []
    for path in sorted((directory or OPS / "snapshots").glob("*-snapshot.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if raw.get("account") != account or item_id not in [str(v) for v in raw.get("item_ids", [])]:
                continue
            captured_at = raw.get("captured_at")
            if not moment(captured_at):
                continue
            public = next((r for r in raw.get("public_detail_metrics", []) if str(r.get("item_id")) == item_id), {})
            if moment(public.get("captured_at")):
                captured_at = public["captured_at"]
            item = (raw.get("items") or {}).get(item_id) or {}
            # A removed/offline item's page can contain unrelated recommendation counts.
            if item.get("status") in {"已删除", "已下架", "删除", "下架", -9, "-9"} or item.get("source_conflicts"):
                public = {**public, "browse": None, "want": None, "status": "unavailable"}
            orders = (raw.get("orders") or {}).get(item_id) or {}
            # Old parsing did not exclude "为你推荐". Raw snapshots remain intact.
            if public.get("parser_version") != 2:
                public = {**public, "want": None}
            history.append({"captured_at": captured_at, "file": path.name,
                "status": raw.get("status", "partial"), "browse": count(public.get("browse")),
                "want": count(public.get("want")), "metric_source": public.get("source", "unavailable"),
                "metric_status": public.get("status", "unavailable"), "title": item.get("title"),
                "price": item.get("price"), "orders": orders, "exposure": None,
                "collection_error": raw.get("collection_error"),
                "order_coverage": raw.get("order_coverage") or {"status": "historical_local_only"}})
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return sorted(history, key=lambda row: (moment(row["captured_at"]), row["file"]))


def analyse(history: list[dict], orders: list[dict], experiment: dict | None = None,
            *, at: datetime | None = None) -> dict:
    at = at or datetime.now(CHINA)
    experiment = experiment or {}
    valid = [r for r in history if r.get("metric_status") == "observed" and
             r.get("metric_source") == "public_item_detail_page" and r.get("browse") is not None]
    latest = history[-1] if history else None
    latest_valid = valid[-1] if valid else None
    online_at = moment(experiment.get("content_live_at"))
    ended_at = moment(experiment.get("content_ended_at"))
    interrupted = bool(ended_at or experiment.get("state") in {"content_changed", "needs_attention"})
    after = [r for r in valid if online_at and moment(r["captured_at"]) >= online_at and
             (not ended_at or moment(r["captured_at"]) < ended_at)]
    # The observation denominator begins at the first confirmed live-version snapshot.
    window = after if online_at else valid
    first = window[0] if len(window) >= 2 else None
    last = window[-1] if window else None
    delta_browse = delta_want = None
    counter_reset = False
    if first and last and moment(last["captured_at"]) > moment(first["captured_at"]):
        diff = last["browse"] - first["browse"]
        if diff >= 0:
            delta_browse = diff
        else:
            counter_reset = True
        if first.get("want") is not None and last.get("want") is not None:
            diff_want = last["want"] - first["want"]
            if diff_want >= 0:
                delta_want = diff_want
            else:
                counter_reset = True

    paid_orders = None
    paid_per_browse = None
    coverage = (last or {}).get("order_coverage") or {}
    missing_paid_time = False
    if first and last and coverage.get("status") == "observed" and coverage.get("complete") is True:
        start, end = moment(first["captured_at"]), moment(last["captured_at"])
        qualified = []
        for order in orders:
            if order.get("source") != "goofish_seller_orders":
                continue
            status = order.get("order_status")
            paid_at = moment(order.get("platform_paid_at"))
            if status in ("paid", "shipped", "completed"):
                if paid_at is None:
                    missing_paid_time = True
                elif start < paid_at <= end:
                    qualified.append(order.get("order_id"))
        if not missing_paid_time:
            paid_orders = len(set(qualified))
            if delta_browse and not counter_reset:
                paid_per_browse = round(paid_orders / delta_browse * 100, 2)

    hours = max((at - online_at).total_seconds() / 3600, 0) if online_at else None
    sampled_hours = ((moment(last["captured_at"]) - online_at).total_seconds() / 3600
                     if last and online_at else None)
    review_due = bool(hours is not None and hours >= 72)
    review_ready = bool(not interrupted and experiment.get("state") not in {"status_unknown", "evidence_incomplete"} and review_due and sampled_hours is not None and sampled_hours >= 72 and
                        first and delta_browse is not None and not counter_reset)
    if experiment.get("state") == "status_unknown":
        state, heading = "status_unknown", "商品状态仍待核实"
        recommendation = "已保留可读取的商品内容，但平台状态字段缺失或尚不能识别；不把未知状态当成在售，也不据此判断审核未通过。"
    elif experiment.get("state") == "evidence_incomplete":
        state, heading = "evidence_incomplete", "商品内容已读取，线上展示待核实"
        recommendation = "本人商品数据已保留；公开页面或必要字段尚未完成核对，暂不开始或完成效果复盘，也不据此要求重新上传。"
    elif experiment.get("state") == "pending_review" and not online_at:
        state, heading = "pending_review", "商品已上传，等待平台审核"
        recommendation = "手机端审核状态来自用户截图；网页接口尚未确认在线。审核通过并回读匹配后才开始观察，不使用页面推荐商品的读数。"
    elif interrupted:
        state, heading = "interrupted", "本轮观察已暂停"
        recommendation = "线上内容、价格或规格与本轮基线出现差异；先核对变化，再准备下一轮素材。差异后的数据不会混入本轮。"
    elif not online_at:
        state, heading = "baseline", "当前是修改前基线"
        recommendation = "先完成本轮手机上传。后台回读到新标题、介绍和主图后，再开始计算这一版的观察窗口。"
    elif not last:
        state, heading = "needs_data", "新版本已识别，等待有效指标"
        recommendation = "继续保留当前素材；先恢复公开浏览和想要的采集，再判断效果。"
    elif counter_reset:
        state, heading = "counter_reset", "平台计数发生回退"
        recommendation = "本轮差值不可靠，保留原始记录并从稳定读数重新观察，不据此更换素材。"
    elif not review_ready:
        state, heading = "observing", "正在观察这次改动"
        recommendation = "保持当前价格和素材，等待生效满 72 小时后的有效采集，避免连续修改使结果无法比较。"
    elif delta_browse is None or delta_browse < 20:
        state, heading = "low_sample", "观察期已到，样本仍少"
        recommendation = "暂不判断文案有效或无效。继续采集，保留当前版本；20 次新增浏览只是本项目的观察提示，不是统计显著性标准。"
    elif paid_orders and paid_orders > 0:
        state, heading = "keep_observing", "观察窗内有支付记录"
        recommendation = "保留当前版本继续观察，并核对订单是否正常交付；现有样本不能证明订单由本次素材改动引起。"
    elif delta_want == 0:
        state, heading = "consider_next_draft", "有新增浏览，暂未增加想要"
        recommendation = "下一轮可提出一份更清楚表达交付形式和用途的主图草案，保持价格不变；先准备素材，再由你手机上传。"
    else:
        state, heading = "keep_observing", "已有兴趣信号，成交仍待核对"
        recommendation = "先保留当前版本，检查交付说明与常见咨询是否清楚；没有同窗口的可靠订单数据时，不推断成交转化率。"
    gaps = ["卖家后台曝光不可得，因此不能计算曝光点击率。", "公开浏览不是去重访客；订单/浏览只能作为描述性比值，不能解释为买家转化概率。"]
    gaps.append("当前来源不能区分卖家自查、采集访问与真实买家浏览；小样本变化不能直接当作自然流量增长。")
    if paid_orders is None:
        gaps.append("缺少完整、同窗口且带支付时间的订单观察，成交/浏览暂不计算。")
    if not online_at:
        gaps.append("尚未确认本轮素材在线生效，历史增量不能归因于本轮优化。")
    return {"state": state, "heading": heading, "recommendation": recommendation,
            "latest": latest, "latest_valid": latest_valid, "window_start": first and first["captured_at"],
            "window_end": last and last["captured_at"], "delta_browse": delta_browse, "delta_want": delta_want,
            "paid_orders": paid_orders, "paid_per_browse_pct": paid_per_browse,
            "counter_reset": counter_reset, "content_live_at": experiment.get("content_live_at"),
            "observed_hours": round(hours, 1) if hours is not None else None,
            "review_due": review_due, "review_ready": review_ready, "gaps": gaps,
            "next_review_at": (online_at + timedelta(hours=72)).isoformat() if online_at else None,
            "analysed_at": at.isoformat(timespec="seconds")}
