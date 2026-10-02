"""Small owned SQLite store; no vendor imports and no HTTP dependency."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterator

from .paths import DATA, prepare_directories

CHINA = timezone(timedelta(hours=8))


def now() -> str:
    return datetime.now(CHINA).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | None = None):
        prepare_directories()
        self.path = path or DATA / "console.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL, key TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL, source TEXT NOT NULL, saved_at TEXT NOT NULL,
                    PRIMARY KEY(kind,key)
                );
                CREATE INDEX IF NOT EXISTS records_kind_account ON records(kind,account);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, action TEXT NOT NULL, account TEXT, item_id TEXT,
                    state TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, ended_at TEXT,
                    result TEXT, error TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at);
            """)

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=20000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, kind: str, key: str, value: dict[str, Any], *, account: str = "", source: str = "console") -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO records(kind,key,account,payload,source,saved_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(kind,key) DO UPDATE SET account=excluded.account,payload=excluded.payload,"
                "source=excluded.source,saved_at=excluded.saved_at",
                (kind, str(key), account, json.dumps(value, ensure_ascii=False), source, now()),
            )

    def get(self, kind: str, key: str, default: Any = None) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM records WHERE kind=? AND key=?", (kind, str(key))).fetchone()
        return json.loads(row[0]) if row else default

    def rows(self, kind: str, account: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT key,payload,source,saved_at FROM records WHERE kind=?"
        params: list[Any] = [kind]
        if account is not None:
            query += " AND account=?"
            params.append(account)
        query += " ORDER BY key"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(json.loads(row["payload"]), _key=row["key"], _source=row["source"], _saved_at=row["saved_at"]) for row in rows]

    def remove(self, kind: str, key: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, str(key)))

    def setting(self, key: str, default: Any = None) -> Any:
        return self.get("setting", key, {"value": default}).get("value", default)

    def set_setting(self, key: str, value: Any) -> None:
        self.put("setting", key, {"value": value})

    def _cipher(self, *, create: bool = False):
        from cryptography.fernet import Fernet
        key_path = self.path.parent / ".account.key"
        if not key_path.exists():
            if not create or self.rows("credential"):
                raise ValueError("账号密钥缺失，请从原备份恢复密钥后重试")
            try:
                with key_path.open("xb") as handle:
                    handle.write(Fernet.generate_key())
            except FileExistsError:
                pass
        return Fernet(key_path.read_bytes().strip())

    def save_cookie(self, account: str, cookie: str) -> None:
        encrypted = self._cipher(create=True).encrypt(cookie.encode("utf-8")).decode("ascii")
        self.put("credential", account, {"encrypted": encrypted, "updated_at": now()}, account=account)

    def cookie(self, account: str) -> str:
        saved = self.get("credential", account)
        if not saved:
            raise ValueError("账号尚未保存登录资料")
        try:
            return self._cipher().decrypt(saved["encrypted"].encode("ascii")).decode("utf-8")
        except Exception as exc:
            raise ValueError("本项目账号密钥不可用，请恢复数据库对应的账号密钥备份") from exc

    def new_job(self, action: str, account: str, item_id: str | None = None) -> dict[str, Any]:
        job = {"id": uuid.uuid4().hex, "action": action, "account": account, "item_id": item_id,
               "state": "queued", "created_at": now()}
        with self.connect() as db:
            db.execute("INSERT INTO jobs(id,action,account,item_id,state,created_at) VALUES(?,?,?,?,?,?)",
                       tuple(job[k] for k in ("id", "action", "account", "item_id", "state", "created_at")))
        return job

    def update_job(self, job_id: str, state: str, *, result: dict | None = None, error: str | None = None) -> None:
        with self.connect() as db:
            if state == "running":
                db.execute("UPDATE jobs SET state=?,started_at=? WHERE id=?", (state, now(), job_id))
            else:
                db.execute("UPDATE jobs SET state=?,ended_at=?,result=?,error=? WHERE id=?",
                           (state, now(), json.dumps(result, ensure_ascii=False) if result is not None else None, error, job_id))

    def jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT ?", (min(max(limit, 1), 200),)).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["result"] = json.loads(value["result"]) if value.get("result") else None
            result.append(value)
        return result

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result["result"]) if result.get("result") else None
        return result

    def recover_jobs(self) -> int:
        """Only called by a newly bound single server after its previous owner is gone."""
        with self.connect() as db:
            result = db.execute("UPDATE jobs SET state='interrupted',ended_at=?,error=? WHERE state IN ('queued','running')",
                                (now(), "服务上次停止时任务未完成；记录已保留，可重新发起只读采集。"))
            return result.rowcount


def product_key(account: str, item_id: str) -> str:
    return f"{account}:{item_id}"
