#!/usr/bin/env python3
"""JAWL Dashboard — Flask web monitoring with SQL database support"""

import os
import sys
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
import json
import urllib.request
from flask import Flask, jsonify, render_template_string, request
from qdrant_client import QdrantClient

QDRANT_PATH = "/home/rem/JAWL/src/utils/local/data/vector_db"

def get_qdrant_client():
    """Создаёт подключение к локальному Qdrant."""
    try:
        client = QdrantClient(path=QDRANT_PATH)
        return client
    except Exception as e:
        print(f"[Knowledge Viewer] Qdrant connection error: {e}")
        return None


def get_knowledge_entries(limit=50):
    """Получает записи из Knowledge DB через Qdrant scroll."""
    client = get_qdrant_client()
    if not client:
        return {"entries": [], "total": 0, "error": "Qdrant unavailable"}
    
    try:
        points, next_offset = client.scroll(
            collection_name="knowledge",
            limit=limit,
            with_payload=True,
            with_vectors=False
        )
        
        entries = []
        for point in points:
            payload = point.payload or {}
            text = payload.get("text", "")
            created_at = payload.get("created_at", 0)
            preview = text[:200] + "..." if len(text) > 200 else text
            entries.append({
                "id": str(point.id),
                "text": text,
                "preview": preview,
                "created_at": created_at,
                "time_formatted": format_timestamp(created_at),
                "char_count": len(text)
            })
        entries.sort(key=lambda x: x["created_at"], reverse=True)
        return {"entries": entries, "total": len(entries)}
    except Exception as e:
        return {"entries": [], "total": 0, "error": str(e)}


def format_timestamp(ts):
    """Форматирует timestamp в читаемый вид."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except:
        return str(ts)
import yaml

LOG_FILE = "/home/rem/JAWL/logs/system.log"
DB_FILE = "/home/rem/JAWL/src/utils/local/data/sql_db/agent.db"
SETTINGS_PATH = "/home/rem/JAWL/config/settings.yaml"
ENV_PATH = "/home/rem/JAWL/.env"
MAIN_SCRIPT = "/home/rem/JAWL/src/main.py"
PID_FILE = "/tmp/jawl.pid"

import os

PROVIDERS_JSON = "/home/rem/JAWL/config/providers.json"

def load_providers():
    try:
        with open(PROVIDERS_JSON) as f:
            data = json.load(f)
        return data
    except Exception as e:
        print(f"[Dashboard] Failed to load providers.json: {e}")
        return {"default_model": "glm-5", "providers": {}}

def get_current_provider_model():
    """Return (provider_id, model_id) for currently active config."""
    try:
        with open(SETTINGS_PATH) as f:
            cfg = yaml.safe_load(f)
        current_model = cfg.get("llm", {}).get("model_name", "")
        providers = load_providers().get("providers", {})
        for pid, pdata in providers.items():
            for mid, mdata in pdata.get("models", {}).items():
                if mid == current_model:
                    return pid, mid
        return None, current_model
    except:
        return None, None


app = Flask(__name__)

# Cache
cache = {"data": {}, "ts": 0}
CACHE_TTL = 3


# ─── Crypto Ticker Cache ────────────────────────────────────────
crypto_cache = {"data": None, "ts": 0}
CRYPTO_CACHE_TTL = 60  # seconds

def fetch_crypto_prices():
    """Fetch BTC/ETH/SOL prices from CoinGecko free API."""
    now = time.time()
    if crypto_cache["data"] and now - crypto_cache["ts"] < CRYPTO_CACHE_TTL:
        return crypto_cache["data"]
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true"
        req = urllib.request.Request(url, headers={"User-Agent": "JAWL-Dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode())
        result = []
        for cid, name, icon in [("bitcoin", "BTC", "₿"), ("ethereum", "ETH", "Ξ"), ("solana", "SOL", "◎")]:
            if cid in raw:
                price = raw[cid]["usd"]
                change = raw[cid].get("usd_24h_change", 0)
                result.append({"id": cid, "name": name, "icon": icon, "price": price, "change_24h": round(change, 2)})
        crypto_cache["data"] = result
        crypto_cache["ts"] = now
        return result
    except Exception:
        return crypto_cache["data"] or []


# ─── Log parsing ───────────────────────────────────────────────

def read_log_lines(logfile, lines=50, max_bytes=50000):
    if not os.path.exists(logfile):
        return []
    with open(logfile, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read().decode('utf-8', errors='replace')
    return data.split('\n')[-lines:]


def strip_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def parse_status_from_logs():
    lines = read_log_lines(LOG_FILE, 500, 500000)
    status = "● ONLINE"
    current_step = None
    current_step_total = None
    model = "minimax-m2.7"
    heartbeat = "300s"
    agent_name = "JAWL"
    last_action = None
    react_actions_count = 0
    uptime = "—"

    for line in lines:
        if not line.strip():
            continue
        lc = strip_ansi(line)

        if "ONLINE" in lc:
            status = "● ONLINE"
        elif "OFFLINE" in lc:
            status = "○ OFFLINE"
        elif " запущен в фоновом режиме" in lc:
            status = "● ONLINE"

        # Skip lines that are actually Python regex patterns (contain \S+, \s*, etc.)
        if '\\S+' in lc or '\\s*' in lc:
            continue
        m = re.search(r'Model:\s*([a-zA-Z0-9_.\-]+)', lc)
        if m: model = m.group(1)
        m = re.search(r'Heartbeat:\s*(\S+)', lc)
        if m: heartbeat = m.group(1)

        m = re.search(r'\[ReAct\]\s*Шаг\s*(\d+)/(\d+)', lc)
        if m:
            current_step = int(m.group(1))
            current_step_total = int(m.group(2))
            react_actions_count += 1
            last_action = "ReAct цикл"

        m = re.search(r'Имя агента:\s*(\S+)', lc)
        if m: agent_name = m.group(1)

        m = re.search(r'\[Agent Action\]\s*(.+)', lc)
        if m:
            action_text = m.group(1).strip()[:100]
            if action_text and len(action_text) > 5:
                last_action = action_text

        # Uptime — from "up X days, H:M" in STDOUT of uptime command
        m = re.search(r'up (\d+) days?,\s*(\d+):(\d+)', lc)
        if m:
            days = int(m.group(1))
            hours = int(m.group(2))
            mins = int(m.group(3))
            uptime = f"{days}d {hours:02d}:{mins:02d}"

    # Resolve display name from providers.json
    model_display = model
    try:
        prov_cfg = load_providers()
        for pid, pdata in prov_cfg.get("providers", {}).items():
            for mid, mdata in pdata.get("models", {}).items():
                if mid == model:
                    model_display = pdata.get("name", pid) + ' / ' + mdata.get("name", mid)
                    break
    except:
        pass

    return {
        "status": status,
        "model": model,
        "model_display": model_display,
        "heartbeat": heartbeat,
        "agent_name": agent_name,
        "current_step": current_step,
        "current_step_total": current_step_total,
        "react_actions_count": react_actions_count,
        "last_action": last_action or "—",
        "uptime": uptime,
    }


# ─── Database queries ──────────────────────────────────────────

def get_db():
    return sqlite3.connect(DB_FILE)


def calc_drive_deficit(last_satisfied_str, decay_rate, decay_interval_sec=3600):
    """Calculate drive deficit using JAWL formula."""
    try:
        last_sat = datetime.strptime(last_satisfied_str, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        try:
            last_sat = datetime.strptime(last_satisfied_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return 0
    now = datetime.now()
    intervals_passed = (now - last_sat).total_seconds() / decay_interval_sec
    return min(100.0, intervals_passed * decay_rate)


def get_drives():
    """Get drives with calculated deficit."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT name, type, decay_rate, last_satisfied_at, description FROM drives")
    rows = cur.fetchall()
    db.close()

    drives = []
    for name, dtype, decay_rate, last_sat, desc in rows:
        deficit = calc_drive_deficit(last_sat, decay_rate)
        drives.append({
            "name": name,
            "type": dtype,
            "deficit": int(deficit),
            "decay_rate": decay_rate,
            "last_satisfied": last_sat,
            "description": (desc or "")[:60],
        })
    return drives


def get_tasks():
    """Get tasks from database."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, description, term, context FROM tasks")
    rows = cur.fetchall()
    db.close()

    tasks = []
    for tid, desc, term, ctx in rows:
        tasks.append({
            "id": tid,
            "description": desc,
            "term": term or "",
            "context": (ctx or "")[:80],
        })
    return tasks


def get_thoughts(limit=100):
    """Get thoughts (ticks) from database — now with full_text field."""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, created_at, thoughts FROM ticks ORDER BY rowid DESC LIMIT ?",
        (limit,)
    )
    rows = cur.fetchall()
    db.close()

    thoughts = []
    for tid, ts, text in rows:
        full = (text or "")
        preview = full[:150] + ("..." if len(full) > 150 else "")
        thoughts.append({
            "id": tid[:8],
            "ts": ts,
            "text": preview,           # truncated for list view
            "full_text": full,          # full text for expand
            "is_long": len(full) > 150,  # flag: show expand button?
        })
    return thoughts


def get_system_resources():
    """Get CPU and RAM usage."""
    try:
        # CPU
        with open('/proc/stat', 'r') as f:
            vals1 = list(map(int, f.readline().split()[1:]))
        time.sleep(0.1)
        with open('/proc/stat', 'r') as f:
            vals2 = list(map(int, f.readline().split()[1:]))
        d_idle = vals2[3] - vals1[3]
        d_total = sum(vals2) - sum(vals1)
        cpu_pct = round((1 - d_idle / max(d_total, 1)) * 100, 1)
    except Exception:
        cpu_pct = 0

    try:
        # RAM
        with open('/proc/meminfo', 'r') as f:
            mem = {}
            for line in f:
                parts = line.split()
                mem[parts[0].rstrip(':')] = int(parts[1])
        total = mem.get('MemTotal', 0) // 1024
        available = mem.get('MemAvailable', 0) // 1024
        used = total - available
        ram_pct = round(used / max(total, 1) * 100, 1)
        ram_str = f"{used}/{total} MB"
    except Exception:
        ram_pct = 0
        ram_str = "?"

    return {"cpu": cpu_pct, "ram_pct": ram_pct, "ram_str": ram_str}


def get_log_lines(lines=100):
    raw = read_log_lines(LOG_FILE, lines)
    return [strip_ansi(l) for l in raw if l.strip()]


def get_errors():
    lines = read_log_lines(LOG_FILE, 200)
    errors = []
    for line in lines:
        lc = strip_ansi(line)
        if "ERROR" in lc or "CRITICAL" in lc:
            errors.append(lc[-150:])
    return errors[-5:]


def get_activity():
    if not os.path.exists(LOG_FILE):
        return {"labels": [], "data": []}
    with open(LOG_FILE, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 500000))
        data = f.read().decode('utf-8', errors='replace')
    lines = data.split('\n')
    from collections import Counter
    minute_counts = Counter()
    for line in lines:
        lc = strip_ansi(line)
        m_ts = re.match(r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})', lc)
        if m_ts and 'ReAct' in lc and 'Шаг' in lc:
            minute_counts[m_ts.group(2)] += 1
    sorted_minutes = sorted(minute_counts.keys())[-30:]
    return {"labels": sorted_minutes, "data": [minute_counts[m] for m in sorted_minutes]}


# ─── Aggregated data ───────────────────────────────────────────

def get_ticks_limit():
    try:
        with open("/tmp/jawl_ticks_limit.txt") as f:
            return int(f.read().strip())
    except:
        return 15

def gather_all():
    status = parse_status_from_logs()
    return {
        **status,
        "drives": get_drives(),
        "tasks": get_tasks(),
        "thoughts": get_thoughts(100),
        "resources": get_system_resources(),
        "errors": get_errors(),
        "activity": get_activity(),
        "ticks_limit": get_ticks_limit(),
        "updated": datetime.now().strftime("%H:%M:%S"),
    }


# ─── Routes ────────────────────────────────────────────────────

@app.route("/")
def index():
    now = time.time()
    if now - cache["ts"] > CACHE_TTL:
        cache["data"] = gather_all()
        cache["ts"] = now
    data = cache["data"]
    data["log_lines"] = get_log_lines(100)
    return render_template_string(TEMPLATE, **data)


@app.route("/api/status")
def api_status():
    now = time.time()
    if now - cache["ts"] > CACHE_TTL:
        cache["data"] = gather_all()
        cache["ts"] = now
    return jsonify(cache["data"])


@app.route("/api/logs")
def api_logs():
    return jsonify(get_log_lines(100))


@app.route("/api/drives")
def api_drives():
    return jsonify(get_drives())


@app.route("/api/tasks")
def api_tasks():
    return jsonify(get_tasks())


@app.route("/api/thoughts")
def api_thoughts():
    return jsonify(get_thoughts(100))


@app.route("/api/activity")
def api_activity():
    return jsonify(get_activity())



@app.route("/api/crypto")
def api_crypto():
    return jsonify(fetch_crypto_prices())


@app.route("/api/knowledge")
def api_knowledge():
    return jsonify(get_knowledge_entries())


@app.route("/api/providers")
def api_providers():
    data = load_providers()
    current_provider, current_model = get_current_provider_model()
    return jsonify({
        "current_provider": current_provider,
        "current_model": current_model,
        "default_model": data.get("default_model", "glm-5"),
        "providers": data.get("providers", {})
    })


@app.route("/api/switch", methods=["POST"])
def api_switch():
    data = request.get_json()
    model_id = data.get("model")

    # Load providers.json
    with open(PROVIDERS_JSON) as f:
        cfg = json.load(f)

    # Find which provider has this model
    provider_id = None
    provider_cfg = None
    model_cfg = None
    for pid, pdata in cfg["providers"].items():
        if model_id in pdata.get("models", {}):
            provider_id = pid
            provider_cfg = pdata
            model_cfg = pdata["models"][model_id]
            break

    if not provider_cfg:
        return jsonify({"error": f"Unknown model: {model_id}"}), 400

    # Read API key from env var
    env_var = provider_cfg.get("api_key_env", "LLM_API_KEY_1")
    api_key = os.environ.get(env_var, "")
    if not api_key:
        # Try reading from .env
        try:
            with open(ENV_PATH) as f:
                for ln in f:
                    if ln.startswith(env_var + "="):
                        api_key = ln.split("=", 1)[1].strip().strip('"')
                        break
        except:
            pass

    # Update settings.yaml
    with open(SETTINGS_PATH) as f:
        settings = yaml.safe_load(f)
    settings["llm"]["model_name"] = model_id
    with open(SETTINGS_PATH, "w") as f:
        yaml.safe_dump(settings, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # Update .env with url + key
    out = []
    has_u = has_k = False
    try:
        with open(ENV_PATH) as f:
            for ln in f:
                if ln.startswith("LLM_API_URL="):
                    out.append('LLM_API_URL="' + provider_cfg["url"] + '"\n')
                    has_u = True
                elif ln.startswith("LLM_API_KEY_1="):
                    out.append('LLM_API_KEY_1="' + api_key + '"\n')
                    has_k = True
                else:
                    out.append(ln)
    except:
        pass
    if not has_u:
        out.append('LLM_API_URL="' + provider_cfg["url"] + '"\n')
    if not has_k:
        out.append('LLM_API_KEY_1="' + api_key + '"\n')
    with open(ENV_PATH, "w") as f:
        f.writelines(out)

    # Save current selection to providers.json
    cfg["current_provider"] = provider_id
    cfg["current_model"] = model_id
    with open(PROVIDERS_JSON, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    # Restart JAWL
    import subprocess
    try:
        with open(PID_FILE) as f:
            os.kill(int(f.read().strip()), 15)
    except:
        pass
    subprocess.run("pkill -9 -f 'python.*src/main.py' || true", shell=True)
    time.sleep(2)
    proc = subprocess.Popen(
        [sys.executable, MAIN_SCRIPT],
        cwd="/home/rem/JAWL",
        stdout=open("/home/rem/JAWL/logs/stdout.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True
    )
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))

    return jsonify({"ok": True, "pid": proc.pid, "provider": provider_id, "model": model_id, "model_name": model_cfg.get("name", model_id)})


@app.route("/api/tool_choice", methods=["GET", "POST"])
def api_tool_choice():
    status_file = "/tmp/jawl_tool_choice.txt"
    if request.method == "POST":
        data = request.get_json() or {}
        enabled = data.get("enabled", True)
        with open(status_file, "w") as f:
            f.write("1" if enabled else "0")
        return jsonify({"ok": True, "enabled": enabled})
    else:
        try:
            with open(status_file) as f:
                enabled = f.read().strip() != "0"
        except FileNotFoundError:
            enabled = True
        # Считаем missed из логов за 30 мин
        missed = 0
        try:
            cutoff = time.time() - 1800
            with open(LOG_FILE) as f:
                for line in f:
                    if "tool_choice failed" in line:
                        missed += 1
        except:
            pass
        return jsonify({"enabled": enabled, "missed_30min": missed})


@app.route("/api/ticks_limit", methods=["POST"])
def api_ticks_limit():
    data = request.get_json() or {}
    limit = data.get("limit", 15)
    limit = max(1, min(30, int(limit)))
    with open("/tmp/jawl_ticks_limit.txt", "w") as f:
        f.write(str(limit))
    return jsonify({"ok": True, "ticks_limit": limit})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    import subprocess, sys
    try:
        # 1. Убиваем старый JAWL (не Dashboard!)
        old_pids = subprocess.run(["pgrep", "-f", "python.*src/main.py"], capture_output=True, text=True).stdout.strip()
        if old_pids:
            for pid in old_pids.split("\n"):
                pid = pid.strip()
                if pid:
                    subprocess.run(["kill", "-9", pid], capture_output=True)
                    print(f"[Dashboard] Killed JAWL PID {pid}")

        # 2. Очистка Qdrant lock
        lock_file = "/home/rem/JAWL/src/utils/local/data/vector_db/.lock"
        if os.path.exists(lock_file):
            os.remove(lock_file)

        # 3. Запускаем JAWL в фоне (start_new_session чтобы пережить смерть Dashboard)
        env = os.environ.copy()
        env["PYTHONPATH"] = "/home/rem/JAWL"
        subprocess.Popen(
            ["/home/rem/JAWL/venv/bin/python", "src/main.py"],
            cwd="/home/rem/JAWL",
            env=env,
            stdout=open("/home/rem/JAWL/logs/stdout.log", "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print("[Dashboard] JAWL restarted")
        return jsonify({"ok": True, "message": "JAWL restarting..."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─── HTML Template ─────────────────────────────────────────────

TEMPLATE = '''
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JAWL Dashboard</title>
<style>
/* JINX STYLING — Neon Pink/Magenta Theme */
:root {
  --jinx-pink: #ff0080;
  --jinx-blue: #00ccff;
  --jinx-green: #00ff88;
  --jinx-yellow: #ffaa00;
  --jinx-red: #ff4444;
  --card-bg: #12121a;
  --text-primary: #c0c0c0;
  --text-secondary: #888;
  --text-muted: #555;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0a0a0f; color: #c0c0c0; font-family: 'Courier New', monospace; font-size: 13px; }
h1 { 
    color: #ff0080; 
    padding: 12px 16px; 
    border-bottom: 2px solid #ff0080; 
    font-size: 18px; 
    display: flex; 
    justify-content: space-between; 
    align-items: center;
    text-shadow: 0 0 10px #ff0080, 0 0 20px #ff00ff;
    animation: glow 2s ease-in-out infinite alternate;
}

h1 .crypto-header {
    font-size: 11px;
    margin-left: 20px;
    display: inline-flex;
    gap: 12px;
}

.crypto-header .crypto-item {
    display: inline-flex;
    align-items: center;
    gap: 4px;
}

.crypto-header .crypto-name {
    color: #00ccff;
    font-weight: bold;
}

.crypto-header .crypto-price {
    color: #ddd;
    font-family: 'Courier New', monospace;
}

.crypto-header .crypto-change {
    font-size: 9px;
}

.crypto-header .crypto-change.up { color: #00ff88; }
.crypto-header .crypto-change.down { color: #ff4444; }

.bottom-bar {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    height: 36px;
    background: #0a0a12;
    border-top: 1px solid #ff0080;
    display: flex;
    align-items: center;
    padding: 0 16px;
    gap: 8px;
    font-size: 11px;
    color: #888;
    z-index: 100;
    box-shadow: 0 -2px 10px rgba(255,0,128,0.2);
}
.bottom-bar .bb-sep { color: #333; }
#bb-status { color: #00ff88; font-weight: bold; }
#bb-uptime { color: #ff69b4; }
#bb-heartbeat { color: #00ccff; }
#bb-cpu { color: #ffaa00; }
#bb-ram { color: #ffaa00; }

@keyframes glow {
    from { text-shadow: 0 0 10px #ff0080, 0 0 20px #ff00ff; }
    to { text-shadow: 0 0 15px #ff69b4, 0 0 30px #ff00ff; }
}

@keyframes glitch {
    0% { transform: translate(0); }
    20% { transform: translate(-2px, 2px); }
    40% { transform: translate(-2px, -2px); }
    60% { transform: translate(2px, 2px); }
    80% { transform: translate(2px, -2px); }
    100% { transform: translate(0); }
}

.grid { display: grid; grid-template-columns: 240px 1fr; gap: 12px; padding: 12px 12px 60px; height: calc(100vh - 50px); overflow: hidden; }
.left { display: flex; flex-direction: column; gap: 8px; overflow-y: auto; overflow-x: hidden; }
.left > * { overflow-x: hidden; }
.right { display: flex; flex-direction: column; gap: 8px; overflow: hidden; }
.card { background: #12121a; border: 1px solid #222; border-radius: 4px; padding: 10px; }
.card:hover { border-color: #ff0080; box-shadow: 0 0 10px rgba(255,0,128,0.3); }
.card h3 { color: #ff69b4; font-size: 14px; margin-bottom: 8px; border-bottom: 1px solid #222; padding-bottom: 6px; }
.log-box { flex: 0 0 160px; overflow-y: auto; overflow-x: hidden; background: #0d0d14; border-radius: 4px; padding: 8px; border: 1px solid #222; }
.log-box h3 { color: #ff69b4; font-size: 10px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
.thoughts-box { flex: 1; overflow-y: auto; overflow-x: hidden; background: #0d0d14; border-radius: 4px; padding: 8px; border: 1px solid #222; }
.thoughts-box h3 { color: #ff69b4; font-size: 10px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
.provider-btns { display: flex; gap: 6px; margin-top: 8px; }
.provider-btn { background: #1a1a2e; border: 1px solid #ff69b4; color: #ff69b4; padding: 5px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; transition: all 0.2s; }
.provider-btn:hover { background: #ff69b4; color: #0a0a0f; }
.provider-status { font-size: 10px; color: #555; margin-top: 4px; min-height: 14px; }

/* Status colors — JINX PALETTE */
.ok { color: #00ff88; }
.warn { color: #ffaa00; }
.error { color: #ff4444; }
.offline { color: #ff0080; }
.action { color: #00ffff; }
.think { color: #ff69b4; }

/* Progress bar */
.progress { height: 6px; background: #222; border-radius: 3px; overflow: hidden; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #ff0080, #ff00ff); transition: width 0.3s; }

/* Log items */
.log-line { padding: 2px 4px; font-size: 11px; white-space: pre-wrap; word-break: break-word; overflow: hidden; text-overflow: ellipsis; }
.log-line:hover { background: #1a1a25; }
.log-line.error { color: #ff4444; background: rgba(255,68,68,0.1); }
.log-line.action { color: #00ffff; }
.log-line.think { color: #ff69b4; }
.log-line.ok { color: #00ff88; }
.log-line .ts { color: #444; margin-right: 8px; }

/* Error box */
.error-box { background: rgba(255,68,68,0.1); border: 1px solid #ff4444; color: #ff4444; padding: 8px; border-radius: 4px; margin: 8px; font-size: 12px; }

/* API colors */
.api-on { color: #00ff88; }
.api-off { color: #ff4444; }

/* JINX additions */
.res-label { color: #ff69b4; }
.res-val.warn { color: #ffaa00; text-shadow: 0 0 5px #ffaa00; }
.crypto-up { color: #00ff88; }
.crypto-down { color: #ff4444; }
.neon-text { color: #ff0080; text-shadow: 0 0 5px #ff0080; }


/* Thoughts expand/collapse */
.thought-item {
    cursor: pointer;
    transition: background 0.2s, box-shadow 0.2s;
    border-radius: 6px;
    padding: 6px 8px;
    margin-bottom: 4px;
}
.thought-item:hover {
    background: rgba(0, 255, 136, 0.05);
    box-shadow: 0 0 8px rgba(0, 255, 136, 0.15);
}
.thought-item.expanded {
    background: rgba(0, 255, 136, 0.08);
    box-shadow: 0 0 12px rgba(0, 255, 136, 0.25);
}
.thought-full {
    display: none;
    color: #b0e0d0;
    font-size: 11px;
    white-space: pre-wrap;
    word-break: break-word;
    line-height: 1.5;
    margin-top: 6px;
    padding: 8px;
    background: rgba(0,0,0,0.3);
    border-left: 2px solid #00ff88;
    border-radius: 4px;
}
.thought-item.expanded .thought-full {
    display: block;
}
.thought-expand-hint {
    font-size: 9px;
    color: #00ff88;
    opacity: 0.6;
    margin-top: 2px;
}
.thought-item.expanded .thought-expand-hint {
    display: none;
}

/* Knowledge DB Viewer */
.knowledge-controls {
    margin-bottom: 12px;
}
.knowledge-search-input {
    width: 100%;
    padding: 8px 12px;
    background: var(--card-bg);
    border: 1px solid var(--jinx-pink);
    border-radius: 6px;
    color: var(--text-primary);
    font-size: 13px;
    outline: none;
    transition: border-color 0.3s;
}
.knowledge-search-input:focus {
    border-color: var(--jinx-blue);
    box-shadow: 0 0 8px rgba(0, 200, 255, 0.3);
}
.knowledge-list {
    max-height: 500px;
    overflow-y: auto;
}
.knowledge-entry {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 8px;
    padding: 10px 12px;
    margin-bottom: 8px;
    cursor: pointer;
    transition: all 0.2s;
}
.knowledge-entry:hover {
    border-color: var(--jinx-pink);
    box-shadow: 0 0 6px rgba(255, 0, 110, 0.2);
}
.knowledge-entry-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 4px;
}
.knowledge-entry-time {
    font-size: 11px;
    color: var(--text-muted);
}
.knowledge-entry-chars {
    font-size: 10px;
    color: var(--text-muted);
    background: rgba(255,255,255,0.05);
    padding: 2px 6px;
    border-radius: 4px;
}
.knowledge-entry-preview {
    font-size: 13px;
    color: var(--text-secondary);
    line-height: 1.4;
}
.knowledge-entry-full {
    display: none;
    font-size: 13px;
    color: var(--text-primary);
    line-height: 1.5;
    white-space: pre-wrap;
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid rgba(255,255,255,0.08);
}
.knowledge-entry.expanded .knowledge-entry-full {
    display: block;
}
.knowledge-entry.expanded .knowledge-entry-preview {
    display: none;
}

/* === CUSTOM SCROLLBARS — JAWL Theme === */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: #0a0a0f; }
::-webkit-scrollbar-thumb { background: #2a2a3a; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #ff0080; }

/* === MOBILE ADAPTATION — by Jinx 💙 === */

/* Tablet breakpoint */
@media (max-width: 1024px) {
    .grid {
        grid-template-columns: 200px 1fr;
        gap: 8px;
        padding: 8px;
    }
    .card { padding: 8px; }
    .card h3 { font-size: 13px; }
    h1 { font-size: 16px; padding: 10px 12px; }
}

/* Mobile breakpoint */
@media (max-width: 768px) {
    .grid {
        grid-template-columns: 1fr;
        grid-template-rows: auto 1fr;
        height: auto;
        min-height: calc(100vh - 50px);
        overflow-y: auto;
    }
    
    .left {
        flex-direction: row;
        flex-wrap: wrap;
        gap: 6px;
        overflow-y: visible;
        max-height: none;
    }
    
    .left .card {
        flex: 1 1 calc(50% - 6px);
        min-width: 140px;
    }
    
    .right {
        min-height: 300px;
    }
    
    h1 {
        font-size: 14px;
        padding: 8px 10px;
        flex-wrap: wrap;
    }
    
    h1 .uptime {
        font-size: 10px;
        width: 100%;
        margin-top: 4px;
    }
    
    body { font-size: 11px; }
    .card { padding: 6px; }
    .card h3 { font-size: 12px; margin-bottom: 5px; padding-bottom: 4px; }
    
    
.crypto-card-hidden { display: none !important; }
.crypto-row {
        flex-direction: column;
        gap: 2px;
    }
    
    .log-box {
        flex: 0 0 120px;
        font-size: 10px;
    }
    
    .log-line {
        font-size: 9px;
        white-space: normal;
        word-break: break-all;
    }
    
    .thought-item {
        padding: 3px 0;
    }
    
    .thought-text {
        font-size: 10px;
    }
    
    .provider-btn {
        padding: 4px 8px;
        font-size: 10px;
    }
    
    .card canvas#chart {
        display: none;
    }
    
    .drive-item {
        font-size: 10px;
    }
    
    .task-list { margin: 0; padding-left: 16px; color: #aaa; }

    .task-item {
        font-size: 10px;
        word-break: break-word;
        white-space: normal;
        overflow: hidden;
        text-overflow: ellipsis;
        display: block;
        max-width: 100%;
    }
    
    .provider-status {
        font-size: 9px;
    }
}

/* Small phones */
@media (max-width: 480px) {
    .grid {
        padding: 4px;
        gap: 4px;
    }
    
    .left .card {
        flex: 1 1 100%;
    }
    
    h1 {
        font-size: 13px;
        padding: 6px 8px;
    }
    
    body { font-size: 10px; }
    .card { padding: 5px; }
    .card h3 { font-size: 11px; }
    
    .log-box {
        flex: 0 0 100px;
    }
    
    .log-line {
        font-size: 8px;
    }
    
    .provider-btns {
        flex-wrap: wrap;
    }
}

/* Touch-friendly improvements for all small screens */
@media (max-width: 768px) {
    .provider-btn {
        min-height: 36px;
        min-width: 60px;
    }
    
    button, .provider-btn {
        -webkit-tap-highlight-color: #ff0080;
    }
    
    .thoughts-box, .log-box, .left {
        -webkit-overflow-scrolling: touch;
        scroll-behavior: smooth;
    }
}
</style>
</head>
<body>
<h1>
  <span>⚡ JAWL Dashboard — {{ agent_name }}</span>
  <span id="cryptoBox" class="crypto-header"></span>
</h1>

<div class="grid">
  <!-- LEFT PANEL -->
  <div class="left">
    <div id="leftPanel"></div>
  </div>

  <!-- RIGHT PANEL: Thoughts (large) + Logs (small) -->
  <div class="right">
    <div class="thoughts-box" id="thoughtsBox">
      <h3>💡 Мысли</h3>
      {% for t in thoughts %}
      <div class="thought-item">
        <div class="thought-ts">{{ t.ts }}</div>
        <div class="thought-text">{{ t.text }}</div>
      </div>
      {% endfor %}
    </div>

    <div class="log-box" id="logBox">
      <h3>📜 Логи</h3>
      <div class="no-data">Загрузка...</div>
    </div>
  </div>
</div>

<div class="updated">Updated: {{ updated }}</div>

<div class="bottom-bar" id="bottomBar">
  <span id="bb-status"></span>
  <span class="bb-sep">|</span>
  <span id="bb-heartbeat"></span>
  <span class="bb-sep">|</span>
  <span id="bb-uptime"></span>
  <span class="bb-sep">|</span>
  <span id="bb-cpu"></span>
  <span class="bb-sep">|</span>
  <span id="bb-ram"></span>
</div>

<script>
// Full left panel refresh
function renderLeftPanel(d) {
  let html = '';



  // Provider + Model selector
  html += '<div class="card"><h3>⚙️ Модель</h3>';
  html += '<div class="val small" id="modelCurrentDisplay">' + (d.model_display || d.model || '—') + '</div>';
  html += '<div style="margin-top:6px">';
  html += '<select id="providerSelect" onchange="onProviderChanged()" style="width:100%;background:#1a1a2e;color:#ff69b4;border:1px solid #333;padding:4px 6px;border-radius:4px;font-size:12px;margin-bottom:4px">';
  html += '</select>';
  html += '<select id="modelSelect" style="width:100%;background:#1a1a2e;color:#ff69b4;border:1px solid #333;padding:4px 6px;border-radius:4px;font-size:12px">';
  html += '</select>';
  html += '</div>';
  html += '<div class="provider-btns" style="margin-top:6px">';
  html += '<button id="btn-switch-model" class="provider-btn" onclick="doSwitchModel()">🔄 Переключить</button>';
  html += '</div>';
  html += '<div class="provider-btns" style="margin-top:4px">';
  html += '<button id="btn-toolchoice" class="provider-btn" onclick="toggleToolChoice()">🔧 Tools: ON</button>';
  html += '<span id="missedTools" style="color:#ff69b4;font-size:11px;margin-left:8px"></span>';
  setTimeout(function(){ loadToolChoice(); }, 500);
  html += '</div>';
  html += '<div class="provider-btns" style="margin-top:4px">';
  html += '<span style="color:#888;font-size:11px">Тиков:</span>';
  html += '<input id="ticksInput" type="number" min="1" max="30" value="' + (d.ticks_limit||15) + '" style="width:40px;background:#1a1a2e;color:#ff69b4;border:1px solid #333;padding:2px 4px;border-radius:3px;font-size:12px;text-align:center">';
  html += '<button class="provider-btn" onclick="applyTicks()" style="padding:2px 8px;font-size:11px">OK</button>';
  html += '</div>';
  html += '<div class="provider-btns" style="margin-top:4px">';
  html += '<button class="provider-btn" onclick="restartJAWL(this)" style="border-color:#ff4444;color:#ff4444">🔄 Restart</button>';
  html += '</div>';
  html += '</div>';


  // ReAct Progress
  html += '<div class="card"><h3>ReAct Progress</h3>';
  if (d.current_step) {
    html += '<div class="val small">Шаг ' + d.current_step + '/' + d.current_step_total + ' (' + d.react_actions_count + ' действий)</div>';
    let pct = Math.round(d.current_step / d.current_step_total * 100);
    html += '<div class="react-bar"><div class="react-fill" style="width:' + pct + '%"></div></div>';
  } else {
    html += '<div class="val small">—</div>';
  }
  html += '</div>';

  // Last Action
  html += '<div class="card"><h3>Последнее действие</h3><div class="val warn small">' + (d.last_action || '—') + '</div></div>';

  // Drives
  html += '<div class="card"><h3>🧠 Драйвы</h3>';
  for (let dr of d.drives) {
    let col = dr.deficit >= 70 ? '#ff4444' : dr.deficit >= 40 ? '#ffaa00' : '#00ff88';
    let cls = dr.deficit >= 70 ? 'fill-crit' : dr.deficit >= 40 ? 'fill-warn' : 'fill-ok';
    html += '<div class="drive-row"><div class="drive-header"><span class="drive-name">' + dr.name + '</span><span class="drive-pct" style="color:' + col + '">' + dr.deficit + '%</span></div>';
    html += '<div class="drive-bar"><div class="drive-fill ' + cls + '" style="width:' + dr.deficit + '%"></div></div></div>';
  }
  html += '</div>';

  // Tasks
  html += '<div class="card"><h3>📋 Задачи (' + d.tasks.length + ')</h3>';
  if (d.tasks.length) {
    html += '<ul class="task-list">';
    for (let t of d.tasks) html += '<li class="task-item">' + (t.description || '').substring(0, 60) + '</li>';
    html += '</ul>';
  } else {
    html += '<div class="no-data">Нет активных задач</div>';
  }
  html += '</div>';


  // Activity chart placeholder
  html += '<div class="card" style="flex:0 0 auto"><h3>ReAct шагов/мин</h3><canvas id="chart" width="220" height="80"></canvas></div>';

  // Knowledge DB
  html += '<div class="card" id="knowledge-card"><h3>🧠 Knowledge DB <span id="knowledge-count" class="badge">0</span></h3>';
  html += '<div class="knowledge-controls"><input type="text" id="knowledge-search" placeholder="🔍 Search knowledge..." class="knowledge-search-input" oninput="filterKnowledge()"></div>';
  html += '<div id="knowledge-list" class="knowledge-list"></div></div>';

  // Errors
  if (d.errors && d.errors.length) {
    html += '<div class="error-box"><h3>❗ Ошибки</h3><div class="errors">';
    for (let e of d.errors) html += e + '<br>';
    html += '</div></div>';
  }

  // Bottom bar
  document.getElementById('bb-status').textContent = d.status;
  document.getElementById('bb-uptime').textContent = '⏱ ' + d.uptime;
  document.getElementById('bb-heartbeat').textContent = '♥ ' + d.heartbeat;
  document.getElementById('bb-cpu').textContent = 'CPU ' + d.resources.cpu + '%';
  document.getElementById('bb-ram').textContent = 'RAM ' + d.resources.ram_pct + '%';

  document.getElementById('leftPanel').innerHTML = html;
  // Redraw chart after DOM update
  drawChart();
  loadCrypto();
}

async function loadLeftPanel() {
  try {
    let r = await fetch('/api/status');
    let d = await r.json();
    renderLeftPanel(d);
  } catch(e) { console.error(e); }
}

// Expand/collapse state tracking (survives refreshes via ID)
let expandedThoughts = new Set();

// Live thought refresh
async function loadThoughts() {
  try {
    let r = await fetch('/api/thoughts');
    let thoughts = await r.json();
    let box = document.getElementById('thoughtsBox');
    let html = '<h3 style="color:#555;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;position:sticky;top:0;background:#12121a;padding:2px 0;">💡 Мысли <span style="color:#333;font-size:9px;">(кликни чтобы развернуть)</span></h3>';
    for (let t of thoughts) {
      let isExpanded = expandedThoughts.has(t.id);
      let cls = isExpanded ? 'thought-item expanded' : 'thought-item';
      html += '<div class="' + cls + '" data-id="' + t.id + '" onclick="toggleThought(this)" title="Кликни для ' + (isExpanded ? 'сворачивания' : 'разворачивания') + '">';
      html += '<div class="thought-ts">' + t.ts + '</div>';
      html += '<div class="thought-text">' + t.text + '</div>';
      if (t.is_long) {
        html += '<div class="thought-expand-hint">▼ показать полностью</div>';
        html += '<div class="thought-full">' + t.full_text + '</div>';
      }
      html += '</div>';
    }
    box.innerHTML = html;
  } catch(e) { console.error(e); }
}

function toggleThought(el) {
  let id = el.getAttribute('data-id');
  if (expandedThoughts.has(id)) {
    expandedThoughts.delete(id);
    el.classList.remove('expanded');
  } else {
    expandedThoughts.add(id);
    el.classList.add('expanded');
  }
}

// Live log refresh
async function loadLogs() {
  try {
    let r = await fetch('/api/logs');
    let lines = await r.json();
    let box = document.getElementById('logBox');
    let html = '<h3 style="color:#555;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;position:sticky;top:0;background:#12121a;padding:2px 0;">📜 Логи</h3>';
    for (let l of lines) {
      if (!l.trim()) continue;
      let cls = 'log-line';
      if (l.includes('ERROR') || l.includes('CRITICAL')) cls += ' error';
      else if (l.includes('[Agent Action]')) cls += ' action';
      else if (l.includes('Мысли') || l.includes('Vector DB')) cls += ' think';
      else if (l.includes('Success') || l.includes('запущен')) cls += ' ok';
      html += '<div class="' + cls + '"><span class="ts">' + l.substring(0, 19) + '</span>' + l.substring(19) + '</div>';
    }
    box.innerHTML = html;
    box.scrollTop = box.scrollHeight;
  } catch(e) { console.error(e); }
}


// Crypto ticker
async function loadCrypto() {
  try {
    let r = await fetch('/api/crypto');
    let coins = await r.json();
    let box = document.getElementById('cryptoBox');
    if (!box) return;
    if (!coins.length) { box.innerHTML = '<span class="no-data">Нет данных</span>'; return; }
    let html = '';
    for (let c of coins) {
      let chCls = c.change_24h >= 0 ? 'up' : 'down';
      let chSign = c.change_24h >= 0 ? '+' : '';
      let priceStr = c.price >= 1000 ? c.price.toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0}) : c.price.toFixed(2);
      html += '<span class="crypto-item">';
      html += '<span class="crypto-name">' + c.icon + '</span>';
      html += '<span class="crypto-price">' + priceStr + '</span>';
      html += '<span class="crypto-change ' + chCls + '">' + chSign + c.change_24h.toFixed(1) + '%</span>';
      html += '</span>';
    }
    box.innerHTML = html;
  } catch(e) { console.error('Crypto fetch error:', e); }
}

loadLeftPanel().then(() => {
  loadProviders();
  loadKnowledge();
  drawChart();
});
loadCrypto();
loadThoughts();
loadLogs();
loadToolChoice();
setInterval(loadToolChoice, 10000);
setInterval(loadCrypto, 60000);
let PROVIDERS_DATA = {};
let currentProviderId = null;
let currentModelId = null;

async function loadProviders() {
  try {
    let r = await fetch("/api/providers");
    let d = await r.json();
    PROVIDERS_DATA = d.providers || {};
    currentProviderId = d.current_provider;
    currentModelId = d.current_model;
    populateDropdowns();
  } catch(e) { console.error('Providers load error:', e); }
}

function populateDropdowns() {
  let pSelect = document.getElementById("providerSelect");
  let mSelect = document.getElementById("modelSelect");
  if (!pSelect || !mSelect) {
    // DOM not ready — retry after short delay
    setTimeout(populateDropdowns, 300);
    return;
  }

  for (let [pid, pdata] of Object.entries(PROVIDERS_DATA)) {
    let opt = document.createElement("option");
    opt.value = pid;
    opt.textContent = pdata.name || pid;
    if (pid === currentProviderId) opt.selected = true;
    pSelect.appendChild(opt);
  }

  mSelect.innerHTML = '';
  if (PROVIDERS_DATA[currentProviderId]) {
    for (let [mid, mdata] of Object.entries(PROVIDERS_DATA[currentProviderId].models || {})) {
      let opt = document.createElement("option");
      opt.value = mid;
      opt.textContent = mdata.name || mid;
      if (mid === currentModelId) opt.selected = true;
      mSelect.appendChild(opt);
    }
  }

  // modelCurrentDisplay is set from real logs by renderLeftPanel(d)
  // Dropdown just shows selection state, not actual running model
}

function onProviderChanged() {
  let pSelect = document.getElementById("providerSelect");
  let mSelect = document.getElementById("modelSelect");
  if (!pSelect || !mSelect) return;
  let pid = pSelect.value;
  mSelect.innerHTML = '';
  for (let [mid, mdata] of Object.entries(PROVIDERS_DATA[pid]?.models || {})) {
    let opt = document.createElement("option");
    opt.value = mid;
    opt.textContent = mdata.name || mid;
    mSelect.appendChild(opt);
  }
}

async function doSwitchModel() {
  let mSelect = document.getElementById("modelSelect");
  let btn = document.getElementById("btn-switch-model");
  if (!mSelect || !btn) return;
  let modelId = mSelect.value;
  btn.textContent = "⏳...";
  btn.disabled = true;
  try {
    let r = await fetch("/api/switch", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ model: modelId })
    });
    let d = await r.json();
    if (d.error) { alert("Ошибка: " + d.error); }
    if (d.ok) {
      currentProviderId = d.provider;
      currentModelId = d.model;
      populateDropdowns();
      setTimeout(function() { location.reload(); }, 2500);
    }
  } catch(e) { alert("Error: " + e); }
  btn.textContent = "🔄 Переключить";
  btn.disabled = false;
}

async function loadToolChoice() {
  try {
    let r = await fetch("/api/tool_choice");
    let d = await r.json();
    let btn = document.getElementById("btn-toolchoice");
    if (btn) {
      btn.textContent = d.enabled ? "🔧 Tools: ON" : "🔧 Tools: OFF";
      btn.style.color = d.enabled ? "#00ff88" : "#ff4444";
      btn.style.borderColor = d.enabled ? "#00ff88" : "#ff4444";
    }
    let miss = document.getElementById("missedTools");
    if (miss) { miss.textContent = d.missed_30min > 0 ? "⏭ " + d.missed_30min + " missed/30m" : ""; }
  } catch(e) { console.error(e); }
}

async function toggleToolChoice() {
  try {
    let r = await fetch("/api/tool_choice");
    let d = await r.json();
    let newVal = !d.enabled;
    await fetch("/api/tool_choice", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({enabled: newVal})
    });
    loadToolChoice();
  } catch(e) { console.error(e); }
}

async function applyTicks() {
  let v = document.getElementById("ticksInput").value;
  try {
    let r = await fetch("/api/ticks_limit", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({limit: parseInt(v)})
    });
    let d = await r.json();
    if (d.ok) {
      document.getElementById("ticksInput").value = d.ticks_limit;
      document.getElementById("ticksInput").style.borderColor = "#00ff88";
      setTimeout(() => document.getElementById("ticksInput").style.borderColor = "#333", 1000);
    }
  } catch(e) { console.error(e); }
}

async function restartJAWL(btn) {
  if(!btn) btn=this;
  btn.textContent = "⏳ Restarting...";
  btn.disabled = true;
  try {
    let r = await fetch("/api/restart", {method: "POST"});
    let d = await r.json();
    btn.textContent = d.ok ? "✅ Restarted" : "❌ Error";
  } catch(e) {
    btn.textContent = "❌ Error";
  }
  setTimeout(() => { btn.textContent = "🔄 Restart"; btn.disabled = false; }, 3000);
}

setInterval(loadThoughts, 3000);
setInterval(loadLogs, 3000);
// Periodic polling removed — causes flicker

// Activity chart
async function drawChart() {
  try {
    let r = await fetch('/api/activity');
    let d = await r.json();
    let c = document.getElementById('chart');
    if (!c) { console.warn("Chart canvas not found"); return; }
    let ctx = c.getContext('2d');
    let w = c.width, h = c.height;
    ctx.clearRect(0, 0, w, h);
    if (!d.data.length) return;
    let max = Math.max(...d.data, 1);
    let step = w / Math.max(d.data.length - 1, 1);
    ctx.strokeStyle = '#1a1a25'; ctx.lineWidth = 1;
    for (let i = 0; i < 4; i++) {
      let y = h * i / 3;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    ctx.strokeStyle = '#00ff88'; ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < d.data.length; i++) {
      let x = i * step, y = h - (d.data[i] / max) * (h - 10) - 5;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.fillStyle = '#00ff88';
    for (let i = 0; i < d.data.length; i++) {
      let x = i * step, y = h - (d.data[i] / max) * (h - 10) - 5;
      ctx.beginPath(); ctx.arc(x, y, 2, 0, 6.28); ctx.fill();
    }
    ctx.fillStyle = '#444'; ctx.font = '9px monospace';
    if (d.labels.length > 0) {
      ctx.fillText(d.labels[0], 2, h - 2);
      ctx.fillText(d.labels[d.labels.length-1], w - 30, h - 2);
    }
    ctx.fillText(max, w - 20, 10);
  } catch(e) { console.error(e); }
}
// drawChart called from init chain and setInterval below
setInterval(drawChart, 5000);

// Knowledge DB Viewer
function loadKnowledge() {
    fetch('/api/knowledge')
        .then(r => r.json())
        .then(data => {
            window.knowledgeData = data.entries || [];
            const kcEl = document.getElementById('knowledge-count');
            if (kcEl) kcEl.textContent = data.total;
            renderKnowledge(window.knowledgeData);
        })
        .catch(e => console.error('Knowledge load error:', e));
}

function renderKnowledge(entries) {
    const list = document.getElementById('knowledge-list');
    if (!list) return;
    if (!entries.length) {
        list.innerHTML = '<div style="color: var(--text-muted); text-align: center; padding: 20px;">No entries found</div>';
        return;
    }
    let html = '';
    for (const e of entries) {
        html += `<div class="knowledge-entry" onclick="toggleKnowledge(this)" data-search="${escapeHtml(e.text.toLowerCase())}">
            <div class="knowledge-entry-header">
                <span class="knowledge-entry-time">${e.time_formatted}</span>
                <span class="knowledge-entry-chars">${e.char_count} chars</span>
            </div>
            <div class="knowledge-entry-preview">${escapeHtml(e.preview)}</div>
            <div class="knowledge-entry-full">${escapeHtml(e.text)}</div>
        </div>`;
    }
    list.innerHTML = html;
}

function toggleKnowledge(el) {
    el.classList.toggle('expanded');
}

function filterKnowledge() {
    const query = document.getElementById('knowledge-search').value.toLowerCase();
    if (!window.knowledgeData) return;
    if (!query) {
        renderKnowledge(window.knowledgeData);
        return;
    }
    const filtered = window.knowledgeData.filter(e =>
        e.text.toLowerCase().includes(query)
    );
    renderKnowledge(filtered);
}

function escapeHtml(str) {
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
</script>
</body>
</html>
'''

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
