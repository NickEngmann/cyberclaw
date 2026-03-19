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
    """Return full current state as JSON."""
    with _state_lock:
        return jsonify(_state)


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
