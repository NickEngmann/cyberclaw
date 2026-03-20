"""Nightcrawler Web UI — hacker terminal dashboard.

Serves a real-time dashboard showing agent status, command feed,
findings, and mission progress. Accessible from phone browser
or remotely via Tailscale IP.
"""

import json
import os
import time
import threading
from flask import Flask, render_template, jsonify, Response, request

# Try to use SQLite backend
try:
    from agent import db as _db
    _HAS_DB = True
    # Auto-init DB if not already initialized
    _db_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    if not _db.DB_PATH:
        _db.init_db(_db_dir)
except ImportError:
    _HAS_DB = False

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
    """Return detailed findings — SQLite first, JSON fallback."""
    if _HAS_DB and _db.DB_PATH:
        try:
            return jsonify(_db.get_findings_summary())
        except Exception:
            pass
    # Fallback to JSON file
    log_dir = os.environ.get("NC_LOG_DIR",
                             os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs"))
    findings_path = os.path.join(log_dir, "findings.json")
    if os.path.exists(findings_path):
        try:
            with open(findings_path) as f:
                return jsonify(json.load(f))
        except (json.JSONDecodeError, IOError):
            pass
    return jsonify({})


@app.route("/api/commands")
def api_commands():
    """Return recent commands — SQLite first, JSONL fallback."""
    if _HAS_DB and _db.DB_PATH:
        try:
            return jsonify(_db.get_commands(100))
        except Exception:
            pass
    # Fallback
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
    return jsonify(commands[-100:])


@app.route("/api/timeline")
def api_timeline():
    """Return recent timeline entries."""
    if _HAS_DB and _db.DB_PATH:
        try:
            return jsonify(_db.get_timeline(50))
        except Exception:
            pass
    # Fallback
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


@app.route("/api/host/<ip>/history")
def api_host_history(ip):
    """Return scan history for a specific host."""
    if _HAS_DB and _db.DB_PATH:
        try:
            return jsonify(_db.get_host_history(ip, limit=20))
        except Exception:
            pass
    return jsonify([])


@app.route("/api/export")
@app.route("/api/export/<path:network>")
def api_export(network=None):
    """Export all data for Thor consumption. Optionally filter by network."""
    if _HAS_DB and _db.DB_PATH:
        try:
            return jsonify(_db.export_network(network))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "SQLite not available"}), 500


@app.route("/api/networks")
def api_networks():
    """Return list of discovered networks."""
    if _HAS_DB and _db.DB_PATH:
        try:
            nets = _db.get_networks()
            # Attach notes from state
            net_notes = _db.get_state("network_notes", {})
            net_names = _db.get_state("network_names", {})
            for n in nets:
                nid = n.get("network_id", "")
                n["display_name"] = net_names.get(nid, "") or n.get("ssid", "") or n.get("cidr", "")
                n["notes"] = net_notes.get(nid, "")
            return jsonify(nets)
        except Exception:
            pass
    return jsonify([])


@app.route("/api/networks/<network_id>", methods=["PATCH"])
def api_edit_network(network_id):
    """Edit network name and notes."""
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"error": "DB not available"}), 500
    data = request.json or {}
    name = data.get("name")
    notes = data.get("notes")

    if name is not None:
        names = _db.get_state("network_names", {})
        names[network_id] = name
        _db.set_state("network_names", names)

    if notes is not None:
        net_notes = _db.get_state("network_notes", {})
        net_notes[network_id] = notes
        _db.set_state("network_notes", net_notes)

    return jsonify({"ok": True, "network_id": network_id})


# ── C2 Control Endpoints ──────────────────────────────────


# 1. Star/Prioritize Hosts
@app.route("/api/hosts/star", methods=["POST"])
def api_star_host():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"error": "DB not available"}), 503
    data = request.get_json(force=True)
    mac = data.get("mac", "")
    iterations = data.get("iterations", 5)
    if not mac:
        return jsonify({"error": "mac required"}), 400

    # Look up IP for this MAC
    starred = _db.get_state("starred_hosts", [])
    # Remove existing entry for this MAC (re-star updates iterations)
    starred = [s for s in starred if s.get("mac") != mac]

    # Resolve IP from hosts table
    ip = ""
    try:
        hosts = _db.get_hosts()
        for h in hosts:
            if h.get("mac") == mac:
                ip = h.get("ip", "")
                break
    except Exception:
        pass

    starred.append({"mac": mac, "ip": ip, "remaining": iterations})
    _db.set_state("starred_hosts", starred)
    return jsonify({"ok": True, "starred": starred})


@app.route("/api/hosts/star", methods=["DELETE"])
def api_unstar_host():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"error": "DB not available"}), 503
    data = request.get_json(force=True)
    mac = data.get("mac", "")
    if not mac:
        return jsonify({"error": "mac required"}), 400

    starred = _db.get_state("starred_hosts", [])
    starred = [s for s in starred if s.get("mac") != mac]
    _db.set_state("starred_hosts", starred)
    return jsonify({"ok": True, "starred": starred})


@app.route("/api/hosts/starred")
def api_starred_hosts():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify([])
    return jsonify(_db.get_state("starred_hosts", []))


# 1b. Blacklist Hosts
@app.route("/api/hosts/blacklist", methods=["POST"])
def api_blacklist_host():
    """Blacklist a host — agent will skip it entirely."""
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"error": "DB not available"}), 503
    data = request.get_json(force=True)
    mac = data.get("mac", "")
    ip = data.get("ip", "")
    if not mac and not ip:
        return jsonify({"error": "mac or ip required"}), 400

    blacklist = _db.get_state("blacklisted_hosts", [])
    # Don't add duplicates
    if not any(b.get("mac") == mac for b in blacklist):
        blacklist.append({"mac": mac, "ip": ip})
        _db.set_state("blacklisted_hosts", blacklist)
    return jsonify({"ok": True, "blacklisted": blacklist})


@app.route("/api/hosts/blacklist", methods=["DELETE"])
def api_unblacklist_host():
    """Remove a host from blacklist."""
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"error": "DB not available"}), 503
    data = request.get_json(force=True)
    mac = data.get("mac", "")
    blacklist = _db.get_state("blacklisted_hosts", [])
    blacklist = [b for b in blacklist if b.get("mac") != mac]
    _db.set_state("blacklisted_hosts", blacklist)
    return jsonify({"ok": True, "blacklisted": blacklist})


@app.route("/api/hosts/blacklisted")
def api_blacklisted_hosts():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify([])
    return jsonify(_db.get_state("blacklisted_hosts", []))


# 2. Force Phase Change
@app.route("/api/phase", methods=["POST"])
def api_force_phase():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"error": "DB not available"}), 503
    data = request.get_json(force=True)
    phase = data.get("phase", "").lower()
    valid = ["recon", "enumerate", "exploit", "report", "cleanup"]
    if phase not in valid:
        return jsonify({"error": f"Invalid phase. Valid: {valid}"}), 400
    _db.set_state("forced_phase", phase)
    return jsonify({"ok": True, "forced_phase": phase})


# 3. Pause/Resume Agent
@app.route("/api/agent/pause", methods=["POST"])
def api_pause():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"error": "DB not available"}), 503
    _db.set_state("paused", True)
    return jsonify({"ok": True, "paused": True})


@app.route("/api/agent/resume", methods=["POST"])
def api_resume():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"error": "DB not available"}), 503
    _db.set_state("paused", False)
    return jsonify({"ok": True, "paused": False})


@app.route("/api/agent/status")
def api_agent_status():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"paused": False, "kill_switch": False})
    return jsonify({
        "paused": _db.get_state("paused", False),
        "kill_switch": _db.get_state("kill_switch", False),
    })


# 4. Manual Command Queue
@app.route("/api/commands/queue", methods=["POST"])
def api_queue_command():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"error": "DB not available"}), 503
    data = request.get_json(force=True)
    cmd = data.get("command", "").strip()
    if not cmd:
        return jsonify({"error": "command required"}), 400
    queue = _db.get_state("command_queue", [])
    queue.append(cmd)
    _db.set_state("command_queue", queue)
    return jsonify({"ok": True, "queue": queue})


@app.route("/api/commands/queue", methods=["GET"])
def api_get_queue():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify([])
    return jsonify(_db.get_state("command_queue", []))


# 5. Tool Preferences
@app.route("/api/tools/preferences", methods=["POST"])
def api_set_tool_prefs():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"error": "DB not available"}), 503
    data = request.get_json(force=True)
    prefs = {
        "disabled": data.get("disabled", []),
        "preferred": data.get("preferred", []),
    }
    _db.set_state("tool_preferences", prefs)
    return jsonify({"ok": True, "preferences": prefs})


@app.route("/api/tools/preferences", methods=["GET"])
def api_get_tool_prefs():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"disabled": [], "preferred": []})
    return jsonify(_db.get_state("tool_preferences",
                                 {"disabled": [], "preferred": []}))


# 6. Host Notes
@app.route("/api/hosts/<mac>/notes", methods=["POST"])
def api_set_host_note(mac):
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"error": "DB not available"}), 503
    data = request.get_json(force=True)
    note = data.get("note", "")
    notes = _db.get_state("host_notes", {})
    notes[mac] = note
    _db.set_state("host_notes", notes)
    return jsonify({"ok": True, "mac": mac, "note": note})


@app.route("/api/hosts/<mac>/notes", methods=["GET"])
def api_get_host_note(mac):
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"note": ""})
    notes = _db.get_state("host_notes", {})
    return jsonify({"note": notes.get(mac, "")})


# 7. Kill Switch
@app.route("/api/killswitch", methods=["POST"])
def api_killswitch():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"error": "DB not available"}), 503
    _db.set_state("kill_switch", True)
    _db.set_state("paused", True)
    return jsonify({"ok": True, "kill_switch": True})


# 8. Agent Config
@app.route("/api/config", methods=["GET"])
def api_get_config():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"temperature": 0.7, "max_tokens": 512})
    defaults = {"temperature": 0.7, "max_tokens": 512}
    return jsonify(_db.get_state("agent_config", defaults))


@app.route("/api/config", methods=["PATCH"])
def api_patch_config():
    if not _HAS_DB or not _db.DB_PATH:
        return jsonify({"error": "DB not available"}), 503
    data = request.get_json(force=True)
    config = _db.get_state("agent_config",
                           {"temperature": 0.7, "max_tokens": 512})
    for k, v in data.items():
        config[k] = v
    _db.set_state("agent_config", config)
    return jsonify({"ok": True, "config": config})


# 9. Command History Search
@app.route("/api/commands/search")
def api_search_commands():
    q = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 50)), 500)
    if not q:
        return jsonify([])

    if _HAS_DB and _db.DB_PATH:
        try:
            from agent.db import _get_conn
            conn = _get_conn()
            rows = conn.execute(
                "SELECT timestamp, command, allowed, reason, result_status "
                "FROM commands WHERE command LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{q}%", limit)
            ).fetchall()
            results = [dict(r) for r in reversed(rows)]

            # Also search timeline
            tl_rows = conn.execute(
                "SELECT timestamp, reasoning, command, status, output_preview "
                "FROM timeline WHERE command LIKE ? OR reasoning LIKE ? "
                "ORDER BY id DESC LIMIT ?",
                (f"%{q}%", f"%{q}%", limit)
            ).fetchall()
            tl_results = [dict(r) for r in reversed(tl_rows)]

            return jsonify({"commands": results, "timeline": tl_results})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Fallback: search JSONL files
    log_dir = os.environ.get("NC_LOG_DIR",
                             os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs"))
    results = []
    cmds_path = os.path.join(log_dir, "commands.jsonl")
    if os.path.exists(cmds_path):
        with open(cmds_path) as f:
            for line in f:
                if q.lower() in line.lower():
                    try:
                        results.append(json.loads(line.strip()))
                    except json.JSONDecodeError:
                        pass
    return jsonify({"commands": results[-limit:], "timeline": []})


# 10. Network Info
@app.route("/api/network/current")
def api_network_current():
    if _HAS_DB and _db.DB_PATH:
        try:
            nets = _db.get_networks()
            if nets:
                # Most recently seen network
                latest = max(nets, key=lambda n: n.get("last_seen", ""))
                info = {
                    "cidr": latest.get("cidr", ""),
                    "ssid": latest.get("ssid", ""),
                    "gateway": latest.get("gateway", ""),
                    "first_seen": latest.get("first_seen", ""),
                    "last_seen": latest.get("last_seen", ""),
                }
                # Uptime from agent start
                wifi = _db.get_state("wifi_connected", False)
                info["wifi_connected"] = wifi

                # Try to get public IP from state
                info["public_ip"] = _db.get_state("public_ip", "unknown")
                return jsonify(info)
        except Exception:
            pass
    return jsonify({"cidr": "", "ssid": "", "gateway": "",
                    "wifi_connected": False, "public_ip": "unknown"})


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
