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
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M")
    except:
        return str(ts)
import yaml

LOG_FILE = "/home/rem/JAWL/logs/system.log"
DB_FILE = "/home/rem/JAWL/src/utils/local/data/sql_db/agent.db"
SETTINGS_PATH = "/home/rem/JAWL/config/settings.yaml"
ENV_PATH = "/home/rem/JAWL/.env"
MAIN_SCRIPT = "/home/rem/JAWL/src/main.py"
PID_FILE = "/tmp/jawl.pid"
MODELS_JSON = "/home/rem/JAWL/config/models.json"
PROVIDERS_JSON = "/home/rem/JAWL/config/providers.json"  # legacy compat

import os

def load_models():
    try:
        with open(MODELS_JSON) as f:
            return json.load(f)
    except Exception as e:
        print(f"[Dashboard] Failed to load models.json: {e}")
        return {"default_provider": "", "default_model": "", "providers": {}}

def get_current_provider_model():
    """Return (provider_id, model_id) from models.json."""
    cfg = load_models()
    return cfg.get("default_provider", ""), cfg.get("default_model", "")


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


def format_uptime(seconds):
    """Format seconds into human-readable uptime string."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        mins = seconds // 60
        secs = seconds % 60
        return f"{mins}м {secs}с" if secs else f"{mins}м"
    elif seconds < 86400:
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        return f"{hours}ч {mins}м" if mins else f"{hours}ч"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days}д {hours}ч" if hours else f"{days}д"


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
    jawl_uptime = "—"

    # Get server uptime directly from uptime command (fallback if not in logs)
    try:
        import subprocess
        result = subprocess.run(["uptime"], capture_output=True, text=True, timeout=3)
        if result.returncode == 0:
            uptime_output = result.stdout.strip()
            # Parse "up X days, H:M" or "up H:M, X days"
            m = re.search(r'up (\d+) days?,\s*(\d+):(\d+)', uptime_output)
            if m:
                days = int(m.group(1))
                hours = int(m.group(2))
                mins = int(m.group(3))
                uptime = f"{days}d {hours:02d}:{mins:02d}"
            else:
                # Try "up X days, Y min" format
                m = re.search(r'up (\d+) days?,\s*(\d+) min', uptime_output)
                if m:
                    days = int(m.group(1))
                    mins = int(m.group(2))
                    uptime = f"{days}d {mins}м"
                else:
                    # Try short format "up X min" or "up X days"
                    m = re.search(r'up (\d+) min', uptime_output)
                    if m:
                        mins = int(m.group(1))
                        uptime = f"{mins}m"
                    else:
                        m = re.search(r'up (\d+) days?', uptime_output)
                        if m:
                            days = int(m.group(1))
                            uptime = f"{days}d"
    except:
        pass

    # Get JAWL uptime from PID file modification time (simple fallback)
    try:
        pid_file = "/tmp/jawl.pid"
        if os.path.exists(pid_file):
            pid_mtime = os.path.getmtime(pid_file)
            elapsed = int(time.time() - pid_mtime)
            jawl_uptime = format_uptime(elapsed)
    except:
        pass

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

        # Prefer startup log naming (most reliable)
        m = re.search(r'Имя агента:\s*(\S+)', lc)
        if m: agent_name = m.group(1)
        # JSON heartbeat as fallback (activity logs use "JAWL" as placeholder)
        if agent_name == "JAWL":
            m2 = re.search(r'"agent_name":\s*"([^"]+)"', lc)
            if m2: agent_name = m2.group(1)
        # Config file as final fallback
        if agent_name == "JAWL":
            try:
                import yaml
                cfg = yaml.safe_load(open('config/settings.yaml'))
                agent_name = cfg.get('agent_name', 'Jinx')
            except:
                pass

        m = re.search(r'\[Agent Action\]\s*(.+)', lc)
        if m:
            action_text = m.group(1).strip()[:100]
            if action_text and len(action_text) > 5:
                last_action = action_text

        # Uptime — from uptime command in logs (fallback if direct call failed)
        m = re.search(r'up (\d+) days?,\s*(\d+):(\d+)', lc)
        if m:
            days = int(m.group(1))
            hours = int(m.group(2))
            mins = int(m.group(3))
            uptime = f"{days}d {hours:02d}:{mins:02d}"
        else:
            m = re.search(r'up (\d+) days?,\s*(\d+) min', lc)
            if m:
                days = int(m.group(1))
                mins = int(m.group(2))
                uptime = f"{days}d {mins}м"
            else:
                m = re.search(r'up (\d+) min', lc)
                if m:
                    uptime = f"{m.group(1)}m"
                else:
                    m = re.search(r'up (\d+) days?', lc)
                    if m:
                        uptime = f"{m.group(1)}d"

    # Resolve display name from models.json
    model_display = model
    try:
        mcfg = load_models()
        for pid, pdata in mcfg.get("providers", {}).items():
            for m in pdata.get("models", []):
                if m["id"] == model:
                    model_display = pdata.get("name", pid) + ' / ' + m.get("name", m["id"])
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
        "jawl_uptime": jawl_uptime,
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
        # Convert UTC timestamp to user timezone (from config)
        ts_str = "—"
        if ts and isinstance(ts, str):
            try:
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                _cfg = yaml.safe_load(open('config/settings.yaml'))
                _tz_offset = _cfg.get('system', {}).get('timezone', 3)
                dt_utc = _dt.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz.utc)
                dt_local = dt_utc.astimezone(_tz(_td(hours=_tz_offset)))
                ts_str = dt_local.strftime("%H:%M")
            except:
                ts_str = str(ts)[:5] if len(str(ts)) >= 16 else str(ts)
        thoughts.append({
            "id": tid[:8],
            "ts": ts,
            "time_formatted": ts_str,
            "text": preview,
            "full_text": full,
            "is_long": len(full) > 150,
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


@app.route("/api/uptime")
def api_uptime():
    """Return JAWL uptime in seconds based on PID start time."""
    try:
        pid_file = "/tmp/jawl.pid"
        if not os.path.exists(pid_file):
            return jsonify({"error": "PID file not found", "uptime_seconds": 0})
        with open(pid_file) as f:
            pid = int(f.read().strip())
        # Get elapsed seconds directly (avoids timezone issues with lstart)
        import subprocess
        result = subprocess.run(
            ["ps", "-o", "etimes=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode != 0 or not result.stdout.strip():
            return jsonify({"error": "Process not found", "uptime_seconds": 0})
        elapsed = int(result.stdout.strip())
        return jsonify({"uptime_seconds": elapsed, "start_time": f"{elapsed}s ago"})
    except Exception as e:
        return jsonify({"error": str(e), "uptime_seconds": 0})



@app.route("/api/crypto")
def api_crypto():
    return jsonify(fetch_crypto_prices())


@app.route("/api/knowledge")
def api_knowledge():
    return jsonify(get_knowledge_entries())


@app.route("/api/providers")
def api_providers():
    cfg = load_models()
    current_provider, current_model = get_current_provider_model()
    # Flatten for dashboard compatibility
    providers_flat = {}
    for pid, pdata in cfg.get("providers", {}).items():
        models_dict = {}
        for m in pdata.get("models", []):
            models_dict[m["id"]] = {"name": m.get("name", m["id"]), "contextWindow": m.get("contextWindow")}
        providers_flat[pid] = {
            "name": pdata.get("name", pid),
            "models": models_dict,
            "api": pdata.get("api", "openai-completions")
        }
    return jsonify({
        "current_provider": current_provider,
        "current_model": current_model,
        "providers": providers_flat
    })


@app.route("/api/switch", methods=["POST"])
def api_switch():
    """Switch provider/model by updating models.json default_provider/default_model."""
    data = request.get_json()
    model_id = data.get("model")
    if not model_id:
        return jsonify({"error": "model is required"}), 400

    # Load models.json
    with open(MODELS_JSON) as f:
        cfg = json.load(f)

    # Find which provider has this model
    provider_id = None
    for pid, pdata in cfg.get("providers", {}).items():
        for m in pdata.get("models", []):
            if m["id"] == model_id:
                provider_id = pid
                break
        if provider_id:
            break

    if not provider_id:
        return jsonify({"error": f"Unknown model: {model_id}"}), 400

    # Update models.json
    cfg["default_provider"] = provider_id
    cfg["default_model"] = model_id
    with open(MODELS_JSON, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    # Also update settings.yaml for backward compat
    with open(SETTINGS_PATH) as f:
        settings = yaml.safe_load(f)
    settings["llm"]["model_name"] = model_id
    with open(SETTINGS_PATH, "w") as f:
        yaml.safe_dump(settings, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

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

    return jsonify({"ok": True, "pid": proc.pid, "provider": provider_id, "model": model_id})


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
<title>{{ agent_name }} Dashboard</title>
<style>
/* Catppuccin Mocha Theme */
:root {
  --red: #f38ba8;
  --maroon: #eba0ac;
  --peach: #fab387;
  --yellow: #f9e2af;
  --green: #a6e3a1;
  --teal: #94e2d5;
  --sky: #89dceb;
  --sapphire: #74c7ec;
  --blue: #89b4fa;
  --lavender: #b4befe;
  --text: #cdd6f4;
  --subtext1: #bac2de;
  --subtext0: #a6adc8;
  --overlay2: #9399b2;
  --overlay1: #7f849c;
  --overlay0: #6c7086;
  --surface2: #585b70;
  --surface1: #45475a;
  --surface0: #313244;
  --base: #1e1e2e;
  --mantle: #181825;
  --crust: #11111b;
  /* RGB decomposed for rgba() */
  --red-rgb: 243,139,168;
  --green-rgb: 166,227,161;
  --pink: #f5c2e7;
  --pink-rgb: 245,194,231;
  --mauve: #cba6f7;
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 14px;
  --transition: 200ms ease;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--base); color: var(--text); font-family: Inter, system-ui, -apple-system, sans-serif; font-size: 14px; }
h1 { 
    color: var(--lavender); 
    padding: 12px 16px; 
    border-bottom: 2px solid var(--surface1); 
    font-size: 18px; 
    display: flex; 
    justify-content: space-between; 
    align-items: center;
    font-weight: 600;
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
    color: var(--blue);
    font-weight: bold;
}

.crypto-header .crypto-price {
    color: var(--subtext1);
    font-family: Inter, system-ui, -apple-system, sans-serif;
}

.crypto-header .crypto-change {
    font-size: 9px;
}

.crypto-header .crypto-change.up { color: var(--green); }
.crypto-header .crypto-change.down { color: var(--red); }

.bottom-bar {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    height: 36px;
    background: var(--crust);
    border-top: 1px solid var(--surface0);
    display: flex;
    align-items: center;
    padding: 0 16px;
    gap: 8px;
    font-size: 12px;
    color: var(--subtext0);
    z-index: 100;
}
.bottom-bar .bb-sep { color: var(--surface0); }
#bb-status { font-weight: bold; }
#bb-status.online { color: var(--green); }
#bb-status.offline { color: var(--red); }
#bb-uptime { color: var(--subtext0); }
#bb-heartbeat { color: var(--subtext0); }
#bb-cpu { color: var(--subtext0); }
#bb-ram { color: var(--subtext0); }

/* JAWL Uptime moved to bottom bar (bb-jawl-uptime) */

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
.card { background: var(--surface0); border: 1px solid var(--surface1); border-radius: var(--radius-md); padding: 14px; }
.card:hover { border-color: var(--blue); box-shadow: 0 2px 8px rgba(0,0,0,0.3); }
.card h3 { color: var(--text); font-size: 14px; margin-bottom: 8px; border-bottom: 1px solid var(--surface1); padding-bottom: 6px; }
.log-box { flex: 0 0 160px; overflow-y: auto; overflow-x: hidden; background: var(--crust); border-radius: 4px; padding: 8px; border: 1px solid var(--surface0); }
.log-box h3 { color: var(--overlay0); font-size: 10px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
.thoughts-box { flex: 1; overflow-y: auto; overflow-x: hidden; background: var(--crust); border-radius: 4px; padding: 8px; border: 1px solid var(--surface0); }
.thoughts-box h3 { color: var(--overlay0); font-size: 10px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
.provider-btns { display: flex; gap: 6px; margin-top: 8px; }
.provider-btn { background: var(--surface1); color: var(--text); border: 1px solid var(--surface1); border-radius: var(--radius-sm); padding: 4px 8px; cursor: pointer; font-size: 12px; transition: var(--transition); font-family: 'Inter', system-ui, sans-serif; }
.provider-btn:hover { background: var(--surface2); border-color: var(--blue); }
.provider-btn.danger { border-color: var(--red); color: var(--red); }
.provider-btn.danger:hover { background: var(--red); color: var(--base); }
.provider-status { font-size: 10px; color: var(--overlay0); margin-top: 4px; min-height: 14px; }

/* Status colors — Catppuccin */
.ok { color: var(--green); }
.warn { color: var(--yellow); }
.error { color: var(--red); }
.offline { color: var(--red); }
.action { color: var(--blue); }
.think { color: var(--pink); }

/* Progress bar */
.progress { height: 6px; background: var(--surface0); border-radius: 3px; overflow: hidden; }
.progress-fill { height: 100%; background: linear-gradient(90deg, var(--pink), var(--mauve)); transition: width 0.3s; }

/* Log items */
.log-line { padding: 2px 4px; font-size: 11px; white-space: pre-wrap; word-break: break-word; overflow: hidden; text-overflow: ellipsis; }
.log-line:hover { background: var(--surface0); }
.log-line.error { color: var(--red); background: rgba(var(--red-rgb), 0.1); }
.log-line.action { color: var(--blue); }
.log-line.think { color: var(--pink); }
.log-line.ok { color: var(--green); }
.log-line .ts { color: var(--overlay0); margin-right: 8px; }

/* Error box */
.error-box { background: rgba(var(--red-rgb), 0.1); border: 1px solid var(--red); color: var(--red); padding: 8px; border-radius: 4px; margin: 8px; font-size: 12px; }

/* API colors */
.api-on { color: var(--green); }
.api-off { color: var(--red); }

/* JINX additions */
.res-label { color: var(--pink); }
.res-val.warn { color: var(--yellow); text-shadow: 0 0 5px var(--yellow); }
.crypto-up { color: var(--green); }
.crypto-down { color: var(--red); }
.neon-text { color: var(--pink); text-shadow: 0 0 5px var(--pink); }


/* Thoughts expand/collapse */
.thought-item {
    cursor: pointer;
    transition: background 0.2s, box-shadow 0.2s;
    border-radius: 6px;
    padding: 6px 8px;
    margin-bottom: 4px;
    background: var(--surface0);
    border: 1px solid var(--surface1);
}
.thought-ts { color: var(--overlay0); font-size: 10px; margin-bottom: 2px; }
.thought-text { color: var(--text); font-size: 12px; line-height: 1.4; }
.thought-item:hover {
    background: var(--surface0);
    box-shadow: 0 0 8px rgba(var(--green-rgb), 0.15);
}
.thought-item.expanded {
    background: var(--surface0);
    border: 1px solid var(--surface1);
    box-shadow: 0 0 12px rgba(var(--green-rgb), 0.25);
}
.thought-full {
    display: none;
    color: var(--subtext1);
    font-size: 11px;
    white-space: pre-wrap;
    word-break: break-word;
    line-height: 1.5;
    margin-top: 6px;
    padding: 8px;
    background: var(--mantle);
    border-left: 2px solid var(--green);
    border-radius: 4px;
}
.thought-item.expanded .thought-full {
    display: block;
}
.thought-expand-hint {
    font-size: 9px;
    color: var(--green);
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
    background: var(--surface0);
    border: 1px solid var(--pink);
    border-radius: 6px;
    color: var(--text);
    font-size: 13px;
    outline: none;
    transition: border-color 0.3s;
}
.knowledge-search-input:focus {
    border-color: var(--blue);
    box-shadow: 0 0 8px rgba(137,180,250, 0.3);
}
.knowledge-list {
    max-height: 500px;
    overflow-y: auto;
}
.knowledge-entry {
    background: var(--surface0);
    border: 1px solid var(--surface1);
    border-radius: 8px;
    padding: 10px 12px;
    margin-bottom: 8px;
    cursor: pointer;
    transition: all 0.2s;
}
.knowledge-entry:hover {
    border-color: var(--pink);
    box-shadow: 0 0 6px rgba(var(--pink-rgb), 0.2);
}
.knowledge-entry-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 4px;
    color: var(--subtext1);
}
.knowledge-entry-time {
    font-size: 11px;
    color: var(--subtext0);
}
.knowledge-entry-chars {
    font-size: 10px;
    color: var(--subtext0);
    background: var(--surface0);
    padding: 2px 6px;
    border-radius: 4px;
}
.knowledge-entry-preview {
    font-size: 13px;
    color: var(--subtext0);
    line-height: 1.4;
}
.knowledge-entry-full {
    display: none;
    font-size: 13px;
    color: var(--text);
    line-height: 1.5;
    white-space: pre-wrap;
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid var(--surface1);
}
.knowledge-entry.expanded .knowledge-entry-full {
    display: block;
}
.knowledge-entry.expanded .knowledge-entry-preview {
    display: none;
}

/* Drive elements */
.drive-row { border-bottom: 1px solid var(--surface0); padding: 6px 0; }
.drive-header { display: flex; justify-content: space-between; align-items: center; }
.drive-name { color: var(--text); font-size: 12px; }
.drive-pct { font-size: 11px; font-weight: bold; }
.drive-bar { height: 4px; background: var(--surface0); border-radius: 2px; margin-top: 4px; overflow: hidden; }
.drive-fill { height: 100%; border-radius: 2px; transition: width 0.3s; }
.drive-fill.fill-ok { background: var(--green); }
.drive-fill.fill-warn { background: var(--yellow); }
.drive-fill.fill-crit { background: var(--red); }

/* === CUSTOM SCROLLBARS — JAWL Theme === */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: var(--crust); }
::-webkit-scrollbar-thumb { background: var(--surface1); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--pink); }

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
    
    .task-list { margin: 0; padding-left: 16px; color: var(--subtext0); }

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
        -webkit-tap-highlight-color: var(--pink);
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
  <span>⚡ {{ agent_name }} Dashboard — {{ agent_name }}</span>
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
        <div class="thought-ts">{{ t.time_formatted or t.ts }}</div>
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
  <span class="bb-sep">|</span>
  <span id="bb-jawl-uptime" style="color:var(--pink);"></span>
</div>

<script>
// Full left panel refresh
function renderLeftPanel(d) {
  let html = '';



  // Provider + Model selector
  html += '<div class="card"><h3>⚙️ Модель</h3>';
  html += '<div class="val small" id="modelCurrentDisplay">' + (d.model_display || d.model || '—') + '</div>';
  html += '<div style="margin-top:6px">';
  html += '<select id="providerSelect" onchange="onProviderChanged()" style="width:100%;background:var(--surface0);color:var(--text);border:1px solid var(--surface1);padding:4px 6px;border-radius:var(--radius-sm);font-size:12px;margin-bottom:4px;font-family:\'Inter\',system-ui,sans-serif">';
  html += '</select>';
  html += '<select id="modelSelect" style="width:100%;background:var(--surface0);color:var(--text);border:1px solid var(--surface1);padding:4px 6px;border-radius:var(--radius-sm);font-size:12px;font-family:\'Inter\',system-ui,sans-serif">';
  html += '</select>';
  html += '</div>';
  html += '<div class="provider-btns" style="margin-top:6px">';
  html += '<button id="btn-switch-model" class="provider-btn" onclick="doSwitchModel()">🔄 Переключить</button>';
  html += '</div>';
  html += '<div class="provider-btns" style="margin-top:4px">';
  html += '<button id="btn-toolchoice" class="provider-btn" onclick="toggleToolChoice()">🔧 Tools: ON</button>';
  html += '<span id="missedTools" style="color:var(--pink);font-size:11px;margin-left:8px"></span>';
  setTimeout(function(){ loadToolChoice(); }, 500);
  html += '</div>';
  html += '<div class="provider-btns" style="margin-top:4px">';
  html += '<span style="color:var(--overlay0);font-size:11px">Тиков:</span>';
  html += '<input id="ticksInput" type="number" min="1" max="30" value="' + (d.ticks_limit||15) + '" style="width:40px;background:var(--surface0);color:var(--text);border:1px solid var(--surface1);padding:2px 4px;border-radius:var(--radius-sm);font-size:12px;text-align:center;font-family:\'Inter\',system-ui,sans-serif">';
  html += '<button class="provider-btn" onclick="applyTicks()" style="padding:2px 8px;font-size:11px">OK</button>';
  html += '</div>';
  html += '<div class="provider-btns" style="margin-top:4px">';
  html += '<button class="provider-btn danger" onclick="restartJAWL(this)">🔄 Restart</button>';
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
    let col = dr.deficit >= 70 ? 'var(--red)' : dr.deficit >= 40 ? 'var(--yellow)' : 'var(--green)';
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
  let bbStatus = document.getElementById('bb-status');
  bbStatus.textContent = d.status;
  bbStatus.className = d.status === 'online' ? 'online' : 'offline';
  document.getElementById('bb-uptime').textContent = '⏱ ' + d.uptime;
  document.getElementById('bb-heartbeat').textContent = '♥ ' + d.heartbeat;
  document.getElementById('bb-cpu').textContent = 'CPU ' + d.resources.cpu + '%';
  document.getElementById('bb-ram').textContent = 'RAM ' + d.resources.ram_pct + '%';
  document.getElementById('bb-jawl-uptime').textContent = d.jawl_uptime && d.jawl_uptime !== '—' ? 'Jinx: ♥ ' + d.jawl_uptime : '';

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
    let html = '<h3 style="color:var(--overlay0);font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;position:sticky;top:0;background:var(--crust);padding:2px 0;">💡 Мысли <span style="color:var(--overlay0);font-size:9px;">(кликни чтобы развернуть)</span></h3>';
    for (let t of thoughts) {
      let isExpanded = expandedThoughts.has(t.id);
      let cls = isExpanded ? 'thought-item expanded' : 'thought-item';
      html += '<div class="' + cls + '" data-id="' + t.id + '" onclick="toggleThought(this)" title="Кликни для ' + (isExpanded ? 'сворачивания' : 'разворачивания') + '">';
      html += '<div class="thought-ts">' + (t.time_formatted || t.ts || '') + '</div>';
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
    let html = '<h3 style="color:var(--overlay0);font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;position:sticky;top:0;background:var(--crust);padding:2px 0;">📜 Логи</h3>';
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
    // If current_provider is null, default to first available provider
    if (!currentProviderId && Object.keys(PROVIDERS_DATA).length > 0) {
      currentProviderId = Object.keys(PROVIDERS_DATA)[0];
    }
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
      btn.style.color = d.enabled ? "var(--green)" : "var(--red)";
      btn.style.borderColor = d.enabled ? "var(--green)" : "var(--red)";
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
      document.getElementById("ticksInput").style.borderColor = "var(--green)";
      setTimeout(() => document.getElementById("ticksInput").style.borderColor = "var(--surface1)", 1000);
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

// JAWL Uptime — bottom-right corner
function formatUptime(seconds) {
    if (seconds < 60) return seconds + " сек";
    if (seconds < 3600) return Math.floor(seconds / 60) + " мин";
    let h = Math.floor(seconds / 3600);
    let m = Math.floor((seconds % 3600) / 60);
    return h + " ч " + m + " мин";
}

async function loadJawlUptime() {
    try {
        let r = await fetch('/api/uptime');
        let d = await r.json();
        if (d.error) {
            // Fallback: try to get jawl_uptime from /api/status
            try {
                let r2 = await fetch('/api/status');
                let d2 = await r2.json();
                if (d2.jawl_uptime && d2.jawl_uptime !== '—') {
                    document.getElementById('bb-jawl-uptime').textContent = 'Jinx: ♥ ' + d2.jawl_uptime;
                } else {
                    document.getElementById('bb-jawl-uptime').textContent = 'Jinx: ♥ ' + d2.jawl_uptime;
                }
            } catch(e2) {
                document.getElementById('bb-jawl-uptime').textContent = 'Jinx: ♥ ?';
            }
            return;
        }
        let uptime = formatUptime(d.uptime_seconds);
        document.getElementById('bb-jawl-uptime').textContent = 'Jinx: ♥ ' + uptime;
    } catch(e) {
        document.getElementById('bb-jawl-uptime').textContent = '';
    }
}
loadJawlUptime();
setInterval(loadJawlUptime, 30000);
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
    ctx.strokeStyle = 'var(--surface0)'; ctx.lineWidth = 1;
    for (let i = 0; i < 4; i++) {
      let y = h * i / 3;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    ctx.strokeStyle = '#a6e3a1'; ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < d.data.length; i++) {
      let x = i * step, y = h - (d.data[i] / max) * (h - 10) - 5;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.fillStyle = '#a6e3a1';
    for (let i = 0; i < d.data.length; i++) {
      let x = i * step, y = h - (d.data[i] / max) * (h - 10) - 5;
      ctx.beginPath(); ctx.arc(x, y, 2, 0, 6.28); ctx.fill();
    }
    ctx.fillStyle = '#6c7086'; ctx.font = '9px monospace';
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
        list.innerHTML = '<div style="color: var(--subtext0); text-align: center; padding: 20px;">No entries found</div>';
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
