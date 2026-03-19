"""Mission log — structured findings and timeline."""

import json
import os
import time


class MissionLog:
    """Tracks findings, timeline, and mission state."""

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.findings_file = os.path.join(log_dir, "findings.json")
        self.timeline_file = os.path.join(log_dir, "timeline.jsonl")

        # In-memory findings state
        self._findings = {
            "wifi_connected": False,
            "live_hosts": 0,
            "open_ports": 0,
            "credentials": 0,
            "vulnerabilities": 0,
            "impact_documented": False,
            "cleanup_done": False,
            "hosts": [],
            "creds": [],
            "vulns": [],
        }
        self._load_existing()

    def record(self, command: str, result: dict, reasoning: str = None):
        """Record a command execution to the timeline."""
        entry = {
            "timestamp": self._ts(),
            "reasoning": reasoning,
            "command": command,
            "status": result.get("status", "unknown"),
            "output_preview": (result.get("output", ""))[:500],
        }
        self._append_timeline(entry)
        self._auto_extract(command, result)

    def record_error(self, error: Exception):
        entry = {
            "timestamp": self._ts(),
            "event": "error",
            "error": str(error),
        }
        self._append_timeline(entry)

    def set_finding(self, key: str, value):
        self._findings[key] = value
        self._save_findings()

    def add_host(self, ip: str, ports: list = None, info: str = ""):
        host = {"ip": ip, "ports": ports or [], "info": info}
        if ip not in [h["ip"] for h in self._findings["hosts"]]:
            self._findings["hosts"].append(host)
            self._findings["live_hosts"] = len(self._findings["hosts"])
            self._save_findings()

    def add_credential(self, service: str, username: str, password: str,
                       host: str = ""):
        cred = {"service": service, "username": username,
                "password": password, "host": host,
                "timestamp": self._ts()}
        self._findings["creds"].append(cred)
        self._findings["credentials"] = len(self._findings["creds"])
        self._save_findings()

    def add_vulnerability(self, host: str, service: str, vuln: str,
                          severity: str = "medium"):
        v = {"host": host, "service": service, "vuln": vuln,
             "severity": severity, "timestamp": self._ts()}
        self._findings["vulns"].append(v)
        self._findings["vulnerabilities"] = len(self._findings["vulns"])
        self._save_findings()

    def get_findings_summary(self) -> dict:
        return dict(self._findings)

    def finalize(self):
        """Mark mission as complete."""
        self._findings["cleanup_done"] = True
        self._findings["finalized_at"] = self._ts()
        self._save_findings()

    def _auto_extract(self, command: str, result: dict):
        """Try to auto-extract findings from command output."""
        output = result.get("output", "")
        status = result.get("status", "")

        # Detect successful wifi connection
        if "wpa_supplicant" in command and status == "success":
            self.set_finding("wifi_connected", True)

        # Detect nmap host discoveries (simple heuristic)
        if "nmap" in command and "open" in output:
            import re
            hosts = re.findall(
                r'Nmap scan report for (\S+)', output
            )
            for h in hosts:
                self.add_host(h)

    def _save_findings(self):
        with open(self.findings_file, "w") as f:
            json.dump(self._findings, f, indent=2)

    def _load_existing(self):
        if os.path.exists(self.findings_file):
            try:
                with open(self.findings_file) as f:
                    self._findings.update(json.load(f))
            except (json.JSONDecodeError, IOError):
                pass

    def _append_timeline(self, entry: dict):
        with open(self.timeline_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    @staticmethod
    def _ts() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
