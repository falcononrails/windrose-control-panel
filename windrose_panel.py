#!/usr/bin/env python3
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOST = os.getenv("PANEL_HOST", "0.0.0.0")
PORT = int(os.getenv("PANEL_PORT", "8790"))
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "changeme")
PANEL_SECRET = os.getenv("PANEL_SECRET", PANEL_PASSWORD + "-windrose-panel")

GAME_DIR = Path(os.getenv("WINDROSE_GAME_DIR", "/opt/windrose-direct/server"))
DATA_DIR = GAME_DIR / "windrose_plus_data"
SERVER_DESC = GAME_DIR / "R5" / "ServerDescription.json"
BACKUP_DIR = Path(os.getenv("WINDROSE_BACKUP_DIR", "/opt/windrose-backups"))
SERVICE_NAME = os.getenv("WINDROSE_SERVICE", "windrose.service")
DASHBOARD_SERVICE = os.getenv("WINDROSE_PLUS_SERVICE", "windrose-plus-dashboard.service")
SOURCE_RCON_HOST = os.getenv("SOURCE_RCON_HOST", "127.0.0.1")
SOURCE_RCON_PORT = int(os.getenv("SOURCE_RCON_PORT", "27065"))

_cpu_lock = threading.Lock()
_last_cpu: tuple[int, int] | None = None


def run(cmd: list[str], timeout: int = 12) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "code": 124, "stdout": exc.stdout or "", "stderr": "Timed out"}
    except Exception as exc:
        return {"ok": False, "code": 1, "stdout": "", "stderr": str(exc)}


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old_stat = None
    try:
        old_stat = path.stat()
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp_name, path)
    if old_stat is not None:
        try:
            os.chown(path, old_stat.st_uid, old_stat.st_gid)
            os.chmod(path, old_stat.st_mode & 0o777)
        except OSError:
            pass


def copy_owner_mode(path: Path, owner_ref: Path, mode: int = 0o664) -> None:
    try:
        st = owner_ref.stat()
        os.chown(path, st.st_uid, st.st_gid)
        os.chmod(path, mode)
    except OSError:
        pass


def tail_file(path: Path, max_bytes: int = 12000) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def service_state(service: str) -> dict[str, Any]:
    out = run(["systemctl", "show", service, "--no-page",
               "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
               "-p", "MemoryCurrent", "-p", "ActiveEnterTimestamp",
               "-p", "NRestarts"], timeout=5)
    data: dict[str, str] = {}
    for line in out["stdout"].splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v
    return {
        "active_state": data.get("ActiveState", "unknown"),
        "sub_state": data.get("SubState", "unknown"),
        "main_pid": int(data.get("MainPID") or 0),
        "memory_current": int(data.get("MemoryCurrent") or 0),
        "active_since": data.get("ActiveEnterTimestamp", ""),
        "restarts": int(data.get("NRestarts") or 0),
    }


def mem_info() -> dict[str, Any]:
    vals: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split()
            vals[parts[0].rstrip(":")] = int(parts[1]) * 1024
    except Exception:
        pass
    total = vals.get("MemTotal", 0)
    available = vals.get("MemAvailable", 0)
    used = max(0, total - available)
    return {
        "total": total,
        "available": available,
        "used": used,
        "percent": round((used / total) * 100, 1) if total else 0,
    }


def cpu_percent() -> float:
    global _last_cpu
    try:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        nums = [int(x) for x in fields]
        idle = nums[3] + nums[4]
        total = sum(nums)
    except Exception:
        return 0.0
    with _cpu_lock:
        if _last_cpu is None:
            _last_cpu = (idle, total)
            return 0.0
        last_idle, last_total = _last_cpu
        _last_cpu = (idle, total)
    delta_total = total - last_total
    delta_idle = idle - last_idle
    if delta_total <= 0:
        return 0.0
    return round((1 - (delta_idle / delta_total)) * 100, 1)


def disk_info() -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(GAME_DIR)
        used = usage.total - usage.free
        return {
            "total": usage.total,
            "used": used,
            "free": usage.free,
            "percent": round((used / usage.total) * 100, 1),
        }
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "percent": 0}


def process_info() -> dict[str, Any]:
    out = run(["ps", "-eo", "pid,pcpu,rss,args"], timeout=5)
    rows = []
    total_cpu = 0.0
    total_rss = 0
    for line in out["stdout"].splitlines()[1:]:
        if "WindroseServer-Win64-Shipping.exe" not in line and "xvfb-run -a wine" not in line:
            continue
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            pcpu = float(parts[1])
            rss = int(parts[2]) * 1024
        except ValueError:
            continue
        total_cpu += pcpu
        total_rss += rss
        rows.append({"pid": pid, "cpu": pcpu, "rss": rss, "args": parts[3]})
    return {"cpu": round(total_cpu, 1), "rss": total_rss, "processes": rows}


def get_windrose_plus_password() -> str:
    cfg = read_json(GAME_DIR / "windrose_plus.json", {})
    return str(((cfg.get("rcon") or {}).get("password")) or "")


def get_source_rcon_password() -> str:
    explicit = os.getenv("SOURCE_RCON_PASSWORD")
    if explicit:
        return explicit
    settings = GAME_DIR / "R5" / "Binaries" / "Win64" / "windrosercon" / "settings.ini"
    text = tail_file(settings, 4000)
    for line in text.splitlines():
        if line.strip().lower().startswith("password="):
            return line.split("=", 1)[1].strip()
    return ""


class SourceRCON:
    AUTH = 3
    EXECCOMMAND = 2
    RESPONSE_VALUE = 0

    def __init__(self, host: str, port: int, password: str, timeout: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.req_id = random.randint(1000, 999999)

    def __enter__(self) -> "SourceRCON":
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock.settimeout(self.timeout)
        self._send(self.req_id, self.AUTH, self.password)
        packet = self._recv()
        if packet["id"] == -1:
            raise RuntimeError("RCON authentication failed")
        return self

    def __exit__(self, *_: object) -> None:
        if self.sock:
            self.sock.close()

    def _send(self, req_id: int, typ: int, body: str) -> None:
        if not self.sock:
            raise RuntimeError("not connected")
        raw = body.encode("utf-8") + b"\x00\x00"
        size = 8 + len(raw)
        self.sock.sendall(struct.pack("<iii", size, req_id, typ) + raw)

    def _recvn(self, n: int) -> bytes:
        if not self.sock:
            raise RuntimeError("not connected")
        chunks = b""
        while len(chunks) < n:
            chunk = self.sock.recv(n - len(chunks))
            if not chunk:
                raise RuntimeError("connection closed")
            chunks += chunk
        return chunks

    def _recv(self) -> dict[str, Any]:
        size = struct.unpack("<i", self._recvn(4))[0]
        payload = self._recvn(size)
        req_id, typ = struct.unpack("<ii", payload[:8])
        body = payload[8:-2].decode("utf-8", "replace")
        return {"id": req_id, "type": typ, "body": body}

    def command(self, command: str) -> str:
        req_id = self.req_id + 1
        self._send(req_id, self.EXECCOMMAND, command)
        return self._recv()["body"]


def source_rcon_status() -> dict[str, Any]:
    password = get_source_rcon_password()
    if not password:
        return {"available": False, "reason": "not_configured"}
    try:
        with socket.create_connection((SOURCE_RCON_HOST, SOURCE_RCON_PORT), timeout=0.4):
            return {"available": True, "host": SOURCE_RCON_HOST, "port": SOURCE_RCON_PORT}
    except Exception as exc:
        return {"available": False, "reason": str(exc), "host": SOURCE_RCON_HOST, "port": SOURCE_RCON_PORT}


def source_rcon_command(command: str) -> dict[str, Any]:
    password = get_source_rcon_password()
    if not password:
        return {"ok": False, "error": "WindroseRCON password is not configured"}
    try:
        with SourceRCON(SOURCE_RCON_HOST, SOURCE_RCON_PORT, password) as client:
            return {"ok": True, "message": client.command(command)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def parse_source_players(text: str) -> list[dict[str, str]]:
    players = []
    for line in text.splitlines():
        match = re.search(r"^\s*(?P<name>.+?)\s+-\s*(?P<account>[0-9A-Fa-f]{0,40})\s*$", line)
        if match:
            players.append({"name": match.group("name").strip(), "account_id": match.group("account")})
    return players


def parse_log_accounts(max_bytes: int = 2_000_000) -> dict[str, dict[str, str]]:
    log_dir = GAME_DIR / "R5" / "Saved" / "Logs"
    try:
        logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        logs = []
    text = tail_file(logs[0], max_bytes) if logs else ""
    accounts: dict[str, dict[str, str]] = {}
    patterns = [
        re.compile(
            r"Name '(?P<name>[^']+)'\. AccountId '(?P<account>[0-9A-Fa-f]{16,40})'\. State '(?P<state>[^']*)'",
            re.IGNORECASE,
        ),
        re.compile(
            r"AccountName '(?P<name>[^']+)'\. AccountId (?P<account>[0-9A-Fa-f]{16,40})",
            re.IGNORECASE,
        ),
    ]
    for line in text.splitlines():
        for pat in patterns:
            match = pat.search(line)
            if not match:
                continue
            name = match.group("name").strip()
            account = match.group("account").strip()
            state = match.groupdict().get("state") or ""
            if name and account:
                accounts[name.lower()] = {
                    "name": name,
                    "account_id": account,
                    "state": state,
                    "source": "game_log",
                }
    return accounts


def windrose_plus_command(command: str, args: list[str] | None = None, timeout: float = 18.0) -> dict[str, Any]:
    password = get_windrose_plus_password()
    if not password or password == "changeme":
        return {"ok": False, "status": "error", "message": "Windrose+ RCON password is not configured"}
    spool = DATA_DIR / "rcon"
    spool.mkdir(parents=True, exist_ok=True)
    cmd_id = f"panel_{int(time.time())}_{random.randint(100000, 999999)}"
    payload = {
        "id": cmd_id,
        "command": command,
        "args": args or [],
        "password": password,
        "admin_user": "Windrose Panel",
        "timestamp": int(time.time()),
    }
    cmd_path = spool / f"cmd_{cmd_id}.json"
    res_path = spool / f"res_{cmd_id}.json"
    write_json_atomic(cmd_path, payload)
    copy_owner_mode(cmd_path, spool, 0o664)
    with (spool / "pending_commands.txt").open("a", encoding="utf-8") as f:
        f.write(f"cmd_{cmd_id}.json\r\n")
    copy_owner_mode(spool / "pending_commands.txt", spool, 0o664)
    deadline = time.time() + timeout
    result: dict[str, Any] | None = None
    while time.time() < deadline:
        if res_path.exists():
            result = read_json(res_path, {})
            try:
                res_path.unlink()
            except OSError:
                pass
            break
        time.sleep(0.1)
    if result is None:
        return {"ok": False, "status": "error", "message": "Windrose+ command timed out"}
    return {"ok": result.get("status") == "ok", **result}


def server_config() -> dict[str, Any]:
    cfg = read_json(SERVER_DESC, {})
    persistent = cfg.get("ServerDescription_Persistent") or {}
    return {
        "raw": cfg,
        "server_name": persistent.get("ServerName", ""),
        "invite_code": persistent.get("InviteCode", ""),
        "max_players": persistent.get("MaxPlayerCount", 0),
        "password_protected": bool(persistent.get("IsPasswordProtected", False)),
        "password": persistent.get("Password", ""),
        "region": persistent.get("UserSelectedRegion", ""),
        "use_direct_connection": bool(persistent.get("UseDirectConnection", False)),
    }


def update_server_config(body: dict[str, Any]) -> dict[str, Any]:
    cfg = read_json(SERVER_DESC, {})
    persistent = cfg.setdefault("ServerDescription_Persistent", {})
    changed: list[str] = []

    if "server_name" in body:
        name = str(body["server_name"]).strip()[:80]
        if name and persistent.get("ServerName") != name:
            persistent["ServerName"] = name
            changed.append("server_name")

    if "max_players" in body:
        max_players = int(body["max_players"])
        if max_players < 1 or max_players > 64:
            raise ValueError("max_players must be between 1 and 64")
        if persistent.get("MaxPlayerCount") != max_players:
            persistent["MaxPlayerCount"] = max_players
            changed.append("max_players")

    if "password_protected" in body:
        protected = bool(body["password_protected"])
        if persistent.get("IsPasswordProtected") != protected:
            persistent["IsPasswordProtected"] = protected
            changed.append("password_protected")

    if "password" in body:
        password = str(body["password"])[:80]
        if persistent.get("Password") != password:
            persistent["Password"] = password
            changed.append("password")

    if changed:
        backup = SERVER_DESC.with_suffix(SERVER_DESC.suffix + f".bak.{int(time.time())}")
        shutil.copy2(SERVER_DESC, backup)
        write_json_atomic(SERVER_DESC, cfg)
    return {"changed": changed, "requires_restart": bool(changed), "config": server_config()}


def create_backup() -> dict[str, Any]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / f"windrose-panel-{ts}.tar.gz"
    includes = [
        "R5/ServerDescription.json",
        "R5/Saved/SaveProfiles",
        "windrose_plus.json",
        "windrose_plus_data",
    ]
    cmd = ["tar", "-czf", str(target), "-C", str(GAME_DIR)] + includes
    out = run(cmd, timeout=180)
    if not out["ok"]:
        return {"ok": False, "error": out["stderr"] or out["stdout"]}
    return {"ok": True, "path": str(target), "size": target.stat().st_size}


def build_state() -> dict[str, Any]:
    status = read_json(DATA_DIR / "server_status.json", {})
    livemap = read_json(DATA_DIR / "livemap_data.json", {})
    rcon_status = read_json(DATA_DIR / "rcon_status.json", {})
    source_status = source_rcon_status()
    source_players: list[dict[str, str]] = []
    if source_status.get("available"):
        res = source_rcon_command("showplayers")
        if res.get("ok"):
            source_players = parse_source_players(res.get("message", ""))
    log_accounts = parse_log_accounts()

    players = status.get("players") or []
    by_name = {p["name"].lower(): p for p in source_players if p.get("name")}
    for key, item in list(by_name.items()):
        if not item.get("account_id") and key in log_accounts:
            item["account_id"] = log_accounts[key]["account_id"]
            item["account_source"] = "game_log"
    enriched = []
    for p in players:
        item = dict(p)
        src = by_name.get(str(p.get("name", "")).lower())
        log_src = log_accounts.get(str(p.get("name", "")).lower())
        if src and src.get("account_id"):
            item["account_id"] = src["account_id"]
            item["account_source"] = src.get("account_source") or "windrosercon"
        elif log_src:
            item["account_id"] = log_src["account_id"]
            item["account_source"] = "game_log"
        enriched.append(item)
    known_names = {str(p.get("name", "")).lower() for p in enriched}
    for src in source_players:
        log_src = log_accounts.get(str(src.get("name", "")).lower())
        if not src.get("account_id") and log_src:
            src = {**src, "account_id": log_src["account_id"], "account_source": "game_log"}
        if src.get("name", "").lower() not in known_names:
            enriched.append(src)

    return {
        "now": int(time.time()),
        "services": {
            "windrose": service_state(SERVICE_NAME),
            "windrose_plus": service_state(DASHBOARD_SERVICE),
        },
        "host": {
            "cpu_percent": cpu_percent(),
            "load": os.getloadavg() if hasattr(os, "getloadavg") else [0, 0, 0],
            "memory": mem_info(),
            "disk": disk_info(),
            "process": process_info(),
        },
        "windrose_plus": {
            "status": status,
            "rcon_status": rcon_status,
            "livemap_ready": bool(livemap and not livemap.get("error")),
        },
        "source_rcon": source_status,
        "known_accounts": list(log_accounts.values())[-20:],
        "server_config": server_config(),
        "players": enriched,
    }


def make_token() -> str:
    exp = str(int(time.time()) + 86400)
    sig = hmac.new(PANEL_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{exp}:{sig}".encode()).decode()


def validate_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        exp, sig = raw.split(":", 1)
        if int(exp) < time.time():
            return False
        expected = hmac.new(PANEL_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


def format_json_error(exc: Exception) -> dict[str, str]:
    return {"error": str(exc)}


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Windrose Panel</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101315;
      --panel: #171c20;
      --panel-2: #1e252a;
      --line: #303b42;
      --text: #e9eef1;
      --muted: #9ba9b1;
      --green: #56c271;
      --blue: #65a9ff;
      --amber: #f0b35a;
      --red: #e36363;
      --ink: #0c0f11;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      letter-spacing: 0;
    }
    button, input, select, textarea { font: inherit; }
    .shell { min-height: 100vh; display: grid; grid-template-columns: 244px 1fr; }
    aside { border-right: 1px solid var(--line); background: #12171a; padding: 18px 14px; }
    main { padding: 20px; max-width: 1480px; width: 100%; }
    .brand { display: flex; align-items: center; gap: 10px; font-weight: 760; font-size: 17px; margin-bottom: 22px; }
    .mark { width: 30px; height: 30px; display: grid; place-items: center; border: 1px solid #487255; color: var(--green); background: #132018; border-radius: 6px; }
    nav { display: grid; gap: 6px; }
    nav button {
      width: 100%; text-align: left; border: 1px solid transparent; background: transparent; color: var(--muted);
      padding: 10px 11px; border-radius: 6px; cursor: pointer; min-height: 38px;
    }
    nav button.active { background: var(--panel-2); color: var(--text); border-color: var(--line); }
    .topbar { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; margin-bottom: 18px; }
    h1 { margin: 0; font-size: 24px; line-height: 1.15; }
    .sub { color: var(--muted); margin-top: 5px; }
    .grid { display: grid; gap: 12px; }
    .stats { grid-template-columns: repeat(5, minmax(150px, 1fr)); }
    .two { grid-template-columns: minmax(0, 1.2fr) minmax(320px, .8fr); }
    .card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
    .stat .label { color: var(--muted); font-size: 12px; text-transform: uppercase; }
    .stat .value { font-size: 24px; font-weight: 760; margin-top: 8px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .stat .hint { color: var(--muted); margin-top: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .button {
      border: 1px solid var(--line); background: var(--panel-2); color: var(--text); border-radius: 6px;
      padding: 8px 11px; min-height: 36px; cursor: pointer;
    }
    .button:hover { border-color: #51616b; }
    .button.primary { background: #244c34; border-color: #3d8153; }
    .button.warn { background: #4b3520; border-color: #8b6034; }
    .button.danger { background: #4d2426; border-color: #9a4b4f; }
    .button:disabled { opacity: .45; cursor: not-allowed; }
    .pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 8px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); background: #12171a; }
    .pill.ok { color: #b8f3c7; border-color: #3b7249; }
    .pill.bad { color: #ffc1c1; border-color: #814046; }
    .pill.warn { color: #ffe0a6; border-color: #86643a; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 10px 9px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: middle; }
    th { color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 650; }
    td.actions { width: 180px; }
    .row-actions { display: flex; gap: 6px; }
    .form { display: grid; gap: 12px; max-width: 680px; }
    label { display: grid; gap: 6px; color: var(--muted); }
    input, select, textarea {
      width: 100%; color: var(--text); background: #0f1417; border: 1px solid var(--line); border-radius: 6px;
      padding: 9px 10px; min-height: 38px;
    }
    textarea { min-height: 120px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .split { display: grid; grid-template-columns: 1fr 160px; gap: 10px; }
    pre {
      white-space: pre-wrap; overflow: auto; max-height: 520px; padding: 12px; background: #0f1417;
      border: 1px solid var(--line); border-radius: 6px; color: #d7e0e5;
    }
    .tab { display: none; }
    .tab.active { display: block; }
    .mini { color: var(--muted); font-size: 12px; }
    .right { display: flex; gap: 8px; align-items: center; justify-content: flex-end; flex-wrap: wrap; }
    @media (max-width: 960px) {
      .shell { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      nav { grid-template-columns: repeat(3, 1fr); }
      .stats, .two { grid-template-columns: 1fr; }
      main { padding: 14px; }
      .topbar { flex-direction: column; }
      .split { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <div class="brand"><span class="mark">W</span><span>Windrose Panel</span></div>
      <nav>
        <button class="active" data-tab="overview">Overview</button>
        <button data-tab="players">Players</button>
        <button data-tab="config">Config</button>
        <button data-tab="console">Console</button>
        <button data-tab="logs">Logs</button>
      </nav>
    </aside>
    <main>
      <div class="topbar">
        <div><h1 id="server-name">Windrose</h1><div class="sub" id="server-line">Loading...</div></div>
        <div class="right">
          <span class="pill" id="rcon-pill">RCON</span>
          <button class="button" id="refresh">Refresh</button>
          <button class="button" id="logout">Logout</button>
        </div>
      </div>

      <section class="tab active" id="tab-overview">
        <div class="grid stats">
          <div class="card stat"><div class="label">Service</div><div class="value" id="stat-service">-</div><div class="hint" id="stat-uptime">-</div></div>
          <div class="card stat"><div class="label">Players</div><div class="value" id="stat-players">-</div><div class="hint" id="stat-invite">-</div></div>
          <div class="card stat"><div class="label">CPU</div><div class="value" id="stat-cpu">-</div><div class="hint" id="stat-load">-</div></div>
          <div class="card stat"><div class="label">Memory</div><div class="value" id="stat-memory">-</div><div class="hint" id="stat-process">-</div></div>
          <div class="card stat"><div class="label">Disk</div><div class="value" id="stat-disk">-</div><div class="hint" id="stat-backups">-</div></div>
        </div>
        <div class="card" style="margin-top:12px">
          <div class="toolbar">
            <button class="button primary" data-service="start">Start</button>
            <button class="button warn" data-service="restart">Restart</button>
            <button class="button danger" data-service="stop">Stop</button>
            <button class="button" id="backup">Backup</button>
          </div>
          <p class="mini" id="action-result"></p>
        </div>
      </section>

      <section class="tab" id="tab-players">
        <div class="card">
          <table>
            <thead><tr><th>Name</th><th>Account ID</th><th>Position</th><th>Session</th><th>Actions</th></tr></thead>
            <tbody id="players-body"></tbody>
          </table>
        </div>
      </section>

      <section class="tab" id="tab-config">
        <div class="card">
          <form class="form" id="config-form">
            <label>Server name <input name="server_name" maxlength="80"></label>
            <label>Max players <input name="max_players" type="number" min="1" max="64"></label>
            <label>Password protected
              <select name="password_protected"><option value="false">No</option><option value="true">Yes</option></select>
            </label>
            <label>Server password <input name="password" maxlength="80"></label>
            <div class="toolbar"><button class="button primary" type="submit">Save Config</button><button class="button warn" type="button" data-service="restart">Restart</button></div>
          </form>
        </div>
      </section>

      <section class="tab" id="tab-console">
        <div class="grid two">
          <div class="card">
            <div class="split">
              <input id="console-command" value="wp.status">
              <button class="button primary" id="run-command">Run</button>
            </div>
            <pre id="console-output"></pre>
          </div>
          <div class="card">
            <table>
              <thead><tr><th>Command</th><th>Backend</th></tr></thead>
              <tbody>
                <tr><td>wp.status</td><td>Windrose+</td></tr>
                <tr><td>wp.players</td><td>Windrose+</td></tr>
                <tr><td>showplayers</td><td>WindroseRCON</td></tr>
                <tr><td>kick &lt;account&gt;</td><td>WindroseRCON</td></tr>
                <tr><td>ban &lt;account&gt; &lt;reason&gt;</td><td>WindroseRCON</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="tab" id="tab-logs">
        <div class="toolbar" style="margin-bottom:10px"><button class="button" id="refresh-logs">Refresh Logs</button></div>
        <pre id="logs-output"></pre>
      </section>
    </main>
  </div>

  <script>
    let state = null;
    const $ = (sel) => document.querySelector(sel);
    const fmtBytes = (n) => {
      if (!n) return "0 B";
      const units = ["B","KB","MB","GB","TB"];
      let i = 0, v = n;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
      return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
    };
    const api = async (url, opts = {}) => {
      const res = await fetch(url, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || data.message || res.statusText);
      return data;
    };
    function setText(id, value) { $(id).textContent = value ?? "-"; }
    function servicePill(active) { return active === "active" ? "Online" : active || "Unknown"; }
    function render(next) {
      state = next;
      const s = next.windrose_plus.status.server || {};
      const service = next.services.windrose || {};
      const mem = next.host.memory || {};
      const disk = next.host.disk || {};
      const proc = next.host.process || {};
      const cfg = next.server_config || {};
      $("#server-name").textContent = s.name || cfg.server_name || "Windrose";
      $("#server-line").textContent = `Invite ${s.invite_code || cfg.invite_code || "-"} · Version ${s.version || "-"} · Windrose+ ${s.windrose_plus || "-"}`;
      setText("#stat-service", servicePill(service.active_state));
      setText("#stat-uptime", service.active_since || "-");
      setText("#stat-players", `${s.player_count ?? 0}/${s.max_players ?? cfg.max_players ?? 0}`);
      setText("#stat-invite", `Invite ${s.invite_code || cfg.invite_code || "-"}`);
      setText("#stat-cpu", `${next.host.cpu_percent || 0}%`);
      setText("#stat-load", `Load ${(next.host.load || []).map(x => Number(x).toFixed(2)).join(" ")}`);
      setText("#stat-memory", `${mem.percent || 0}%`);
      setText("#stat-process", `Process ${fmtBytes(proc.rss || service.memory_current || 0)}`);
      setText("#stat-disk", `${disk.percent || 0}%`);
      setText("#stat-backups", fmtBytes(disk.free || 0) + " free");
      const rcon = next.source_rcon || {};
      $("#rcon-pill").className = `pill ${rcon.available ? "ok" : "warn"}`;
      $("#rcon-pill").textContent = rcon.available ? "WindroseRCON ready" : "Kick/ban offline";
      const body = $("#players-body");
      body.innerHTML = "";
      const players = next.players || [];
      if (!players.length) {
        body.innerHTML = `<tr><td colspan="5" class="mini">No players online</td></tr>`;
      } else {
        for (const p of players) {
          const account = p.account_id || "";
          const pos = p.x !== undefined ? `${Math.round(p.x)}, ${Math.round(p.y)}, ${Math.round(p.z || 0)}` : "-";
          const tr = document.createElement("tr");
          const source = p.account_source ? ` (${p.account_source})` : "";
          tr.innerHTML = `<td>${escapeHtml(p.name || "-")}</td><td class="mini">${escapeHtml(account ? account + source : "-")}</td><td>${escapeHtml(pos)}</td><td>${escapeHtml(p.session || "-")}</td><td class="actions"><div class="row-actions"><button class="button warn">Kick</button><button class="button danger">Ban</button></div></td>`;
          const [kick, ban] = tr.querySelectorAll("button");
          kick.disabled = !account || !rcon.available;
          ban.disabled = !account || !rcon.available;
          kick.onclick = () => playerAction("kick", account);
          ban.onclick = () => {
            const reason = prompt("Ban reason", "Banned from panel") || "Banned from panel";
            playerAction("ban", account, reason);
          };
          body.appendChild(tr);
        }
      }
      const form = $("#config-form");
      form.server_name.value = cfg.server_name || "";
      form.max_players.value = cfg.max_players || 10;
      form.password_protected.value = String(!!cfg.password_protected);
      form.password.value = cfg.password || "";
    }
    function escapeHtml(v) {
      return String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
    }
    async function refresh() {
      try { render(await api("/api/state")); }
      catch (e) { $("#action-result").textContent = e.message; }
    }
    async function loadLogs() {
      try {
        const data = await api("/api/logs");
        $("#logs-output").textContent = data.logs || "";
      } catch (e) { $("#logs-output").textContent = e.message; }
    }
    async function playerAction(action, account_id, reason = "") {
      try {
        const data = await api("/api/player-action", { method: "POST", body: JSON.stringify({ action, account_id, reason }) });
        $("#action-result").textContent = data.message || "Done";
        await refresh();
      } catch (e) { alert(e.message); }
    }
    document.querySelectorAll("nav button").forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll("nav button,.tab").forEach(x => x.classList.remove("active"));
        btn.classList.add("active");
        $("#tab-" + btn.dataset.tab).classList.add("active");
        if (btn.dataset.tab === "logs") loadLogs();
      };
    });
    document.querySelectorAll("[data-service]").forEach(btn => {
      btn.onclick = async () => {
        const action = btn.dataset.service;
        if ((action === "stop" || action === "restart") && !confirm(`${action} Windrose server?`)) return;
        $("#action-result").textContent = `${action} requested...`;
        try {
          const data = await api("/api/service", { method: "POST", body: JSON.stringify({ action }) });
          $("#action-result").textContent = data.message || "Done";
          setTimeout(refresh, 1500);
        } catch (e) { $("#action-result").textContent = e.message; }
      };
    });
    $("#backup").onclick = async () => {
      $("#action-result").textContent = "Creating backup...";
      try {
        const data = await api("/api/backup", { method: "POST", body: "{}" });
        $("#action-result").textContent = `Backup ${data.path} (${fmtBytes(data.size)})`;
      } catch (e) { $("#action-result").textContent = e.message; }
    };
    $("#config-form").onsubmit = async (ev) => {
      ev.preventDefault();
      const f = ev.currentTarget;
      try {
        const data = await api("/api/config", { method: "POST", body: JSON.stringify({
          server_name: f.server_name.value,
          max_players: Number(f.max_players.value),
          password_protected: f.password_protected.value === "true",
          password: f.password.value
        }) });
        $("#action-result").textContent = data.changed.length ? "Config saved. Restart to apply." : "No config changes.";
        await refresh();
      } catch (e) { alert(e.message); }
    };
    $("#run-command").onclick = async () => {
      const command = $("#console-command").value.trim();
      $("#console-output").textContent = "Running...";
      try {
        const data = await api("/api/rcon", { method: "POST", body: JSON.stringify({ command }) });
        $("#console-output").textContent = data.message || data.error || JSON.stringify(data, null, 2);
      } catch (e) { $("#console-output").textContent = e.message; }
    };
    $("#refresh").onclick = refresh;
    $("#refresh-logs").onclick = loadLogs;
    $("#logout").onclick = async () => { await fetch("/logout", { method: "POST" }); location.href = "/login"; };
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""


LOGIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Windrose Panel Login</title>
  <style>
    body { margin:0; min-height:100vh; display:grid; place-items:center; background:#101315; color:#e9eef1; font:14px Inter,ui-sans-serif,system-ui; }
    form { width:min(360px, calc(100vw - 28px)); background:#171c20; border:1px solid #303b42; border-radius:8px; padding:18px; display:grid; gap:12px; }
    h1 { margin:0; font-size:22px; }
    input,button { min-height:40px; border-radius:6px; border:1px solid #303b42; background:#0f1417; color:#e9eef1; padding:9px 10px; font:inherit; }
    button { background:#244c34; border-color:#3d8153; cursor:pointer; }
    .error { color:#ffc1c1; min-height:18px; }
  </style>
</head>
<body>
  <form method="post" action="/login">
    <h1>Windrose Panel</h1>
    <input name="password" type="password" autocomplete="current-password" autofocus>
    <button type="submit">Login</button>
    <div class="error">__ERROR__</div>
  </form>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "WindrosePanel/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie(raw)
        morsel = jar.get("wp_session")
        return morsel.value if morsel else None

    def authenticated(self) -> bool:
        return validate_token(self.cookie_token())

    def send_text(self, text: str, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, path: str) -> None:
        self.send_response(302)
        self.send_header("Location", path)
        self.end_headers()

    def require_auth(self) -> bool:
        if self.authenticated():
            return True
        if self.path.startswith("/api/"):
            self.send_json({"error": "Authentication required"}, 401)
        else:
            self.redirect("/login")
        return False

    def body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/login":
                if self.authenticated():
                    self.redirect("/")
                else:
                    self.send_text(LOGIN_HTML.replace("__ERROR__", ""))
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if path == "/api/state":
                if not self.require_auth():
                    return
                self.send_json(build_state())
                return
            if path == "/api/logs":
                if not self.require_auth():
                    return
                journal = run(["journalctl", "-u", SERVICE_NAME, "-n", "180", "--no-pager"], timeout=8)
                game_log_dir = GAME_DIR / "R5" / "Saved" / "Logs"
                latest_log = ""
                try:
                    logs = sorted(game_log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
                    latest_log = tail_file(logs[0], 9000) if logs else ""
                except Exception:
                    latest_log = ""
                self.send_json({"logs": (journal["stdout"] + "\n\n--- Game log ---\n" + latest_log).strip()})
                return
            if path == "/" or path == "/index.html":
                if not self.require_auth():
                    return
                self.send_text(INDEX_HTML)
                return
            self.send_json({"error": "Not found"}, 404)
        except Exception as exc:
            self.send_json(format_json_error(exc), 500)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/login":
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8")
                params = urllib.parse.parse_qs(body)
                password = (params.get("password") or [""])[0]
                if hmac.compare_digest(password, PANEL_PASSWORD) and PANEL_PASSWORD != "changeme":
                    token = make_token()
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", f"wp_session={token}; Max-Age=86400; Path=/; HttpOnly; SameSite=Lax")
                    self.end_headers()
                else:
                    self.send_text(LOGIN_HTML.replace("__ERROR__", html.escape("Invalid password")), 403)
                return
            if path == "/logout":
                self.send_response(204)
                self.send_header("Set-Cookie", "wp_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax")
                self.end_headers()
                return
            if not self.require_auth():
                return
            body = self.body_json()
            if path == "/api/service":
                action = str(body.get("action", ""))
                if action not in {"start", "stop", "restart"}:
                    self.send_json({"error": "Invalid service action"}, 400)
                    return
                out = run(["systemctl", action, SERVICE_NAME], timeout=20)
                self.send_json({"ok": out["ok"], "message": out["stderr"] or out["stdout"] or f"{action} sent"}, 200 if out["ok"] else 500)
                return
            if path == "/api/config":
                self.send_json(update_server_config(body))
                return
            if path == "/api/backup":
                result = create_backup()
                self.send_json(result, 200 if result.get("ok") else 500)
                return
            if path == "/api/rcon":
                command = str(body.get("command", "")).strip()
                if not command:
                    self.send_json({"error": "Command is required"}, 400)
                    return
                if command.lower().startswith("wp."):
                    self.send_json(windrose_plus_command(command))
                else:
                    result = source_rcon_command(command)
                    self.send_json(result, 200 if result.get("ok") else 503)
                return
            if path == "/api/player-action":
                action = str(body.get("action", ""))
                account_id = str(body.get("account_id", "")).strip()
                reason = str(body.get("reason", "")).strip()
                if action not in {"kick", "ban"} or not account_id:
                    self.send_json({"error": "Invalid player action"}, 400)
                    return
                command = f"kick {account_id}" if action == "kick" else f"ban {account_id} {reason or 'Banned from panel'}"
                result = source_rcon_command(command)
                self.send_json(result, 200 if result.get("ok") else 503)
                return
            self.send_json({"error": "Not found"}, 404)
        except Exception as exc:
            self.send_json(format_json_error(exc), 500)


def main() -> None:
    if PANEL_PASSWORD == "changeme":
        print("Refusing to start with PANEL_PASSWORD=changeme", flush=True)
        raise SystemExit(2)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Windrose panel listening on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
