"""Read evidence for one explicitly selected item; no publishing or vendor imports."""
from __future__ import annotations

from typing import Any

from .engine import ops
from .marketplace import EDIT_DETAIL_API, ITEM_DETAIL_API, MarketError, normalize_item_status


def _item_identity(value: dict, expected_item_id: str) -> None:
    actual = value.get("itemId", value.get("id"))
    if actual is None or isinstance(actual, (dict, list, bool)):
        raise MarketError("ITEM_ID_MISSING", "商品响应缺少明确的商品 ID，未采用其中的数据。")
    if str(actual) != str(expected_item_id):
        raise MarketError("ITEM_ID_MISMATCH", "响应不属于目标商品，未采用其中的数据。")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def public_item(body: dict, item_id: str, observed_at: str) -> dict:
    item = body.get("itemDO") if isinstance(body, dict) else None
    if not isinstance(item, dict) or not item:
        raise MarketError("ITEM_MISSING", "公开详情没有返回目标商品。")
    _item_identity(item, item_id)
    current = ops().slim_platform_item(item_id, item, observed_at)
    current["status_code"] = item.get("itemStatus")
    current["status"] = normalize_item_status(item.get("itemStatusStr", item.get("itemStatus")))
    current["field_sources"] = {
        key: ITEM_DETAIL_API for key in ("title", "description", "image_urls", "status")
        if current.get(key) not in (None, "unknown")
    }
    return current


def owned_item(body: dict, item_id: str, observed_at: str, owner_id: str | None = None) -> dict:
    if not isinstance(body, dict) or not body:
        raise MarketError("ITEM_MISSING", "本人商品详情没有返回可识别的数据。")
    _item_identity(body, item_id)
    returned_owner = str(body.get("userId") or "")
    # editDetail can return userId="0"; a placeholder cannot identify another seller.
    if owner_id and returned_owner not in {"", "0", "-1", str(owner_id)}:
        raise MarketError("ITEM_OWNER_MISMATCH", "商品详情中的卖家与当前账号不一致。")
    text = body.get("itemTextDTO") if isinstance(body.get("itemTextDTO"), dict) else {}
    images = body.get("imageInfoDOList")
    image_urls = [v["url"] for v in images if isinstance(v, dict) and _text(v.get("url"))] if isinstance(images, list) else None
    separate = text.get("titleDescSeparate")
    current = {
        "item_id": str(item_id), "title": _text(text.get("title")),
        "description": _text(text.get("desc")),
        "title_desc_separate": str(separate).lower() == "true" if str(separate).lower() in {"true", "false"} else None,
        "image_urls": image_urls, "status": normalize_item_status(body.get("itemStatus"), owned=True),
        "status_code": body.get("itemStatus"), "observed_at": observed_at,
        "source": "goofish_owned_edit_detail", "source_updated_at": "unknown",
        "skus": None, "price_cents": None, "quantity": None, "category_id": None,
        "edit_detail_status": "observed",
        "field_sources": {key: EDIT_DETAIL_API for key in ("title", "description", "image_urls", "status")},
    }
    current["field_sources"] = {key: source for key, source in current["field_sources"].items()
                                if current.get(key) not in (None, "unknown")}
    ops().merge_editable_invariants(current, body)
    return current


def combine_item_reads(public: dict | None, owned: dict | None) -> dict | None:
    """Prefer a fresh owner response; conflicting known state stays reviewable."""
    if owned is None:
        return public
    if public is None:
        return owned
    current = dict(owned)
    conflicts = {}
    for key in ("status", "price_cents", "category_id"):
        left, right = public.get(key), owned.get(key)
        if left not in (None, "unknown") and right not in (None, "unknown") and str(left) != str(right):
            conflicts[key] = {"public": left, "owned": right}
    if conflicts:
        current["source_conflicts"] = conflicts
    current["read_sources"] = [ITEM_DETAIL_API, EDIT_DETAIL_API]
    return current
