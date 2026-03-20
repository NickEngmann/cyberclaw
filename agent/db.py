"""SQLite-backed persistent storage for findings, timeline, and commands.

Replaces JSON/JSONL files with a single nightcrawler.db that the OS
pages efficiently. All processes can read/write concurrently via WAL mode.
"""

import sqlite3
import json
import os
import time
import threading

_local = threading.local()
DB_PATH = None


def init_db(log_dir: str):
    """Initialize the database. Call once at startup."""
    global DB_PATH
    os.makedirs(log_dir, exist_ok=True)
    DB_PATH = os.path.join(log_dir, "nightcrawler.db")
    conn = _get_conn()
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE IF NOT EXISTS hosts (
            ip TEXT PRIMARY KEY,
            ports TEXT DEFAULT '[]',
            info TEXT DEFAULT '',
            first_seen TEXT,
            last_seen TEXT
        );

        CREATE TABLE IF NOT EXISTS credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT,
            username TEXT,
            password TEXT,
            host TEXT,
            timestamp TEXT
        );

        CREATE TABLE IF NOT EXISTS vulnerabilities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host TEXT,
            service TEXT,
            vuln TEXT,
            severity TEXT DEFAULT 'medium',
            timestamp TEXT
        );

        CREATE TABLE IF NOT EXISTS timeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            reasoning TEXT,
            command TEXT,
            status TEXT,
            output_preview TEXT
        );

        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            command TEXT,
            allowed INTEGER,
            reason TEXT,
            result_status TEXT
        );

        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()


def _get_conn() -> sqlite3.Connection:
    """Get a thread-local connection."""
    if not hasattr(_local, 'conn') or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, timeout=10)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Hosts ────────────────────────────────────────────

def upsert_host(ip: str, ports: list = None, info: str = ""):
    """Insert or update a host, merging ports."""
    conn = _get_conn()
    existing = conn.execute("SELECT ports, info FROM hosts WHERE ip=?", (ip,)).fetchone()
    if existing:
        old_ports = json.loads(existing["ports"])
        merged = list(set(old_ports + (ports or [])))
        merged.sort()
        new_info = info if info and len(info) > len(existing["info"]) else existing["info"]
        conn.execute(
            "UPDATE hosts SET ports=?, info=?, last_seen=? WHERE ip=?",
            (json.dumps(merged), new_info, _ts(), ip)
        )
    else:
        conn.execute(
            "INSERT INTO hosts (ip, ports, info, first_seen, last_seen) VALUES (?,?,?,?,?)",
            (ip, json.dumps(ports or []), info, _ts(), _ts())
        )
    conn.commit()


def get_hosts() -> list:
    conn = _get_conn()
    rows = conn.execute("SELECT ip, ports, info FROM hosts ORDER BY ip").fetchall()
    return [{"ip": r["ip"], "ports": json.loads(r["ports"]), "info": r["info"]} for r in rows]


def get_host_count() -> int:
    conn = _get_conn()
    return conn.execute("SELECT COUNT(*) FROM hosts").fetchone()[0]


def get_total_ports() -> int:
    conn = _get_conn()
    rows = conn.execute("SELECT ports FROM hosts").fetchall()
    return sum(len(json.loads(r["ports"])) for r in rows)


# ── Credentials ──────────────────────────────────────

def add_credential(service: str, username: str, password: str, host: str = ""):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO credentials (service, username, password, host, timestamp) VALUES (?,?,?,?,?)",
        (service, username, password, host, _ts())
    )
    conn.commit()


def get_credentials() -> list:
    conn = _get_conn()
    rows = conn.execute("SELECT service, username, password, host, timestamp FROM credentials").fetchall()
    return [dict(r) for r in rows]


def get_cred_count() -> int:
    conn = _get_conn()
    return conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0]


# ── Vulnerabilities ──────────────────────────────────

def add_vulnerability(host: str, service: str, vuln: str, severity: str = "medium"):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO vulnerabilities (host, service, vuln, severity, timestamp) VALUES (?,?,?,?,?)",
        (host, service, vuln, severity, _ts())
    )
    conn.commit()


def get_vulnerabilities() -> list:
    conn = _get_conn()
    rows = conn.execute("SELECT host, service, vuln, severity, timestamp FROM vulnerabilities").fetchall()
    return [dict(r) for r in rows]


def get_vuln_count() -> int:
    conn = _get_conn()
    return conn.execute("SELECT COUNT(*) FROM vulnerabilities").fetchone()[0]


# ── Timeline ─────────────────────────────────────────

def add_timeline(reasoning: str = None, command: str = None,
                 status: str = None, output_preview: str = None):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO timeline (timestamp, reasoning, command, status, output_preview) VALUES (?,?,?,?,?)",
        (_ts(), reasoning, command, status, (output_preview or "")[:500])
    )
    conn.commit()


def add_timeline_event(event: str, error: str = None):
    """Log a non-command event (errors, phase transitions, etc.)."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO timeline (timestamp, reasoning, command, status, output_preview) VALUES (?,?,?,?,?)",
        (_ts(), f"[{event}]", None, "event", error or "")
    )
    conn.commit()


def get_timeline(limit: int = 50) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT timestamp, reasoning, command, status, output_preview "
        "FROM timeline ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_timeline_count() -> int:
    conn = _get_conn()
    return conn.execute("SELECT COUNT(*) FROM timeline").fetchone()[0]


# ── Commands (audit log) ─────────────────────────────

def add_command_log(command: str, allowed: bool, reason: str,
                    result_status: str = None):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO commands (timestamp, command, allowed, reason, result_status) VALUES (?,?,?,?,?)",
        (_ts(), command, int(allowed), reason, result_status)
    )
    conn.commit()


def get_commands(limit: int = 100) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT timestamp, command, allowed, reason, result_status "
        "FROM commands ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


# ── State (key-value) ────────────────────────────────

def set_state(key: str, value):
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        (key, json.dumps(value))
    )
    conn.commit()


def get_state(key: str, default=None):
    conn = _get_conn()
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


# ── Findings summary (replaces findings.json) ────────

def get_findings_summary() -> dict:
    """Get the full findings dict — compatible with the old JSON format."""
    return {
        "wifi_connected": get_state("wifi_connected", False),
        "live_hosts": get_host_count(),
        "open_ports": get_total_ports(),
        "credentials": get_cred_count(),
        "vulnerabilities": get_vuln_count(),
        "impact_documented": get_state("impact_documented", False),
        "cleanup_done": get_state("cleanup_done", False),
        "hosts": get_hosts(),
        "creds": get_credentials(),
        "vulns": get_vulnerabilities(),
    }


# ── Migration: import existing JSON/JSONL files ─────

def migrate_from_files(log_dir: str):
    """One-time import of existing log files into SQLite."""
    import os

    # Import findings.json
    findings_path = os.path.join(log_dir, "findings.json")
    if os.path.exists(findings_path):
        try:
            with open(findings_path) as f:
                data = json.load(f)
            if data.get("wifi_connected"):
                set_state("wifi_connected", True)
            for h in data.get("hosts", []):
                upsert_host(h["ip"], h.get("ports", []), h.get("info", ""))
            for c in data.get("creds", []):
                add_credential(c["service"], c["username"], c["password"], c.get("host", ""))
            for v in data.get("vulns", []):
                add_vulnerability(v["host"], v["service"], v["vuln"], v.get("severity", "medium"))
        except (json.JSONDecodeError, IOError, KeyError):
            pass

    # Import timeline.jsonl
    timeline_path = os.path.join(log_dir, "timeline.jsonl")
    if os.path.exists(timeline_path):
        try:
            with open(timeline_path) as f:
                for line in f:
                    try:
                        d = json.loads(line.strip())
                        if d.get("command"):
                            add_timeline(
                                reasoning=d.get("reasoning"),
                                command=d.get("command"),
                                status=d.get("status"),
                                output_preview=d.get("output_preview"),
                            )
                        elif d.get("event"):
                            add_timeline_event(d["event"], d.get("error"))
                    except (json.JSONDecodeError, KeyError):
                        pass
        except IOError:
            pass
