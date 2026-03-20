"""Mission log — structured findings and timeline.

Uses SQLite for persistent storage with MAC-keyed hosts
and multi-network support.
"""

import json
import os
import re
import subprocess
import time

from agent import db


def _detect_network() -> str:
    """Detect current network CIDR from ip route or scope config."""
    try:
        result = subprocess.run(
            ["ip", "addr", "show", "wlan0"],
            capture_output=True, text=True, timeout=5,
        )
        match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)/(\d+)', result.stdout)
        if match:
            import ipaddress
            net = ipaddress.ip_network(f"{match.group(1)}/{match.group(2)}", strict=False)
            return str(net)
    except Exception:
        pass
    return "192.168.1.0/24"  # fallback


class MissionLog:
    """Tracks findings, timeline, and mission state via SQLite."""

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        self.network = _detect_network()
        db.init_db(log_dir)
        db.upsert_network(self.network)

        # Migrate existing JSON files into SQLite (one-time)
        findings_path = os.path.join(log_dir, "findings.json")
        if os.path.exists(findings_path) and db.get_host_count() == 0:
            # The init_db migration handles this now
            pass

    def record(self, command: str, result: dict, reasoning: str = None):
        output_preview = (result.get("output", ""))[:500]
        status = result.get("status", "unknown")
        db.add_timeline(reasoning=reasoning, command=command,
                        status=status, output_preview=output_preview,
                        network=self.network)
        self._auto_extract(command, result)
        self._write_compat_files(command, result, reasoning)

    def record_error(self, error: Exception):
        db.add_timeline_event("error", str(error), network=self.network)
        self._append_jsonl({
            "timestamp": self._ts(), "event": "error",
            "error": str(error)
        })

    def set_finding(self, key: str, value):
        db.set_state(key, value)
        self._save_findings_json()

    def add_host(self, ip: str, ports: list = None, info: str = "",
                 mac: str = "", hostname: str = ""):
        db.upsert_host(ip=ip, ports=ports, info=info,
                       mac=mac, hostname=hostname, network=self.network)
        self._save_findings_json()

    def add_credential(self, service: str, username: str, password: str,
                       host: str = ""):
        db.add_credential(service, username, password, host,
                          network=self.network)
        self._save_findings_json()

    def add_vulnerability(self, host: str, service: str, vuln: str,
                          severity: str = "medium"):
        db.add_vulnerability(host, service, vuln, severity,
                             network=self.network)
        self._save_findings_json()

    def get_findings_summary(self) -> dict:
        return db.get_findings_summary(network=self.network)

    def finalize(self):
        db.set_state("cleanup_done", True)
        self._save_findings_json()

    def _auto_extract(self, command: str, result: dict):
        """Extract hosts, ports, MACs, hostnames from command output."""
        output = result.get("output", "")
        status = result.get("status", "")

        if "wpa_supplicant" in command and status == "success":
            self.set_finding("wifi_connected", True)

        if "nmap" in command and ("open" in output or "Host is up" in output):
            host_blocks = re.split(r'Nmap scan report for ', output)
            for block in host_blocks[1:]:
                lines = block.strip().split("\n")
                first_line = lines[0]

                # Extract hostname and IP
                # Format: "hostname (192.168.1.2)" or just "192.168.1.2"
                hostname = ""
                ip_match = re.match(r'(\S+)\s+\((\d+\.\d+\.\d+\.\d+)\)', first_line)
                if ip_match:
                    hostname = ip_match.group(1)
                    ip = ip_match.group(2)
                else:
                    ip_match = re.match(r'(\d+\.\d+\.\d+\.\d+)', first_line)
                    if not ip_match:
                        continue
                    ip = ip_match.group(1)

                ports = []
                info_parts = []
                mac = ""
                for line in lines[1:]:
                    port_match = re.match(r'(\d+)/tcp\s+open\s+(\S+)', line)
                    if port_match:
                        ports.append(int(port_match.group(1)))
                        info_parts.append(f"{port_match.group(1)}/{port_match.group(2)}")
                    mac_match = re.match(r'MAC Address:\s+([0-9A-Fa-f:]{17})\s+\((.+?)\)', line)
                    if mac_match:
                        mac = mac_match.group(1).upper()
                        info_parts.append(f"MAC:{mac} ({mac_match.group(2)})")
                info = ", ".join(info_parts) if info_parts else ""
                self.add_host(ip, ports=ports, info=info,
                              mac=mac, hostname=hostname)

    def _save_findings_json(self):
        try:
            path = os.path.join(self.log_dir, "findings.json")
            with open(path, "w") as f:
                json.dump(db.get_findings_summary(self.network), f, indent=2)
        except IOError:
            pass

    def _write_compat_files(self, command, result, reasoning):
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
