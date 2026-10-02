#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闲鱼运营闭环的薄运行层。

这个模块只负责把现有运行时、闲鱼网页回读、本地缓存和项目数据连接起来。
它不创建新的浏览器配置、不读取或打印 Cookie，也不自动操作手机 App。
历史 prepare/apply 接口保留为兼容与证据读取入口；自有平台适配器不允许
它修改线上商品。本轮发布统一由用户手机上传素材包完成。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import html
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


PROJECT = Path(__file__).resolve().parents[1]
OPS_DATA = PROJECT / "data" / "ops"
SNAPSHOT_DIR = OPS_DATA / "snapshots"
PACKAGE_DIR = OPS_DATA / "packages"
EVIDENCE_DIR = OPS_DATA / "evidence"
LEDGER_DIR = PROJECT / "data" / "opportunity-loop"
LEDGER_PATH = LEDGER_DIR / "audit.jsonl"

DEFAULT_API = "http://127.0.0.1:8090"
DEFAULT_CDP = "http://127.0.0.1:9223"
ONLINE_STATUS = {"在线", "online", "ON_LINE"}


class OpsError(RuntimeError):
    """用户可操作的失败，不携带敏感响应正文。"""


def now_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()) or "unknown"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(value: Any) -> Any:
    """去掉读取时间等噪声，形成可比较的结构。"""
    if isinstance(value, dict):
        return {
            key: canonical(item)
            for key, item in sorted(value.items())
            if key
            not in {
                "observed_at",
                "captured_at",
                "checked_at",
                "created_at",
                # 回读来源标记帮助解释未知字段，但不应让同一线上基线
                # 因为换了读取入口而被误判为商品已变化。
                "field_sources",
                "field_state",
                "editable_invariants",
                "editable_invariant_state",
                "editable_invariant_sources",
                "edit_detail_status",
                "edit_detail_error",
            }
        }
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def stable_hash(value: Any) -> str:
    encoded = json.dumps(canonical(value), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return sha256_bytes(encoded)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpsError(f"无法读取 JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OpsError(f"JSON 顶层不是对象: {path}")
    return value


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def http_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 20,
) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise OpsError(f"本地接口 {method} {path} 返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OpsError(f"本地接口 {method} {path} 不可用: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OpsError(f"本地接口 {method} {path} 返回非 JSON") from exc
    return value


def require_object(value: Any, context: str) -> dict[str, Any]:
    """把允许返回数组的 HTTP 层结果收窄到对象接口。"""
    if not isinstance(value, dict):
        raise OpsError(f"{context} 返回结构异常")
    return value


def require_success_object(value: Any, context: str) -> dict[str, Any]:
    """检查本地接口的业务 success，而不是只看 HTTP 200。"""
    result = require_object(value, context)
    if result.get("success") is not True:
        detail = result.get("message") or result.get("error") or "业务响应未成功"
        raise OpsError(f"{context} 业务失败: {detail}")
    return result


def parse_copy_file(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise OpsError(f"文案文件不可读: {path}: {exc}") from exc
    title_match = re.search(r"^## 标题\s*$\n(.*?)(?=^## 主要介绍\s*$)", text, re.M | re.S)
    desc_match = re.search(r"^## 主要介绍\s*$\n(.*)\Z", text, re.M | re.S)
    if not title_match or not desc_match:
        raise OpsError("文案文件必须包含 `## 标题` 和 `## 主要介绍` 两个区块")
    title = title_match.group(1).strip()
    description = desc_match.group(1).strip()
    if not title or not description:
        raise OpsError("标题和主要介绍都不能为空")
    return title, description


def target_args(parser: argparse.ArgumentParser, *, many: bool = False) -> None:
    parser.add_argument("--account", default=os.environ.get("XIANYU_ACCOUNT"), help="闲鱼账号标识")
    parser.add_argument(
        "--item-id",
        dest="item_ids",
        nargs="+" if many else None,
        default=None,
        help="商品 item_id；snapshot/collect 可传多个",
    )


def require_target(args: argparse.Namespace, *, many: bool = False) -> tuple[str, list[str]]:
    account = (args.account or "").strip()
    values = args.item_ids
    if isinstance(values, str):
        item_ids = [values.strip()]
    else:
        item_ids = [str(value).strip() for value in (values or [])]
    if not account:
        raise OpsError("必须提供 --account，或设置 XIANYU_ACCOUNT")
    if not item_ids or any(not value for value in item_ids):
        raise OpsError("必须提供 --item-id")
    if not many and len(item_ids) != 1:
        raise OpsError("该操作只接受一个 --item-id")
    return account, item_ids


def launcher_json(check_only: bool = False) -> dict[str, Any]:
    if str(PROJECT) not in sys.path:
        sys.path.insert(0, str(PROJECT))
    from console import runtime
    try:
        return runtime.status() if check_only else runtime.start()
    except (OSError, ValueError) as exc:
        raise OpsError(f"Runtime unavailable: {exc}") from exc


def check_runtime(account: str, api_base: str, cdp_url: str) -> dict[str, Any]:
    health: dict[str, Any] = {"ok": False}
    runtime: dict[str, Any] = {"ok": False}
    cdp_ready = False
    try:
        response = require_object(http_json(api_base, "GET", "/health", timeout=5), "本地健康检查")
        health = {
            "ok": response.get("status") == "healthy",
            "status": response.get("status", "unknown"),
        }
    except OpsError as exc:
        health = {"ok": False, "error": str(exc)}
    if health.get("ok"):
        try:
            response = require_object(
                http_json(api_base, "GET", f"/cookies/{account}/runtime-status", timeout=5),
                "账号运行时状态",
            )
            state = response.get("runtime_status") if isinstance(response.get("runtime_status"), dict) else {}
            runtime = {
                "ok": bool(state.get("running") and state.get("can_attempt_read")) if state.get("backend") == "owned" else all(bool(state.get(key)) for key in ("running", "ws_ready", "session_ready")),
                "running": bool(state.get("running")),
                "ws_ready": bool(state.get("ws_ready")),
                "session_ready": bool(state.get("session_ready")),
                "message_stream_ready": bool(state.get("message_stream_ready")),
                "connection_state": state.get("connection_state", "unknown"),
                "im_transport_mode": state.get("im_transport_mode", "unknown"),
            }
        except OpsError as exc:
            runtime = {"ok": False, "error": str(exc)}
    try:
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/version", timeout=5) as response:
            cdp_ready = response.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        cdp_ready = False
    return {
        "checked_at": now_local(),
        "account": account,
        "api": {"base_url": api_base, **health},
        "runtime": runtime,
        "edge_cdp": {"base_url": cdp_url, "ready": cdp_ready},
        "ok": bool(health.get("ok") and runtime.get("ok") and cdp_ready),
    }


def load_release_module():
    if str(PROJECT) not in sys.path:
        sys.path.insert(0, str(PROJECT))
    from console import marketplace
    return marketplace


def slim_skus(raw_skus: Any) -> list[dict[str, Any]]:
    rows = raw_skus if isinstance(raw_skus, list) else []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        props = row.get("propertyList") if isinstance(row.get("propertyList"), list) else []
        first = props[0] if props and isinstance(props[0], dict) else {}
        result.append(
            {
                "sku_id": row.get("skuId"),
                "price_cents": str(row.get("priceInCent")) if row.get("priceInCent") is not None else None,
                "spec_name": first.get("propertyText"),
                "spec_value": first.get("valueText"),
            }
        )
    return result


PRESERVE_FIELDS = ("price_cents", "quantity", "category_id", "skus")


def known_field(value: Any, field: str | None = None) -> bool:
    """判断字段是否有可用于不变量比较的真实值。"""
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if field == "skus" and not isinstance(value, list):
        return False
    return True


def preserve_field_state(value: dict[str, Any]) -> dict[str, str]:
    return {
        field: "known" if known_field(value.get(field), field) else "unknown"
        for field in PRESERVE_FIELDS
    }


def _editable_sku_values(editable: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str]:
    """Return SKU rows only when editDetail explicitly describes their state."""
    for field in ("itemSkuList", "skuList"):
        raw_skus = editable.get(field)
        if isinstance(raw_skus, list):
            return slim_skus(raw_skus), f"editDetail.{field}"
    # simpleItem is also used by the existing multi-SKU publisher, so it is not
    # sufficient to infer an empty list from that flag or from empty maps.
    # A missing list therefore remains unknown until the platform returns rows
    # (or a stronger, direct no-SKU field is observed).
    return None, "editDetail.sku_list_missing"


def editable_invariant_values(editable: dict[str, Any]) -> dict[str, Any]:
    """从 editDetail 只提取和编辑白名单相关的保留字段。"""
    price_dto = editable.get("itemPriceDTO") if isinstance(editable.get("itemPriceDTO"), dict) else {}
    category = editable.get("itemCatDTO") if isinstance(editable.get("itemCatDTO"), dict) else {}
    skus, _sku_source = _editable_sku_values(editable)
    return {
        "price_cents": price_dto.get("priceInCent"),
        "quantity": editable.get("quantity"),
        "category_id": category.get("catId") or category.get("categoryId"),
        "skus": skus,
    }


def editable_invariant_sources(editable: dict[str, Any]) -> dict[str, str]:
    _skus, sku_source = _editable_sku_values(editable)
    price_source = (
        "editDetail.itemPriceDTO.priceInCent"
        if isinstance(editable.get("itemPriceDTO"), dict)
        and editable["itemPriceDTO"].get("priceInCent") is not None
        else "editDetail.price_missing"
    )
    quantity_source = "editDetail.quantity" if editable.get("quantity") is not None else "editDetail.quantity_missing"
    category = editable.get("itemCatDTO") if isinstance(editable.get("itemCatDTO"), dict) else {}
    category_source = (
        "editDetail.itemCatDTO"
        if category.get("catId") or category.get("categoryId")
        else "editDetail.category_missing"
    )
    return {
        "price_cents": price_source,
        "quantity": quantity_source,
        "category_id": category_source,
        "skus": sku_source,
    }


def editable_invariant_state(value: dict[str, Any]) -> dict[str, str]:
    return {
        field: "known" if known_field(value.get(field), field) else "unknown"
        for field in PRESERVE_FIELDS
    }


def merge_editable_invariants(current: dict[str, Any], editable: dict[str, Any]) -> dict[str, Any]:
    """用真实 editDetail 补齐公开详情可能省略的字段，并保留来源。"""
    values = editable_invariant_values(editable)
    sources = current.setdefault("field_sources", {})
    for field, value in values.items():
        if not known_field(current.get(field), field) and known_field(value, field):
            current[field] = value
            sources[field] = "mtop.idle.pc.idleitem.editDetail"
        elif known_field(current.get(field), field):
            sources.setdefault(field, "mtop.taobao.idle.pc.detail")
    current["editable_invariants"] = values
    current["editable_invariant_state"] = editable_invariant_state(values)
    current["editable_invariant_sources"] = editable_invariant_sources(editable)
    current["field_state"] = preserve_field_state(current)
    return current


def compare_editable_invariants(
    baseline: dict[str, Any], current: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Compare editDetail invariants without substituting public-detail values."""
    expected = baseline.get("editable_invariants") if isinstance(baseline.get("editable_invariants"), dict) else {}
    observed = current.get("editable_invariants") if isinstance(current.get("editable_invariants"), dict) else {}
    checks: dict[str, dict[str, Any]] = {}
    for field in PRESERVE_FIELDS:
        expected_value = expected.get(field)
        observed_value = observed.get(field)
        expected_known = known_field(expected_value, field)
        observed_known = known_field(observed_value, field)
        checks[field] = {
            "baseline": expected_value,
            "current": observed_value,
            "baseline_known": expected_known,
            "current_known": observed_known,
            "unchanged": bool(
                expected_known
                and observed_known
                and canonical(expected_value) == canonical(observed_value)
            ),
        }
    return checks


def slim_platform_item(item_id: str, item: dict[str, Any], observed_at: str) -> dict[str, Any]:
    price_dto = item.get("itemPriceDTO") if isinstance(item.get("itemPriceDTO"), dict) else {}
    category = item.get("itemCatDTO") if isinstance(item.get("itemCatDTO"), dict) else {}
    raw_skus = item.get("skuList")
    raw_images = item.get("imageInfos")
    return {
        "item_id": item_id,
        "title": item.get("title"),
        "description": item.get("desc"),
        "status": item.get("itemStatusStr"),
        "image_urls": (
            [
                image.get("url")
                for image in raw_images
                if isinstance(image, dict) and image.get("url")
            ]
            if isinstance(raw_images, list)
            else None
        ),
        "skus": slim_skus(raw_skus) if isinstance(raw_skus, list) else None,
        "price_cents": price_dto.get("priceInCent") if price_dto else item.get("priceInCent"),
        "quantity": item.get("quantity") if "quantity" in item else item.get("stockQuantity"),
        "category_id": category.get("catId") or category.get("categoryId"),
        "observed_at": observed_at,
        "source": "goofish_mtop_detail",
        "source_updated_at": "unknown",
    }


async def _platform_read_async(account: str, item_id: str) -> dict[str, Any]:
    release = load_release_module()
    try:
        _db, publisher = await release.client({"account": account})
    except Exception as exc:  # account boundary; do not expose response/cookie data
        raise OpsError(f"闲鱼网页回读客户端不可用: {exc}") from exc
    async with publisher:
        result = await publisher._post_mtop(
            api_name="mtop.taobao.idle.pc.detail",
            version="1.0",
            payload={"itemId": item_id},
            spm_cnt="a21ybx.im.0.0",
            spm_pre="a21ybx.home.sidebar.1.46413da6EPl7v5",
        )
        if not publisher.is_success_response(result):
            ret = result.get("ret") if isinstance(result, dict) else None
            raise OpsError(f"闲鱼网页回读失败: {ret or 'unknown response'}")
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        item = data.get("itemDO") if isinstance(data.get("itemDO"), dict) else {}
        if not item:
            raise OpsError("闲鱼网页回读没有 itemDO")
        current = slim_platform_item(item_id, item, now_local())
        # 公共详情经常省略单规格价格或编辑页不变量。读取 editDetail
        # 作为同一回读的一部分；失败时保留 unknown，让 verify 阻断而不是
        # 把两个缺失值比较成“未变化”。
        try:
            editable_result = await publisher._post_mtop(
                api_name="mtop.idle.pc.idleitem.editDetail",
                version="1.0",
                payload={"itemId": item_id},
                spm_cnt="a21ybx.publish.0.0",
                spm_pre="a21ybx.home.sidebar.1.46413da6EPl7v5",
            )
            if not publisher.is_success_response(editable_result):
                current["edit_detail_status"] = "unavailable"
                current["edit_detail_error"] = "editDetail business response failed"
            else:
                editable = editable_result.get("data") if isinstance(editable_result.get("data"), dict) else {}
                if not editable:
                    current["edit_detail_status"] = "unavailable"
                    current["edit_detail_error"] = "editDetail data missing"
                else:
                    current["edit_detail_status"] = "observed"
                    merge_editable_invariants(current, editable)
        except Exception as exc:
            current["edit_detail_status"] = "unavailable"
            current["edit_detail_error"] = str(exc)
        current.setdefault("field_state", preserve_field_state(current))
        return current


def read_platform_item(account: str, item_id: str) -> dict[str, Any]:
    try:
        return asyncio.run(_platform_read_async(account, item_id))
    except OpsError:
        raise
    except Exception as exc:
        raise OpsError(f"闲鱼网页回读异常: {exc}") from exc


def local_item_rows(account: str, api_base: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    response = require_object(http_json(api_base, "GET", f"/items/{account}"), "本地商品列表")
    if response.get("success") is False:
        raise OpsError(f"本地商品列表业务失败: {response.get('message') or response.get('error') or 'unknown'}")
    if not isinstance(response.get("items"), list):
        raise OpsError("本地商品列表缺少 items 数组")
    rows = [row for row in response["items"] if isinstance(row, dict)]
    return rows, None


def summarize_local_item(account: str, item_id: str, api_base: str) -> dict[str, Any]:
    rows, _ = local_item_rows(account, api_base)
    for row in rows:
        if str(row.get("item_id")) == item_id:
            return {
                "item_id": item_id,
                "title": row.get("item_title", ""),
                "description": row.get("item_description", ""),
                "price": row.get("item_price", ""),
                "status": row.get("item_status", row.get("status", "unknown")),
                "captured_at": now_local(),
                "source": "local_8090",
                "source_updated_at": row.get("updated_at", "unknown"),
            }
    return {
        "item_id": item_id,
        "status": "unavailable",
        "captured_at": now_local(),
        "source": "local_8090",
        "source_updated_at": "unknown",
    }


def _parse_display_count(match: re.Match[str] | None) -> int | None:
    if not match:
        return None
    raw = match.group("number").replace(",", "")
    unit = match.group("unit") or ""
    try:
        value = float(raw)
    except ValueError:
        return None
    if unit == "万":
        value *= 10_000
    elif unit == "千":
        value *= 1_000
    elif not value.is_integer():
        # 不把未经单位说明的小数截成一个看似精确的整数。
        return None
    return int(value)


def parse_page_header(body: str) -> dict[str, Any]:
    header = body[:1600]
    for marker in ("为你推荐", "推荐商品", "猜你喜欢", "相关商品", "你可能还喜欢"):
        header = header.split(marker, 1)[0]
    view_match = re.search(
        r"(?<![\d.,])(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>万|千)?\s*浏览",
        header,
    )
    want_match = re.search(
        r"(?<![\d.,])(?P<number>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>万|千)?\s*人想要",
        header,
    )
    browse = _parse_display_count(view_match)
    want = _parse_display_count(want_match)
    return {
        "browse": browse if browse is not None else ("not_visible" if not view_match else "unavailable"),
        "want": want if want is not None else ("not_visible" if not want_match else "unavailable"),
        "parser_version": 2,
        "display_count_evidence": {
            "browse": header[max(0, view_match.start() - 50):view_match.end() + 50] if view_match else None,
            "want": header[max(0, want_match.start() - 50):want_match.end() + 50] if want_match else None,
        },
        "source": "public_item_detail_page",
        "status": (
            "observed"
            if browse is not None or want is not None
            else ("unavailable" if view_match or want_match else "not_visible")
        ),
        "source_updated_at": "unknown",
    }


def verify_page_item(body: str, url: str, item_id: str, expected: dict[str, Any]) -> dict[str, Any]:
    """Require the selected item's copy before using counts from a buyer page."""
    parsed = urllib.parse.urlparse(url)
    identity = (parsed.hostname == "www.goofish.com" and parsed.path == "/item"
                and urllib.parse.parse_qs(parsed.query).get("id") == [str(item_id)])
    content = body
    for marker in ("为你推荐", "推荐商品", "猜你喜欢", "相关商品", "你可能还喜欢"):
        content = content.split(marker, 1)[0]
    normalize = lambda value: re.sub(r"\s+", "", value or "")
    title = normalize(expected.get("title"))
    description = expected.get("description") or ""
    first, separator, remaining = description.partition("\n")
    if expected.get("title_desc_separate") is False and separator and normalize(first) == title:
        description = remaining
    description = normalize(description)
    visible = normalize(content)
    title_visible = bool(title and title in visible)
    description_visible = bool(description and description in visible)
    unavailable = any(marker in content for marker in ("宝贝已下架", "商品已下架", "宝贝不存在", "商品不存在", "审核中", "访问受限", "滑动验证"))
    return {"match": bool(identity and title_visible and description_visible and not unavailable),
            "item_url_match": identity, "title_visible": title_visible,
            "description_visible": description_visible, "unavailable_notice": unavailable}


def public_metrics(item_ids: list[str], cdp_url: str, expected_items: dict[str, dict] | None = None) -> list[dict[str, Any]]:
    """在固定 Edge 中使用独立标签页读取公开详情；每个商品最多读一次。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [
            {
                "item_id": item_id,
                "status": "unavailable",
                "browse": "unavailable",
                "want": "unavailable",
                "source": "public_item_detail_page",
                "error": "Playwright 未安装",
            }
            for item_id in item_ids
        ]
    rows: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(cdp_url)
            if not browser.contexts:
                raise OpsError("固定 Edge 没有可用浏览器上下文")
            page = browser.contexts[0].new_page()
        except Exception as exc:
            return [
                {
                    "item_id": item_id,
                    "status": "unavailable",
                    "browse": "unavailable",
                    "want": "unavailable",
                    "source": "public_item_detail_page",
                    "error": f"固定 Edge 详情页不可用: {exc}",
                }
                for item_id in item_ids
            ]
        try:
            for item_id in item_ids:
                observed_at = now_local()
                url = f"https://www.goofish.com/item?id={item_id}"
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    page.wait_for_timeout(4_000)
                    body = page.locator("body").inner_text(timeout=10_000)
                    metrics = parse_page_header(body)
                    if expected_items is not None:
                        evidence = verify_page_item(body, page.url, item_id, expected_items.get(item_id, {}))
                        metrics["item_evidence"] = evidence
                        if not evidence["match"]:
                            metrics.update({"status": "unavailable", "browse": None, "want": None,
                                            "error": "页面未完整显示目标商品文案，不采用可能来自推荐区域的读数。"})
                    rows.append(
                        {
                            "item_id": item_id,
                            "url": page.url,
                            "captured_at": observed_at,
                            "freshness": "读取时页面值",
                            "notes": "公开详情页读数，不代表卖家后台曝光统计。",
                            **metrics,
                        }
                    )
                except Exception as exc:  # live browser branch
                    rows.append(
                        {
                            "item_id": item_id,
                            "url": url,
                            "captured_at": observed_at,
                            "freshness": "读取失败",
                            "browse": "unavailable",
                            "want": "unavailable",
                            "source": "public_item_detail_page",
                            "status": "unavailable",
                            "source_updated_at": "unknown",
                            "error": str(exc),
                        }
                    )
        finally:
            page.close()
            # sync_playwright 上下文只关闭本客户端；固定 Edge 留给项目运行时。
    return rows


def snapshot_once(
    account: str,
    item_ids: list[str],
    *,
    api_base: str,
    cdp_url: str,
    sync_account: bool,
    output_dir: Path,
) -> tuple[dict[str, Any], Path]:
    captured_at = now_local()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{stamp}-{safe_id(account)}-snapshot.json"
    snapshot: dict[str, Any] = {
        "schema": "xianyu-live-snapshot-v2",
        "captured_at": captured_at,
        "account": account,
        "item_ids": item_ids,
        "runtime": {"local_api": api_base, "edge_cdp": cdp_url},
        "source_status": {},
        "items": {},
        "public_detail_metrics": [],
        "orders": {},
        "exposure": {
            "value": "unavailable",
            "source": "seller_backend",
            "status": "unavailable",
            "notes": "本地 8090 与公开详情页没有可对齐的卖家曝光看板字段。",
        },
    }
    atomic_json(path, snapshot)

    if sync_account:
        try:
            snapshot["sync"] = require_success_object(
                http_json(
                    api_base,
                    "POST",
                    "/items/get-all-from-account",
                    {"cookie_id": account},
                    timeout=60,
                ),
                "账号商品同步",
            )
            snapshot["source_status"]["account_sync"] = "ok"
        except OpsError as exc:
            snapshot["source_status"]["account_sync"] = {"status": "unavailable", "error": str(exc)}
    else:
        snapshot["source_status"]["account_sync"] = "skipped"
    atomic_json(path, snapshot)

    try:
        rows, _ = local_item_rows(account, api_base)
        by_id = {str(row.get("item_id")): row for row in rows}
        missing_item_ids: list[str] = []
        for item_id in item_ids:
            row = by_id.get(item_id)
            if row is None:
                missing_item_ids.append(item_id)
            snapshot["items"][item_id] = (
                {
                    "item_id": item_id,
                    "title": row.get("item_title", ""),
                    "price": row.get("item_price", ""),
                    "status": row.get("item_status", row.get("status", "unknown")),
                    "source": "local_8090",
                    "captured_at": captured_at,
                    "source_updated_at": row.get("updated_at", "unknown"),
                }
                if row
                else {
                    "item_id": item_id,
                    "source": "local_8090",
                    "status": "unavailable",
                    "captured_at": captured_at,
                    "source_updated_at": "unknown",
                }
            )
        snapshot["source_status"]["local_items"] = (
            "ok"
            if not missing_item_ids
            else {"status": "partial", "missing_item_ids": missing_item_ids}
        )
    except OpsError as exc:
        snapshot["source_status"]["local_items"] = {"status": "unavailable", "error": str(exc)}
        for item_id in item_ids:
            snapshot["items"][item_id] = {
                "item_id": item_id,
                "source": "local_8090",
                "status": "unavailable",
                "captured_at": captured_at,
                "source_updated_at": "unknown",
            }
    atomic_json(path, snapshot)

    try:
        order_response = require_object(http_json(api_base, "GET", "/api/orders", timeout=20), "订单接口")
        if order_response.get("success") is not True:
            raise OpsError(f"订单接口业务失败: {order_response.get('message') or order_response.get('error') or 'unknown'}")
        orders = order_response.get("data")
        if not isinstance(orders, list):
            raise OpsError("订单接口缺少 data 数组")
        for item_id in item_ids:
            statuses = Counter(
                str(row.get("order_status", "unknown"))
                for row in orders
                if isinstance(row, dict) and str(row.get("item_id")) == item_id
            )
            snapshot["orders"][item_id] = {
                "all_records": sum(statuses.values()),
                "statuses": dict(statuses),
                "source": "local_8090_orders",
                "captured_at": captured_at,
                "source_updated_at": "unknown",
                "notes": "订单记录不是曝光、浏览或想要统计。",
            }
        snapshot["source_status"]["orders"] = "ok"
    except OpsError as exc:
        snapshot["source_status"]["orders"] = {"status": "unavailable", "error": str(exc)}
    atomic_json(path, snapshot)

    snapshot["public_detail_metrics"] = public_metrics(item_ids, cdp_url)
    public_ok = all(row.get("status") not in {"unavailable"} for row in snapshot["public_detail_metrics"])
    snapshot["source_status"]["public_detail"] = "ok" if public_ok else "partial"
    snapshot["status"] = "complete" if all(value == "ok" or value == "skipped" for value in snapshot["source_status"].values()) else "partial"
    atomic_json(path, snapshot)
    return snapshot, path


def latest_package(account: str, item_id: str) -> Path:
    candidates = sorted(PACKAGE_DIR.glob(f"{safe_id(account)}-{safe_id(item_id)}-*.json"))
    if not candidates:
        raise OpsError(f"没有找到 {account}/{item_id} 的改动包，请先运行 prepare")
    return candidates[-1]


def select_active_package(account: str, item_id: str) -> Path | None:
    """为每日采集确定选择当前商品的唯一已线上核验改动包。"""
    candidates = sorted(PACKAGE_DIR.glob(f"{safe_id(account)}-{safe_id(item_id)}-*.json"), reverse=True)
    for path in candidates:
        try:
            package = read_package(path)
            verify = latest_verify(package)
        except OpsError:
            continue
        if not verify:
            continue
        online_verified = bool(
            verify.get("online_verified")
            or verify.get("status") in {"verified", "online_verified_pending_local_sync"}
        )
        if online_verified and verify.get("package_sha256") == package.get("package_sha256"):
            return path
    return None


def latest_snapshot_for(account: str, item_id: str) -> tuple[dict[str, Any], Path] | None:
    """找到同账号、同商品的真实快照；无匹配时返回 None。"""
    candidates = sorted(SNAPSHOT_DIR.glob(f"*-{safe_id(account)}-snapshot.json"), reverse=True)
    for path in candidates:
        try:
            snapshot = load_json(path)
        except OpsError:
            continue
        if snapshot.get("account") != account:
            continue
        item_ids = snapshot.get("item_ids")
        if isinstance(item_ids, list) and item_id in {str(value) for value in item_ids}:
            snapshot.setdefault("output_file", str(path))
            return snapshot, path
    return None


def snapshot_reference(snapshot: dict[str, Any], path: Path, item_id: str) -> dict[str, Any]:
    return {
        "file": str(path),
        "sha256": sha256_file(path),
        "account": snapshot.get("account"),
        "item_id": item_id,
        "captured_at": snapshot.get("captured_at"),
        "status": snapshot.get("status", "unknown"),
    }


def package_hash(package: dict[str, Any]) -> str:
    content = {key: value for key, value in package.items() if key != "package_sha256"}
    return stable_hash(content)


def read_package(path: Path) -> dict[str, Any]:
    package = load_json(path)
    expected = package.get("package_sha256")
    if not expected or expected != package_hash(package):
        raise OpsError(f"改动包哈希不匹配，拒绝继续: {path}")
    return package


def evidence_path(package: dict[str, Any], suffix: str) -> Path:
    stem = safe_id(str(package.get("package_id", "package")))
    return EVIDENCE_DIR / f"{stem}-{suffix}.json"


def evidence_html_path(package: dict[str, Any], suffix: str) -> Path:
    """Return the HTML companion path without colliding with JSON evidence."""
    return evidence_path(package, suffix).with_suffix(".html")


def desired_from_args(args: argparse.Namespace) -> tuple[str, str, Path | None]:
    if args.copy_file:
        title, description = parse_copy_file(Path(args.copy_file))
    else:
        title = (args.title or "").strip()
        description = (args.description or "").strip()
        if not title or not description:
            raise OpsError("prepare 必须提供 --copy-file，或同时提供 --title 与 --description")
    image = Path(args.image_file).resolve() if args.image_file else None
    if image and (not image.is_file() or image.stat().st_size <= 0):
        raise OpsError(f"商品图片不存在或为空: {image}")
    return title, description, image


def cmd_check(args: argparse.Namespace) -> int:
    account = (args.account or "").strip()
    if not account:
        raise OpsError("必须提供 --account，或设置 XIANYU_ACCOUNT")
    result = check_runtime(account, args.api_base, args.cdp_url)
    result["summary"] = "运行时可用，可继续读取商品真实状态。" if result["ok"] else "运行时未完全就绪，先处理缺失的接口、Edge 或账号会话。"
    print_json(result)
    return 0 if result["ok"] else 2


def cmd_snapshot(args: argparse.Namespace) -> int:
    account, item_ids = require_target(args, many=True)
    runtime = check_runtime(account, args.api_base, args.cdp_url)
    snapshot, path = snapshot_once(
        account,
        item_ids,
        api_base=args.api_base,
        cdp_url=args.cdp_url,
        sync_account=not args.no_sync,
        output_dir=Path(args.output_dir).resolve() if args.output_dir else SNAPSHOT_DIR,
    )
    snapshot["runtime_check"] = runtime
    snapshot["output_file"] = str(path)
    snapshot["summary"] = (
        "本轮真实来源已保存，可用于复盘。"
        if snapshot.get("status") == "complete"
        else "快照已保存，但至少一个来源不可用；缺口保持 unavailable，不能按 0 处理。"
    )
    atomic_json(path, snapshot)
    print_json(snapshot)
    return 0 if snapshot.get("status") == "complete" else 2


def write_prepare_html(path: Path, package: dict[str, Any], result: dict[str, Any]) -> None:
    """Write the human review page next to the machine-readable package."""
    baseline = package.get("baseline_live") if isinstance(package.get("baseline_live"), dict) else {}
    desired = package.get("desired") if isinstance(package.get("desired"), dict) else {}
    editable = baseline.get("editable_invariants") if isinstance(baseline.get("editable_invariants"), dict) else {}
    sources = baseline.get("editable_invariant_sources") if isinstance(baseline.get("editable_invariant_sources"), dict) else {}
    field_sources = baseline.get("field_sources") if isinstance(baseline.get("field_sources"), dict) else {}
    image = package.get("image") if isinstance(package.get("image"), dict) else {}
    image_path = Path(str(image.get("path", "")))
    image_src = image_path.as_uri() if image_path.is_file() else str(image.get("path", ""))
    current_image = (baseline.get("image_urls") or [""])[0]
    rows = [
        ("公开详情库存", baseline.get("quantity"), "goofish_mtop_detail"),
        ("编辑库存", editable.get("quantity"), sources.get("quantity", "unknown")),
        ("线上主价格字段（分）", baseline.get("price_cents"), field_sources.get("price_cents", "unknown")),
        ("编辑价格（分）", editable.get("price_cents"), sources.get("price_cents", "unknown")),
        ("公开类目", baseline.get("category_id"), "goofish_mtop_detail"),
        ("编辑类目", editable.get("category_id"), sources.get("category_id", "unknown")),
        ("SKU", "unknown" if editable.get("skus") is None else editable.get("skus"), sources.get("skus", "unknown")),
    ]
    row_html = "".join(
        f"<tr><th>{html.escape(str(name))}</th><td><code>{html.escape(str(value))}</code></td><td>{html.escape(str(source))}</td></tr>"
        for name, value, source in rows
    )
    missing = result.get("baseline_missing", []) + result.get("editable_missing", [])
    status = result.get("status", "unknown")
    if missing:
        decision = "自动 apply 已阻断：" + ", ".join(str(item) for item in missing)
    else:
        decision = "改动包已具备字段核对条件；仍需用户确认哈希后才可 apply。"
    document = f"""<!doctype html>
<meta charset='utf-8'>
<title>闲鱼 DSH 改动包预览</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1120px;margin:2rem auto;line-height:1.55}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}th,td{{border:1px solid #ccc;padding:.5rem;text-align:left;vertical-align:top}}th{{background:#f5f5f5;width:18%}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:2rem}}.panel{{border:1px solid #ddd;padding:1rem;border-radius:.4rem}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}img{{max-width:100%;max-height:600px;object-fit:contain;border:1px solid #ccc}}.blocked{{background:#fff4e5;border-left:4px solid #e09b00;padding:.8rem}}code{{overflow-wrap:anywhere}}</style>
<h1>闲鱼 DSH 商品改动包预览</h1>
<p><b>账号 / 商品：</b><code>{html.escape(str(package.get('account')))} / {html.escape(str(package.get('item_id')))}</code></p>
<p><b>改动包：</b><code>{html.escape(str(package.get('package_id')))}</code><br><b>哈希：</b><code>{html.escape(str(package.get('package_sha256')))}</code><br><b>状态：</b><code>{html.escape(str(status))}</code></p>
<div class='blocked'><b>{html.escape(decision)}</b><br>公开详情库存与 editDetail 编辑库存必须分别比较；SKU 缺少可核对列表时保持 unknown。用户可在闲鱼 App 保留原规格人工完成文案/主图修改，但当前 verify 仍会因 SKU unknown 阻断完整验收；72 小时观察计时只有在 online_verified 成立后才开始。</div>
<h2>保留字段对照</h2>
<table><thead><tr><th>字段</th><th>当前值</th><th>来源</th></tr></thead><tbody>{row_html}</tbody></table>
<p>公开详情库存为 <b>{html.escape(str(baseline.get('quantity')))}</b>，编辑页库存为 <b>{html.escape(str(editable.get('quantity')))}</b>。只看到公开库存不构成编辑库存不变量证据。</p>
<div class='grid'><section class='panel'><h2>改前标题</h2><pre>{html.escape(str(baseline.get('title', '')))}</pre><h2>改前介绍</h2><pre>{html.escape(str(baseline.get('description', '')))}</pre></section><section class='panel'><h2>改后标题</h2><pre>{html.escape(str(desired.get('title', '')))}</pre><h2>改后介绍</h2><pre>{html.escape(str(desired.get('description', '')))}</pre></section></div>
<h2>主图</h2><p><b>当前主图（公开详情首 URL）：</b><br><code>{html.escape(str(current_image))}</code></p><p><b>改后素材：</b><br><code>{html.escape(str(image.get('path')))}</code><br>SHA-256: <code>{html.escape(str(image.get('sha256')))}</code></p><img src="{html.escape(image_src, quote=True)}" alt="改后主图素材">
<h2>人工操作边界</h2><ol><li>用户先确认这组标题、介绍和主图。</li><li>SKU unknown 时不调用自动 apply；若平台网页不支持，按本页内容在闲鱼 App 修改，并保留原规格与编辑库存。</li><li>修改前先在闲鱼 App 核对并保存 baseline 人工规格记录；修改后另行核对并保存 current 记录，再重新执行 verify。人工证据独立保留，不填充机器未知字段；标题、介绍、在线状态、价格、库存、类目、图片及其他真实回读仍需通过。</li><li>editDetail 缺失、编辑库存变化、公开 SKU/价格/类目变化或图片证据不足时不会自动通过；在 online_verified 之前不启动 72 小时复盘计时。</li></ol>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def cmd_prepare(args: argparse.Namespace) -> int:
    account, item_ids = require_target(args)
    item_id = item_ids[0]
    title, description, image = desired_from_args(args)
    runtime = check_runtime(account, args.api_base, args.cdp_url)
    if not runtime["ok"]:
        raise OpsError("prepare 需要健康的真实运行时；先运行 ensure-runtime.ps1")
    baseline_live = read_platform_item(account, item_id)
    try:
        baseline_local = summarize_local_item(account, item_id, args.api_base)
    except OpsError as exc:
        baseline_local = {"item_id": item_id, "status": "unavailable", "error": str(exc)}
    try:
        baseline_snapshot, baseline_snapshot_path = snapshot_once(
            account,
            [item_id],
            api_base=args.api_base,
            cdp_url=args.cdp_url,
            sync_account=False,
            output_dir=SNAPSHOT_DIR,
        )
        baseline_snapshot_ref = snapshot_reference(baseline_snapshot, baseline_snapshot_path, item_id)
    except OpsError as exc:
        baseline_snapshot_ref = {
            "status": "unavailable",
            "error": str(exc),
            "account": account,
            "item_id": item_id,
        }
    package_id = f"{safe_id(account)}-{safe_id(item_id)}-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}"
    package: dict[str, Any] = {
        "schema": "xianyu-change-package-v1",
        "package_id": package_id,
        "created_at": now_local(),
        "account": account,
        "item_id": item_id,
        "baseline_live": baseline_live,
        "baseline_live_hash": stable_hash(baseline_live),
        "baseline_local": baseline_local,
        "baseline_snapshot": baseline_snapshot_ref,
        "desired": {"title": title, "description": description},
        "image": (
            {
                "path": str(image),
                "sha256": sha256_file(image),
                "bytes": image.stat().st_size,
            }
            if image
            else None
        ),
        "approval": {
            "required": True,
            "state": "pending",
            "instruction": "用户确认该具体商品改动包后，apply 才允许调用平台编辑路由。",
        },
        "verify_policy": {
            "preserve": ["price_cents", "quantity", "category_id", "skus"],
            "image": "平台 CDN 变换不能用源文件 SHA 直接硬判；无法建立证据时保持 pending_image_review。",
            "local_sync": "仅在线上标题、介绍、状态、价格/SKU/库存不变量和图片证据满足后执行。",
        },
    }
    package["package_sha256"] = package_hash(package)
    path = PACKAGE_DIR / f"{package_id}.json"
    atomic_json(path, package)
    missing_baseline = [field for field in PRESERVE_FIELDS if not known_field(baseline_live.get(field), field)]
    editable_values = baseline_live.get("editable_invariants") if isinstance(baseline_live.get("editable_invariants"), dict) else {}
    editable_missing: list[str] = []
    if baseline_live.get("edit_detail_status") != "observed":
        editable_missing.append("edit_detail")
    elif not known_field(editable_values.get("quantity"), "quantity"):
        editable_missing.append("editable_quantity")
    all_missing = missing_baseline + editable_missing
    baseline_readiness = "ready" if not all_missing else "blocked_missing_preserve_fields"
    result = {
        "status": baseline_readiness,
        "package": str(path),
        "package_sha256": package["package_sha256"],
        "account": account,
        "item_id": item_id,
        "baseline": {
            "title": baseline_live.get("title"),
            "description": baseline_live.get("description"),
            "status": baseline_live.get("status"),
            "images": baseline_live.get("image_urls", []),
            "preserve_fields": baseline_live.get("field_state", preserve_field_state(baseline_live)),
            "public_quantity": baseline_live.get("quantity"),
            "editable_invariants": baseline_live.get("editable_invariants", {}),
            "editable_invariant_state": baseline_live.get("editable_invariant_state", {}),
            "editable_invariant_sources": baseline_live.get("editable_invariant_sources", {}),
            "snapshot": package["baseline_snapshot"],
        },
        "after": {
            "title": title,
            "description": description,
            "image": package["image"],
        },
        "baseline_missing": missing_baseline,
        "editable_missing": editable_missing,
        "summary": (
            "具体商品改动包已生成，等待用户对这组标题、介绍和图片做一次明确确认。"
            if not all_missing
            else "改动包已保留，但线上基线缺少可核对保留字段；禁止确认或 apply，先补齐真实价格/库存/类目/SKU 回读。"
        ),
    }
    preview_path = path.with_name(path.stem + "-preview.html")
    write_prepare_html(preview_path, package, result)
    result["preview_html"] = str(preview_path)
    print_json(result)
    return 0 if not all_missing else 2


def _mtop_call(release: Any, publisher: Any, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    async def call() -> dict[str, Any]:
        result = await publisher._post_mtop(
            api_name=name,
            version="1.0",
            payload=payload,
            spm_cnt="a21ybx.publish.0.0",
            spm_pre="a21ybx.home.sidebar.1.46413da6EPl7v5",
        )
        if not publisher.is_success_response(result):
            ret = result.get("ret") if isinstance(result, dict) else None
            raise OpsError(f"平台调用 {name} 被拒绝: {ret or 'unknown response'}")
        return result

    return asyncio.get_event_loop().run_until_complete(call())


async def _apply_async(package: dict[str, Any], approval_note: str, attempt_path: Path) -> dict[str, Any]:
    release = load_release_module()
    account = str(package["account"])
    item_id = str(package["item_id"])
    desired = package["desired"]
    image_info = package.get("image") if isinstance(package.get("image"), dict) else None
    baseline = package.get("baseline_live") if isinstance(package.get("baseline_live"), dict) else {}
    missing_baseline = [field for field in PRESERVE_FIELDS if not known_field(baseline.get(field), field)]
    if missing_baseline:
        raise OpsError(
            "改动包线上基线缺少可核对保留字段，拒绝提交: " + ", ".join(missing_baseline)
        )
    _db, publisher = await release.client({"account": account})
    async with publisher:
        before_result = await publisher._post_mtop(
            api_name="mtop.taobao.idle.pc.detail",
            version="1.0",
            payload={"itemId": item_id},
            spm_cnt="a21ybx.im.0.0",
            spm_pre="a21ybx.home.sidebar.1.46413da6EPl7v5",
        )
        if not publisher.is_success_response(before_result):
            raise OpsError("apply 前线上基线回读失败，未提交修改")
        before_data = before_result.get("data") if isinstance(before_result.get("data"), dict) else {}
        before_item = before_data.get("itemDO") if isinstance(before_data.get("itemDO"), dict) else {}
        if not before_item:
            raise OpsError("apply 前线上基线没有 itemDO，未提交修改")
        before = slim_platform_item(item_id, before_item, now_local())
        public_baseline_complete = all(
            known_field(before.get(field), field) and known_field(baseline.get(field), field)
            for field in PRESERVE_FIELDS
        )
        if public_baseline_complete and stable_hash(before) != package["baseline_live_hash"]:
            raise OpsError("apply 前线上基线已变化，拒绝覆盖；请重新 prepare")
        detail_result = await publisher._post_mtop(
            api_name="mtop.idle.pc.idleitem.editDetail",
            version="1.0",
            payload={"itemId": item_id},
            spm_cnt="a21ybx.publish.0.0",
            spm_pre="a21ybx.home.sidebar.1.46413da6EPl7v5",
        )
        if not publisher.is_success_response(detail_result):
            raise OpsError("平台编辑详情读取失败，未提交修改")
        original = detail_result.get("data") if isinstance(detail_result.get("data"), dict) else {}
        if str(original.get("itemId")) != item_id:
            raise OpsError("平台编辑详情的 item_id 不一致，未提交修改")
        merge_editable_invariants(before, original)
        editable_checks = compare_editable_invariants(baseline, before)
        editable_baseline = baseline.get("editable_invariants")
        if isinstance(editable_baseline, dict):
            editable_drift = [
                field
                for field, check in editable_checks.items()
                if check["baseline_known"] and not check["unchanged"]
            ]
            if editable_drift:
                raise OpsError(
                    "apply 前线上 editDetail 保留字段已变化或缺少可核对值，未提交修改: "
                    + ", ".join(editable_drift)
                )
        missing_before = [field for field in PRESERVE_FIELDS if not known_field(before.get(field), field)]
        if missing_before:
            raise OpsError(
                "apply 前线上回读缺少可核对保留字段，未提交修改: " + ", ".join(missing_before)
            )
        if stable_hash(before) != package["baseline_live_hash"]:
            raise OpsError("apply 前线上基线已变化，拒绝覆盖；请重新 prepare")
        invariant_names = ("itemSkuList", "itemProperties", "itemPriceDTO", "quantity", "itemCatDTO", "userRightsProtocols")
        invariants = {name: stable_hash(original.get(name)) for name in invariant_names}
        uploaded: dict[str, Any] | None = None
        payload = copy.deepcopy(original)
        payload["itemTextDTO"] = {
            "title": desired["title"],
            "desc": desired["description"],
            "titleDescSeparate": True,
        }
        if image_info:
            image_path = Path(str(image_info["path"]))
            if not image_path.is_file() or sha256_file(image_path) != image_info["sha256"]:
                raise OpsError("改动包中的图片文件已变化，拒绝提交")
            uploaded = await publisher.prepare_image_for_publish(
                {"content": image_path.read_bytes(), "filename": image_path.name}
            )
            payload["imageInfoDOList"] = [
                {
                    "extraInfo": {"isH": "false", "isT": "false", "raw": "false"},
                    "isQrCode": False,
                    "url": uploaded["url"],
                    "heightSize": uploaded["height"],
                    "widthSize": uploaded["width"],
                    "major": True,
                    "type": 0,
                    "status": "done",
                }
            ]
        payload.update(
            uniqueCode=publisher._build_unique_code(),
            sourceId="pcMainPublish",
            bizcode="pcMainPublish",
            publishScene="pcMainPublish",
        )
        allowed = {"itemTextDTO", "imageInfoDOList", "uniqueCode", "sourceId", "bizcode", "publishScene"}
        if any(payload.get(key) != value for key, value in original.items() if key not in allowed):
            raise OpsError("待提交 payload 改动超出标题、介绍、图片和发布标识白名单")
        attempt = {
            "schema": "xianyu-change-attempt-v1",
            "package_id": package["package_id"],
            "package_sha256": package["package_sha256"],
            "account": account,
            "item_id": item_id,
            "state": "submitted_or_uncertain",
            "started_at": now_local(),
            "approval_note": approval_note,
            "changes": ["title", "description"] + (["main_image"] if image_info else []),
            "baseline_hash": package["baseline_live_hash"],
            "preserved_invariant_hashes": invariants,
            "uploaded_image_url": uploaded.get("url") if uploaded else None,
        }
        atomic_json(attempt_path, attempt)
        try:
            result = await publisher._post_mtop(
                api_name="mtop.idle.pc.idleitem.edit",
                version="1.0",
                payload=payload,
                spm_cnt="a21ybx.publish.0.0",
                spm_pre="a21ybx.home.sidebar.1.46413da6EPl7v5",
            )
        except Exception as exc:
            atomic_json(
                evidence_path(package, "response"),
                {"status": "transport_uncertain", "package_id": package["package_id"], "error": str(exc)},
            )
            raise OpsError("平台编辑提交结果不确定；已保存 attempt，先运行 verify，禁止重复提交") from exc
        response = {
            "package_id": package["package_id"],
            "package_sha256": package["package_sha256"],
            "ret": result.get("ret"),
            "accepted": bool(publisher.is_success_response(result)),
            "item_id": result.get("data", {}).get("itemId") if isinstance(result.get("data"), dict) else None,
            "verification_required": result.get("data", {}).get("retNeedIntoVerifyPage") if isinstance(result.get("data"), dict) else None,
            "uploaded_image_url": uploaded.get("url") if uploaded else None,
            "checked_at": now_local(),
        }
        atomic_json(evidence_path(package, "response"), response)
        return response


def cmd_apply(args: argparse.Namespace) -> int:
    package_path = Path(args.package).resolve() if args.package else None
    if package_path is None:
        account, item_ids = require_target(args)
        package_path = latest_package(account, item_ids[0])
    package = read_package(package_path)
    if args.approval_hash != package["package_sha256"]:
        raise OpsError("批准哈希与改动包不一致，拒绝平台写入")
    note = (args.approval_note or "").strip()
    if not note:
        raise OpsError("apply 必须提供 --approval-note，记录用户对具体改动包的确认")
    attempt_path = evidence_path(package, "attempt")
    if attempt_path.exists():
        raise OpsError(f"该改动包已有 attempt，先 verify，禁止重复提交: {attempt_path}")
    runtime = check_runtime(str(package["account"]), args.api_base, args.cdp_url)
    if not runtime["ok"]:
        raise OpsError("apply 需要健康的真实运行时")
    approval = {
        "package_id": package["package_id"],
        "package_sha256": package["package_sha256"],
        "approved_at": now_local(),
        "approval_note": note,
        "scope": "single_concrete_listing_change",
    }
    atomic_json(evidence_path(package, "approval"), approval)
    try:
        response = asyncio.run(_apply_async(package, note, attempt_path))
    except OpsError:
        raise
    result = {
        "status": "submitted" if response.get("accepted") else "rejected_or_pending",
        "package": str(package_path),
        "package_sha256": package["package_sha256"],
        "response": response,
        "summary": "平台已接受一次提交，下一步必须 verify；若显示网页不支持，请转手机 App 修改后再 verify。"
        if response.get("accepted")
        else "平台拒绝或要求其他入口；attempt 已保留，禁止盲目重提。",
    }
    print_json(result)
    return 0 if response.get("accepted") else 2


def current_image_state(package: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    image = package.get("image") if isinstance(package.get("image"), dict) else None
    urls = current.get("image_urls")
    baseline = package.get("baseline_live") if isinstance(package.get("baseline_live"), dict) else {}
    baseline_urls = baseline.get("image_urls")
    if not isinstance(urls, list):
        return {
            "status": "unavailable",
            "current_urls": urls,
            "baseline_urls": baseline_urls,
            "notes": "线上回读没有 image_urls 数组，不能把缺失图片当成未变化。",
        }
    if not image:
        if not isinstance(baseline_urls, list):
            return {
                "status": "unavailable",
                "current_urls": urls,
                "baseline_urls": baseline_urls,
                "notes": "改动包基线没有 image_urls 数组，不能验证未请求图片时的保留不变量。",
            }
        unchanged = canonical(urls) == canonical(baseline_urls)
        return {
            "status": "not_requested" if unchanged else "changed",
            "current_urls": urls,
            "baseline_urls": baseline_urls,
            "unchanged": unchanged,
        }
    expected_path = Path(str(image.get("path")))
    expected_available = expected_path.is_file() and sha256_file(expected_path) == image.get("sha256")
    if not expected_available:
        return {
            "status": "pending_image_review",
            "current_urls": urls,
            "expected_file": image.get("path"),
            "expected_sha256": image.get("sha256"),
            "notes": "改动包期望图片文件不可核对，不能形成图片审阅证据。",
        }
    uploaded_url = None
    attempt_path = evidence_path(package, "attempt")
    if attempt_path.exists():
        attempt = load_json(attempt_path)
        uploaded_url = attempt.get("uploaded_image_url")
    # The first URL is the platform's main image. A URL appearing later in
    # the gallery does not prove that the requested main-image replacement
    # took effect, so keep the evidence bound to position zero.
    main_url = urls[0] if urls else None
    if uploaded_url and main_url and uploaded_url == main_url:
        return {"status": "verified_url", "current_urls": urls, "matched_url": uploaded_url}
    if not urls:
        return {"status": "unavailable", "current_urls": []}
    review_path = evidence_path(package, "image-review")
    if review_path.exists():
        try:
            review = load_json(review_path)
        except OpsError:
            review = {}
        current_file = Path(str(review.get("current_file", ""))) if review.get("current_file") else None
        review_urls = review.get("current_urls") if isinstance(review.get("current_urls"), list) else []
        review_matches = (
            review.get("package_sha256") == package.get("package_sha256")
            and review.get("decision") == "match"
            and review.get("expected_sha256") == image.get("sha256")
            and review.get("current_url") == main_url
            and review_urls
            and review_urls[0] == main_url
            and current_file is not None
            and current_file.is_file()
            and sha256_file(current_file) == review.get("current_file_sha256")
        )
        if review_matches:
            return {
                "status": "verified_visual",
                "current_urls": urls,
                "matched_url": review.get("current_url"),
                "review_file": str(review_path),
                "review_html_file": str(evidence_html_path(package, "image-review")),
                "current_file": str(current_file),
                "current_file_sha256": review.get("current_file_sha256"),
            }
    return {
        "status": "pending_image_review",
        "current_urls": urls,
        "expected_file": image.get("path"),
        "expected_sha256": image.get("sha256"),
        "review_file": str(review_path),
        "review_html_file": str(evidence_html_path(package, "image-review")),
        "notes": "当前图片 URL 与准备包没有可证明的对应关系；请打开审阅页并用实际当前图片文件落证，不能用口述替代。",
    }


def write_image_review_html(path: Path, package: dict[str, Any], current: dict[str, Any], state: dict[str, Any]) -> None:
    """生成可打开的期望图/当前图对照页，供实际视觉核验后落证。"""
    # Keep a defensive suffix guard for callers carrying a legacy JSON path;
    # HTML must never overwrite the machine-readable review evidence.
    if path.suffix.lower() != ".html":
        path = path.with_suffix(".html")
    image = package.get("image") if isinstance(package.get("image"), dict) else {}
    expected_path = Path(str(image.get("path", "")))
    expected_src = html.escape(expected_path.as_uri() if expected_path.is_file() else str(image.get("path", "")))
    current_urls = current.get("image_urls") if isinstance(current.get("image_urls"), list) else []
    current_rows = "".join(
        f"<figure><figcaption>{html.escape(str(url))}</figcaption>"
        f"<img src=\"{html.escape(str(url), quote=True)}\" alt=\"当前线上图片\"></figure>"
        for url in current_urls
    ) or "<p>线上没有可显示的当前图片 URL。</p>"
    document = f"""<!doctype html>
<meta charset='utf-8'>
<title>闲鱼图片人工核验</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto}}img{{max-width:480px;max-height:480px;object-fit:contain;border:1px solid #ccc}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:2rem}}code{{overflow-wrap:anywhere}}</style>
<h1>闲鱼图片人工核验</h1>
<p>商品：<code>{html.escape(str(package.get('account')))} / {html.escape(str(package.get('item_id')))}</code></p>
<p>改动包：<code>{html.escape(str(package.get('package_sha256')))}</code></p>
<p>当前状态：<code>{html.escape(str(state.get('status')))}</code>；URL 变化或文件哈希变化后必须重新核验。</p>
<div class='grid'>
<section><h2>期望图片</h2><p><code>{html.escape(str(image.get('path')))}</code><br>SHA-256: <code>{html.escape(str(image.get('sha256')))}</code></p><img src="{expected_src}" alt="改动包期望图片"></section>
<section><h2>当前线上图片</h2>{current_rows}</section>
</div>
<h2>落证方式</h2><p>实际查看当前图与期望图后，把当前图保存为本地文件，再运行 <code>image-review</code>；仅填写口述或备注不能通过。</p>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def cmd_image_review(args: argparse.Namespace) -> int:
    package_path = Path(args.package).resolve() if args.package else None
    if package_path is None:
        account, item_ids = require_target(args)
        package_path = latest_package(account, item_ids[0])
    package = read_package(package_path)
    image = package.get("image") if isinstance(package.get("image"), dict) else None
    if not image:
        raise OpsError("该改动包没有图片改动，不需要图片人工核验")
    expected_path = Path(str(image.get("path")))
    if not expected_path.is_file() or sha256_file(expected_path) != image.get("sha256"):
        raise OpsError("改动包期望图片文件不可核对，不能写入图片审阅证据")
    current_file = Path(args.current_file).resolve()
    if not current_file.is_file() or current_file.stat().st_size <= 0:
        raise OpsError(f"当前图片证据文件不存在或为空: {current_file}")
    verify = latest_verify(package)
    if not verify:
        raise OpsError("尚无 verify 回读记录；先 verify 获取当前线上图片 URL")
    current = verify.get("current") if isinstance(verify.get("current"), dict) else {}
    current_urls = current.get("image_urls") if isinstance(current.get("image_urls"), list) else []
    current_url = (args.current_url or "").strip()
    if not current_url or current_url not in current_urls:
        raise OpsError("--current-url 必须来自最近一次 verify 的当前图片 URL；URL 变化后重新 verify")
    if not current_urls or current_url != current_urls[0]:
        raise OpsError("--current-url 必须是最近一次 verify 回读的主图首个 URL；图片顺序变化后必须重新核验")
    reviewed_by = (args.reviewed_by or "").strip()
    if not reviewed_by:
        raise OpsError("image-review 必须提供 --reviewed-by，记录实际查看者")
    decision = args.decision
    review = {
        "schema": "xianyu-image-review-v1",
        "package_id": package["package_id"],
        "package_sha256": package["package_sha256"],
        "account": package["account"],
        "item_id": package["item_id"],
        "expected_file": str(expected_path),
        "expected_sha256": image["sha256"],
        "current_url": current_url,
        "current_urls": current_urls,
        "current_file": str(current_file),
        "current_file_sha256": sha256_file(current_file),
        "current_file_bytes": current_file.stat().st_size,
        "decision": decision,
        "reviewed_by": reviewed_by,
        "reviewed_at": now_local(),
        "method": "manual_visual_review_expected_vs_current_file",
        "review_note": (args.review_note or "").strip(),
    }
    atomic_json(evidence_path(package, "image-review"), review)
    print_json({"status": "recorded", "review": review, "next": "重新运行 verify 让图片证据参与完整验收。"})
    return 0


def sku_review_state_hash(current: dict[str, Any]) -> str:
    """Hash the current readback state that makes a manual SKU review stale."""
    # Do not use stable_hash here: canonical() intentionally removes
    # editable_invariants and other provenance fields for package equality.
    # A manual review must become stale when either the public SKU list or an
    # editDetail value changes, so hash the selected business state directly.
    state = {
        "item_id": current.get("item_id"),
        "title": current.get("title"),
        "description": current.get("description"),
        "status": current.get("status"),
        "image_urls": current.get("image_urls"),
        "price_cents": current.get("price_cents"),
        "quantity": current.get("quantity"),
        "category_id": current.get("category_id"),
        "skus": current.get("skus"),
        "editable_invariants": current.get("editable_invariants"),
        "editable_invariant_state": current.get("editable_invariant_state"),
        "edit_detail_status": current.get("edit_detail_status"),
    }
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(encoded)


def latest_sku_review(package: dict[str, Any]) -> dict[str, Any] | None:
    path = evidence_path(package, "sku-review")
    return load_json(path) if path.exists() else None


def sku_baseline_review_path(package: dict[str, Any]) -> Path:
    return evidence_path(package, "sku-baseline-review")


def baseline_sku_review_state(package: dict[str, Any]) -> dict[str, Any]:
    """Return the validated baseline App review used to bind later evidence."""
    path = sku_baseline_review_path(package)
    if not path.exists():
        return {"status": "missing", "review_file": str(path)}
    try:
        review = load_json(path)
    except OpsError as exc:
        return {"status": "invalid", "review_file": str(path), "error": str(exc)}
    evidence_file = Path(str(review.get("evidence_file", ""))) if review.get("evidence_file") else None
    valid = (
        review.get("scope") == "baseline"
        and review.get("package_sha256") == package.get("package_sha256")
        and review.get("account") == package.get("account")
        and str(review.get("item_id")) == str(package.get("item_id"))
        and review.get("decision") == "match"
        and review.get("reviewed_by")
        and review.get("evidence_source")
        and review.get("before_spec")
        and not review.get("after_spec")
        and review.get("spec_mode") in {"single_no_sku", "multi_sku"}
        and evidence_file is not None
        and evidence_file.is_file()
        and evidence_file.stat().st_size > 0
        and sha256_file(evidence_file) == review.get("evidence_sha256")
    )
    if not valid:
        return {
            "status": "invalid",
            "review_file": str(path),
            "error": "baseline App 规格核验记录缺少绑定字段或证据文件哈希不一致",
        }
    return {
        "status": "verified_baseline",
        "review_file": str(path),
        "review_sha256": sha256_file(path),
        "before_spec": review.get("before_spec"),
        "spec_mode": review.get("spec_mode"),
        "reviewed_by": review.get("reviewed_by"),
        "reviewed_at": review.get("reviewed_at"),
    }


def current_sku_review_state(package: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Validate a user-provided App SKU/spec review without filling SKU fields."""
    path = evidence_path(package, "sku-review")
    if not path.exists():
        return {
            "status": "pending_manual_review",
            "review_file": str(path),
            "notes": "当前 SKU 未形成机器可核对列表；需人工提交同商品、同改动包的规格证据。",
        }
    try:
        review = load_json(path)
    except OpsError as exc:
        return {"status": "invalid_manual_review", "review_file": str(path), "error": str(exc)}
    evidence_file = Path(str(review.get("evidence_file", ""))) if review.get("evidence_file") else None
    baseline = baseline_sku_review_state(package)
    baseline_binding_ok = baseline.get("status") == "missing" or (
        baseline.get("status") == "verified_baseline"
        and review.get("baseline_review_sha256") == baseline.get("review_sha256")
        and review.get("baseline_before_spec") == baseline.get("before_spec")
        and review.get("baseline_spec_mode") == baseline.get("spec_mode")
    )
    required = (
        review.get("scope") == "current"
        and review.get("method") == "manual_app_spec_review_bound_to_latest_verify_state"
        and review.get("package_sha256") == package.get("package_sha256")
        and review.get("account") == package.get("account")
        and str(review.get("item_id")) == str(package.get("item_id"))
        and review.get("decision") == "match"
        and review.get("reviewed_by")
        and review.get("evidence_source")
        and review.get("before_spec")
        and review.get("after_spec")
        and review.get("before_spec") == review.get("after_spec")
        and review.get("spec_mode") in {"single_no_sku", "multi_sku"}
        and review.get("current_state_hash") == sku_review_state_hash(current)
        and baseline_binding_ok
        and evidence_file is not None
        and evidence_file.is_file()
        and evidence_file.stat().st_size > 0
        and sha256_file(evidence_file) == review.get("evidence_sha256")
    )
    if not required:
        return {
            "status": "stale_or_invalid_manual_review",
            "review_file": str(path),
            "evidence_file": str(evidence_file) if evidence_file else None,
            "baseline_review_file": baseline.get("review_file"),
            "notes": "人工规格证据与当前 verify 回读、baseline 规格或改动包不一致；不得把 unknown 当成未变化。",
        }
    return {
        "status": "verified_manual",
        "review_file": str(path),
        "evidence_file": str(evidence_file),
        "evidence_sha256": review.get("evidence_sha256"),
        "evidence_source": review.get("evidence_source"),
        "spec_mode": review.get("spec_mode"),
        "reviewed_by": review.get("reviewed_by"),
        "reviewed_at": review.get("reviewed_at"),
        "baseline_review_file": baseline.get("review_file") if baseline.get("status") != "missing" else None,
    }


def cmd_sku_review(args: argparse.Namespace) -> int:
    package_path = Path(args.package).resolve() if args.package else None
    if package_path is None:
        account, item_ids = require_target(args)
        package_path = latest_package(account, item_ids[0])
    package = read_package(package_path)
    scope = args.scope
    verify = latest_verify(package)
    if scope == "current" and (not verify or not isinstance(verify.get("current"), dict)):
        raise OpsError("sku-review --scope current 需要最近一次 verify 的真实当前回读；先运行 verify")
    evidence_file = Path(args.evidence_file).resolve()
    if not evidence_file.is_file() or evidence_file.stat().st_size <= 0:
        raise OpsError(f"规格证据文件不存在或为空: {evidence_file}")
    reviewed_by = (args.reviewed_by or "").strip()
    evidence_source = (args.evidence_source or "").strip()
    before_spec = (args.before_spec or "").strip()
    after_spec = (getattr(args, "after_spec", "") or "").strip()
    if not reviewed_by:
        raise OpsError("sku-review 必须提供 --reviewed-by")
    if not evidence_source:
        raise OpsError("sku-review 必须提供 --evidence-source，说明证据来自闲鱼 App 哪个页面或导出")
    if not before_spec:
        raise OpsError("sku-review 必须提供改前规格说明")
    if scope == "current" and not after_spec:
        raise OpsError("sku-review --scope current 必须提供改后规格说明")
    if scope == "baseline" and after_spec:
        raise OpsError("sku-review --scope baseline 只记录 baseline 的改前规格，不接受改后规格")
    if scope == "current" and args.decision == "match" and before_spec != after_spec:
        raise OpsError("decision=match 时，改前/改后规格说明必须完全一致；不要用口述替代证据")
    baseline = baseline_sku_review_state(package)
    if scope == "current" and baseline.get("status") == "invalid":
        raise OpsError("已有 baseline SKU 人工核验记录无效，不能写入当前 SKU 证据；请修复或重新记录 baseline")
    if scope == "current" and baseline.get("status") == "verified_baseline":
        if before_spec != baseline.get("before_spec") or args.spec_mode != baseline.get("spec_mode"):
            raise OpsError("当前 SKU 证据必须沿用 baseline 已确认的改前规格和规格模式")
    current = verify["current"] if scope == "current" else package["baseline_live"]
    evidence_suffix = "sku-review" if scope == "current" else "sku-baseline-review"
    review = {
        "schema": "xianyu-sku-review-v1",
        "scope": scope,
        "package_id": package["package_id"],
        "package_sha256": package["package_sha256"],
        "account": package["account"],
        "item_id": package["item_id"],
        "verify_last_checked_at": verify.get("last_checked_at") if verify else None,
        "current_state_hash": sku_review_state_hash(current),
        "evidence_file": str(evidence_file),
        "evidence_sha256": sha256_file(evidence_file),
        "evidence_bytes": evidence_file.stat().st_size,
        "evidence_source": evidence_source,
        "spec_mode": args.spec_mode,
        "before_spec": before_spec,
        "after_spec": after_spec if scope == "current" else None,
        "decision": args.decision,
        "reviewed_by": reviewed_by,
        "reviewed_at": now_local(),
        "review_note": (args.review_note or "").strip(),
        "method": "manual_app_spec_review_bound_to_latest_verify_state",
    }
    if scope == "current" and baseline.get("status") == "verified_baseline":
        review.update(
            {
                "baseline_review_file": baseline.get("review_file"),
                "baseline_review_sha256": baseline.get("review_sha256"),
                "baseline_before_spec": baseline.get("before_spec"),
                "baseline_spec_mode": baseline.get("spec_mode"),
            }
        )
    atomic_json(evidence_path(package, evidence_suffix), review)
    print_json(
        {
            "status": "recorded",
            "review": review,
            "next": "重新运行 verify；SKU 人工证据仍不填充未知字段，当前回读变化后必须重新 review。",
        }
    )
    return 0


def _delivery_rules(api_base: str) -> list[dict[str, Any]]:
    value = http_json(api_base, "GET", "/delivery-rules")
    if isinstance(value, list):
        if not all(isinstance(rule, dict) for rule in value):
            raise OpsError("发货规则接口返回的数组包含非对象")
        return value
    response = require_object(value, "发货规则接口")
    rules = response.get("rules")
    if isinstance(rules, list) and all(isinstance(rule, dict) for rule in rules):
        return rules
    raise OpsError("发货规则接口缺少 rules 数组")


DELIVERY_RULE_VOLATILE_FIELDS = frozenset(
    {"delivery_times", "updated_at", "created_at", "last_delivery_date", "today_delivery_times"}
)
DELIVERY_RULE_MUTATION_FIELDS = ("keyword", "card_id", "delivery_count", "enabled", "description")


def _delivery_rule_projection(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in rule.items()
        if key not in DELIVERY_RULE_VOLATILE_FIELDS
    }


def _delivery_rules_unrelated_hash(rules: list[dict[str, Any]], target_id: Any) -> str:
    unrelated = [
        _delivery_rule_projection(rule)
        for rule in rules
        if str(rule.get("id")) != str(target_id)
    ]
    unrelated.sort(key=lambda rule: str(rule.get("id", "")))
    return stable_hash(unrelated)


def _require_delivery_rule_update_success(value: Any) -> dict[str, Any]:
    """Accept the real route contract and reject unconfirmed business results."""
    response = require_object(value, "发货规则更新接口")
    if response.get("success") is False:
        detail = response.get("message") or response.get("error") or "业务响应未成功"
        raise OpsError(f"发货规则更新业务失败: {detail}")
    if response.get("success") is True:
        return response
    # The current FastAPI route returns HTTP 200 with this message and no
    # success boolean. A bare/unknown JSON object is not enough evidence.
    if response.get("message") == "发货规则更新成功":
        return response
    raise OpsError("发货规则更新响应未确认成功")


def _delivery_rule_fields_match(rule: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(rule.get(field) == expected.get(field) for field in DELIVERY_RULE_MUTATION_FIELDS)


def sync_local_cache(package: dict[str, Any], current: dict[str, Any], api_base: str) -> dict[str, Any]:
    account = str(package["account"])
    item_id = str(package["item_id"])
    title = str(package["desired"]["title"])
    description = str(package["desired"]["description"])
    result: dict[str, Any] = {"item_cache": "pending", "delivery_rule": "pending"}
    try:
        from console.store import Store, product_key
        store = Store()
        key = product_key(account, item_id)
        row = store.get("product", key)
        if row is None:
            result["item_cache"] = "unavailable"
        else:
            row.update(current)
            row.update({"title": title, "description": description, "source": "goofish_mtop_detail"})
            store.put("product", key, row, account=account, source="goofish_mtop_detail")
            result["item_cache"] = "synced"
    except Exception as exc:
        result["item_cache"] = {"status": "unavailable", "error": str(exc)}
    try:
        rules = _delivery_rules(api_base)
        baseline_title = str(package.get("baseline_live", {}).get("title") or "")
        matches = [rule for rule in rules if str(rule.get("keyword", "")) in {baseline_title, title}]
        if len(matches) == 1:
            rule = matches[0]
            rule_id = rule.get("id")
            if rule_id is None:
                raise OpsError("发货规则缺少 id，不能安全回读目标规则")
            expected_rule = {
                "keyword": title,
                "card_id": rule.get("card_id"),
                "delivery_count": rule.get("delivery_count"),
                "enabled": rule.get("enabled"),
                "description": rule.get("description"),
            }
            unrelated_before_hash = _delivery_rules_unrelated_hash(rules, rule_id)
            if str(rule.get("keyword")) != title:
                _require_delivery_rule_update_success(
                    http_json(
                        api_base,
                        "PUT",
                        f"/delivery-rules/{rule_id}",
                        expected_rule,
                    )
                )
            reread_rules = _delivery_rules(api_base)
            reread = next(
                (candidate for candidate in reread_rules if str(candidate.get("id")) == str(rule_id)),
                None,
            )
            if not isinstance(reread, dict) or not _delivery_rule_fields_match(reread, expected_rule):
                raise OpsError("发货规则回读未确认目标字段已按预期更新")
            unrelated_after_hash = _delivery_rules_unrelated_hash(reread_rules, rule_id)
            if unrelated_after_hash != unrelated_before_hash:
                raise OpsError("发货规则回读发现无关规则发生变化，未标记同步完成")
            result["delivery_rule"] = "synced"
            result["delivery_rule_evidence"] = {
                "rule_id": rule_id,
                "target_fields_checked": list(DELIVERY_RULE_MUTATION_FIELDS),
                "unrelated_before_hash": unrelated_before_hash,
                "unrelated_after_hash": unrelated_after_hash,
            }
        elif not matches:
            result["delivery_rule"] = "not_found_pending"
        else:
            result["delivery_rule"] = "ambiguous_pending"
    except Exception as exc:
        result["delivery_rule"] = {"status": "unavailable", "error": str(exc)}
    return result


def latest_verify(package: dict[str, Any]) -> dict[str, Any] | None:
    path = evidence_path(package, "verify")
    return load_json(path) if path.exists() else None


def local_sync_complete(sync_result: dict[str, Any] | None) -> bool:
    if not isinstance(sync_result, dict):
        return False
    return sync_result.get("item_cache") == "synced" and sync_result.get("delivery_rule") == "synced"


def cmd_verify(args: argparse.Namespace) -> int:
    package_path = Path(args.package).resolve() if args.package else None
    if package_path is None:
        account, item_ids = require_target(args)
        package_path = latest_package(account, item_ids[0])
    package = read_package(package_path)
    runtime = check_runtime(str(package["account"]), args.api_base, args.cdp_url)
    if not runtime["ok"]:
        raise OpsError("verify 需要健康的真实运行时")
    current = read_platform_item(str(package["account"]), str(package["item_id"]))
    baseline = package["baseline_live"]
    desired = package["desired"]
    image_state = current_image_state(package, current)
    manual_sku_review = current_sku_review_state(package, current)
    previous = latest_verify(package)
    checked_at = now_local()
    editable_checks = compare_editable_invariants(baseline, current)
    editable_sku_check = editable_checks.get("skus", {})
    editable_sku_direct_known = bool(
        editable_sku_check.get("baseline_known") and editable_sku_check.get("current_known")
    )
    editable_sku_direct_conflict = editable_sku_direct_known and not editable_sku_check.get("unchanged", False)
    sku_direct_known = known_field(current.get("skus"), "skus") and known_field(baseline.get("skus"), "skus")
    sku_direct_unchanged = (
        sku_direct_known
        and canonical(current.get("skus")) == canonical(baseline.get("skus"))
    )
    sku_direct_conflict = (
        (sku_direct_known and not sku_direct_unchanged)
        or editable_sku_direct_conflict
    )
    sku_manual_verified = manual_sku_review.get("status") == "verified_manual"
    sku_manual_conflict = False
    if sku_manual_verified:
        spec_mode = manual_sku_review.get("spec_mode")
        editable_current = current.get("editable_invariants") if isinstance(current.get("editable_invariants"), dict) else {}
        editable_baseline = baseline.get("editable_invariants") if isinstance(baseline.get("editable_invariants"), dict) else {}
        machine_sku_values = [
            (current.get("skus"), "public.current"),
            (baseline.get("skus"), "public.baseline"),
            (editable_current.get("skus"), "editDetail.current"),
            (editable_baseline.get("skus"), "editDetail.baseline"),
        ]
        known_machine_skus = [value for value, _source in machine_sku_values if known_field(value, "skus")]
        if spec_mode == "single_no_sku":
            sku_manual_conflict = any(bool(value) for value in known_machine_skus)
        elif spec_mode == "multi_sku":
            sku_manual_conflict = any(not bool(value) for value in known_machine_skus)
    if sku_manual_conflict:
        manual_sku_review = {
            **manual_sku_review,
            "status": "conflicting_manual_review",
            "notes": "人工规格模式与机器明确回读的 SKU 列表矛盾；不能用人工证据覆盖机器冲突。",
        }
    invariant_checks = {
        "price_cents_unchanged": (
            known_field(current.get("price_cents"), "price_cents")
            and known_field(baseline.get("price_cents"), "price_cents")
            and current.get("price_cents") == baseline.get("price_cents")
        ),
        "quantity_unchanged": (
            known_field(current.get("quantity"), "quantity")
            and known_field(baseline.get("quantity"), "quantity")
            and current.get("quantity") == baseline.get("quantity")
        ),
        "category_unchanged": (
            known_field(current.get("category_id"), "category_id")
            and known_field(baseline.get("category_id"), "category_id")
            and current.get("category_id") == baseline.get("category_id")
        ),
        # A package-bound App review may discharge an unknown SKU list.  The
        # direct machine comparison remains exposed separately in the result.
        "skus_unchanged": (
            not sku_direct_conflict
            and not sku_manual_conflict
            and (sku_direct_unchanged or sku_manual_verified)
        ),
    }
    editable_check_flags = {
        f"editable_{field}_unchanged": check["unchanged"]
        for field, check in editable_checks.items()
        if check["baseline_known"]
    }
    checks = {
        "title": current.get("title") == desired.get("title"),
        "description": current.get("description") == desired.get("description"),
        "online": current.get("status") in ONLINE_STATUS,
        **invariant_checks,
        **editable_check_flags,
        "editable_detail_observed": current.get("edit_detail_status") == "observed",
        "image": image_state["status"] in {"not_requested", "verified_url", "verified_visual"},
    }
    platform_verified = all(checks.values())
    sync_result: dict[str, Any] | None = None
    if platform_verified:
        sync_result = sync_local_cache(package, current, args.api_base)
    sync_verified = local_sync_complete(sync_result) if platform_verified else False
    online_verified_at = previous.get("verified_at") if previous else None
    if platform_verified and not online_verified_at:
        online_verified_at = checked_at
    verification_history = list(previous.get("verification_history", [])) if previous else []
    verification_history.append(
        {
            "checked_at": checked_at,
            "online_verified": platform_verified,
            "local_sync_complete": sync_verified,
            "status": "verified" if platform_verified and sync_verified else (
                "online_verified_pending_local_sync" if platform_verified else "not_verified"
            ),
        }
    )
    if image_state.get("review_html_file") and image_state.get("status") != "verified_visual":
        write_image_review_html(Path(str(image_state["review_html_file"])), package, current, image_state)
    full_verified = platform_verified and sync_verified
    status = (
        "verified"
        if full_verified
        else "online_verified_pending_local_sync"
        if platform_verified
        else "pending_image_review"
        if image_state.get("status") in {"pending_image_review", "unavailable"} and package.get("image")
        else "not_verified"
    )
    result = {
        "schema": "xianyu-verification-v1",
        "package_id": package["package_id"],
        "package_sha256": package["package_sha256"],
        "verified_at": online_verified_at,
        "last_checked_at": checked_at,
        "previous_verified_at": previous.get("verified_at") if previous else None,
        "online_verified": platform_verified,
        "local_sync_complete": sync_verified,
        "status": status,
        "checks": checks,
        "sku_direct_unchanged": sku_direct_unchanged,
        "sku_direct_known": sku_direct_known,
        "sku_direct_conflict": sku_direct_conflict,
        "editable_sku_direct_known": editable_sku_direct_known,
        "editable_sku_direct_conflict": editable_sku_direct_conflict,
        "sku_manual_conflict": sku_manual_conflict,
        "sku_review": manual_sku_review,
        "editable_invariants": editable_checks,
        "image": image_state,
        "current": current,
        "sync": sync_result,
        "verification_history": verification_history,
        "summary": (
            "线上字段和保留不变量已回读通过，本地缓存与发货规则也已同步。"
            if full_verified
            else "线上回读已通过但本地缓存或发货规则同步未完成；可重试 verify，不能把本轮称为完整闭环。"
            if platform_verified
            else "线上回读未形成完整通过；未同步本地缓存，图片、editDetail 或其他字段仍需处理。"
        ),
    }
    atomic_json(evidence_path(package, "verify"), result)
    print_json(result)
    return 0 if full_verified else 2


def ledger_events() -> list[dict[str, Any]]:
    if not LEDGER_PATH.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def append_ledger(event: dict[str, Any]) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def review_window(verified_at: str | None, now: datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    verified = parse_time(verified_at)
    if verified is None:
        return {"status": "pending_verified_at", "elapsed_hours": None, "due": False}
    elapsed = (now - verified).total_seconds() / 3600
    return {"status": "due" if elapsed >= 72 else "observing", "elapsed_hours": round(elapsed, 2), "due": elapsed >= 72}


REVIEW_OBSERVATION_HOURS = 72
OPERATIONAL_REVIEW_SOURCES = frozenset({"public_item_detail_page", "local_8090_orders"})


def _review_capture_window(
    baseline: dict[str, Any] | None,
    current: dict[str, Any] | None,
    verified_at: str | None,
) -> dict[str, Any]:
    """Validate that the two real snapshots bracket the verified observation window."""
    baseline_captured_at = baseline.get("captured_at") if isinstance(baseline, dict) else None
    current_captured_at = current.get("captured_at") if isinstance(current, dict) else None
    verified = parse_time(verified_at)
    baseline_time = parse_time(baseline_captured_at)
    current_time = parse_time(current_captured_at)
    required_current_time = (
        verified + timedelta(hours=REVIEW_OBSERVATION_HOURS) if verified else None
    )
    gaps: list[str] = []
    if verified is None:
        gaps.append("没有可解析的线上 verified_at，不能开始 72 小时观察窗口。")
    if baseline_time is None:
        gaps.append("baseline 快照缺少可解析的 captured_at。")
    if current_time is None:
        gaps.append("当前快照缺少可解析的 captured_at。")
    if baseline_time and verified and baseline_time > verified:
        gaps.append("baseline 快照晚于线上 verified_at，不能作为生效前基线。")
    if baseline_time and current_time and current_time <= baseline_time:
        gaps.append("当前快照没有晚于 baseline 快照，不能形成时间顺序上的对比。")
    if current_time and required_current_time and current_time < required_current_time:
        gaps.append("当前快照尚未达到 verified_at 后完整 72 小时观察窗口。")
    return {
        "valid": not gaps,
        "verified_at": verified_at,
        "baseline_captured_at": baseline_captured_at,
        "current_captured_at": current_captured_at,
        "required_current_captured_at": required_current_time.isoformat() if required_current_time else None,
        "gaps": gaps,
    }


def _snapshot_metric_value(snapshot: dict[str, Any], item_id: str) -> dict[str, Any]:
    public_rows = snapshot.get("public_detail_metrics") if isinstance(snapshot.get("public_detail_metrics"), list) else []
    public = next((row for row in public_rows if isinstance(row, dict) and str(row.get("item_id")) == item_id), {})
    items = snapshot.get("items") if isinstance(snapshot.get("items"), dict) else {}
    local = items.get(item_id) if isinstance(items.get(item_id), dict) else {}
    orders = snapshot.get("orders") if isinstance(snapshot.get("orders"), dict) else {}
    order = orders.get(item_id) if isinstance(orders.get(item_id), dict) else {}
    return {
        "public_item_detail_page": {
            "browse": public.get("browse", "unavailable"),
            "want": public.get("want", "unavailable"),
        },
        "local_8090": {
            "title": local.get("title", "unavailable"),
            "price": local.get("price", "unavailable"),
            "status": local.get("status", "unavailable"),
        },
        "local_8090_orders": {
            "all_records": order.get("all_records", "unavailable"),
        },
    }


def _compare_snapshot_metrics(baseline: dict[str, Any], current: dict[str, Any], item_id: str) -> dict[str, Any]:
    before_values = _snapshot_metric_value(baseline, item_id)
    after_values = _snapshot_metric_value(current, item_id)
    metrics: list[dict[str, Any]] = []
    gaps: list[str] = []
    changes: list[str] = []
    for source, fields in before_values.items():
        for metric, before in fields.items():
            after = after_values.get(source, {}).get(metric, "unavailable")
            comparable = (
                before not in {None, "unavailable", "not_visible", "unknown"}
                and after not in {None, "unavailable", "not_visible", "unknown"}
            )
            delta: int | float | None = None
            if comparable and isinstance(before, (int, float)) and not isinstance(before, bool) and isinstance(after, (int, float)) and not isinstance(after, bool):
                delta = after - before
            status = "comparable" if comparable else "missing"
            if comparable and before == after:
                status = "unchanged"
            elif comparable:
                status = "changed"
                changes.append(f"{source}.{metric}: {before} → {after}")
            else:
                gaps.append(f"{source}.{metric} 缺少可比值（baseline={before!r}, current={after!r}）")
            metrics.append(
                {
                    "source": source,
                    "metric": metric,
                    "baseline": before,
                    "current": after,
                    "delta": delta,
                    "status": status,
                }
            )
    operational_metrics = [
        row
        for row in metrics
        if row["source"] in OPERATIONAL_REVIEW_SOURCES
        and row["status"] in {"comparable", "unchanged", "changed"}
    ]
    return {
        # Title/price/status are useful descriptive state fields, but they do
        # not prove an operating outcome. At least one same-source public or
        # order metric must be comparable before a 72h review can complete.
        "status": "ready" if operational_metrics else "insufficient",
        "metrics": metrics,
        "changes": changes,
        "gaps": gaps,
        "operational_comparable": bool(operational_metrics),
        "operational_metrics": operational_metrics,
    }


def _load_snapshot_reference(package: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    reference = package.get("baseline_snapshot") if isinstance(package.get("baseline_snapshot"), dict) else None
    if not reference or not reference.get("file"):
        return None, "改动包没有关联 prepare 起点真实快照"
    path = Path(str(reference["file"]))
    if not path.is_file():
        return None, f"baseline 快照文件不存在: {path}"
    expected_hash = reference.get("sha256")
    if expected_hash and sha256_file(path) != expected_hash:
        return None, f"baseline 快照哈希已变化: {path}"
    try:
        snapshot = load_json(path)
    except OpsError as exc:
        return None, str(exc)
    if snapshot.get("account") != package.get("account") or package.get("item_id") not in {
        str(item_id) for item_id in snapshot.get("item_ids", [])
    }:
        return None, "baseline 快照账号或商品不匹配"
    return snapshot, None


def write_review_html(path: Path, result: dict[str, Any]) -> None:
    package = result.get("package", {})
    verify = result.get("verify") or {}
    snapshot = result.get("snapshot") or {}
    comparison = result.get("comparison") if isinstance(result.get("comparison"), dict) else {}
    rows = [
        ("商品", f"{html.escape(str(package.get('account', '')))} / {html.escape(str(package.get('item_id', '')))}"),
        ("观察状态", html.escape(str(result.get("status", "unknown")))),
        ("72 小时窗口", html.escape(str(result.get("window", {}).get("status", "unknown")))),
        ("线上核验", html.escape(str(verify.get("status", "没有核验记录")))),
        ("baseline 采集时间", html.escape(str(comparison.get("baseline_captured_at", "缺失")))),
        ("当前采集时间", html.escape(str(comparison.get("current_captured_at", snapshot.get("captured_at", "缺失"))))),
    ]
    summary_rows = "\n".join(f"<tr><th>{key}</th><td>{value}</td></tr>" for key, value in rows)
    metric_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(row.get('source')))}</td>"
        f"<td>{html.escape(str(row.get('metric')))}</td>"
        f"<td>{html.escape(str(row.get('baseline')))}</td>"
        f"<td>{html.escape(str(row.get('current')))}</td>"
        f"<td>{html.escape(str(row.get('delta')))}</td>"
        f"<td>{html.escape(str(row.get('status')))}</td></tr>"
        for row in comparison.get("metrics", [])
        if isinstance(row, dict)
    ) or "<tr><td colspan='6'>没有同商品同来源的可比数据。</td></tr>"
    gaps = "".join(f"<li>{html.escape(str(gap))}</li>" for gap in result.get("limitations", [])) or "<li>无额外缺口记录。</li>"
    decision = result.get("decision") if isinstance(result.get("decision"), dict) else {}
    document = f"""<!doctype html>
<meta charset='utf-8'><title>闲鱼运营复盘</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}th,td{{border:1px solid #ccc;padding:.45rem;text-align:left;vertical-align:top}}th{{background:#f5f5f5}}code{{overflow-wrap:anywhere}}</style>
<h1>闲鱼运营复盘</h1>
<table>{summary_rows}</table>
<h2>同商品同来源的 baseline → 当前</h2>
<table><thead><tr><th>来源</th><th>指标</th><th>baseline</th><th>当前</th><th>delta</th><th>状态</th></tr></thead><tbody>{metric_rows}</tbody></table>
<h2>下一步判断</h2><p>{html.escape(str(decision.get('judgment', '暂不能形成判断。')))}</p><p>建议：{html.escape(str(decision.get('next_action', '继续采集。')))}</p>
<h2>缺口与样本局限</h2><ul>{gaps}</ul>
<p>证据文件：baseline={html.escape(str(comparison.get('baseline_snapshot_file', '缺失')))}；current={html.escape(str(snapshot.get('output_file', '缺失')))}</p>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def make_review(package: dict[str, Any], snapshot: dict[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    verify = latest_verify(package)
    verified_at = verify.get("verified_at") if verify and verify.get("online_verified", verify.get("status") in {"verified", "online_verified_pending_local_sync"}) else None
    window = review_window(verified_at, now)
    baseline_snapshot, baseline_error = _load_snapshot_reference(package)
    current_error: str | None = None
    if snapshot is None:
        current_error = "没有提供当前真实采集快照"
    elif snapshot.get("account") != package.get("account") or package.get("item_id") not in {
        str(item_id) for item_id in snapshot.get("item_ids", [])
    }:
        current_error = "当前快照账号或商品不匹配"
    comparison: dict[str, Any] = {
        "status": "insufficient",
        "baseline_snapshot_file": baseline_snapshot.get("output_file") if baseline_snapshot else None,
        "baseline_captured_at": baseline_snapshot.get("captured_at") if baseline_snapshot else None,
        "current_snapshot_file": snapshot.get("output_file") if snapshot else None,
        "current_captured_at": snapshot.get("captured_at") if snapshot else None,
        "metrics": [],
        "changes": [],
        "gaps": [],
    }
    capture_window = _review_capture_window(baseline_snapshot, snapshot, verified_at)
    comparison["capture_window"] = capture_window
    limitations: list[str] = [
        "卖家后台曝光字段当前不可得，公开浏览/想要和订单只能作为各自来源的描述性读数。",
        "单个起点与单个当前采集点不足以证明增长因果。",
    ]
    if baseline_error:
        limitations.insert(0, baseline_error)
    if current_error:
        limitations.insert(0, current_error)
    if baseline_snapshot and not current_error and snapshot:
        comparison.update(_compare_snapshot_metrics(baseline_snapshot, snapshot, str(package["item_id"])))
        if baseline_snapshot.get("status") != "complete" or snapshot.get("status") != "complete":
            limitations.append("至少一个采集点是 partial，缺失来源不能按 0 处理。")
        limitations.extend(comparison.get("gaps", []))
    if not comparison.get("operational_comparable", False):
        limitations.append("没有同商品同来源的可比较运营指标；本地标题、价格和在线状态只能展示字段状态，不能写成 72 小时运营复盘完成。")
    limitations.extend(capture_window.get("gaps", []))
    ready_for_completion = bool(
        window["due"]
        and capture_window.get("valid")
        and comparison.get("status") == "ready"
        and not baseline_error
        and not current_error
    )
    if ready_for_completion:
        judgment = "已形成同商品同来源的描述性 delta；变化可被观察，但仍不能归因于单一文案或图片改动。"
        next_action = "继续按北京时间每天 21:00 采集；若需要判断曝光或转化因果，补齐卖家后台曝光与成交口径。"
    elif window["due"]:
        if not capture_window.get("valid"):
            judgment = "72 小时窗口按线上 verified_at 已到，但当前采集没有覆盖生效后的完整观察窗口或没有晚于 baseline；暂不写复盘完成。"
            next_action = "继续每日采集，直到出现晚于 baseline 且达到 verified_at 后 72 小时的真实当前快照。"
        else:
            judgment = "72 小时窗口已到，但没有同商品同来源的可比较运营指标，暂不写复盘完成。"
            next_action = "继续每日采集；补齐可比较的公开浏览/想要或订单来源后再判断运营变化。"
    else:
        judgment = "尚未到 72 小时复盘窗口，当前只建立观察基线。"
        next_action = "继续按北京时间每天 21:00 采集，等待首个到期点。"
    return {
        "schema": "xianyu-review-v1",
        "reviewed_at": now_local(),
        "status": "due" if window["due"] else window["status"],
        "package": {
            "package_id": package.get("package_id"),
            "package_sha256": package.get("package_sha256"),
            "account": package.get("account"),
            "item_id": package.get("item_id"),
        },
        "window": window,
        "verify": verify,
        "snapshot": snapshot,
        "comparison": comparison,
        "limitations": limitations,
        "decision": {"judgment": judgment, "next_action": next_action},
        "review_ready": ready_for_completion,
        "causal_claim": "descriptive_delta_only_with_missing_backend_exposure_data" if ready_for_completion else "insufficient_same_source_comparison",
        "summary": judgment,
    }


def cmd_review(args: argparse.Namespace) -> int:
    package_path = Path(args.package).resolve() if args.package else None
    if package_path is None:
        account, item_ids = require_target(args)
        package_path = latest_package(account, item_ids[0])
    package = read_package(package_path)
    if args.snapshot:
        snapshot = load_json(Path(args.snapshot).resolve())
    else:
        found = latest_snapshot_for(str(package["account"]), str(package["item_id"]))
        snapshot = found[0] if found else None
    result = make_review(package, snapshot)
    json_path = evidence_path(package, "review")
    html_path = json_path.with_suffix(".html")
    atomic_json(json_path, result)
    write_review_html(html_path, result)
    result["json_file"] = str(json_path)
    result["html_file"] = str(html_path)
    print_json(result)
    return 0


def maybe_complete_72h_review(package: dict[str, Any], snapshot: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    verify = latest_verify(package)
    verified_at = (
        verify.get("verified_at")
        if verify and verify.get("online_verified", verify.get("status") in {"verified", "online_verified_pending_local_sync"})
        else None
    )
    window = review_window(verified_at, now)
    event_key = package["package_sha256"]
    already = any(
        event.get("event_type") == "ops_72h_review_completed"
        and event.get("package_sha256") == event_key
        for event in ledger_events()
    )
    completed = False
    review_ready = False
    blocked_reason = None
    if window["due"] and not already:
        result = make_review(package, snapshot, now=now)
        atomic_json(evidence_path(package, "review"), result)
        review_ready = bool(result.get("review_ready"))
        if review_ready:
            append_ledger(
                {
                    "event_type": "ops_72h_review_completed",
                    "package_id": package["package_id"],
                    "package_sha256": event_key,
                    "item_id": package["item_id"],
                    "reviewed_at": now_local(),
                    "elapsed_hours": window["elapsed_hours"],
                    "causal_claim": result["causal_claim"],
                    "snapshot_file": snapshot.get("output_file"),
                }
            )
            completed = True
        else:
            blocked_reason = "72 小时窗口已到，但缺少同商品同来源的 baseline→当前可比证据；未记为 review_completed。"
    return {
        "window": window,
        "review_due": window["due"],
        "review_completed_now": completed,
        "review_already_completed": already,
        "review_ready": review_ready,
        "blocked_reason": blocked_reason,
    }


def cmd_collect(args: argparse.Namespace) -> int:
    account, item_ids = require_target(args, many=True)
    runtime = check_runtime(account, args.api_base, args.cdp_url)
    snapshot, path = snapshot_once(
        account,
        item_ids,
        api_base=args.api_base,
        cdp_url=args.cdp_url,
        sync_account=not args.no_sync,
        output_dir=Path(args.output_dir).resolve() if args.output_dir else SNAPSHOT_DIR,
    )
    snapshot["runtime_check"] = runtime
    snapshot["output_file"] = str(path)
    append_ledger(
        {
            "event_type": "ops_snapshot_collected",
            "collected_at": snapshot["captured_at"],
            "account": account,
            "item_ids": item_ids,
            "status": snapshot.get("status"),
            "snapshot_file": str(path),
            "source_status": snapshot.get("source_status"),
        }
    )
    review_state: dict[str, Any] = {"status": "not_requested"}
    selected_package: str | None = None
    package_path = Path(args.package).resolve() if args.package else select_active_package(account, item_ids[0])
    if package_path:
        package = read_package(package_path)
        selected_package = str(package_path)
        review_state = maybe_complete_72h_review(package, snapshot)
    elif not args.package:
        review_state = {
            "status": "no_active_verified_package",
            "summary": "当前商品尚无已线上核验的改动包；已完成采集，未伪造 72 小时复盘起点。",
        }
    result = {
        "status": snapshot.get("status"),
        "snapshot_file": str(path),
        "source_status": snapshot.get("source_status"),
        "review": review_state,
        "package": selected_package,
        "summary": "采集已写入快照和追加式台账；来源缺口保持原样，未推导曝光。",
    }
    print_json(result)
    return 0 if snapshot.get("status") == "complete" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="闲鱼运营闭环薄 CLI")
    parser.add_argument("--api-base", default=DEFAULT_API, help=argparse.SUPPRESS)
    parser.add_argument("--cdp-url", default=DEFAULT_CDP, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="检查本地服务、账号会话和固定 Edge")
    target_args(check)
    check.set_defaults(handler=cmd_check)

    snapshot = sub.add_parser("snapshot", help="采集一轮真实来源快照")
    target_args(snapshot, many=True)
    snapshot.add_argument("--no-sync", action="store_true", help="不先刷新账号商品清单")
    snapshot.add_argument("--output-dir", default=None)
    snapshot.set_defaults(handler=cmd_snapshot)

    prepare = sub.add_parser("prepare", help="回读线上状态并生成具体改动包")
    target_args(prepare)
    prepare.add_argument("--copy-file")
    prepare.add_argument("--title")
    prepare.add_argument("--description")
    prepare.add_argument("--image-file")
    prepare.set_defaults(handler=cmd_prepare)

    apply = sub.add_parser("apply", help="在具体批准哈希下提交一次平台修改")
    target_args(apply)
    apply.add_argument("--package")
    apply.add_argument("--approval-hash", required=True)
    apply.add_argument("--approval-note", required=True)
    apply.set_defaults(handler=cmd_apply)

    verify = sub.add_parser("verify", help="回读线上结果并按条件同步本地缓存")
    target_args(verify)
    verify.add_argument("--package")
    verify.set_defaults(handler=cmd_verify)

    image_review = sub.add_parser("image-review", help="记录实际当前图片与改动包期望图片的人工核验")
    target_args(image_review)
    image_review.add_argument("--package")
    image_review.add_argument("--current-file", required=True, help="从当前线上图片保存的本地证据文件")
    image_review.add_argument("--current-url", required=True, help="最近一次 verify 回读的当前图片 URL")
    image_review.add_argument("--decision", choices=["match", "mismatch"], required=True)
    image_review.add_argument("--reviewed-by", required=True)
    image_review.add_argument("--review-note", default="")
    image_review.set_defaults(handler=cmd_image_review)

    sku_review = sub.add_parser("sku-review", help="记录绑定到改动包的 App 规格人工核验")
    target_args(sku_review)
    sku_review.add_argument("--package")
    sku_review.add_argument("--scope", choices=["baseline", "current"], required=True)
    sku_review.add_argument("--evidence-file", required=True, help="App 核验记录或截图说明文件")
    sku_review.add_argument("--decision", choices=["match", "mismatch"], required=True)
    sku_review.add_argument("--reviewed-by", required=True)
    sku_review.add_argument("--evidence-source", required=True, help="证据来源，例如 user/App/user statement")
    sku_review.add_argument("--spec-mode", choices=["single_no_sku", "multi_sku"], required=True)
    sku_review.add_argument("--before-spec", required=True)
    sku_review.add_argument("--after-spec", default="")
    sku_review.add_argument("--review-note", default="")
    sku_review.set_defaults(handler=cmd_sku_review)

    review = sub.add_parser("review", help="生成当前观察窗口与证据复盘")
    target_args(review)
    review.add_argument("--package")
    review.add_argument("--snapshot")
    review.set_defaults(handler=cmd_review)

    collect = sub.add_parser("collect", help="采集并追加台账，必要时触发一次 72 小时复盘")
    target_args(collect, many=True)
    collect.add_argument("--package")
    collect.add_argument("--no-sync", action="store_true")
    collect.add_argument("--output-dir", default=None)
    collect.set_defaults(handler=cmd_collect)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except OpsError as exc:
        print_json({"status": "blocked", "error": str(exc), "summary": "操作被安全边界阻止，先处理给出的具体缺口。"})
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print_json({"status": "blocked", "error": str(exc), "summary": "操作未完成，未进行平台写入。"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
