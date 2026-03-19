"""Nightcrawler Web UI — hacker terminal dashboard.

Serves a real-time dashboard showing agent status, command feed,
findings, and mission progress. Accessible from phone browser
or remotely via Tailscale IP.
"""

import json
import os
import time
import threading
from flask import Flask, render_template, jsonify, Response

app = Flask(__name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
            static_folder=os.path.join(os.path.dirname(__file__), "static"))

# Shared state — updated by the agent loop
_state = {
    "phase": "INIT",
    "mode": "LOCAL",
    "uptime": "00:00:00",
    "watchdog": "08:00:00",
    "thor_online": False,
    "wifi_up": False,
    "commands_total": 0,
    "commands_blocked": 0,
    "errors": 0,
    "iteration": 0,
    "findings": {
        "hosts": 0,
        "ports": 0,
        "creds": 0,
        "vulns": 0,
    },
    "feed": [],        # recent agent actions [{ts, type, content}]
    "hosts": [],       # discovered hosts
    "creds": [],       # found credentials (redacted)
    "vulns": [],       # vulnerabilities
}
_state_lock = threading.Lock()


def update_state(updates: dict):
    """Called by the agent loop to push state updates."""
    with _state_lock:
        for k, v in updates.items():
            if k in _state and isinstance(_state[k], dict) and isinstance(v, dict):
                _state[k].update(v)
            else:
                _state[k] = v


def push_feed(event_type: str, content: str):
    """Push a new event to the live feed."""
    entry = {
        "ts": time.strftime("%H:%M:%S"),
        "type": event_type,
        "content": content,
    }
    with _state_lock:
        _state["feed"].append(entry)
        # Keep last 200 entries
        if len(_state["feed"]) > 200:
            _state["feed"] = _state["feed"][-200:]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    """Return current state — merges in-memory state with disk data."""
    log_dir = os.environ.get("NC_LOG_DIR",
                             os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs"))

    # Read findings from disk
    findings_path = os.path.join(log_dir, "findings.json")
    disk_findings = {}
    if os.path.exists(findings_path):
        try:
            with open(findings_path) as f:
                disk_findings = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    # Read recent timeline entries as feed
    timeline_path = os.path.join(log_dir, "timeline.jsonl")
    feed = []
    if os.path.exists(timeline_path):
        try:
            with open(timeline_path) as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        ts = entry.get("timestamp", "")
                        if ts:
                            ts = ts.split("T")[1].split("Z")[0] if "T" in ts else ts
                        if entry.get("command"):
                            if entry.get("reasoning"):
                                feed.append({"ts": ts, "type": "thought",
                                             "content": entry["reasoning"][:300]})
                            feed.append({"ts": ts, "type": "command",
                                         "content": entry["command"]})
                            status = entry.get("status", "?")
                            preview = entry.get("output_preview", "")[:300]
                            if status == "error":
                                feed.append({"ts": ts, "type": "error",
                                             "content": preview})
                            elif status == "success":
                                feed.append({"ts": ts, "type": "result",
                                             "content": preview})
                        elif entry.get("event"):
                            feed.append({"ts": ts, "type": "thought",
                                         "content": f"[{entry['event']}]"})
                    except json.JSONDecodeError:
                        pass
        except IOError:
            pass

    # Build state from disk + in-memory
    with _state_lock:
        state = dict(_state)

    # Override with disk data (more reliable than in-memory when separate processes)
    state["findings"] = {
        "hosts": disk_findings.get("live_hosts", 0),
        "ports": sum(len(h.get("ports", [])) for h in disk_findings.get("hosts", [])),
        "creds": disk_findings.get("credentials", 0),
        "vulns": disk_findings.get("vulnerabilities", 0),
    }
    state["hosts"] = disk_findings.get("hosts", [])
    state["creds"] = disk_findings.get("creds", [])
    state["vulns"] = disk_findings.get("vulns", [])
    state["wifi_up"] = disk_findings.get("wifi_connected", False)

    # Use disk feed if in-memory is empty
    if not state.get("feed") and feed:
        state["feed"] = feed[-200:]
    elif feed:
        # Merge: use whichever has more entries
        if len(feed) > len(state.get("feed", [])):
            state["feed"] = feed[-200:]

    # Infer phase from disk
    hosts = disk_findings.get("live_hosts", 0)
    creds = disk_findings.get("credentials", 0)
    vulns = disk_findings.get("vulnerabilities", 0)
    if state.get("phase", "INIT") == "INIT":
        if disk_findings.get("wifi_connected"):
            if hosts >= 3:
                state["phase"] = "ENUMERATE"
            elif hosts > 0:
                state["phase"] = "RECON & MAP"
            else:
                state["phase"] = "RECON & MAP"

    # Count commands from log
    cmds_path = os.path.join(log_dir, "commands.jsonl")
    if os.path.exists(cmds_path):
        try:
            with open(cmds_path) as f:
                lines = f.readlines()
            state["commands_total"] = len(lines)
            state["commands_blocked"] = sum(
                1 for l in lines
                if '"allowed": false' in l or '"allowed":false' in l
            )
        except IOError:
            pass

    return jsonify(state)


@app.route("/api/feed")
def api_feed():
    """Return recent feed entries."""
    since = int(float(os.environ.get("feed_since", "0")))
    with _state_lock:
        return jsonify(_state["feed"][since:])


@app.route("/api/stream")
def api_stream():
    """SSE stream for real-time updates."""
    def generate():
        last_len = 0
        while True:
            with _state_lock:
                current_len = len(_state["feed"])
                if current_len > last_len:
                    new_entries = _state["feed"][last_len:]
                    last_len = current_len
                    for entry in new_entries:
                        yield f"data: {json.dumps(entry)}\n\n"
                # Always send heartbeat state
                state_snapshot = {k: v for k, v in _state.items()
                                  if k != "feed"}
                yield f"event: state\ndata: {json.dumps(state_snapshot)}\n\n"
            time.sleep(1)
    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/findings")
def api_findings():
    """Return detailed findings from disk (always fresh)."""
    log_dir = os.environ.get("NC_LOG_DIR",
                             os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs"))
    findings_path = os.path.join(log_dir, "findings.json")
    if os.path.exists(findings_path):
        try:
            with open(findings_path) as f:
                data = json.load(f)
            # Also update in-memory state from disk
            with _state_lock:
                _state["hosts"] = data.get("hosts", [])
                _state["creds"] = data.get("creds", [])
                _state["vulns"] = data.get("vulns", [])
                _state["findings"] = {
                    "hosts": data.get("live_hosts", 0),
                    "ports": data.get("open_ports", 0),
                    "creds": data.get("credentials", 0),
                    "vulns": data.get("vulnerabilities", 0),
                }
            return jsonify(data)
        except (json.JSONDecodeError, IOError):
            pass
    return jsonify({})


@app.route("/api/commands")
def api_commands():
    """Return recent commands from audit log."""
    log_dir = os.environ.get("NC_LOG_DIR",
                             os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs"))
    commands_path = os.path.join(log_dir, "commands.jsonl")
    commands = []
    if os.path.exists(commands_path):
        with open(commands_path) as f:
            for line in f:
                try:
                    commands.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    pass
    # Return last 100
    return jsonify(commands[-100:])


@app.route("/api/timeline")
def api_timeline():
    """Return recent timeline entries (reasoning + command pairs)."""
    log_dir = os.environ.get("NC_LOG_DIR",
                             os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs"))
    timeline_path = os.path.join(log_dir, "timeline.jsonl")
    entries = []
    if os.path.exists(timeline_path):
        with open(timeline_path) as f:
            for line in f:
                try:
                    entries.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    pass
    return jsonify(entries[-50:])


def get_tailscale_ip() -> str:
    """Get the Tailscale interface IP. Returns 127.0.0.1 if not available."""
    import subprocess

    # Method 1: tailscale CLI (works on Android side)
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=5,
        )
        ip = result.stdout.strip()
        if ip:
            return ip
    except Exception:
        pass

    # Method 2: parse ip addr for tailscale0 (works in Kali chroot)
    try:
        result = subprocess.run(
            ["ip", "addr", "show", "tailscale0"],
            capture_output=True, text=True, timeout=5,
        )
        import re
        match = re.search(r'inet (100\.[\d.]+)/\d+', result.stdout)
        if match:
            return match.group(1)
    except Exception:
        pass

    return "127.0.0.1"


def run_webui(port: int = 8888, host: str = None, ssl: bool = True):
    """Start the web UI server in a background thread.

    By default binds to Tailscale IP only (not exposed on target network).
    Falls back to 127.0.0.1 if Tailscale isn't available.
    Pass host="0.0.0.0" to override and listen on all interfaces.
    SSL enabled by default using self-signed cert for Tailscale HTTPS.
    """
    if host is None:
        host = get_tailscale_ip()

    ssl_ctx = None
    if ssl:
        import ssl as _ssl
        cert_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "certs")
        cert_file = os.path.join(cert_dir, "cert.pem")
        key_file = os.path.join(cert_dir, "key.pem")
        if os.path.exists(cert_file) and os.path.exists(key_file):
            ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(cert_file, key_file)

    thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False,
                               use_reloader=False, ssl_context=ssl_ctx),
        daemon=True,
    )
    thread.start()
    proto = "https" if ssl_ctx else "http"
    print(f"WebUI: {proto}://{host}:{port}")
    return thread
