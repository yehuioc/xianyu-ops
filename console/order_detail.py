"""Fail-closed extraction of multi-spec evidence from observed order detail responses."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .store import CHINA, now


DETAIL_API_MARKERS = (
    "mtop.idle.web.trade.order.detail",
    "trade.order.detail",
)
ORDER_KEYS = frozenset({"orderId", "bizOrderId", "order_id"})
ITEM_KEYS = frozenset({"itemId", "item_id"})
BUYER_KEYS = frozenset({"buyerId", "buyer_id", "buyerUserId"})
PEER_KEYS = frozenset({"peerUserId", "peer_user_id"})
QUANTITY_KEYS = frozenset({
    "quantity", "buyQuantity", "buyAmount", "itemQuantity", "skuQuantity", "itemCount", "count", "num",
})
STATUS_KEYS = frozenset({"orderStatus", "order_status", "tradeStatus", "trade_status"})
SPEC_KEYS = frozenset({
    "skuInfo", "sku_text", "skuText", "skuDesc", "skuContent",
    "specInfo", "specText", "itemSku", "itemSpec",
})
NAME_KEYS = ("specName", "skuName", "propertyName", "name", "title", "label", "preText")
VALUE_KEYS = ("specValue", "skuValue", "propertyValue", "value", "text", "content", "displayText")


class OrderDetailError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class OrderDetailEvidence:
    order_id: str
    item_id: str
    buyer_id: str | None
    quantity: int | None
    status: str | None
    specs: tuple[tuple[str, str], ...]
    source_url: str
    observed_at: str

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["specs"] = [list(pair) for pair in self.specs]
        return value

    def validate(
        self, *, order_id: str, item_id: str, buyer_id: str | None = None,
        require_specs: bool = False,
    ) -> None:
        if self.order_id != str(order_id):
            raise OrderDetailError("ORDER_MISMATCH", "订单详情证据不属于目标订单。")
        if self.item_id != str(item_id):
            raise OrderDetailError("ITEM_MISMATCH", "订单详情证据不属于目标商品。")
        if buyer_id and not self.buyer_id:
            raise OrderDetailError("BUYER_ID_MISSING", "订单详情证据没有明确的买家 ID。")
        if buyer_id and self.buyer_id != str(buyer_id):
            raise OrderDetailError("BUYER_MISMATCH", "订单详情证据中的买家与目标订单不一致。")
        if require_specs and not self.specs:
            raise OrderDetailError("SPEC_MISSING", "订单详情没有明确的规格名称和值。")


def is_order_detail_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return any(marker.lower() in lowered for marker in DETAIL_API_MARKERS)


def _walk(value: Any, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            key = str(key)
            yield path, key, child
            yield from _walk(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, path + (str(index),))


def _unique(payload: Any, keys: frozenset[str], code: str, label: str) -> str:
    values = {
        str(child).strip()
        for _path, key, child in _walk(payload)
        if key in keys and child not in (None, "", 0, "0") and not isinstance(child, (dict, list))
    }
    if not values:
        raise OrderDetailError(code + "_MISSING", f"订单详情响应没有明确的{label}。")
    if len(values) != 1:
        raise OrderDetailError(code + "_AMBIGUOUS", f"订单详情响应含有冲突的{label}。")
    return next(iter(values))


def _optional_unique(payload: Any, keys: frozenset[str]) -> str | None:
    values = {
        str(child).strip()
        for _path, key, child in _walk(payload)
        if key in keys and child not in (None, "", 0, "0") and not isinstance(child, (dict, list))
    }
    return next(iter(values)) if len(values) == 1 else None


def _buyer_id(body: dict[str, Any]) -> str | None:
    explicit_values = {
        str(child).strip()
        for _path, key, child in _walk(body)
        if key in BUYER_KEYS and child not in (None, "", 0, "0") and not isinstance(child, (dict, list))
    }
    if len(explicit_values) > 1:
        raise OrderDetailError("BUYER_ID_AMBIGUOUS", "订单详情响应含有冲突的买家 ID。")
    explicit = next(iter(explicit_values)) if explicit_values else None
    seller_view = str(body.get("seller") or "").strip().casefold() in {"true", "1", "yes"}
    peer_values = {
        str(child).strip()
        for _path, key, child in _walk(body)
        if key in PEER_KEYS and child not in (None, "", 0, "0") and not isinstance(child, (dict, list))
    }
    if seller_view and len(peer_values) > 1:
        raise OrderDetailError("PEER_ID_AMBIGUOUS", "卖家视角订单详情含有冲突的对端用户 ID。")
    peer = next(iter(peer_values)) if seller_view and peer_values else None
    if explicit and peer and explicit != peer:
        raise OrderDetailError("BUYER_ID_CONFLICT", "显式买家 ID 与卖家视角对端用户 ID 冲突。")
    return explicit or peer


def _optional_quantity(payload: Any) -> int | None:
    values: set[int] = set()
    for _path, key, child in _walk(payload):
        if key not in QUANTITY_KEYS or child in (None, "") or isinstance(child, (dict, list, bool)):
            continue
        try:
            number = int(str(child).strip())
        except (TypeError, ValueError):
            continue
        if number > 0:
            values.add(number)
    for _path, _key, child in _walk(payload):
        if not isinstance(child, dict):
            continue
        label = next((child.get(key) for key in NAME_KEYS if child.get(key) not in (None, "")), None)
        value = next((child.get(key) for key in VALUE_KEYS if child.get(key) not in (None, "")), None)
        if not isinstance(label, (str, int, float)) or not isinstance(value, (str, int, float)):
            continue
        if not any(token in str(label) for token in ("数量", "购买数量", "件数")):
            continue
        match = re.fullmatch(r"\s*[xX×]?\s*(\d+)\s*(?:件)?\s*", str(value))
        if match and int(match.group(1)) > 0:
            values.add(int(match.group(1)))
    if len(values) > 1:
        raise OrderDetailError("QUANTITY_AMBIGUOUS", "订单详情响应含有冲突的购买数量。")
    return next(iter(values)) if values else None


def _pair_from_dict(value: dict[str, Any], path: tuple[str, ...]) -> tuple[str, str] | None:
    relevant = any(
        "spec" in part.lower() or "sku" in part.lower() or "iteminfolines" in part.lower()
        for part in path
    )
    if not relevant:
        return None
    name = next((value.get(key) for key in NAME_KEYS if value.get(key) not in (None, "")), None)
    selected = next((value.get(key) for key in VALUE_KEYS if value.get(key) not in (None, "")), None)
    if name is None or selected is None or isinstance(name, (dict, list)) or isinstance(selected, (dict, list)):
        return None
    if any(token in str(name) for token in ("数量", "购买数量", "件数")):
        return None
    return str(name).strip(), str(selected).strip()


def _pairs_from_text(value: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for segment in re.split(r"[;,，；|/\n]+", value):
        match = re.match(r"^\s*([^:：=]{1,40})\s*[:：=]\s*(.{1,100}?)\s*$", segment)
        if match:
            pairs.append((match.group(1).strip(), match.group(2).strip()))
    return pairs


def _collect_specs(payload: Any) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    for path, key, child in _walk(payload):
        if isinstance(child, dict):
            pair = _pair_from_dict(child, path + (key,))
            if pair:
                found.append(pair)
        if key in SPEC_KEYS and isinstance(child, str):
            found.extend(_pairs_from_text(child))
        elif key in SPEC_KEYS and isinstance(child, list):
            for value in child:
                if isinstance(value, str):
                    found.extend(_pairs_from_text(value))
                elif isinstance(value, dict):
                    pair = _pair_from_dict(value, path + (key,))
                    if pair:
                        found.append(pair)
    normalized: dict[str, str] = {}
    for name, value in found:
        if not name or not value:
            continue
        identity = name.casefold()
        if identity in normalized and normalized[identity] != value:
            raise OrderDetailError("SPEC_AMBIGUOUS", f"规格“{name}”在订单详情中存在冲突值。")
        normalized[identity] = value
    # Preserve stable display names while deduplicating case-insensitively.
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in found:
        identity = name.casefold()
        if identity not in seen and normalized.get(identity) == value:
            result.append((name, value))
            seen.add(identity)
    return tuple(result)


def extract_order_detail_evidence(
    url: str,
    payload: dict[str, Any],
    *,
    expected_order_id: str | None = None,
    expected_item_id: str | None = None,
) -> OrderDetailEvidence:
    """Accept only a matching response with explicit order/item identifiers."""
    if not is_order_detail_url(url):
        raise OrderDetailError("WRONG_ENDPOINT", "响应不是闲鱼订单详情接口。")
    if not isinstance(payload, dict):
        raise OrderDetailError("INVALID_RESPONSE", "订单详情响应不是对象。")
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    order_id = _unique(body, ORDER_KEYS, "ORDER_ID", "订单号")
    item_id = _unique(body, ITEM_KEYS, "ITEM_ID", "商品号")
    if expected_order_id is not None and order_id != str(expected_order_id):
        raise OrderDetailError("ORDER_MISMATCH", "观察到的订单详情不是目标订单。")
    if expected_item_id is not None and item_id != str(expected_item_id):
        raise OrderDetailError("ITEM_MISMATCH", "观察到的订单详情不是目标商品。")
    specs = _collect_specs(body)
    return OrderDetailEvidence(
        order_id=order_id,
        item_id=item_id,
        buyer_id=_buyer_id(body),
        quantity=_optional_quantity(body),
        status=(str(body.get("status")).strip() if body.get("status") not in (None, "") else _optional_unique(body, STATUS_KEYS)),
        specs=specs,
        source_url=url,
        observed_at=now(),
    )


class OrderDetailCache:
    """Process-local cache of passively observed evidence; it never fetches a page."""

    def __init__(self, max_age_seconds: int = 1800):
        self.max_age_seconds = max(30, int(max_age_seconds))
        self._values: dict[str, OrderDetailEvidence] = {}
        self.last_error: dict[str, str] | None = None

    def observe(self, url: str, payload: dict[str, Any]) -> OrderDetailEvidence | None:
        if not is_order_detail_url(url):
            return None
        try:
            evidence = extract_order_detail_evidence(url, payload)
        except OrderDetailError as exc:
            self.last_error = {"code": exc.code, "message": str(exc), "at": now()}
            return None
        self._values[evidence.order_id] = evidence
        self.last_error = None
        return evidence

    def get(self, order_id: str) -> OrderDetailEvidence | None:
        evidence = self._values.get(str(order_id))
        if not evidence:
            return None
        try:
            observed = datetime.fromisoformat(evidence.observed_at)
            if (datetime.now(CHINA) - observed).total_seconds() > self.max_age_seconds:
                self._values.pop(str(order_id), None)
                return None
        except (TypeError, ValueError):
            return None
        return evidence
