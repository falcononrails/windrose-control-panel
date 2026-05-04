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
APP_ID = os.getenv("WINDROSE_APP_ID", "4129620")
PANEL_MODE = os.getenv("WINDROSE_PANEL_MODE", "auto").strip().lower()
CONTROL_DIR = Path(os.getenv("WINDROSE_CONTROL_DIR", str(GAME_DIR / "windrose_panel_data")))
INSTALL_PARENT = Path(os.getenv("WINDROSE_INSTALL_PARENT", str(GAME_DIR.parent)))
PIN_FILE = Path(os.getenv("WINDROSE_VERSION_PIN_FILE", str(INSTALL_PARENT / "version-pin.json")))
UPDATE_LOG = Path(os.getenv("WINDROSE_UPDATE_LOG", "/var/log/windrose-update.log"))
ROLLBACK_LOG = Path(os.getenv("WINDROSE_ROLLBACK_LOG", "/var/log/windrose-rollback.log"))
STEAM_LATEST_CACHE = Path(os.getenv("WINDROSE_STEAM_LATEST_CACHE", str(INSTALL_PARENT / "steam-latest.json")))
SNAPSHOT_PREFIXES = ("server-before-update-", "server-before-rollback-", "server-snapshot-")
READY_MARKER = "Host server is ready for owner to connect"
BROKEN_REGISTRATION_MARKERS = (
    "SetBrokenState",
    "Cannot create Coop NetServer",
    "Server Authorization failed",
    "Server registration finished with error",
    "Cannot establish connection to HTTP server",
)
RUNTIME_PATHS = (
    "R5/Saved",
    "R5/ServerDescription.json",
    ".windrose_panel_password",
    ".windrose_panel_secret",
    ".windrose_plus_dashboard_password",
    ".windrose_plus_rcon_password",
    "windrose_plus.json",
    "windrose_panel_data",
    "UE4SS-settings.ini",
    "WindrosePlus",
    "windrose_plus",
    "windrose_plus_data",
    "server",
    "tools",
    "cpp-mods",
    "R5/Binaries/Win64/dwmapi.dll",
    "R5/Binaries/Win64/version.dll",
    "R5/Binaries/Win64/ue4ss",
    "R5/Binaries/Win64/windrosercon",
)

_cpu_lock = threading.Lock()
_last_cpu: tuple[int, int] | None = None


def safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


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


def panel_mode() -> str:
    if PANEL_MODE in {"systemd", "container"}:
        return PANEL_MODE
    if Path("/run/systemd/system").exists() and shutil.which("systemctl"):
        return "systemd"
    return "container"


def is_container_mode() -> bool:
    return panel_mode() == "container"


def ensure_install_parent_safe() -> None:
    game = GAME_DIR.resolve()
    parent = INSTALL_PARENT.resolve()
    if parent == game or game in parent.parents:
        raise ValueError("WINDROSE_INSTALL_PARENT must not be inside WINDROSE_GAME_DIR")


def utc_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S")


def iso_from_ts(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_systemd_timestamp(value: str) -> float | None:
    value = value.strip()
    if not value or value.lower() == "n/a":
        return None
    try:
        return dt.datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=dt.UTC).timestamp()
    except ValueError:
        return None


def manifest_build(root: Path) -> str:
    manifest = root / "steamapps" / f"appmanifest_{APP_ID}.acf"
    text = tail_file(manifest, 20000)
    match = re.search(r'"buildid"\s+"([^"]+)"', text)
    return match.group(1) if match else ""


def snapshot_version(root: Path) -> str:
    status = read_json(root / "windrose_plus_data" / "server_status.json", {})
    server = status.get("server") or {}
    version = str(server.get("version") or "")
    if version:
        return version
    cfg = read_json(root / "R5" / "ServerDescription.json", {})
    deployment = str(cfg.get("DeploymentId") or "")
    return deployment.split("-", 1)[0] if deployment else ""


def snapshot_size(root: Path) -> int:
    out = run(["du", "-sb", str(root)], timeout=20)
    if out["ok"] and out["stdout"]:
        return safe_int(out["stdout"].split()[0])
    return 0


def version_pin() -> dict[str, Any]:
    data = read_json(PIN_FILE, {})
    target = str(data.get("target_build") or "latest")
    return {
        "target_build": target,
        "auto_update": target == "latest",
        "updated_at": data.get("updated_at", ""),
        "reason": data.get("reason", ""),
    }


def write_version_pin(target_build: str, reason: str) -> dict[str, Any]:
    target = str(target_build or "latest").strip() or "latest"
    data = {
        "target_build": target,
        "reason": reason,
        "updated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    write_json_atomic(PIN_FILE, data)
    return version_pin()


def clear_version_pin() -> dict[str, Any]:
    write_version_pin("latest", "resume latest auto-update")
    return version_pin()


def append_rollback_log(message: str) -> None:
    ROLLBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ROLLBACK_LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}] {message}\n")


def is_snapshot_name(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in SNAPSHOT_PREFIXES)


def resolve_snapshot(snapshot_id: str) -> Path:
    name = Path(str(snapshot_id)).name
    if not is_snapshot_name(name):
        raise ValueError("Invalid rollback snapshot")
    path = (INSTALL_PARENT / name).resolve()
    if path.parent != INSTALL_PARENT.resolve() or not path.is_dir():
        raise ValueError("Rollback snapshot not found")
    return path


def version_entry(path: Path, live: bool = False) -> dict[str, Any]:
    try:
        st = path.stat()
        created = iso_from_ts(st.st_mtime)
    except OSError:
        created = ""
    return {
        "id": "__live__" if live else path.name,
        "path": str(path),
        "live": live,
        "source": "current" if live else "saved",
        "build": manifest_build(path),
        "version": snapshot_version(path),
        "created": created,
        "size": snapshot_size(path),
    }


def list_versions() -> list[dict[str, Any]]:
    entries = [version_entry(GAME_DIR, live=True)]
    try:
        snapshots = sorted(
            [p for p in INSTALL_PARENT.iterdir() if p.is_dir() and is_snapshot_name(p.name)],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        snapshots = []
    entries.extend(version_entry(path) for path in snapshots)
    return entries


def list_saved_versions() -> list[dict[str, Any]]:
    versions = list_versions()
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in versions:
        build = str(item.get("build") or "")
        key = build or str(item.get("path") or item.get("id") or "")
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
    return selected


def steam_latest_state() -> dict[str, Any]:
    data = read_json(STEAM_LATEST_CACHE, {})
    return {
        "app_id": APP_ID,
        "latest_build": str(data.get("latest_build") or ""),
        "checked_at": data.get("checked_at", ""),
        "error": data.get("error", ""),
    }


def check_steam_latest() -> dict[str, Any]:
    out = run(["/usr/local/bin/windrose-latest-build"], timeout=220)
    latest = re.sub(r"\D", "", out.get("stdout", "").splitlines()[-1] if out.get("stdout") else "")
    data = {
        "latest_build": latest,
        "checked_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "error": "" if out["ok"] and latest else (out.get("stderr") or out.get("stdout") or "Could not read Steam latest build"),
    }
    write_json_atomic(STEAM_LATEST_CACHE, data)
    return steam_latest_state()


def snapshot_history_event(path: Path) -> dict[str, Any]:
    item = version_entry(path)
    name = path.name
    if name.startswith("server-before-update-"):
        action = "Saved before update"
    elif name.startswith("server-before-rollback-"):
        action = "Saved before switch"
    elif name.startswith("server-snapshot-manual-"):
        action = "Manual snapshot"
    elif name.startswith("server-snapshot-pre-update-"):
        action = "Saved before update"
    else:
        action = "Saved install"
    return {
        "time": item.get("created", ""),
        "action": action,
        "build": item.get("build", ""),
        "version": item.get("version", ""),
        "detail": name,
    }


def rollback_log_history(max_lines: int = 80) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    pattern = re.compile(r"^\[(?P<time>[^\]]+)\]\s+(?P<message>.*)$")
    for line in tail_file(ROLLBACK_LOG, 32000).splitlines()[-max_lines:]:
        match = pattern.match(line.strip())
        if not match:
            continue
        message = match.group("message")
        action = "Version activity"
        build = ""
        detail = message
        target = re.search(r"target_build=([0-9]+)", message)
        active = re.search(r"active_build=([0-9]+)", message)
        if target:
            build = target.group(1)
        elif active:
            build = active.group(1)
        if message.startswith("rollback start"):
            action = "Switch started"
            detail = "Preparing install swap"
        elif message.startswith("rollback complete"):
            action = "Switch complete"
            detail = "Server start requested"
        elif message.startswith("rollback service start requested"):
            action = "Server start requested"
            detail = "Starting selected version"
        elif "latest auto-update resumed" in message:
            action = "Auto-update resumed"
            detail = "Tracking latest Steam build"
        elif message.startswith("manual recovery"):
            action = "Manual recovery"
        events.append({
            "time": match.group("time"),
            "action": action,
            "build": build,
            "version": "",
            "detail": detail,
        })
    return events


def version_history() -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    try:
        snapshots = [p for p in INSTALL_PARENT.iterdir() if p.is_dir() and is_snapshot_name(p.name)]
    except Exception:
        snapshots = []
    events.extend(snapshot_history_event(path) for path in snapshots)
    events.extend(rollback_log_history())
    return sorted(events, key=lambda item: item.get("time", ""), reverse=True)[:40]


def versions_state() -> dict[str, Any]:
    return {
        "pin": version_pin(),
        "steam": steam_latest_state(),
        "versions": list_saved_versions(),
        "history": version_history(),
        "logs": {
            "update": str(UPDATE_LOG),
            "rollback": str(ROLLBACK_LOG),
        },
    }


def process_rows() -> list[dict[str, Any]]:
    out = run(["ps", "-eo", "pid,pcpu,rss,args"], timeout=5)
    rows = []
    for line in out["stdout"].splitlines()[1:]:
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append({
                "pid": int(parts[0]),
                "cpu": float(parts[1]),
                "rss": int(parts[2]) * 1024,
                "args": parts[3],
            })
        except ValueError:
            continue
    return rows


def rows_for_process(*needles: str) -> list[dict[str, Any]]:
    return [
        row for row in process_rows()
        if any(needle in row["args"] for needle in needles)
    ]


def write_control_command(action: str) -> None:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(CONTROL_DIR / "command.json", {
        "action": action,
        "source": "panel",
        "timestamp": int(time.time()),
    })


def container_service_state(service: str) -> dict[str, Any]:
    if service == SERVICE_NAME:
        rows = rows_for_process("WindroseServer-Win64-Shipping.exe", "xvfb-run -a wine")
    else:
        rows = rows_for_process("windrose_plus_server.ps1")
    main_pid = rows[0]["pid"] if rows else 0
    memory = sum(row["rss"] for row in rows)
    return {
        "active_state": "active" if rows else "inactive",
        "sub_state": "running" if rows else "dead",
        "main_pid": main_pid,
        "memory_current": memory,
        "active_since": "",
        "restarts": 0,
    }


def create_install_snapshot(reason: str = "manual") -> dict[str, Any]:
    ensure_install_parent_safe()
    build = manifest_build(GAME_DIR) or "unknown"
    safe_reason = re.sub(r"[^a-z0-9-]+", "-", reason.lower()).strip("-") or "manual"
    target = INSTALL_PARENT / f"server-snapshot-{safe_reason}-{build}-{utc_stamp()}"
    if target.exists():
        raise ValueError("Snapshot target already exists")
    INSTALL_PARENT.mkdir(parents=True, exist_ok=True)
    append_rollback_log(f"snapshot start target={target}")
    out = run(["cp", "-a", str(GAME_DIR), str(target)], timeout=900)
    if not out["ok"]:
        append_rollback_log(f"snapshot failed target={target} error={out['stderr'] or out['stdout']}")
        raise RuntimeError(out["stderr"] or out["stdout"] or "Snapshot failed")
    owner = f"{os.getuid()}:{os.getgid()}" if is_container_mode() else "ubuntu:ubuntu"
    run(["chown", "-R", owner, str(target)], timeout=180)
    append_rollback_log(f"snapshot complete target={target} build={build}")
    return version_entry(target)


def copy_runtime_data(src_root: Path, dst_root: Path) -> None:
    for rel in RUNTIME_PATHS:
        src = src_root / rel
        dst = dst_root / rel
        if dst.exists() or dst.is_symlink():
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if not src.exists() and not src.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)


def clear_volatile_runtime(root: Path) -> None:
    data_dir = root / "windrose_plus_data"
    for rel in (
        "server_status.json",
        "rcon_status.json",
        "pending_commands.txt",
    ):
        try:
            (data_dir / rel).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            append_rollback_log(f"could not clear volatile file {data_dir / rel}: {exc}")
    rcon_dir = data_dir / "rcon"
    if rcon_dir.exists():
        for pattern in ("cmd_*.json", "res_*.json"):
            for path in rcon_dir.glob(pattern):
                try:
                    path.unlink()
                except OSError as exc:
                    append_rollback_log(f"could not clear volatile rcon file {path}: {exc}")


def empty_directory(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def wait_for_container_service(active: bool, timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = container_service_state(SERVICE_NAME)
        is_active = state["active_state"] == "active"
        if is_active == active:
            return True
        time.sleep(1)
    return False


def stop_service_for_swap() -> None:
    if is_container_mode():
        write_control_command("stop")
        if not wait_for_container_service(False, 120):
            raise RuntimeError("Timed out waiting for Windrose to stop")
        return

    stop = run(["systemctl", "stop", SERVICE_NAME], timeout=90)
    if not stop["ok"]:
        append_rollback_log(f"rollback stop failed error={stop['stderr'] or stop['stdout']}")
        raise RuntimeError(stop["stderr"] or stop["stdout"] or "Failed to stop Windrose")


def start_service_after_swap() -> None:
    if is_container_mode():
        write_control_command("start")
        return

    start = run(["systemctl", "start", "--no-block", SERVICE_NAME], timeout=20)
    if not start["ok"]:
        append_rollback_log(f"rollback start failed error={start['stderr'] or start['stdout']}")
        raise RuntimeError(start["stderr"] or start["stdout"] or "Rollback swapped files but service did not start")


def swap_install(stage: Path, current_snapshot: Path, selected_snapshot: Path) -> None:
    ensure_install_parent_safe()
    if is_container_mode():
        current_snapshot.mkdir(parents=True)
        copy_current = run(["cp", "-a", f"{GAME_DIR}/.", str(current_snapshot)], timeout=900)
        if not copy_current["ok"]:
            raise RuntimeError(copy_current["stderr"] or copy_current["stdout"] or "Failed to snapshot current install")
        empty_directory(GAME_DIR)
        copy_stage = run(["cp", "-a", f"{stage}/.", str(GAME_DIR)], timeout=900)
        if not copy_stage["ok"]:
            raise RuntimeError(copy_stage["stderr"] or copy_stage["stdout"] or "Failed to install selected version")
        run(["chown", "-R", f"{os.getuid()}:{os.getgid()}", str(GAME_DIR), str(current_snapshot), str(selected_snapshot)], timeout=240)
        return

    shutil.move(str(GAME_DIR), str(current_snapshot))
    shutil.move(str(stage), str(GAME_DIR))
    run(["chown", "-R", "ubuntu:ubuntu", str(GAME_DIR), str(current_snapshot), str(selected_snapshot)], timeout=240)


def rollback_to_snapshot(snapshot_id: str) -> dict[str, Any]:
    snapshot = resolve_snapshot(snapshot_id)
    target_build = manifest_build(snapshot)
    if not target_build:
        raise ValueError("Selected snapshot has no Steam build manifest")

    player_count = live_player_count()
    if player_count > 0:
        append_rollback_log(f"rollback requested with players_online={player_count}")

    backup = create_backup()
    if not backup.get("ok"):
        raise RuntimeError("Pre-rollback backup failed: " + str(backup.get("error") or "unknown error"))

    current_build = manifest_build(GAME_DIR) or "unknown"
    stamp = utc_stamp()
    current_snapshot = INSTALL_PARENT / f"server-before-rollback-{current_build}-{stamp}"
    stage = INSTALL_PARENT / f"server-rollback-stage-{target_build}-{stamp}"
    append_rollback_log(
        f"rollback start selected={snapshot} target_build={target_build} "
        f"current_build={current_build} save_backup={backup.get('path')}"
    )

    stop_service_for_swap()

    try:
        stage.mkdir(parents=True)
        copy = run(["cp", "-a", f"{snapshot}/.", str(stage)], timeout=900)
        if not copy["ok"]:
            raise RuntimeError(copy["stderr"] or copy["stdout"] or "Failed to stage rollback snapshot")
        copy_runtime_data(GAME_DIR, stage)
        clear_volatile_runtime(stage)
        swap_install(stage, current_snapshot, snapshot)
        write_version_pin(target_build, f"rollback to {snapshot.name}")
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    except Exception as exc:
        append_rollback_log(f"rollback swap failed error={exc}")
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise

    append_rollback_log(f"rollback service start requested build={target_build}")
    start_service_after_swap()

    append_rollback_log(f"rollback complete active_build={target_build} previous_saved={current_snapshot}")
    return {
        "ok": True,
        "build": target_build,
        "previous_snapshot": str(current_snapshot),
        "save_backup": backup.get("path"),
        "pin": version_pin(),
        "message": f"Switched to build {target_build}. Auto-update is pinned until you resume latest.",
    }


def service_state(service: str) -> dict[str, Any]:
    if is_container_mode():
        return container_service_state(service)

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
        "main_pid": safe_int(data.get("MainPID")),
        "memory_current": safe_int(data.get("MemoryCurrent")),
        "active_since": data.get("ActiveEnterTimestamp", ""),
        "restarts": safe_int(data.get("NRestarts")),
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
    rows = []
    total_cpu = 0.0
    total_rss = 0
    for row in process_rows():
        if "WindroseServer-Win64-Shipping.exe" not in row["args"] and "xvfb-run -a wine" not in row["args"]:
            continue
        total_cpu += row["cpu"]
        total_rss += row["rss"]
        rows.append(row)
    return {"cpu": round(total_cpu, 1), "rss": total_rss, "processes": rows}


def join_state(service: dict[str, Any]) -> dict[str, Any]:
    if service.get("active_state") != "active":
        return {"state": "offline", "joinable": False, "message": "Server process is not running"}

    log_path = GAME_DIR / "R5" / "Saved" / "Logs" / "R5.log"
    active_ts = parse_systemd_timestamp(str(service.get("active_since") or ""))
    try:
        log_mtime = log_path.stat().st_mtime
    except OSError:
        return {"state": "starting", "joinable": False, "message": "Waiting for game log"}
    if active_ts is not None and log_mtime + 5 < active_ts:
        return {"state": "starting", "joinable": False, "message": "Waiting for current boot log"}

    text = tail_file(log_path, 240_000)
    if READY_MARKER in text:
        return {"state": "ready", "joinable": True, "message": "Invite/direct join is ready"}

    for marker in BROKEN_REGISTRATION_MARKERS:
        if marker in text:
            return {"state": "registration_failed", "joinable": False, "message": marker}

    return {"state": "starting", "joinable": False, "message": "Waiting for Windrose host registration"}


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


def mod_layer_state(dashboard_state: dict[str, Any] | None = None) -> dict[str, Any]:
    win64 = GAME_DIR / "R5" / "Binaries" / "Win64"
    hook_paths = [
        win64 / "dwmapi.dll",
        win64 / "version.dll",
        win64 / "ue4ss",
    ]
    windrose_plus_hook = win64 / "ue4ss" / "Mods" / "WindrosePlus" / "enabled.txt"
    source_rcon_dir = win64 / "windrosercon"
    dashboard = dashboard_state or service_state(DASHBOARD_SERVICE)
    hook_installed = any(path.exists() for path in hook_paths)
    windrose_plus_installed = windrose_plus_hook.exists()
    source_rcon_installed = source_rcon_dir.exists()
    dashboard_running = dashboard.get("active_state") == "active"
    return {
        "mode": "modded" if hook_installed or dashboard_running or source_rcon_installed else "vanilla",
        "hook_installed": hook_installed,
        "windrose_plus_installed": windrose_plus_installed,
        "windrose_plus_dashboard_running": dashboard_running,
        "source_rcon_installed": source_rcon_installed,
        "live_players": bool(windrose_plus_installed and dashboard_running),
        "admin_actions": bool(source_rcon_installed),
        "console": bool(windrose_plus_installed or source_rcon_installed),
    }


def config_version(cfg: dict[str, Any]) -> str:
    deployment = str((cfg.get("raw") or {}).get("DeploymentId") or "")
    return deployment.split("-", 1)[0] if deployment else ""


def effective_server_status(status: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    cfg = server_config()
    server = status.get("server") if capabilities.get("live_players") else {}
    if not isinstance(server, dict):
        server = {}
    return {
        "name": server.get("name") or cfg.get("server_name") or "Windrose",
        "invite_code": server.get("invite_code") or cfg.get("invite_code") or "",
        "version": server.get("version") or cfg.get("deployment_version") or "",
        "windrose_plus": server.get("windrose_plus") if capabilities.get("live_players") else "",
        "player_count": safe_int(server.get("player_count"), 0) if capabilities.get("live_players") else None,
        "max_players": safe_int(server.get("max_players"), safe_int(cfg.get("max_players"))),
    }


def live_player_count() -> int:
    dashboard = service_state(DASHBOARD_SERVICE)
    capabilities = mod_layer_state(dashboard)
    if not capabilities.get("live_players"):
        return 0
    status = read_json(DATA_DIR / "server_status.json", {})
    return safe_int((status.get("server") or {}).get("player_count"))


def windrose_plus_command(command: str, args: list[str] | None = None, timeout: float = 18.0) -> dict[str, Any]:
    if not mod_layer_state().get("windrose_plus_installed"):
        return {"ok": False, "status": "error", "message": "Windrose+ is disabled; commands are unavailable in vanilla mode"}
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
    result = {
        "raw": cfg,
        "server_name": persistent.get("ServerName", ""),
        "invite_code": persistent.get("InviteCode", ""),
        "max_players": persistent.get("MaxPlayerCount", 0),
        "password_protected": bool(persistent.get("IsPasswordProtected", False)),
        "password": persistent.get("Password", ""),
        "region": persistent.get("UserSelectedRegion", ""),
        "use_direct_connection": bool(persistent.get("UseDirectConnection", False)),
    }
    result["deployment_version"] = config_version(result)
    return result


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
    ts = utc_stamp()
    target = BACKUP_DIR / f"windrose-panel-{ts}.tar.gz"
    desired_includes = [
        "R5/ServerDescription.json",
        "R5/Saved/SaveProfiles",
        "windrose_plus.json",
        "windrose_plus_data",
    ]
    includes = [rel for rel in desired_includes if (GAME_DIR / rel).exists()]
    if not includes:
        return {"ok": False, "error": "No backup paths were found"}
    cmd = ["tar", "-czf", str(target), "-C", str(GAME_DIR)] + includes
    out = run(cmd, timeout=180)
    if not out["ok"]:
        return {"ok": False, "error": out["stderr"] or out["stdout"]}
    return {"ok": True, "path": str(target), "size": target.stat().st_size}


def build_state() -> dict[str, Any]:
    windrose_service = service_state(SERVICE_NAME)
    dashboard_service = service_state(DASHBOARD_SERVICE)
    windrose_join = join_state(windrose_service)
    capabilities = mod_layer_state(dashboard_service)
    raw_status = read_json(DATA_DIR / "server_status.json", {})
    status = raw_status if capabilities.get("live_players") else {}
    server_status = effective_server_status(status, capabilities)
    livemap = read_json(DATA_DIR / "livemap_data.json", {}) if capabilities.get("live_players") else {}
    rcon_status = read_json(DATA_DIR / "rcon_status.json", {}) if capabilities.get("live_players") else {}
    source_status = source_rcon_status() if capabilities.get("admin_actions") else {
        "available": False,
        "reason": "vanilla_mode",
        "host": SOURCE_RCON_HOST,
        "port": SOURCE_RCON_PORT,
    }
    source_players: list[dict[str, str]] = []
    if source_status.get("available"):
        res = source_rcon_command("showplayers")
        if res.get("ok"):
            source_players = parse_source_players(res.get("message", ""))
    log_accounts = parse_log_accounts() if capabilities.get("admin_actions") else {}

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
            "windrose": windrose_service,
            "windrose_plus": dashboard_service,
        },
        "join": windrose_join,
        "capabilities": capabilities,
        "host": {
            "cpu_percent": cpu_percent(),
            "load": os.getloadavg() if hasattr(os, "getloadavg") else [0, 0, 0],
            "memory": mem_info(),
            "disk": disk_info(),
            "process": process_info(),
        },
        "windrose_plus": {
            "status": {"server": server_status, "raw": raw_status if capabilities.get("live_players") else {}},
            "rcon_status": rcon_status,
            "livemap_ready": bool(livemap and not livemap.get("error")),
        },
        "source_rcon": source_status,
        "known_accounts": list(log_accounts.values())[-20:],
        "server_config": server_config(),
        "versions": versions_state(),
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
    .notice { border: 1px solid #86643a; color: #ffe0a6; background: #1b1710; border-radius: 6px; padding: 10px 12px; margin-bottom: 12px; }
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
    .section-title { margin: 0 0 10px; font-size: 15px; line-height: 1.2; }
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
        <button data-tab="versions">Versions</button>
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
        <div class="notice" id="players-notice" style="display:none"></div>
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

      <section class="tab" id="tab-versions">
        <div class="grid">
          <div class="card">
            <div class="toolbar" style="margin-bottom:10px">
              <button class="button" id="refresh-versions">Refresh Versions</button>
              <button class="button" id="check-steam">Check Steam Latest</button>
              <button class="button" id="create-snapshot">Create Snapshot</button>
              <button class="button primary" id="resume-latest">Resume Latest Auto-update</button>
            </div>
            <div class="toolbar">
              <span class="pill" id="steam-latest-pill">Steam latest not checked</span>
              <span class="pill warn" id="version-pin-alert">Loading version pin...</span>
            </div>
          </div>
          <div class="card">
            <h2 class="section-title">Saved Versions</h2>
            <table>
              <thead><tr><th>Status</th><th>Steam Build</th><th>Game Version</th><th>Saved</th><th>Size</th><th>Action</th></tr></thead>
              <tbody id="versions-body"></tbody>
            </table>
          </div>
          <div class="card">
            <div class="toolbar" style="margin-bottom:10px">
              <h2 class="section-title" style="margin-right:auto">Activity History</h2>
              <button class="button" id="refresh-version-logs">Refresh Raw Logs</button>
            </div>
            <table>
              <thead><tr><th>Time</th><th>Action</th><th>Steam Build</th><th>Game Version</th><th>Detail</th></tr></thead>
              <tbody id="version-history-body"></tbody>
            </table>
            <pre id="version-logs-output" style="display:none; margin-top:12px"></pre>
          </div>
        </div>
      </section>

      <section class="tab" id="tab-console">
        <div class="notice" id="console-notice" style="display:none"></div>
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
    let serviceActionPending = false;
    const $ = (sel) => document.querySelector(sel);
    const fmtBytes = (n) => {
      if (!n) return "0 B";
      const units = ["B","KB","MB","GB","TB"];
      let i = 0, v = n;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
      return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
    };
    const fmtDate = (value) => {
      if (!value) return "-";
      const text = String(value).trim();
      const date = new Date(text);
      if (Number.isNaN(date.getTime())) return text;
      return new Intl.DateTimeFormat(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit"
      }).format(date);
    };
    const api = async (url, opts = {}) => {
      const res = await fetch(url, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || data.message || res.statusText);
      return data;
    };
    function setText(id, value) { $(id).textContent = value ?? "-"; }
    function servicePill(active) { return active === "active" ? "Online" : active || "Unknown"; }
    function joinLabel(join, service) {
      if (service.active_state !== "active") return servicePill(service.active_state);
      if (join?.state === "ready") return "Joinable";
      if (join?.state === "registration_failed") return "Registration failed";
      if (join?.state === "starting") return "Starting";
      return servicePill(service.active_state);
    }
    function joinHint(join, service) {
      if (join?.state === "ready") return fmtDate(service.active_since);
      return join?.message || fmtDate(service.active_since);
    }
    function serviceActionDisabled(action, service) {
      const active = service.active_state === "active";
      const changing = ["activating", "deactivating", "reloading"].includes(service.active_state);
      if (serviceActionPending || changing) return true;
      if (action === "start") return active;
      if (action === "stop") return !active;
      if (action === "restart") return false;
      return false;
    }
    function updateServiceButtons(service) {
      document.querySelectorAll("[data-service]").forEach(btn => {
        const action = btn.dataset.service;
        const disabled = serviceActionDisabled(action, service);
        btn.disabled = disabled;
        if (disabled) {
          if (serviceActionPending) btn.title = "Another service action is still pending";
          else if (action === "start") btn.title = "Server is already running";
          else btn.title = "Server is not running";
        } else {
          btn.removeAttribute("title");
        }
      });
    }
    function livePlayerCountForPrompt() {
      if (!state?.capabilities?.live_players) return null;
      return Number(state?.windrose_plus?.status?.server?.player_count ?? state?.players?.length ?? 0);
    }
    function renderVersions(next) {
      const versions = next.versions || {};
      const pin = versions.pin || {};
      const steam = versions.steam || {};
      const rows = versions.versions || [];
      const history = versions.history || [];
      const live = rows.find(v => v.live) || rows[0] || {};
      const body = $("#versions-body");
      body.innerHTML = "";
      $("#resume-latest").disabled = pin.auto_update !== false;
      const steamEl = $("#steam-latest-pill");
      if (steam.error) {
        steamEl.className = "pill warn";
        steamEl.textContent = `Steam latest check failed`;
      } else if (steam.latest_build) {
        const checked = steam.checked_at ? ` · checked ${fmtDate(steam.checked_at)}` : "";
        steamEl.className = steam.latest_build === live.build ? "pill ok" : "pill warn";
        steamEl.textContent = `Steam latest ${steam.latest_build}${checked}`;
      } else {
        steamEl.className = "pill";
        steamEl.textContent = "Steam latest not checked";
      }
      const pinEl = $("#version-pin-alert");
      if (pin.auto_update === false) {
        pinEl.className = "pill warn";
        pinEl.textContent = `Pinned to ${pin.target_build}. Latest auto-update is paused.`;
      } else {
        pinEl.className = "pill ok";
        pinEl.textContent = "Tracking latest Steam build automatically.";
      }
      if (!rows.length) {
        body.innerHTML = `<tr><td colspan="6" class="mini">No versions found</td></tr>`;
        return;
      }
      for (const item of rows) {
        const tr = document.createElement("tr");
        const stateLabel = item.live ? "Current" : "Saved locally";
        const action = item.live ? `<span class="mini">Running now</span>` : `<button class="button warn">Switch</button>`;
        tr.innerHTML = `<td>${escapeHtml(stateLabel)}</td><td>${escapeHtml(item.build || "-")}</td><td>${escapeHtml(item.version || "-")}</td><td class="mini" title="${escapeHtml(item.created || "")}">${escapeHtml(fmtDate(item.created))}</td><td>${fmtBytes(item.size || 0)}</td><td>${action}</td>`;
        const btn = tr.querySelector("button");
        if (btn) {
          btn.onclick = () => rollbackVersion(item, live);
        }
        body.appendChild(tr);
      }
      const historyBody = $("#version-history-body");
      historyBody.innerHTML = "";
      if (!history.length) {
        historyBody.innerHTML = `<tr><td colspan="5" class="mini">No version activity yet</td></tr>`;
      } else {
        for (const item of history) {
          const tr = document.createElement("tr");
          tr.innerHTML = `<td class="mini" title="${escapeHtml(item.time || "")}">${escapeHtml(fmtDate(item.time))}</td><td>${escapeHtml(item.action || "-")}</td><td>${escapeHtml(item.build || "-")}</td><td>${escapeHtml(item.version || "-")}</td><td class="mini">${escapeHtml(item.detail || "-")}</td>`;
          historyBody.appendChild(tr);
        }
      }
    }
    function render(next) {
      state = next;
      const s = next.windrose_plus.status.server || {};
      const liveVersion = ((next.versions || {}).versions || [])[0] || {};
      const service = next.services.windrose || {};
      const mem = next.host.memory || {};
      const disk = next.host.disk || {};
      const proc = next.host.process || {};
      const cfg = next.server_config || {};
      const caps = next.capabilities || {};
      const join = next.join || {};
      $("#server-name").textContent = s.name || cfg.server_name || "Windrose";
      const modeText = caps.mode === "vanilla" ? "Vanilla mode" : `Windrose+ ${s.windrose_plus || "enabled"}`;
      $("#server-line").textContent = `Invite ${s.invite_code || cfg.invite_code || "-"} · Version ${s.version || cfg.deployment_version || "-"} · Steam build ${liveVersion.build || "-"} · ${modeText} · ${joinLabel(join, service)}`;
      setText("#stat-service", joinLabel(join, service));
      setText("#stat-uptime", joinHint(join, service));
      setText("#stat-players", caps.live_players ? `${s.player_count ?? 0}/${s.max_players ?? cfg.max_players ?? 0}` : `?/${s.max_players ?? cfg.max_players ?? 0}`);
      setText("#stat-invite", caps.live_players ? `Invite ${s.invite_code || cfg.invite_code || "-"}` : `Invite ${s.invite_code || cfg.invite_code || "-"} · live count unavailable`);
      setText("#stat-cpu", `${next.host.cpu_percent || 0}%`);
      setText("#stat-load", `Load ${(next.host.load || []).map(x => Number(x).toFixed(2)).join(" ")}`);
      setText("#stat-memory", `${mem.percent || 0}%`);
      setText("#stat-process", `Process ${fmtBytes(proc.rss || service.memory_current || 0)}`);
      setText("#stat-disk", `${disk.percent || 0}%`);
      setText("#stat-backups", fmtBytes(disk.free || 0) + " free");
      updateServiceButtons(service);
      const rcon = next.source_rcon || {};
      $("#rcon-pill").className = `pill ${rcon.available ? "ok" : (caps.mode === "vanilla" ? "" : "warn")}`;
      $("#rcon-pill").textContent = rcon.available ? "Admin commands ready" : (caps.mode === "vanilla" ? "Vanilla mode" : "Kick/ban offline");
      const playersNotice = $("#players-notice");
      playersNotice.style.display = caps.live_players ? "none" : "block";
      playersNotice.textContent = "Live player list and kick/ban are unavailable while UE4SS, Windrose+, and WindroseRCON are disabled for stability.";
      const consoleNotice = $("#console-notice");
      consoleNotice.style.display = caps.console ? "none" : "block";
      consoleNotice.textContent = "Console commands are unavailable in vanilla mode. Service controls, config, backups, versions, and logs still work.";
      $("#console-command").disabled = !caps.console;
      $("#run-command").disabled = !caps.console;
      if (!caps.console) $("#console-output").textContent = "Vanilla mode: no Windrose+ or WindroseRCON command backend is loaded.";
      const body = $("#players-body");
      body.innerHTML = "";
      const players = next.players || [];
      if (!caps.live_players) {
        body.innerHTML = `<tr><td colspan="5" class="mini">Live player data unavailable in vanilla mode</td></tr>`;
      } else if (!players.length) {
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
      renderVersions(next);
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
    async function loadVersionLogs() {
      try {
        const data = await api("/api/version-logs");
        $("#version-logs-output").textContent = data.logs || "";
        $("#version-logs-output").style.display = $("#version-logs-output").style.display === "none" ? "block" : "none";
      } catch (e) { $("#version-logs-output").textContent = e.message; }
    }
    async function refreshVersions() {
      await refresh();
    }
    async function createSnapshot() {
      $("#action-result").textContent = "Creating version snapshot...";
      try {
        const data = await api("/api/version-snapshot", { method: "POST", body: "{}" });
        $("#action-result").textContent = `Snapshot created for build ${data.build || "-"} (${fmtBytes(data.size || 0)})`;
        await refreshVersions();
      } catch (e) { $("#action-result").textContent = e.message; }
    }
    async function checkSteamLatest() {
      $("#action-result").textContent = "Checking Steam latest build...";
      try {
        const data = await api("/api/check-steam-latest", { method: "POST", body: "{}" });
        $("#action-result").textContent = data.error ? `Steam check failed: ${data.error}` : `Steam latest build ${data.latest_build || "-"}`;
        await refresh();
      } catch (e) { $("#action-result").textContent = e.message; }
    }
    async function resumeLatest() {
      if (!confirm("Resume latest auto-update? The next idle update check may restart the server if Steam has a newer build.")) return;
      try {
        const data = await api("/api/resume-latest", { method: "POST", body: "{}" });
        $("#action-result").textContent = data.message || "Latest auto-update resumed";
        await refreshVersions();
      } catch (e) { $("#action-result").textContent = e.message; }
    }
    async function rollbackVersion(item, live) {
      const count = livePlayerCountForPrompt();
      let confirmPlayers = false;
      const label = `${item.version || "unknown version"} / build ${item.build || "-"}`;
      if (count === null) {
        if (!confirm(`Switch to ${label}? Live player count is unavailable in vanilla mode, so this may disconnect anyone currently online.`)) return;
      } else if (count > 0) {
        const typed = prompt(`${count} player${count === 1 ? "" : "s"} will be disconnected. Type ROLLBACK to switch to ${label}.`);
        if (typed !== "ROLLBACK") return;
        confirmPlayers = true;
      } else if (!confirm(`Roll back to ${label}? The server will stop, swap versions, then start again.`)) {
        return;
      }
      $("#action-result").textContent = `Switching to build ${item.build || "-"}...`;
      try {
        const data = await api("/api/rollback", { method: "POST", body: JSON.stringify({ snapshot_id: item.id, confirm_players: confirmPlayers }) });
        $("#action-result").textContent = data.message || "Version switch complete";
        await refreshVersions();
      } catch (e) { $("#action-result").textContent = e.message; }
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
        if (btn.disabled) return;
        const action = btn.dataset.service;
        if (action === "stop" || action === "restart") {
          const count = livePlayerCountForPrompt();
          if (count === null) {
            if (!confirm(`${action} Windrose server? Live player count is unavailable in vanilla mode, so this may disconnect anyone currently online.`)) return;
          } else if (count > 0) {
            const typed = prompt(`${count} player${count === 1 ? "" : "s"} will be kicked. Type ${action.toUpperCase()} to ${action} the server.`);
            if (typed !== action.toUpperCase()) return;
          } else if (!confirm(`${action} Windrose server?`)) {
            return;
          }
        }
        serviceActionPending = true;
        if (state?.services?.windrose) updateServiceButtons(state.services.windrose);
        $("#action-result").textContent = `${action} requested...`;
        try {
          const data = await api("/api/service", { method: "POST", body: JSON.stringify({ action }) });
          $("#action-result").textContent = data.message || "Done";
          setTimeout(async () => {
            serviceActionPending = false;
            await refresh();
          }, 1500);
        } catch (e) {
          serviceActionPending = false;
          $("#action-result").textContent = e.message;
          if (state?.services?.windrose) updateServiceButtons(state.services.windrose);
        }
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
    $("#refresh-versions").onclick = refreshVersions;
    $("#refresh-version-logs").onclick = loadVersionLogs;
    $("#check-steam").onclick = checkSteamLatest;
    $("#create-snapshot").onclick = createSnapshot;
    $("#resume-latest").onclick = resumeLatest;
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

    def audit(self, message: str) -> None:
        print(f"{self.address_string()} - {message}", flush=True)

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
            if path == "/api/versions":
                if not self.require_auth():
                    return
                self.send_json(versions_state())
                return
            if path == "/api/version-logs":
                if not self.require_auth():
                    return
                logs = (
                    "--- Update log ---\n"
                    + tail_file(UPDATE_LOG, 16000)
                    + "\n\n--- Rollback log ---\n"
                    + tail_file(ROLLBACK_LOG, 16000)
                )
                self.send_json({"logs": logs.strip()})
                return
            if path == "/api/logs":
                if not self.require_auth():
                    return
                journal_text = ""
                if is_container_mode():
                    journal_text = (
                        "--- Panel log ---\n"
                        + tail_file(CONTROL_DIR / "panel.log", 9000)
                        + "\n\n--- Windrose+ dashboard log ---\n"
                        + tail_file(DATA_DIR / "dashboard.log", 9000)
                    )
                else:
                    journal = run(["journalctl", "-u", SERVICE_NAME, "-n", "180", "--no-pager"], timeout=8)
                    journal_text = journal["stdout"]
                game_log_dir = GAME_DIR / "R5" / "Saved" / "Logs"
                latest_log = ""
                try:
                    logs = sorted(game_log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
                    latest_log = tail_file(logs[0], 9000) if logs else ""
                except Exception:
                    latest_log = ""
                self.send_json({"logs": (journal_text + "\n\n--- Game log ---\n" + latest_log).strip()})
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
                current = service_state(SERVICE_NAME)
                active = current.get("active_state") == "active"
                player_count = live_player_count()
                self.audit(
                    f"service action requested action={action} "
                    f"state={current.get('active_state')} players={player_count}"
                )
                if action == "start" and active:
                    self.send_json({"ok": True, "message": "Server is already running", "state": current})
                    return
                if action == "stop" and not active:
                    self.send_json({"error": "Server is not running", "state": current}, 409)
                    return
                if is_container_mode():
                    write_control_command(action)
                    self.send_json({"ok": True, "message": f"{action} requested", "state": current})
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
            if path == "/api/version-snapshot":
                result = create_install_snapshot("manual")
                self.send_json({"ok": True, **result})
                return
            if path == "/api/check-steam-latest":
                result = check_steam_latest()
                self.send_json(result, 200 if not result.get("error") else 502)
                return
            if path == "/api/resume-latest":
                pin = clear_version_pin()
                append_rollback_log("latest auto-update resumed from panel")
                self.send_json({"ok": True, "pin": pin, "message": "Latest auto-update resumed"})
                return
            if path == "/api/rollback":
                snapshot_id = str(body.get("snapshot_id", ""))
                player_count = live_player_count()
                if player_count > 0 and not body.get("confirm_players"):
                    self.send_json({"error": f"{player_count} players are online; confirmation is required"}, 409)
                    return
                result = rollback_to_snapshot(snapshot_id)
                self.send_json(result)
                return
            if path == "/api/rcon":
                command = str(body.get("command", "")).strip()
                if not command:
                    self.send_json({"error": "Command is required"}, 400)
                    return
                if command.lower().startswith("wp."):
                    result = windrose_plus_command(command)
                    self.send_json(result, 200 if result.get("ok") else 503)
                else:
                    if not mod_layer_state().get("admin_actions"):
                        self.send_json({"ok": False, "error": "WindroseRCON is disabled; commands are unavailable in vanilla mode"}, 503)
                        return
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
                if not mod_layer_state().get("admin_actions"):
                    self.send_json({"error": "Kick/ban are unavailable in vanilla mode because WindroseRCON is disabled"}, 503)
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
