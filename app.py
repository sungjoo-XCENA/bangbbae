"""Bangbbae - Dev server resource monitoring & Teams alerts"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import asyncio
import httpx
import json
import os
import psutil
import shutil
import subprocess
import time

# --- Config ---

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "port": 8000,
    "teams_webhook": "",
    "dashboard_url": "http://localhost:8000",
    "warning_percent": 120,
    "critical_percent": 150,
    "cooldown_minutes": 5,
    "exclude_users": [],
    "top_process_count": 10,
    "refresh_interval": 3,
}


def load_config():
    config = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            config.update(json.load(f))
    return config


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


# --- State ---

alert_cooldowns = {}  # (user, resource, level) -> timestamp
disk_usage_cache = {}  # user -> bytes
disk_cache_time = 0


# --- Collectors ---


def get_home_users(exclude):
    home = Path("/home")
    if not home.exists():
        return []
    return sorted(
        d.name for d in home.iterdir()
        if d.is_dir() and d.name not in exclude
    )


def collect_user_resources(home_users):
    user_stats = {u: {"cpu": 0.0, "ram_mb": 0.0, "procs": []} for u in home_users}

    for proc in psutil.process_iter(
        ["pid", "name", "username", "cpu_percent", "memory_info", "cmdline"]
    ):
        try:
            info = proc.info
            user = info["username"]
            if user not in user_stats:
                continue

            cpu = info["cpu_percent"] or 0
            ram = (info["memory_info"].rss / 1024 / 1024) if info["memory_info"] else 0

            user_stats[user]["cpu"] += cpu
            user_stats[user]["ram_mb"] += ram
            user_stats[user]["procs"].append({
                "pid": info["pid"],
                "name": info["name"],
                "cpu": round(cpu, 1),
                "ram_mb": round(ram, 1),
                "cmd": " ".join((info["cmdline"] or [info["name"] or ""])[:5])[:80],
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return user_stats


def collect_disk_usage(home_users):
    global disk_usage_cache, disk_cache_time

    if time.time() - disk_cache_time < 60 and disk_usage_cache:
        return disk_usage_cache

    result = {}
    for user in home_users:
        try:
            out = subprocess.run(
                ["du", "-sb", f"/home/{user}"],
                capture_output=True, text=True, timeout=30,
            )
            if out.returncode == 0 and out.stdout.strip():
                result[user] = int(out.stdout.split()[0])
            else:
                result[user] = -1
        except Exception:
            result[user] = -1

    disk_usage_cache = result
    disk_cache_time = time.time()
    return result


def get_last_login(username):
    try:
        out = subprocess.run(
            ["last", "-1", username],
            capture_output=True, text=True, timeout=5,
        )
        line = out.stdout.strip().split("\n")[0] if out.stdout.strip() else ""
        if line and "wtmp" not in line and username in line:
            parts = line.split()
            return " ".join(parts[3:7]) if len(parts) > 6 else " ".join(parts[3:])
        return "No record"
    except Exception:
        return "Unknown"


def get_snapshot():
    config = load_config()
    exclude = config.get("exclude_users", [])
    home_users = get_home_users(exclude)

    # System totals
    cpu_pct = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = shutil.disk_usage("/")

    # Per-user
    user_res = collect_user_resources(home_users)
    user_disk = collect_disk_usage(home_users)

    # Classify active / inactive
    active, inactive = [], []
    for user in home_users:
        stats = user_res[user]
        disk_bytes = user_disk.get(user, 0)
        info = {
            "name": user,
            "cpu": round(stats["cpu"], 1),
            "ram_gb": round(stats["ram_mb"] / 1024, 2),
            "disk_gb": round(disk_bytes / (1024 ** 3), 2) if disk_bytes > 0 else -1,
            "proc_count": len(stats["procs"]),
        }
        if stats["procs"]:
            active.append(info)
        else:
            info["last_login"] = get_last_login(user)
            inactive.append(info)

    active.sort(key=lambda x: x["cpu"], reverse=True)

    # Fair share: CPU/RAM by active count, Disk by total user count
    n_active = max(len(active), 1)
    n_all = max(len(home_users), 1)
    fair = {
        "n_active": n_active,
        "n_all": n_all,
        "cpu": round(psutil.cpu_count() * 100 / n_active, 1),
        "ram_gb": round(mem.total / (1024 ** 3) / n_active, 1),
        "disk_gb": round(disk.total / (1024 ** 3) / n_all, 1),
    }

    # Alert check
    warn_r = config["warning_percent"] / 100
    crit_r = config["critical_percent"] / 100

    def check_alerts(u, checks):
        u["alerts"] = []
        for res, val, base in checks:
            if val < 0:
                continue
            if val > base * crit_r:
                u["alerts"].append({"resource": res, "level": "critical", "current": val, "fair": base})
            elif val > base * warn_r:
                u["alerts"].append({"resource": res, "level": "warning", "current": val, "fair": base})

    for u in active:
        check_alerts(u, [
            ("CPU", u["cpu"], fair["cpu"]),
            ("RAM", u["ram_gb"], fair["ram_gb"]),
            ("Disk", u["disk_gb"], fair["disk_gb"]),
        ])

    for u in inactive:
        check_alerts(u, [("Disk", u["disk_gb"], fair["disk_gb"])])

    # Top processes
    all_procs = []
    for user in home_users:
        for p in user_res[user]["procs"]:
            p["user"] = user
            all_procs.append(p)
    top_n = config.get("top_process_count", 10)
    top = sorted(all_procs, key=lambda x: x["cpu"] + x["ram_mb"] / 1024, reverse=True)[:top_n]

    return {
        "time": datetime.now().strftime("%H:%M:%S"),
        "system": {
            "cpu": cpu_pct,
            "cores": psutil.cpu_count(),
            "ram_total": round(mem.total / (1024 ** 3), 1),
            "ram_used": round(mem.used / (1024 ** 3), 1),
            "ram_pct": mem.percent,
            "disk_total": round(disk.total / (1024 ** 3), 1),
            "disk_used": round(disk.used / (1024 ** 3), 1),
            "disk_pct": round(disk.used / disk.total * 100, 1),
        },
        "fair": fair,
        "active": active,
        "inactive": inactive,
        "top_procs": top,
    }


# --- Teams Alert ---


async def send_teams_alert(config, user, alert):
    url = config.get("teams_webhook")
    if not url:
        return

    key = (user["name"], alert["resource"], alert["level"])
    now = time.time()
    cooldown = config.get("cooldown_minutes", 5) * 60

    if key in alert_cooldowns and now - alert_cooldowns[key] < cooldown:
        return
    alert_cooldowns[key] = now

    emoji = "\U0001f534" if alert["level"] == "critical" else "\u26a0\ufe0f"
    label = "Critical" if alert["level"] == "critical" else "Warning"
    unit = "%" if alert["resource"] == "CPU" else "GB"
    dashboard = config.get("dashboard_url", "http://localhost:8000")

    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "version": "1.4",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "body": [
                    {
                        "type": "TextBlock",
                        "text": f"{emoji} Server Resource {label}",
                        "weight": "Bolder",
                        "size": "Medium",
                    },
                    {
                        "type": "FactSet",
                        "facts": [
                            {"title": "User", "value": user["name"]},
                            {"title": "Resource", "value": alert["resource"]},
                            {"title": "Current", "value": f"{alert['current']}{unit} (fair: {alert['fair']}{unit})"},
                            {"title": "Usage", "value": f"{round(alert['current'] / max(alert['fair'], 0.01) * 100)}%"},
                            {"title": "Time", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                        ],
                    },
                ],
                "actions": [
                    {"type": "Action.OpenUrl", "title": "Open Dashboard", "url": dashboard}
                ],
            },
        }],
    }

    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=card, timeout=10)
    except Exception as e:
        print(f"[Teams] Alert failed: {e}")


# --- App ---


@asynccontextmanager
async def lifespan(app):
    psutil.cpu_percent(interval=None)
    for p in psutil.process_iter(["cpu_percent"]):
        try:
            p.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    await asyncio.sleep(0.5)
    yield


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/", response_class=HTMLResponse)
async def page(request: Request):
    config = load_config()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "refresh_interval": config.get("refresh_interval", 3),
    })


@app.get("/api/snapshot")
async def api_snapshot():
    config = load_config()
    data = get_snapshot()

    # Fire alerts
    for user in data["active"]:
        for alert in user.get("alerts", []):
            await send_teams_alert(config, user, alert)
    for user in data["inactive"]:
        for alert in user.get("alerts", []):
            await send_teams_alert(config, user, alert)

    return data


@app.get("/api/settings")
async def api_get_settings():
    return load_config()


@app.post("/api/settings")
async def api_save_settings(request: Request):
    body = await request.json()
    config = load_config()
    for key in ["teams_webhook", "dashboard_url", "warning_percent", "critical_percent",
                "cooldown_minutes", "refresh_interval", "top_process_count", "exclude_users"]:
        if key in body:
            config[key] = body[key]
    save_config(config)
    return {"status": "ok"}


@app.post("/api/test-alert")
async def api_test_alert():
    config = load_config()
    url = config.get("teams_webhook")
    if not url:
        return JSONResponse({"error": "Webhook URL not configured"}, 400)

    dashboard = config.get("dashboard_url", "http://localhost:8000")
    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "type": "AdaptiveCard",
                "version": "1.4",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "body": [
                    {"type": "TextBlock", "text": "Test Alert", "weight": "Bolder", "size": "Medium"},
                    {"type": "TextBlock", "text": "Bangbbae Teams integration is working."},
                    {"type": "FactSet", "facts": [
                        {"title": "Time", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                    ]},
                ],
                "actions": [
                    {"type": "Action.OpenUrl", "title": "Open Dashboard", "url": dashboard}
                ],
            },
        }],
    }

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=card, timeout=10)
        if r.status_code < 300:
            return {"status": "ok"}
        return JSONResponse({"error": f"HTTP {r.status_code}"}, 500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


if __name__ == "__main__":
    import argparse
    import socket
    import uvicorn

    parser = argparse.ArgumentParser(description="Bangbbae")
    parser.add_argument("-p", "--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    internal_ip = socket.gethostbyname(socket.gethostname())
    dashboard_url = f"http://{internal_ip}:{args.port}"

    config = load_config()
    config["dashboard_url"] = dashboard_url
    save_config(config)

    print(f"Serving on http://{args.host}:{args.port}")
    print(f"→ Internal network: {dashboard_url}")

    uvicorn.run(app, host=args.host, port=args.port)
