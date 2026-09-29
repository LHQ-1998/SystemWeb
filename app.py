#!/usr/bin/env python3
"""自建应用卡片。

参考 https://github.com/1999AZZAR/Systemd-Service-Manager-Web-UI
只管理 apps.json 白名单里的应用，提供状态、启动、停止、重启和日志。
"""

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
APPS_FILE = ROOT / "apps.json"
INDEX_FILE = ROOT / "static" / "index.html"
AUTH_FILE = ROOT / "auth.json"
SERVICE_DIR = ROOT / "services"
ICON_DIR = ROOT / "static" / "icons"
SESSIONS = {}
SESSION_TTL = 12 * 3600
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.@\\-]{0,180}\.service$")
CREATE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,40}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_LAST_CPU = None
_LAST_NET = None
_LAST_PROC = None
HISTORY = []
PROCESS_HINTS = (
    ("/opt/MX_WorkSpace", "MX看板"),
    ("/opt/TH-server", "温湿度服务"),
    ("/root/zerotier_controller", "ZeroTier 控制台"),
    ("zerotier-one", "ZeroTier"),
    ("/opt/system-mgn", "设备管理"),
    ("/opt/codelog", "CodeLog"),
    ("/opt/app-cards", "服务面板"),
    ("/usr/local/bin/frps", "FRP 服务端"),
    ("site_total", "站点统计"),
    ("mystic", "Mystic 接口"),
    ("emqx", "EMQX"),
    ("nginx", "Nginx"),
    ("cursor-server", "Cursor"),
    ("aegis", "阿里云盾"),
    ("/opt/1panel", "1Panel"),
    ("mysqld", "MySQL"),
)
PM2 = "/root/.nvm/versions/node/v18.20.8/bin/pm2"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5071"))
ACTIONS = {"start", "stop", "restart"}

PM2_ENV = os.environ.copy()
PM2_ENV["PATH"] = "/root/.nvm/versions/node/v18.20.8/bin:" + PM2_ENV.get("PATH", "")
PM2_ENV["PM2_HOME"] = "/root/.pm2"


def load_apps():
    data = json.loads(APPS_FILE.read_text(encoding="utf-8"))
    apps = data.get("apps") or []
    return {app["id"]: app for app in apps}


def run(cmd, timeout=20, env=None):
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def format_memory(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return ""
    if value < 0:
        return ""
    if value < 1024 * 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value / 1024 / 1024:.1f} MB"


def map_systemd_state(active, sub):
    if active == "failed" or sub == "failed":
        return "failed"
    if active == "active" and sub in ("running", "exited"):
        return "running" if sub == "running" else "exited"
    if active == "activating":
        return "starting"
    if active == "deactivating":
        return "stopping"
    if active == "inactive":
        return "stopped"
    return sub or active or "unknown"


def systemd_status(unit):
    code, out, err = run(
        [
            "systemctl",
            "show",
            unit,
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "MainPID",
            "-p",
            "UnitFileState",
            "-p",
            "ActiveEnterTimestamp",
            "-p",
            "NRestarts",
            "-p",
            "MemoryCurrent",
        ]
    )
    if code != 0:
        return {"state": "unknown", "detail": (err or out).strip()}
    info = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            info[key] = value
    active = info.get("ActiveState", "unknown")
    sub = info.get("SubState", "")
    pid = int(info.get("MainPID") or 0)
    return {
        "state": map_systemd_state(active, sub),
        "active": active,
        "sub": sub,
        "pid": pid,
        "enabled": info.get("UnitFileState", ""),
        "since": info.get("ActiveEnterTimestamp", ""),
        "restarts": info.get("NRestarts", ""),
        "memory": format_memory(info.get("MemoryCurrent")),
    }


def pm2_snapshot():
    code, out, err = run([PM2, "jlist"], timeout=20, env=PM2_ENV)
    if code != 0:
        raise RuntimeError((err or out).strip() or "pm2 jlist 失败")
    items = json.loads(out or "[]")
    snapshot = {}
    for item in items:
        env = item.get("pm2_env") or {}
        monit = item.get("monit") or {}
        status = env.get("status") or "unknown"
        state = {
            "online": "running",
            "stopped": "stopped",
            "stopping": "stopping",
            "launching": "starting",
            "errored": "failed",
            "waiting restart": "starting",
        }.get(status, status)
        uptime = env.get("pm_uptime") or 0
        since = ""
        if status == "online" and uptime:
            since = datetime.fromtimestamp(uptime / 1000).strftime("%Y-%m-%d %H:%M:%S")
        snapshot[item.get("name")] = {
            "state": state,
            "active": status,
            "sub": status,
            "pid": item.get("pid") or 0,
            "enabled": "pm2",
            "since": since,
            "restarts": str(env.get("restart_time", "")),
            "memory": format_memory(monit.get("memory")),
            "out_log": env.get("pm_out_log_path") or "",
            "err_log": env.get("pm_err_log_path") or "",
        }
    return snapshot


def tail_file(path, lines):
    file_path = Path(path)
    if not path or not file_path.is_file():
        return ""
    size = file_path.stat().st_size
    with file_path.open("rb") as handle:
        handle.seek(max(0, size - 262144))
        text = handle.read().decode("utf-8", "replace")
    return "\n".join(text.splitlines()[-lines:])


def app_view(app, status):
    return {
        "id": app["id"],
        "name": app["name"],
        "description": app.get("description") or "",
        "kind": app["kind"],
        "target": app.get("unit") or app.get("pm2") or "",
        "port": app.get("port"),
        "state": status.get("state") or "unknown",
        "active": status.get("active") or "",
        "sub": status.get("sub") or "",
        "pid": status.get("pid") or 0,
        "enabled": status.get("enabled") or "",
        "since": status.get("since") or "",
        "restarts": status.get("restarts") or "",
        "memory": status.get("memory") or "",
        "links": app.get("links") or [],
        "note": app.get("note") or "",
        "page": app.get("page") or "",
        "icon": app.get("icon") or "",
    }


def save_apps(apps):
    APPS_FILE.write_text(
        json.dumps({"apps": list(apps.values())}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return salt, digest.hex()


def ensure_auth():
    SERVICE_DIR.mkdir(exist_ok=True)
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    if AUTH_FILE.exists():
        return
    password = secrets.token_urlsafe(9)
    salt, digest = hash_password(password)
    AUTH_FILE.write_text(json.dumps({"salt": salt, "hash": digest}), encoding="utf-8")
    AUTH_FILE.chmod(0o600)
    hint = ROOT / "initial-password"
    hint.write_text(password + "\n", encoding="utf-8")
    hint.chmod(0o600)
    print(f"config password saved to {hint}")


def check_password(password):
    data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    _, digest = hash_password(password, data["salt"])
    return secrets.compare_digest(digest, data["hash"])


def set_password(password):
    salt, digest = hash_password(password)
    AUTH_FILE.write_text(json.dumps({"salt": salt, "hash": digest}), encoding="utf-8")
    AUTH_FILE.chmod(0o600)


def new_session():
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = time.time() + SESSION_TTL
    return token


def session_token(header):
    for part in (header or "").split(";"):
        part = part.strip()
        if part.startswith("panel_session="):
            return part.split("=", 1)[1].strip()
    return ""


def session_ok(header):
    token = session_token(header)
    expiry = SESSIONS.get(token)
    if not expiry or expiry < time.time():
        SESSIONS.pop(token, None)
        return False
    return True


def validate_unit(unit):
    if not UNIT_RE.match(unit or ""):
        raise RuntimeError("服务名不合法")
    return unit


def unit_id(unit):
    return unit[: -len(".service")].replace("@", "-").replace(".", "-")


def read_show(unit):
    keys = [
        "Id", "Description", "FragmentPath", "DropInPaths", "LoadState",
        "ActiveState", "SubState", "UnitFileState", "ExecStart", "ExecStop",
        "WorkingDirectory", "User", "Group", "EnvironmentFiles", "Restart",
        "RestartUSec", "After", "Requires", "WantedBy",
    ]
    code, out, err = run(["systemctl", "show", unit, "-p", ",".join(keys)])
    if code != 0:
        raise RuntimeError((err or out).strip() or "读取服务失败")
    info = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            info[key] = value
    return info


def unit_preview(unit):
    unit = validate_unit(unit)
    code, cat, err = run(["systemctl", "cat", unit], timeout=15)
    if code != 0:
        raise RuntimeError((err or cat).strip() or "读不到单元文件")
    return {
        "unit": unit,
        "cat": cat,
        "properties": read_show(unit),
        "savedAt": datetime.now().isoformat(timespec="seconds"),
    }


def unit_snapshot(unit):
    snapshot = unit_preview(unit)
    SERVICE_DIR.mkdir(exist_ok=True)
    (SERVICE_DIR / f"{unit_id(unit)}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return snapshot


def load_snapshot(unit):
    path = SERVICE_DIR / f"{unit_id(unit)}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_units(query):
    query = (query or "").strip().lower()
    if not query:
        rows = []
        for path in sorted(Path("/etc/systemd/system").glob("*.service")):
            rows.append({"unit": path.name, "file": str(path), "state": "file"})
        return rows
    code, out, err = run(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"])
    if code != 0:
        raise RuntimeError((err or out).strip() or "列出服务失败")
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].endswith(".service"):
            continue
        if query not in parts[0].lower():
            continue
        rows.append({"unit": parts[0], "file": "", "state": parts[1]})
        if len(rows) >= 60:
            break
    return rows


def import_unit(unit):
    snapshot = unit_snapshot(unit)
    apps = load_apps()
    existing = next((app for app in apps.values() if app.get("unit") == unit), None)
    description = (snapshot["properties"].get("Description") or unit).strip()
    if existing:
        existing["description"] = existing.get("description") or description
        save_apps(apps)
        return existing, snapshot, False
    app_id = unit_id(unit)
    base = app_id
    n = 2
    while app_id in apps:
        app_id = f"{base}-{n}"
        n += 1
    app = {
        "id": app_id,
        "name": description[:40] or app_id,
        "description": description,
        "page": snapshot["properties"].get("FragmentPath") or "",
        "kind": "systemd",
        "unit": unit,
        "port": None,
        "links": [],
        "note": "从本机 systemd 导入",
    }
    apps[app_id] = app
    save_apps(apps)
    return app, snapshot, True


def create_unit(body):
    name = (body.get("name") or "").strip()
    if not CREATE_RE.match(name) or name.startswith("systemd"):
        raise RuntimeError("服务名只允许字母、数字和连字符")
    if name in {"ssh", "sshd", "app-cards"}:
        raise RuntimeError("不能覆盖系统或面板自身的服务")
    unit = name + ".service"
    path = Path("/etc/systemd/system") / unit
    if path.exists():
        raise RuntimeError("这个服务已经存在")
    command = (body.get("command") or "").strip()
    if not command or "\n" in command or "\x00" in command:
        raise RuntimeError("启动命令不能为空，也不能换行")
    workdir = (body.get("workdir") or "").strip()
    if workdir and (not workdir.startswith("/") or ".." in workdir.split("/")):
        raise RuntimeError("工作目录必须是绝对路径")
    user = (body.get("user") or "").strip()
    if user and not USER_RE.match(user):
        raise RuntimeError("运行用户不合法")
    description = (body.get("description") or name).replace("\n", " ").strip()
    restart = "on-failure" if body.get("restart", True) else "no"
    lines = [
        "# managed-by: app-cards",
        "[Unit]",
        f"Description={description}",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
    ]
    if workdir:
        lines.append(f"WorkingDirectory={workdir}")
    if user:
        lines.append(f"User={user}")
    lines.append(f"ExecStart={command}")
    lines.append(f"Restart={restart}")
    if restart != "no":
        lines.append("RestartSec=3")
    lines += ["", "[Install]", "WantedBy=multi-user.target", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    code, out, err = run(["systemctl", "daemon-reload"], timeout=30)
    if code != 0:
        path.unlink(missing_ok=True)
        raise RuntimeError((err or out).strip() or "daemon-reload 失败")
    if body.get("enable"):
        run(["systemctl", "enable", unit], timeout=20)
    app, snapshot, _created = import_unit(unit)
    return app, snapshot


def update_meta(app_id, body):
    apps = load_apps()
    app = apps.get(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    for key in ("name", "description", "page", "note"):
        if key in body:
            value = str(body.get(key) or "").replace("\r", "")
            if "\n" in value and key != "note":
                raise RuntimeError("文本字段不能换行")
            app[key] = value.strip()
    if "port" in body:
        raw = body.get("port")
        app["port"] = int(raw) if str(raw or "").strip() else None
    if "links" in body:
        links = []
        for item in body.get("links") or []:
            label = str(item.get("label") or "").strip()
            href = str(item.get("href") or "").strip()
            if label and href and "\n" not in href:
                links.append({"label": label[:40], "href": href[:300]})
        app["links"] = links
    save_apps(apps)
    return app


def icon_ext(raw):
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    sample = raw[:8192]
    if sample.startswith(b"\xff\xfe") or sample.startswith(b"\xfe\xff"):
        text = sample.decode("utf-16", "ignore")
    else:
        text = sample.decode("utf-8", "ignore")
    if "<svg" in text.lower():
        return ".svg"
    return ""


def decode_icon(content):
    content = str(content or "").strip()
    if content.startswith("data:"):
        content = content.split(",", 1)[-1]
    try:
        raw = base64.b64decode(content, validate=False)
    except Exception:
        raw = b""
    if icon_ext(raw):
        return raw
    from urllib.parse import unquote
    text = unquote(content)
    raw = text.encode("utf-8")
    return raw


def save_icon(app_id, body):
    apps = load_apps()
    app = apps.get(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    raw = decode_icon(body.get("content") or "")
    if not raw or len(raw) > 2 * 1024 * 1024:
        raise RuntimeError("图标需要是小于 2MB 的 PNG 或 SVG")
    ext = icon_ext(raw)
    if not ext:
        raise RuntimeError("只支持 PNG 或 SVG")
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for old in ICON_DIR.glob(app_id + ".*"):
        old.unlink()
    (ICON_DIR / f"{app_id}{ext}").write_bytes(raw)
    app["icon"] = f"/static/icons/{app_id}{ext}"
    save_apps(apps)
    return app


def remove_card(app_id):
    apps = load_apps()
    app = apps.pop(app_id, None)
    if not app:
        raise RuntimeError("应用不存在")
    save_apps(apps)
    if app.get("unit"):
        snap = SERVICE_DIR / f"{unit_id(app['unit'])}.json"
        snap.unlink(missing_ok=True)
    return app


def meminfo():
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        info[key] = int(value.strip().split()[0]) * 1024
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    return {
        "total": total,
        "used": max(0, total - available),
        "available": available,
        "swapTotal": swap_total,
        "swapUsed": max(0, swap_total - swap_free),
    }


def cpu_percent():
    global _LAST_CPU
    parts = [int(item) for item in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    total = sum(parts)
    percent = None
    if _LAST_CPU and total > _LAST_CPU[1]:
        percent = round((1 - (idle - _LAST_CPU[0]) / (total - _LAST_CPU[1])) * 100, 1)
    _LAST_CPU = (idle, total)
    return percent


def net_rates():
    global _LAST_NET
    now = time.time()
    current = {}
    for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
        name, rest = line.split(":", 1)
        name = name.strip()
        if name == "lo":
            continue
        cols = rest.split()
        current[name] = (int(cols[0]), int(cols[8]))
    rates = []
    for name, (rx, tx) in current.items():
        item = {"name": name, "rx": rx, "tx": tx, "rxRate": None, "txRate": None}
        if _LAST_NET:
            prev_time, prev = _LAST_NET
            gap = now - prev_time
            if gap > 0 and name in prev:
                item["rxRate"] = (rx - prev[name][0]) / gap
                item["txRate"] = (tx - prev[name][1]) / gap
        rates.append(item)
    _LAST_NET = (now, current)
    return rates


DISK_TOP = {}
DISK_TOP_AT = 0.0
DISK_TOP_RUNNING = False
DISK_TOP_LOCK = threading.Lock()


def disk_entry_name(path):
    path = path.rstrip("/") or "/"
    parent, _, base = path.rpartition("/")
    if parent in ("", "/"):
        return "/" + base
    return base


def scan_disk_top(path, limit=3):
    code, out, _err = run(["du", "-x", "-a", "-B1", "-d", "1", path], timeout=90)
    if code not in (0, 1):
        return []
    root = str(Path(path))
    skip = {"lost+found", ".Recycle_bin"}
    rows = []
    for line in out.splitlines():
        bits = line.split(maxsplit=1)
        if len(bits) != 2:
            continue
        try:
            size = int(bits[0])
        except ValueError:
            continue
        name = bits[1]
        if name.rstrip("/") == root.rstrip("/") or size <= 0:
            continue
        label = disk_entry_name(name)
        if label in skip or label.startswith("."):
            continue
        rows.append({"name": label, "bytes": size})
    rows.sort(key=lambda item: item["bytes"], reverse=True)
    return rows[:limit]


def refresh_disk_top():
    global DISK_TOP, DISK_TOP_AT, DISK_TOP_RUNNING
    found = {}
    try:
        for target in ("/", "/data"):
            if target != "/" and not os.path.ismount(target):
                continue
            try:
                found[target] = scan_disk_top(target)
            except (OSError, subprocess.TimeoutExpired):
                found[target] = []
        with DISK_TOP_LOCK:
            if found:
                DISK_TOP = found
                DISK_TOP_AT = time.time()
    finally:
        with DISK_TOP_LOCK:
            DISK_TOP_RUNNING = False


def disk_top_cached():
    global DISK_TOP_RUNNING
    with DISK_TOP_LOCK:
        fresh = DISK_TOP and time.time() - DISK_TOP_AT < 600
        if fresh or DISK_TOP_RUNNING:
            return dict(DISK_TOP)
        DISK_TOP_RUNNING = True
        snapshot = dict(DISK_TOP)
    threading.Thread(target=refresh_disk_top, daemon=True).start()
    return snapshot


def disks():
    tops = disk_top_cached()
    rows = []
    seen = set()
    for line in Path("/proc/mounts").read_text().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        source, target, fstype = parts[0], parts[1], parts[2]
        if fstype not in {"ext4", "xfs", "btrfs", "vfat"} or target in seen:
            continue
        seen.add(target)
        try:
            stat = os.statvfs(target)
        except OSError:
            continue
        total = stat.f_blocks * stat.f_frsize
        used = total - stat.f_bfree * stat.f_frsize
        rows.append({
            "source": source,
            "target": target,
            "fstype": fstype,
            "total": total,
            "used": used,
            "percent": round(used / total * 100, 1) if total else 0,
            "top": tops.get(target) or [],
        })
    return rows


def nvme_role(ctrl):
    prefix = "/dev/" + ctrl
    mounts = []
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].startswith(prefix):
                mounts.append(parts[1])
    except OSError:
        return ctrl
    if "/" in mounts:
        return "系统盘"
    if "/data" in mounts:
        return "数据盘"
    return mounts[0] if mounts else ctrl


def sensor_title(name, ctrl, label):
    if ctrl.startswith("nvme"):
        title = f"{nvme_role(ctrl)} {ctrl}"
        if label and label.lower() != "composite":
            title = f"{title} {label}"
        return title
    if label and label.lower() != "composite":
        return f"{name} {label}".strip()
    return name or ctrl or label


def temperatures():
    rows = []
    root = Path("/sys/class/hwmon")
    if not root.is_dir():
        return rows
    for hw in sorted(root.glob("hwmon*")):
        name = (hw / "name").read_text().strip() if (hw / "name").is_file() else hw.name
        ctrl = ""
        device = hw / "device"
        if device.exists():
            ctrl = device.resolve().name
        for sensor in sorted(hw.glob("temp*_input")):
            label_path = hw / sensor.name.replace("_input", "_label")
            label = label_path.read_text().strip() if label_path.is_file() else sensor.name
            try:
                celsius = int(sensor.read_text().strip()) / 1000
            except ValueError:
                continue
            rows.append({
                "device": name,
                "label": label,
                "name": sensor_title(name, ctrl, label),
                "celsius": round(celsius, 1),
            })
    return rows


def record_history(cpu, memory, network, temps):
    mem_pct = round(memory["used"] / memory["total"] * 100, 1) if memory["total"] else 0
    has_rate = any(item["rxRate"] is not None for item in network)
    point = {
        "t": datetime.now().strftime("%H:%M:%S"),
        "cpu": cpu,
        "mem": mem_pct,
        "rx": round(sum(item["rxRate"] or 0 for item in network), 1) if has_rate else None,
        "tx": round(sum(item["txRate"] or 0 for item in network), 1) if has_rate else None,
        "temp": max((item["celsius"] for item in temps), default=None),
    }
    HISTORY.append(point)
    del HISTORY[:-60]
    return list(HISTORY)


def process_unit(pid):
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        tail = line.rsplit("/", 1)[-1]
        if tail.endswith(".service"):
            return tail
    return ""


def process_name(comm, cmd, unit, apps):
    blob = f"{comm} {cmd}"
    for needle, title in PROCESS_HINTS:
        if needle in blob:
            return title
    for app in apps.values():
        if app.get("unit") and app["unit"] == unit:
            return app["name"]
    if unit.endswith(".service") and unit != "pm2-root.service":
        return unit[:-8]
    return comm


def top_processes(limit=8):
    global _LAST_PROC
    clk = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    apps = load_apps()
    snap = {}
    info = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text()
            end = stat.rfind(")")
            comm = stat[stat.find("(") + 1:end]
            fields = stat[end + 2:].split()
            ticks = int(fields[11]) + int(fields[12])
            rss = int((entry / "statm").read_text().split()[1]) * page
            cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").strip().decode("utf-8", "replace")
        except (OSError, ValueError, IndexError):
            continue
        if not cmd:
            continue
        snap[pid] = ticks
        info[pid] = (process_name(comm, cmd[:240], process_unit(pid), apps), rss)
    now = time.monotonic()
    prev = _LAST_PROC
    grouped = {}
    elapsed = max(now - prev[0], 0.001) if prev else None
    prev_snap = prev[1] if prev else {}
    for pid, ticks in snap.items():
        name, rss = info[pid]
        cpu = 0.0
        if elapsed and pid in prev_snap:
            cpu = max(0.0, (ticks - prev_snap[pid]) / clk / elapsed * 100)
        row = grouped.setdefault(name, {"name": name, "cpu": 0.0, "memory": 0})
        row["cpu"] += cpu
        row["memory"] += rss
    _LAST_PROC = (now, snap)
    rows = sorted(grouped.values(), key=lambda row: (row["cpu"], row["memory"]), reverse=True)
    return [
        {"name": row["name"], "cpu": round(row["cpu"], 1), "memory": row["memory"]}
        for row in rows[:limit]
    ]


def overview():
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    load = Path("/proc/loadavg").read_text().split()[:3]
    model = ""
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    cpu = cpu_percent()
    memory = meminfo()
    network = net_rates()
    temps = temperatures()
    return {
        "hostname": socket.gethostname(),
        "uptime": uptime,
        "load": [float(item) for item in load],
        "cpuModel": model,
        "cpuCount": os.cpu_count() or 1,
        "cpuPercent": cpu,
        "memory": memory,
        "disks": disks(),
        "network": network,
        "temperatures": temps,
        "processes": top_processes(),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "history": record_history(cpu, memory, network, temps),
    }


def collect_apps():
    apps = load_apps()
    needs_pm2 = any(app.get("kind") == "pm2" for app in apps.values())
    pm2 = pm2_snapshot() if needs_pm2 else {}
    result = []
    for app in apps.values():
        if app["kind"] == "systemd":
            status = systemd_status(app["unit"])
        elif app["kind"] == "pm2":
            status = pm2.get(app["pm2"]) or {
                "state": "stopped",
                "active": "missing",
                "sub": "missing",
                "pid": 0,
                "enabled": "pm2",
                "since": "",
                "restarts": "",
                "memory": "",
            }
        else:
            status = {"state": "unknown"}
        result.append(app_view(app, status))
    return result


def do_action(app, action):
    if app["kind"] == "systemd":
        code, out, err = run(["systemctl", action, app["unit"]], timeout=150)
    elif app["kind"] == "pm2":
        code, out, err = run([PM2, action, app["pm2"]], timeout=40, env=PM2_ENV)
    else:
        raise RuntimeError("未知的应用类型")
    if code != 0:
        raise RuntimeError((err or out).strip() or f"{action} 失败")
    return (out or err).strip()


def read_logs(app, lines):
    if app["kind"] == "systemd":
        code, out, err = run(
            [
                "journalctl",
                "-u",
                app["unit"],
                "-n",
                str(lines),
                "--no-pager",
                "-o",
                "short-iso",
            ],
            timeout=20,
        )
        if code != 0:
            raise RuntimeError((err or out).strip() or "读取日志失败")
        return out
    if app["kind"] == "pm2":
        snapshot = pm2_snapshot().get(app["pm2"]) or {}
        out_log = tail_file(snapshot.get("out_log", ""), lines)
        err_log = tail_file(snapshot.get("err_log", ""), lines)
        parts = []
        if out_log:
            parts.append("----- stdout -----\n" + out_log)
        if err_log:
            parts.append("----- stderr -----\n" + err_log)
        return "\n\n".join(parts) or "暂无日志"
    raise RuntimeError("未知的应用类型")


class Handler(BaseHTTPRequestHandler):
    server_version = "AppCards/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def send_json(self, payload, status=200, cookie=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"error": message}, status)

    def authed(self):
        return session_ok(self.headers.get("Cookie"))

    def require_auth(self):
        if self.authed():
            return True
        self.send_error_json(401, "请先在配置页登录")
        return False

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4_000_000:
            raise RuntimeError("请求过大")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise RuntimeError("请求格式不对")
        return data

    def send_file(self, path, content_type):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_file(INDEX_FILE, "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            file_path = (ROOT / "static" / rel).resolve()
            root = (ROOT / "static").resolve()
            if root not in file_path.parents or file_path.suffix.lower() not in {".svg", ".png"} or not file_path.is_file():
                self.send_error_json(404, "未找到")
                return
            kind = "image/png" if file_path.suffix.lower() == ".png" else "image/svg+xml"
            self.send_file(file_path, kind)
            return
        if path == "/api/session":
            self.send_json({"authed": self.authed()})
            return
        if path == "/api/overview":
            try:
                self.send_json(overview())
            except Exception as exc:
                self.send_error_json(500, str(exc))
            return
        if path == "/api/apps":
            try:
                self.send_json({"apps": collect_apps()})
            except Exception as exc:
                self.send_error_json(500, str(exc))
            return
        if path == "/api/units":
            if not self.require_auth():
                return
            query = parse_qs(parsed.query)
            try:
                self.send_json({"units": list_units((query.get("q") or [""])[0])})
            except Exception as exc:
                self.send_error_json(500, str(exc))
            return
        if path.startswith("/api/units/") and path.endswith("/full"):
            if not self.require_auth():
                return
            unit = path[len("/api/units/"): -len("/full")].strip("/")
            try:
                self.send_json(unit_preview(unit))
            except Exception as exc:
                self.send_error_json(400, str(exc))
            return
        if path.startswith("/api/apps/") and path.endswith("/definition"):
            if not self.require_auth():
                return
            app_id = path[len("/api/apps/"): -len("/definition")].strip("/")
            app = load_apps().get(app_id)
            if not app:
                self.send_error_json(404, "应用不存在")
                return
            if app.get("kind") != "systemd":
                self.send_json({"cat": "", "properties": {}, "note": "这个应用由 PM2 托管，没有 systemd 单元文件。"})
                return
            try:
                snapshot = load_snapshot(app["unit"]) or unit_snapshot(app["unit"])
                self.send_json(snapshot)
            except Exception as exc:
                self.send_error_json(500, str(exc))
            return
        if path.startswith("/api/apps/") and path.endswith("/logs"):
            app_id = path[len("/api/apps/"): -len("/logs")].strip("/")
            app = load_apps().get(app_id)
            if not app:
                self.send_error_json(404, "应用不在白名单中")
                return
            query = parse_qs(parsed.query)
            try:
                lines = int((query.get("lines") or ["200"])[0])
            except ValueError:
                lines = 200
            lines = max(20, min(lines, 500))
            try:
                self.send_json({"logs": read_logs(app, lines)})
            except Exception as exc:
                self.send_error_json(500, str(exc))
            return
        self.send_error_json(404, "未找到")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/login":
            try:
                body = self.read_json()
                if not check_password(str(body.get("password") or "")):
                    self.send_error_json(401, "密码不对")
                    return
                token = new_session()
                cookie = f"panel_session={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_TTL}"
                self.send_json({"ok": True}, cookie=cookie)
            except Exception as exc:
                self.send_error_json(400, str(exc))
            return
        if path == "/api/logout":
            token = session_token(self.headers.get("Cookie"))
            SESSIONS.pop(token, None)
            self.send_json({"ok": True}, cookie="panel_session=; HttpOnly; Path=/; Max-Age=0")
            return
        if not self.require_auth():
            return
        try:
            if path == "/api/password":
                body = self.read_json()
                if not check_password(str(body.get("old") or "")):
                    self.send_error_json(401, "原密码不对")
                    return
                new = str(body.get("new") or "")
                if len(new) < 6:
                    raise RuntimeError("新密码至少 6 位")
                set_password(new)
                hint = ROOT / "initial-password"
                hint.unlink(missing_ok=True)
                self.send_json({"ok": True})
                return
            if path == "/api/import":
                body = self.read_json()
                app, snapshot, created = import_unit(body.get("unit") or "")
                self.send_json({"ok": True, "created": created, "app": app, "snapshot": snapshot, "apps": collect_apps()})
                return
            if path == "/api/services/create":
                app, snapshot = create_unit(self.read_json())
                self.send_json({"ok": True, "app": app, "snapshot": snapshot, "apps": collect_apps()})
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "apps":
                app_id, action = parts[2], parts[3]
                if action in ACTIONS:
                    app = load_apps().get(app_id)
                    if not app:
                        self.send_error_json(404, "应用不在白名单中")
                        return
                    output = do_action(app, action)
                    self.send_json({"ok": True, "output": output, "apps": collect_apps()})
                    return
                if action == "meta":
                    update_meta(app_id, self.read_json())
                    self.send_json({"ok": True, "apps": collect_apps()})
                    return
                if action == "icon":
                    save_icon(app_id, self.read_json())
                    self.send_json({"ok": True, "apps": collect_apps()})
                    return
                if action == "remove":
                    remove_card(app_id)
                    self.send_json({"ok": True, "apps": collect_apps()})
                    return
            self.send_error_json(404, "未找到")
        except Exception as exc:
            self.send_error_json(400, str(exc))


def main():
    ensure_auth()
    global DISK_TOP_RUNNING
    DISK_TOP_RUNNING = True
    threading.Thread(target=refresh_disk_top, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"app-cards listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
