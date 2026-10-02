"""Project-owned, bounded lifecycle for the loopback console and existing Edge."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil

from .paths import PROJECT, DATA
from .store import now


def server_lock():
    """One owned server per database, even when a second port is requested."""
    DATA.mkdir(parents=True, exist_ok=True)
    handle = (DATA / "server.lock").open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ValueError("已有自有后台持有这个数据库，请使用现有实例。") from exc
    return handle


def collect_via_server(account: str, item_ids: list[str] | None, *, port: int = 8090, scheduled: bool = False) -> dict:
    """Route CLI/heartbeat and UI through the same durable job queue."""
    import urllib.parse
    if scheduled:
        from .store import Store
        if not Store().setting("collection_enabled", True):
            return {"status": "complete", "paused": True, "jobs": [], "message": "定期采集已暂停，本次未访问平台。"}
    start(port)
    base = f"http://127.0.0.1:{port}"
    results = []
    paths = [f"/api/products/{urllib.parse.quote(item)}/collect" for item in item_ids] if item_ids else ["/api/collect"]
    for path in paths:
        request = urllib.request.Request(base + path + "?account=" + urllib.parse.quote(account),
                  data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            job = json.load(response)
        deadline = time.monotonic() + 240
        while job["state"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(1)
            with urllib.request.urlopen(base + "/api/jobs/" + job["id"], timeout=10) as response:
                job = json.load(response)
        results.append(job)
        if job["state"] in {"queued", "running", "blocked"}:
            break
    return {"status": "complete" if all(j["state"] == "succeeded" for j in results) else "partial", "jobs": results}


def health(port: int = 8090) -> dict:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            return json.load(response)
    except (OSError, ValueError, TimeoutError):
        return {}


def listener(port: int):
    for connection in psutil.net_connections(kind="tcp"):
        if connection.status == "LISTEN" and connection.laddr.port == port and connection.pid:
            return psutil.Process(connection.pid)
    return None


def process_kind(process) -> str:
    try:
        args = process.cmdline()
        cwd = Path(process.cwd()).resolve()
        if cwd == PROJECT and any(Path(arg).name == "xianyu-console.py" for arg in args) and "serve" in args:
            return "owned"
        if cwd == PROJECT / "vendor" / "xianyu-auto-reply-fix" and any(Path(arg).name == "Start.py" for arg in args):
            return "legacy"
    except (psutil.Error, OSError):
        pass
    return "unknown"


def spawn(args: list[str], name: str, *, cwd: Path = PROJECT):
    logs = DATA / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    with (logs / f"{name}.log").open("ab") as output:
        return subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                creationflags=flags, close_fds=True)


def ensure_browser() -> dict:
    existing = listener(9223)
    if existing:
        return {"state": "already_running", "port": 9223}
    profile = PROJECT / "data" / "browser-profile"
    candidates = [Path(os.environ.get(name, "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe"
                  for name in ("PROGRAMFILES(X86)", "PROGRAMFILES")]
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None or not profile.is_dir():
        return {"state": "unavailable", "message": "原 Edge 或原登录目录不存在，请在账号页检查。"}
    spawn([str(executable), f"--user-data-dir={profile}", "--remote-debugging-port=9223",
           "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
           "https://www.goofish.com/im"], "edge")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if listener(9223):
            return {"state": "started", "port": 9223}
        time.sleep(.5)
    return {"state": "unavailable", "message": "原浏览器启动未在限定时间内就绪。"}


def start(port: int = 8090, *, take_over_legacy: bool = False) -> dict:
    current = listener(port)
    if current and health(port).get("service") == "xianyu-owned-console":
        return {"state": "already_running", "url": f"http://127.0.0.1:{port}", "browser": ensure_browser()}
    if current:
        kind = process_kind(current)
        if kind != "legacy" or not take_over_legacy:
            raise ValueError(f"端口 {port} 已被 {kind} 服务占用；没有停止该进程。")
        # Stop only the verified legacy owner. The original Edge/profile remains.
        old = {"pid": current.pid, "created_at": current.create_time(), "kind": kind, "stopped_at": now()}
        current.terminate()
        try:
            current.wait(timeout=12)
        except psutil.TimeoutExpired as exc:
            raise ValueError("原后台未在限定时间内退出，未强制结束其他进程。") from exc
        (DATA / "last-cutover.json").write_text(json.dumps(old, indent=2), encoding="utf-8")
    browser = ensure_browser()
    child = spawn([sys.executable, "-X", "utf8", "-B", str(PROJECT / "scripts/xianyu-console.py"), "serve", "--port", str(port)], "console")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if health(port).get("service") == "xianyu-owned-console":
            record = {"pid": child.pid, "port": port, "started_at": now(), "url": f"http://127.0.0.1:{port}"}
            (DATA / "runtime.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            return {"state": "started", **record, "browser": browser}
        if child.poll() is not None:
            raise ValueError("后台进程已退出；检查 data/console/logs/console.log。")
        time.sleep(.5)
    raise ValueError("后台未在限定时间内就绪；进程可能仍在启动，请先 status，不重复启动。")


def stop(port: int = 8090) -> dict:
    process = listener(port)
    if not process:
        return {"state": "stopped"}
    if process_kind(process) != "owned":
        raise ValueError("该端口不是本项目自有后台，未停止。")
    process.terminate()
    process.wait(timeout=12)
    return {"state": "stopped", "browser": "preserved"}


def status(port: int = 8090) -> dict:
    process = listener(port)
    return {"port": port, "owner": process_kind(process) if process else "none",
            "health": health(port), "browser_ready": bool(listener(9223))}
