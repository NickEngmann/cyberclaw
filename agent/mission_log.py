"""Mission log — structured findings and timeline.

Uses SQLite for persistent storage. Also writes findings.json and
timeline.jsonl for backward compatibility with the web UI.
"""

import json
import os
import re
import time

from agent import db


class MissionLog:
    """Tracks findings, timeline, and mission state via SQLite."""

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        db.init_db(log_dir)

        # Migrate existing JSON files into SQLite (one-time)
        findings_path = os.path.join(log_dir, "findings.json")
        if os.path.exists(findings_path) and db.get_host_count() == 0:
            db.migrate_from_files(log_dir)

    def record(self, command: str, result: dict, reasoning: str = None):
        """Record a command execution."""
        output_preview = (result.get("output", ""))[:500]
        status = result.get("status", "unknown")
        db.add_timeline(reasoning=reasoning, command=command,
                        status=status, output_preview=output_preview)
        self._auto_extract(command, result)
        # Write compat files
        self._write_compat_files(command, result, reasoning)

    def record_error(self, error: Exception):
        db.add_timeline_event("error", str(error))
        # Append to timeline.jsonl for compat
        self._append_jsonl({
            "timestamp": self._ts(), "event": "error",
            "error": str(error)
        })

    def set_finding(self, key: str, value):
        db.set_state(key, value)
        self._save_findings_json()

    def add_host(self, ip: str, ports: list = None, info: str = ""):
        db.upsert_host(ip, ports, info)
        self._save_findings_json()

    def add_credential(self, service: str, username: str, password: str,
                       host: str = ""):
        db.add_credential(service, username, password, host)
        self._save_findings_json()

    def add_vulnerability(self, host: str, service: str, vuln: str,
                          severity: str = "medium"):
        db.add_vulnerability(host, service, vuln, severity)
        self._save_findings_json()

    def get_findings_summary(self) -> dict:
        return db.get_findings_summary()

    def finalize(self):
        db.set_state("cleanup_done", True)
        self._save_findings_json()

    def _auto_extract(self, command: str, result: dict):
        """Try to auto-extract findings from command output."""
        output = result.get("output", "")
        status = result.get("status", "")

        if "wpa_supplicant" in command and status == "success":
            self.set_finding("wifi_connected", True)

        if "nmap" in command and ("open" in output or "Host is up" in output):
            host_blocks = re.split(r'Nmap scan report for ', output)
            for block in host_blocks[1:]:
                lines = block.strip().split("\n")
                ip_match = re.match(r'(\d+\.\d+\.\d+\.\d+)', lines[0])
                if not ip_match:
                    continue
                ip = ip_match.group(1)
                ports = []
                info_parts = []
                for line in lines[1:]:
                    port_match = re.match(r'(\d+)/tcp\s+open\s+(\S+)', line)
                    if port_match:
                        ports.append(int(port_match.group(1)))
                        info_parts.append(f"{port_match.group(1)}/{port_match.group(2)}")
                    mac_match = re.match(r'MAC Address:\s+(\S+)\s+\((.+?)\)', line)
                    if mac_match:
                        info_parts.append(f"MAC:{mac_match.group(1)} ({mac_match.group(2)})")
                info = ", ".join(info_parts) if info_parts else ""
                self.add_host(ip, ports=ports, info=info)

    def _save_findings_json(self):
        """Write findings.json for backward compat with web UI."""
        try:
            path = os.path.join(self.log_dir, "findings.json")
            with open(path, "w") as f:
                json.dump(db.get_findings_summary(), f, indent=2)
        except IOError:
            pass

    def _write_compat_files(self, command, result, reasoning):
        """Append to timeline.jsonl for backward compat."""
        self._append_jsonl({
            "timestamp": self._ts(),
            "reasoning": reasoning,
            "command": command,
            "status": result.get("status", "unknown"),
            "output_preview": (result.get("output", ""))[:500],
        })

    def _append_jsonl(self, entry: dict):
        try:
            path = os.path.join(self.log_dir, "timeline.jsonl")
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except IOError:
            pass

    @staticmethod
    def _ts() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
