"""Small project-owned codec for the official Goofish IM browser connection.

Protocol field names were inferred from the project's archived AGPL reference
implementation and observed browser traffic.  This module imports no vendor
runtime and deliberately supports only text.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

try:  # Optional compatibility path for older/special sync payloads.
    import msgpack  # type: ignore
except ImportError:  # The normal live path is JSON; missing fallback fails closed.
    msgpack = None


SEND_LWP = "/r/MessageSend/sendByReceiverScope"
HISTORY_LWP = "/r/MessageManager/listUserMessages"
PAID_MARKERS = frozenset({"等待卖家发货", "买家已付款", "待发货"})
ORDER_KEYS = frozenset({"orderId", "bizOrderId", "order_id"})
ITEM_KEYS = frozenset({"itemId", "item_id"})


@dataclass(frozen=True)
class MessageEvent:
    account: str
    message_id: str
    kind: str
    cid: str
    sender_id: str | None
    sender_name: str | None
    item_id: str | None
    order_id: str | None
    text: str | None
    created_ms: int | None
    raw: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HistoryMessage:
    cid: str
    sender_id: str
    text: str
    created_ms: int
    message_id: str | None = None


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _decode_b64_json(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        padded = value + "=" * (-len(value) % 4)
        parsed = json.loads(base64.b64decode(padded).decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError, UnicodeDecodeError):
        return {}


def _string_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _string_keys(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_string_keys(child) for child in value]
    return value


def _decode_sync_entry(value: Any) -> dict[str, Any]:
    """Decode JSON sync data plus the observed base64-wrapped MessagePack fallback."""
    if not isinstance(value, str) or not value:
        return {}
    try:
        padded = value + "=" * (-len(value) % 4)
        first = base64.b64decode(padded)
        text = first.decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError):
        return {}
    try:
        parsed = json.loads(text)
        return _string_keys(parsed) if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        pass
    if msgpack is None:
        return {}
    try:
        second = base64.b64decode(text + "=" * (-len(text) % 4))
        parsed = msgpack.unpackb(second, raw=False, strict_map_key=False)
        return _string_keys(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _walk(value: Any, *, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            yield path, key_text, child
            yield from _walk(child, path=path + (key_text,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, path=path + (str(index),))
    elif isinstance(value, str):
        nested = _json_dict(value)
        if nested:
            yield from _walk(nested, path=path + ("$json",))


def _unique_field(value: Any, keys: frozenset[str]) -> str | None:
    found: set[str] = set()
    for _path, key, child in _walk(value):
        if key in keys and child not in (None, "", 0, "0") and not isinstance(child, (dict, list)):
            found.add(str(child).strip())
    return next(iter(found)) if len(found) == 1 else None


def _query_id(value: Any, names: tuple[str, ...]) -> str | None:
    found: set[str] = set()
    for _path, _key, child in _walk(value):
        if not isinstance(child, str) or "?" not in child:
            continue
        try:
            query = parse_qs(urlparse(child).query)
        except ValueError:
            continue
        for name in names:
            found.update(str(v).strip() for v in query.get(name, []) if str(v).strip())
    return next(iter(found)) if len(found) == 1 else None


def _extract_message_id(message_1: dict[str, Any], message_10: dict[str, Any]) -> str | None:
    for field in ("bizTag", "extJson"):
        value = _json_dict(message_10.get(field))
        if value.get("messageId"):
            return str(value["messageId"])
    direct = _unique_field(message_1, frozenset({"messageId", "message_id"}))
    return direct


def _custom_text(value: Any) -> str | None:
    custom = value if isinstance(value, dict) else {}
    decoded = _decode_b64_json(custom.get("data"))
    text = (decoded.get("text") or {}).get("text") if isinstance(decoded.get("text"), dict) else None
    return str(text) if text not in (None, "") else None


def _fallback_id(account: str, inner: dict[str, Any]) -> str:
    """Build a stable identity that does not depend on sync-batch ordering."""
    message = inner.get("1")
    detail = message.get("10") if isinstance(message, dict) and isinstance(message.get("10"), dict) else {}
    reminder = inner.get("3") if isinstance(inner.get("3"), dict) else {}
    material = {
        "account": account,
        "cid": message.get("2") if isinstance(message, dict) else message,
        "sender": detail.get("senderUserId"),
        "text": detail.get("reminderContent") or reminder.get("redReminder"),
        "created": message.get("5") if isinstance(message, dict) else None,
        "order": _unique_field(inner, ORDER_KEYS) or _query_id(inner, ("orderId", "bizOrderId")),
        "item": _unique_field(inner, ITEM_KEYS) or _query_id(inner, ("itemId",)),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "fallback:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _disallowed_text_route(message_10: dict[str, Any], inner: dict[str, Any]) -> bool:
    """Reject explicit group, task, and security traffic from reply routing."""
    metadata: dict[str, Any] = {}
    for field in ("bizTag", "extJson"):
        metadata.update(_json_dict(message_10.get(field)))
    combined = {**metadata, **message_10}
    for _path, key, child in _walk(combined):
        lowered = key.casefold()
        if lowered in {"isgroup", "groupid", "group_id"} and child not in (None, "", 0, "0", False):
            return True
        if lowered in {"conversationtype", "sessiontype"} and str(child).strip().casefold() in {
            "2", "group", "groupchat", "multi",
        }:
            return True
        if lowered.startswith("task") or "security" in lowered or lowered.startswith("risk"):
            if child not in (None, "", 0, "0", False):
                return True
    return False


def _normalize_cid(value: Any) -> str:
    cid = str(value or "").strip()
    return cid[:-8] if cid.endswith("@goofish") else cid


def _new_mid() -> str:
    """Generate the numeric DingTalk-LWP message ID accepted by the live endpoint."""
    return f"{secrets.randbelow(1000)}{int(time.time() * 1000)} 0"


def unwrap_sync_payloads(frame: str | bytes | dict[str, Any]) -> list[dict[str, Any]]:
    """Return decoded inner JSON messages; invalid/encrypted entries are ignored."""
    if isinstance(frame, bytes):
        try:
            frame = frame.decode("utf-8")
        except UnicodeDecodeError:
            return []
    if isinstance(frame, str):
        try:
            frame = json.loads(frame)
        except (TypeError, ValueError):
            return []
    if not isinstance(frame, dict):
        return []
    if "1" in frame:
        return [frame]
    # The official socket also emits control responses with list bodies.  They
    # are not sync messages and must not terminate the long-running consumer.
    body = frame.get("body")
    if not isinstance(body, dict):
        return []
    package = body.get("syncPushPackage")
    if not isinstance(package, dict):
        return []
    entries = package.get("data")
    if not isinstance(entries, list):
        return []
    decoded: list[dict[str, Any]] = []
    for entry in entries:
        inner = _decode_sync_entry(entry.get("data") if isinstance(entry, dict) else None)
        if inner:
            decoded.append(inner)
    return decoded


def decode_events(
    frame: str | bytes | dict[str, Any], *, account: str, self_user_id: str
) -> list[MessageEvent]:
    """Decode only buyer text, seller text echoes, and paid/pending-ship events."""
    events: list[MessageEvent] = []
    for inner in unwrap_sync_payloads(frame):
        message_1 = inner.get("1")
        if isinstance(message_1, str):
            reminder = inner.get("3") if isinstance(inner.get("3"), dict) else {}
            marker = str(reminder.get("redReminder") or "").strip()
            if marker not in PAID_MARKERS:
                continue
            cid = _normalize_cid(message_1)
            order_id = _unique_field(inner, ORDER_KEYS) or _query_id(inner, ("orderId", "bizOrderId"))
            item_id = _unique_field(inner, ITEM_KEYS) or _query_id(inner, ("itemId",))
            events.append(MessageEvent(
                account=account,
                message_id=_fallback_id(account, inner),
                kind="paid",
                cid=cid,
                sender_id=None,
                sender_name=None,
                item_id=item_id,
                order_id=order_id,
                text=marker,
                created_ms=None,
                raw=inner,
            ))
            continue
        if not isinstance(message_1, dict):
            continue
        message_10 = message_1.get("10") if isinstance(message_1.get("10"), dict) else {}
        cid = _normalize_cid(message_1.get("2"))
        sender_id = str(message_10.get("senderUserId") or "").strip() or None
        sender_name = str(message_10.get("senderNick") or "").strip() or None
        text = str(message_10.get("reminderContent") or "").strip() or None
        content = message_1.get("6") if isinstance(message_1.get("6"), dict) else {}
        content_3 = content.get("3") if isinstance(content.get("3"), dict) else {}
        content_type = content_3.get("4")
        if text is None:
            text = _custom_text(content_3.get("5") if isinstance(content_3.get("5"), dict) else {})
        direction = message_1.get("7")
        marker = str((inner.get("3") or {}).get("redReminder") or "").strip() if isinstance(inner.get("3"), dict) else ""
        order_id = _unique_field(inner, ORDER_KEYS) or _query_id(inner, ("orderId", "bizOrderId"))
        item_id = _unique_field(inner, ITEM_KEYS) or _query_id(inner, ("itemId",))
        created = message_1.get("5")
        try:
            created_ms = int(created) if created not in (None, "") else None
        except (TypeError, ValueError):
            created_ms = None
        message_id = _extract_message_id(message_1, message_10) or _fallback_id(account, inner)
        if marker in PAID_MARKERS:
            kind = "paid"
        elif _disallowed_text_route(message_10, inner):
            continue
        elif sender_id == str(self_user_id) and text:
            kind = "self_text"
        elif direction == 2 and content_type in (None, 1, "1") and sender_id and text:
            kind = "buyer_text"
        else:
            continue
        events.append(MessageEvent(
            account=account,
            message_id=message_id,
            kind=kind,
            cid=cid,
            sender_id=sender_id,
            sender_name=sender_name,
            item_id=item_id,
            order_id=order_id,
            text=text or marker,
            created_ms=created_ms,
            raw=inner,
        ))
    return events


def build_text_send_frame(*, cid: str, recipient_id: str, self_user_id: str, text: str) -> dict[str, Any]:
    if not all(str(value or "").strip() for value in (cid, recipient_id, self_user_id, text)):
        raise ValueError("cid, recipient_id, self_user_id and text are required")
    content = {"contentType": 1, "text": {"text": str(text)}}
    encoded = base64.b64encode(
        json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return {
        "lwp": SEND_LWP,
        "headers": {"mid": _new_mid()},
        "body": [
            {
                "uuid": uuid.uuid4().hex,
                "cid": f"{_normalize_cid(cid)}@goofish",
                "conversationType": 1,
                "content": {"contentType": 101, "custom": {"type": 1, "data": encoded}},
                "redPointPolicy": 0,
                "extension": {"extJson": "{}"},
                "ctx": {"appVersion": "1.0", "platform": "web"},
                "mtags": {},
                "msgReadStatusSetting": 1,
            },
            {"actualReceivers": [f"{recipient_id}@goofish", f"{self_user_id}@goofish"]},
        ],
    }


def build_history_request(cid: str, *, limit: int = 50, cursor: int | None = None) -> dict[str, Any]:
    bounded = min(max(int(limit), 1), 100)
    return {
        "lwp": HISTORY_LWP,
        "headers": {"mid": _new_mid()},
        "body": [f"{_normalize_cid(cid)}@goofish", False, cursor or 9007199254740991, bounded, False],
    }


def _coerce_ms(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        number = float(value)
        return int(number * 1000) if number < 10**11 else int(number)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except (TypeError, ValueError):
            return None


def decode_history(body: dict[str, Any], *, cid: str) -> list[HistoryMessage]:
    messages: list[HistoryMessage] = []
    for row in body.get("userMessageModels") or []:
        if not isinstance(row, dict):
            continue
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        extension = message.get("extension") if isinstance(message.get("extension"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), dict) else {}
        custom = content.get("custom") if isinstance(content.get("custom"), dict) else {}
        decoded = _decode_b64_json(custom.get("data"))
        text_node = decoded.get("text") if isinstance(decoded.get("text"), dict) else {}
        text = str(text_node.get("text") or "")
        sender = str(extension.get("senderUserId") or "")
        created_ms = next((v for candidate in (
            row.get("createTime"), row.get("gmtCreate"), row.get("createdAt"),
            row.get("messageTime"), row.get("sendTime"), row.get("timestamp"), extension.get("createTime"),
            message.get("createAt"), message.get("createTime"),
        ) if (v := _coerce_ms(candidate)) is not None), None)
        if not sender or not text or created_ms is None:
            continue
        tag = _json_dict(extension.get("bizTag")) or _json_dict(extension.get("extJson"))
        message_id_value = message.get("messageId") or tag.get("messageId")
        message_id = str(message_id_value) if message_id_value else None
        messages.append(HistoryMessage(_normalize_cid(cid), sender, text, created_ms, message_id))
    return messages


def frame_json(frame: dict[str, Any]) -> str:
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
