"""One-time, read-only import. Imported records become owned console data."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .paths import PROJECT, DEFAULT_ACCOUNT, DEFAULT_ITEM
from .store import Store, now, product_key
from .marketplace import parse_cookie

LEGACY_DB = PROJECT / "vendor" / "xianyu-auto-reply-fix" / "data" / "xianyu_data.db"


def _decrypt(value: str, key: bytes | None) -> str:
    if not value.startswith("enc$"):
        return value
    if not key:
        raise ValueError("旧账号资料已加密，但原密钥不可用")
    from cryptography.fernet import Fernet
    try:
        return Fernet(key).decrypt(value[4:].encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise ValueError("无法解密原账号资料；未修改原数据库") from exc


def _image_urls(detail: str | dict | None) -> list[str]:
    try:
        value = json.loads(detail) if isinstance(detail, str) else (detail or {})
        if not isinstance(value, dict):
            return []
        image = (value.get("pic_info") or {}).get("picUrl") or (value.get("detail_params") or {}).get("picUrl")
        return [image] if isinstance(image, str) and image.startswith(("http://", "https://")) else []
    except (ValueError, TypeError, AttributeError):
        return []


def migrate(store: Store, legacy_db: Path = LEGACY_DB) -> dict:
    existing = store.setting("legacy_import")
    if existing:
        return {**existing, "already_imported": True}
    if not legacy_db.is_file():
        return {"status": "source_missing", "message": "未找到原数据，后台可使用，但尚未迁入账号和商品。"}
    key_value = os.environ.get("SECRET_ENCRYPTION_KEY")
    key_path = legacy_db.parent / ".secret_encryption.key"
    key = key_value.encode("ascii") if key_value else (key_path.read_bytes().strip() if key_path.is_file() else None)
    db = sqlite3.connect(legacy_db.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        def rows(table):
            return [dict(r) for r in db.execute(f'SELECT * FROM "{table}"')] if table in tables else []

        accounts = rows("cookies")
        # Decrypt every account first, so a missing key cannot leave a misleading completed import.
        credentials = [(str(r["id"]), _decrypt(str(r.get("value") or ""), key)) for r in accounts]
        for row, (account, cookie) in zip(accounts, credentials):
            store.save_cookie(account, cookie)
            store.put("account", account, {
                "id": account, "label": account, "auth_state": "unchecked",
                "credential_imported_at": now(), "last_verified_at": None,
                "source": "legacy_import", "legacy_owner_id": row.get("user_id"),
                "platform_user_id": parse_cookie(cookie).get("unb"),
                "proxy_configured": str(row.get("proxy_type") or "none") not in ("none", ""),
            }, account=account, source="legacy_import")
        counts["accounts"] = len(accounts)

        products = rows("item_info")
        for row in products:
            account = str(row.get("cookie_id") or DEFAULT_ACCOUNT)
            item_id = str(row["item_id"])
            store.put("product", product_key(account, item_id), {
                "account": account, "item_id": item_id, "title": row.get("item_title") or "未命名商品",
                "description": row.get("item_description") or "", "price": row.get("item_price") or None,
                "status": "unknown", "image_urls": _image_urls(row.get("item_detail")),
                "source": "legacy_local_database", "source_updated_at": row.get("updated_at"),
                "observed_at": None, "watch": item_id == DEFAULT_ITEM,
                "slug": "dsh-orangebook" if item_id == DEFAULT_ITEM else None,
                "is_multi_spec": bool(row.get("is_multi_spec")),
                "multi_quantity_delivery": bool(row.get("multi_quantity_delivery")),
            }, account=account, source="legacy_import")
        counts["products"] = len(products)

        for table, kind, id_name in (
            ("orders", "order", "order_id"), ("delivery_rules", "delivery_rule", "id"),
            ("cards", "card", "id"), ("keywords", "keyword", "id"),
            ("delivery_logs", "delivery_log", "id"), ("ai_reply_settings", "legacy_reply_setting", "cookie_id"),
        ):
            records = rows(table)
            for i, row in enumerate(records):
                account = str(row.get("cookie_id") or DEFAULT_ACCOUNT)
                row["source"] = "legacy_local_database"
                row["imported_at"] = now()
                # A historical row is never relabeled as a fresh platform observation.
                store.put(kind, str(row.get(id_name, i)), row, account=account, source="legacy_import")
            counts[kind] = len(records)
    finally:
        db.close()
    result = {"status": "imported", "at": now(), "source": str(legacy_db), "counts": counts,
              "source_modified": False}
    store.set_setting("legacy_import", result)
    store.set_setting("collection_enabled", True)
    store.set_setting("collection_time", "21:00")
    store.set_setting("timezone", "Asia/Shanghai")
    return result
