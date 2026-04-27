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
import yaml

LOG_FILE = "/home/rem/JAWL/logs/system.log"
DB_FILE = "/home/rem/JAWL/src/utils/local/data/sql_db/agent.db"
SETTINGS_PATH = "/home/rem/JAWL/config/settings.yaml"
ENV_PATH = "/home/rem/JAWL/.env"
MAIN_SCRIPT = "/home/rem/JAWL/src/main.py"
PID_FILE = "/tmp/jawl.pid"

PROVIDERS = {
    "minimax": {"name": "MiniMax M2.7", "url": "https://api.minimax.io/v1", "model": "minimax-m2.7", "icon": "M2.7"},
    "glm": {"name": "GLM-5 (Z.AI)", "url": "https://api.z.ai/api/coding/paas/v4", "model": "glm-5", "icon": "GLM"},
}


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

        m = re.search(r'Model:\s*(\S+)', lc)
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

        # Uptime — from last "Инициализация JAWL"
        m_ts = re.match(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', lc)
        if m_ts and 'Инициализация JAWL' in lc:
            try:
                start = datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S")
                now = datetime.now()
                delta = now - start
                hours, rem = divmod(int(delta.total_seconds()), 3600)
                minutes, secs = divmod(rem, 60)
                uptime = f"{hours:02d}:{minutes:02d}:{secs:02d}"
            except Exception:
                pass

    return {
        "status": status,
        "model": model,
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
    """Get thoughts (ticks) from database."""
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
        thoughts.append({
            "id": tid[:8],
            "ts": ts,
            "text": (text or "")[:600],
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



@app.route("/api/providers")
def api_providers():
    current = None
    try:
        with open(SETTINGS_PATH) as f:
            cfg = yaml.safe_load(f)
        m = cfg.get("llm", {}).get("model_name", "")
        for pid, p in PROVIDERS.items():
            if p["model"] == m:
                current = pid
                break
    except:
        pass
    return jsonify({"current": current, "providers": PROVIDERS})


@app.route("/api/switch", methods=["POST"])
def api_switch():
    data = request.get_json()
    pid = data.get("provider")
    if pid not in PROVIDERS:
        return jsonify({"error": "Unknown provider"}), 400
    p = PROVIDERS[pid]

    with open(SETTINGS_PATH) as f:
        cfg = yaml.safe_load(f)
    cfg["llm"]["model_name"] = p["model"]
    with open(SETTINGS_PATH, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    if pid == "minimax":
        key = ("sk-cp-JqkXlcj0NLALaq1zVJM33J4mwMs2U-Lj5lv3y3Ai2WTCDOB-JNwtjxAWeGSP8TL3jmsHVQ4aWS7u6j4uyVz8e9P_iX5gKm0fi1qUpx3npuwLXP1cQ9BQDzg")
    else:
        key = ("c421f4a13d9f4e499a059aa7153280d6.wIYrvNBYbVFWBElS")

    out = []
    has_u = has_k = False
    try:
        with open(ENV_PATH) as f:
            for ln in f:
                if ln.startswith("LLM_API_URL="):
                    out.append('LLM_API_URL="' + p["url"] + '"\n')
                    has_u = True
                elif ln.startswith("LLM_API_KEY_1="):
                    out.append('LLM_API_KEY_1="' + key + '"\n')
                    has_k = True
                else:
                    out.append(ln)
    except:
        pass
    if not has_u:
        out.append('LLM_API_URL="' + p["url"] + '"\n')
    if not has_k:
        out.append('LLM_API_KEY_1="' + key + '"\n')
    with open(ENV_PATH, "w") as f:
        f.writelines(out)

    import subprocess
    try:
        with open(PID_FILE) as f:
            os.kill(int(f.read().strip()), 15)
    except:
        pass
    subprocess.run("pkill -f 'python.*src/main.py' || true", shell=True)
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

    return jsonify({"ok": True, "pid": proc.pid, "provider": pid, "model": p["model"]})


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
    import subprocess
    script = "/home/rem/JAWL/scripts/start-safe.sh"
    try:
        proc = subprocess.run(["bash", script], capture_output=True, text=True, timeout=30)
        return jsonify({"ok": True, "stdout": proc.stdout[-200:], "stderr": proc.stderr[-200:]})
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
h1 .uptime { font-size: 12px; color: #ff69b4; }

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

.grid { display: grid; grid-template-columns: 240px 1fr; gap: 12px; padding: 12px; height: calc(100vh - 50px); overflow: hidden; }
.left { display: flex; flex-direction: column; gap: 8px; overflow-y: auto; }
.right { display: flex; flex-direction: column; gap: 8px; overflow: hidden; }
.card { background: #12121a; border: 1px solid #222; border-radius: 4px; padding: 10px; }
.card:hover { border-color: #ff0080; box-shadow: 0 0 10px rgba(255,0,128,0.3); }
.card h3 { color: #ff69b4; font-size: 14px; margin-bottom: 8px; border-bottom: 1px solid #222; padding-bottom: 6px; }
.log-box { flex: 0 0 160px; overflow-y: auto; background: #0d0d14; border-radius: 4px; padding: 8px; border: 1px solid #222; }
.log-box h3 { color: #ff69b4; font-size: 10px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
.thoughts-box { flex: 1; overflow-y: auto; background: #0d0d14; border-radius: 4px; padding: 8px; border: 1px solid #222; }
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
.log-line { padding: 2px 4px; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
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
</style>
</head>
<body>
<h1>
  <span>⚡ JAWL Dashboard — {{ agent_name }}</span>
  <span class="uptime">⏱ Uptime: {{ uptime }}</span>
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

<script>
// Full left panel refresh
function renderLeftPanel(d) {
  let html = '';

  // Status
  html += '<div class="card"><h3>Статус</h3><div class="val">' + d.status + '</div></div>';

  // Model + Heartbeat
  html += '<div class="card"><h3>Модель</h3><div class="val small">' + d.model + '</div>';
  html += '<div class="provider-btns">';
  html += '<button id="btn-minimax" class="provider-btn" onclick="switchProvider(&#39;minimax&#39;)">M2.7</button>';
  html += '<button id="btn-glm" class="provider-btn" onclick="switchProvider(&#39;glm&#39;)">GLM-5</button>';
  html += '</div>';
  html += '<div class="provider-btns" style="margin-top:4px">';
  html += '<button id="btn-toolchoice" class="provider-btn" onclick="toggleToolChoice()">🔧 Tools: ON</button>';
  html += '<span id="missedTools" style="color:#ff69b4;font-size:11px;margin-left:8px"></span>';
  html += '</div>';
  html += '<div id="providerStatus" class="provider-status"></div>';
  html += '<div class="provider-btns" style="margin-top:4px">';
  html += '<span style="color:#888;font-size:11px">Тиков:</span>';
  html += '<input id="ticksInput" type="number" min="1" max="30" value="' + (d.ticks_limit||15) + '" style="width:40px;background:#1a1a2e;color:#ff69b4;border:1px solid #333;padding:2px 4px;border-radius:3px;font-size:12px;text-align:center">';
  html += '<button class="provider-btn" onclick="applyTicks()" style="padding:2px 8px;font-size:11px">OK</button>';
  html += '</div>';
  html += '<div class="provider-btns" style="margin-top:4px">';
  html += '<button class="provider-btn" onclick="restartJAWL()" style="border-color:#ff4444;color:#ff4444">🔄 Restart</button>';
  html += '</div>';
  html += '</div>';
  html += '<div class="card"><h3>Heartbeat</h3><div class="val" style="font-size:16px">' + d.heartbeat + '</div></div>';

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
    for (let t of d.tasks) html += '<div class="task-item">' + (t.description || '').substring(0, 60) + '</div>';
  } else {
    html += '<div class="no-data">Нет активных задач</div>';
  }
  html += '</div>';

  // Resources
  let cpuCls = d.resources.cpu > 60 ? 'warn' : '';
  let ramCls = d.resources.ram_pct > 80 ? 'warn' : '';
  html += '<div class="card"><h3>🧮 Ресурсы</h3>';
  html += '<div class="res-row"><span class="res-label">CPU</span><span class="res-val ' + cpuCls + '">' + d.resources.cpu + '%</span></div>';
  html += '<div class="res-row"><span class="res-label">RAM</span><span class="res-val ' + ramCls + '">' + d.resources.ram_pct + '% (' + d.resources.ram_str + ')</span></div>';
  html += '</div>';

  // Crypto Ticker
  html += '<div class="card"><h3>📈 Crypto</h3><div id="cryptoBox"><div class="no-data">Загрузка...</div></div></div>';

  // Activity chart placeholder
  html += '<div class="card" style="flex:0 0 auto"><h3>ReAct шагов/мин</h3><canvas id="chart" width="220" height="80"></canvas></div>';

  // Errors
  if (d.errors && d.errors.length) {
    html += '<div class="error-box"><h3>❗ Ошибки</h3><div class="errors">';
    for (let e of d.errors) html += e + '<br>';
    html += '</div></div>';
  }

  document.getElementById('leftPanel').innerHTML = html;
  // Redraw chart after DOM update
  drawChart();
}

async function loadLeftPanel() {
  try {
    let r = await fetch('/api/status');
    let d = await r.json();
    renderLeftPanel(d);
  } catch(e) { console.error(e); }
}

// Live thought refresh
async function loadThoughts() {
  try {
    let r = await fetch('/api/thoughts');
    let thoughts = await r.json();
    let box = document.getElementById('thoughtsBox');
    let html = '<h3 style="color:#555;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;position:sticky;top:0;background:#12121a;padding:2px 0;">💡 Мысли</h3>';
    for (let t of thoughts) {
      html += '<div class="thought-item"><div class="thought-ts">' + t.ts + '</div><div class="thought-text">' + t.text + '</div></div>';
    }
    box.innerHTML = html;
  } catch(e) { console.error(e); }
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
    if (!coins.length) { box.innerHTML = '<div class="no-data">Нет данных</div>'; return; }
    let html = '';
    for (let c of coins) {
      let chCls = c.change_24h >= 0 ? 'up' : 'down';
      let chSign = c.change_24h >= 0 ? '+' : '';
      let priceStr = c.price >= 1000 ? c.price.toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0}) : c.price.toFixed(2);
      html += '<div class="crypto-row">';
      html += '<span class="crypto-name">' + c.icon + ' ' + c.name + '</span>';
      html += '<span><span class="crypto-price">$' + priceStr + '</span>';
      html += '<span class="crypto-change ' + chCls + '">' + chSign + c.change_24h.toFixed(2) + '%</span></span>';
      html += '</div>';
    }
    html += '<div class="crypto-updated">Updated: ' + new Date().toLocaleTimeString() + '</div>';
    box.innerHTML = html;
  } catch(e) { console.error('Crypto fetch error:', e); }
}

loadCrypto();
loadThoughts();
loadLogs();
loadLeftPanel();
loadProviders();
loadToolChoice();
setInterval(loadToolChoice, 10000);
setInterval(loadCrypto, 60000);
async function loadProviders() {
  try {
    let r = await fetch("/api/providers");
    let d = await r.json();
    updateProviderUI(d.current);
  } catch(e) { console.error(e); }
}

function updateProviderUI(current) {
  let bm = document.getElementById("btn-minimax");
  let bg = document.getElementById("btn-glm");
  if (!bm || !bg) { console.warn("Provider buttons not yet rendered"); return; }
  bm.style.color = current=="minimax" ? "#00ff88" : "#555";
  bm.style.borderColor = current=="minimax" ? "#00ff88" : "#333";
  bg.style.color = current=="glm" ? "#00ff88" : "#555";
  bg.style.borderColor = current=="glm" ? "#00ff88" : "#333";
  document.getElementById("providerStatus").textContent = current ? PROVIDERS[current].name + " selected" : "";
}

async function switchProvider(pid) {
  let btn = document.getElementById("btn-"+pid);
  btn.textContent = "...";
  btn.disabled = true;
  document.getElementById("providerStatus").textContent = "Switching...";
  try {
    let r = await fetch("/api/switch", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({provider: pid})
    });
    let d = await r.json();
    if (d.ok) {
      document.getElementById("providerStatus").textContent = "Restarted! PID:"+d.pid;
      updateProviderUI(pid);
      setTimeout(function() { location.reload(); }, 3000);
    } else {
      document.getElementById("providerStatus").textContent = "Error:"+(d.error||"?");
    }
  } catch(e) {
    document.getElementById("providerStatus").textContent = "Error:"+e;
  }
  btn.disabled = false;
  btn.textContent = pid=="minimax" ? "M2.7" : "GLM-5";
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
    if (miss && d.missed_30min > 0) miss.textContent = "⏭ " + d.missed_30min + " missed/30m";
    else if (miss) miss.textContent = "";
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

async function restartJAWL() {
  let btn = event.target;
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
setInterval(loadLeftPanel, 3000);

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
drawChart();
setInterval(drawChart, 5000);
</script>
</body>
</html>
'''

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
