"""Core agent decision loop."""

import asyncio
import re
import time

from agent.llm_client import LLMClient
from agent.planner import PhasePlanner
from agent.context import ContextManager
from agent.watchdog import Watchdog
from agent.mission_log import MissionLog

from agent import db
from agent import host_memory
from agent import training_capture
from agent import cve_db
from agent import output_parser
from agent import attack_planner

try:
    from agent.ui_bridge import update_state, push_feed
except ImportError:
    def update_state(u): pass
    def push_feed(t, c): pass


class AgentLoop:
    """Main decision engine — calls LLM, executes commands through proxy."""

    MAX_CONSECUTIVE_ERRORS = 8
    RETRY_DELAYS = [2, 3, 5, 8, 10, 15, 20, 30]
    HEARTBEAT_INTERVAL = 10
    STUCK_TIMEOUT_SEC = 300  # 5 min without a command = stuck

    def __init__(self, config: dict, llm_client: LLMClient,
                 proxy_url: str, ui):
        self.config = config
        self.llm = llm_client
        self.proxy_url = proxy_url
        self.ui = ui
        self.context = ContextManager(max_tokens=6000)
        self.planner = PhasePlanner(config)
        self.mission_log = MissionLog(
            config.get("logging", {}).get("dir", "logs")
        )
        self.watchdog = Watchdog(0)  # uptime tracker only, no expiry
        self.consecutive_errors = 0
        self.garbage_streak = 0
        self.recent_commands = []  # last N commands for dup detection
        self.last_executed_ip = ""  # for same-host enforcement
        self.last_command_time = time.time()  # for time-based stuck detection
        self.multi_turn_remaining = 0  # allow consecutive cmds on same host
        self.multi_turn_ip = ""
        self.active_playbook = None  # current playbook steps
        self.playbook_step = 0
        self.iteration = 0
        self.total_commands = 0
        self.total_blocked = 0
        self.total_garbage = 0

    async def run(self):
        """Main loop — runs until mission complete or killed."""
        self.ui.render_boot_complete()
        self.watchdog.start()

        # Phase-aware few-shot seed
        from agent.planner import Phase
        if self.planner.current_phase <= Phase.RECON:
            # RECON: teach nmap scanning
            self.context.append_user("Scan the network.")
            self.context.append_assistant(
                "REASONING: Ping sweep for live hosts.\n"
                "COMMAND: nmap -sn -T2 192.168.1.0/24"
            )
            # Run real ping sweep for live host data
            try:
                import subprocess
                sweep = subprocess.run(
                    ["nmap", "-sn", "-T3", "--max-retries", "1", "192.168.1.0/24"],
                    capture_output=True, text=True, timeout=30,
                )
                import re as _re
                live_ips = _re.findall(
                    r'Nmap scan report for (192\.168\.1\.\d+)', sweep.stdout
                )
                excluded = set(self.config["mission"]["scope"].get(
                    "excluded_hosts", []))
                live_ips = [ip for ip in live_ips if ip not in excluded]
                sweep_summary = f"{len(live_ips)} hosts up: " + ", ".join(live_ips[:15])
                if len(live_ips) > 15:
                    sweep_summary += f" (+{len(live_ips)-15} more)"
            except Exception:
                sweep_summary = "4 hosts up: 192.168.1.2, 192.168.1.13, 192.168.1.15, 192.168.1.20"
            self.context.append_user(
                f"[STATUS]: success\n[OUTPUT]:\n{sweep_summary}\n\n"
                "Port-scan each live host. Use: nmap -sS -T2 --top-ports 100 <ip>"
            )
        else:
            # ENUMERATE/EXPLOIT: dynamic seed based on known hosts
            # Pick a tool appropriate to the host's known ports
            import random as _rand
            is_exploit = self.planner.current_phase >= Phase.EXPLOIT
            try:
                hosts_with_ports = [h for h in db.get_hosts()
                                    if len(h.get("ports", [])) > 0]
                if hosts_with_ports:
                    h = _rand.choice(hosts_with_ports)
                    ports = h.get("ports", [])
                    ip = h["ip"]
                    ps = set(ports)

                    # In EXPLOIT phase, 50/50 seed with exploit vs enumerate
                    if is_exploit and _rand.random() < 0.5:
                        # EXPLOIT phase: seed with credential-testing examples
                        if 23 in ps:
                            seed_cmd = f"nxc telnet {ip} -u admin -p admin"
                            seed_out = "TELNET  {ip}:23  admin:admin [+] SUCCESS"
                            seed_reason = f"Test default telnet creds on {ip}."
                        elif 22 in ps:
                            seed_cmd = f"nxc ssh {ip} -u pi -p raspberry"
                            seed_out = "SSH  {ip}:22  pi:raspberry [-] AUTH FAILED"
                            seed_reason = f"Test default SSH creds on {ip}."
                        elif ps & {445, 139}:
                            seed_cmd = f"nxc smb {ip} -u '' -p '' --shares"
                            seed_out = "SMB  {ip}:445  \\\\{ip}\\share READ"
                            seed_reason = f"Try SMB null session on {ip}."
                        elif ps & {80, 8080, 8888}:
                            p = min(ps & {80, 8080, 8888})
                            port_part = "" if p == 80 else f":{p}"
                            seed_cmd = f"curl -s http://{ip}{port_part}/admin/"
                            seed_out = "HTTP/1.1 200 OK\n<title>Admin</title>"
                            seed_reason = f"Check admin panel on {ip}."
                        elif 5900 in ps:
                            seed_cmd = f"nxc vnc {ip} -p password"
                            seed_out = "VNC  {ip}:5900  [-] AUTH FAILED"
                            seed_reason = f"Test VNC default password on {ip}."
                        else:
                            seed_cmd = f"nxc ssh {ip} -u root -p root"
                            seed_out = "SSH  {ip}:22  root:root [-] AUTH FAILED"
                            seed_reason = f"Test default creds on {ip}."
                    else:
                        # ENUMERATE phase: probe services
                        http_ports = ps & {80, 443, 8080, 8443, 8000, 8888, 3000, 9000, 9200}
                        if http_ports:
                            p = min(http_ports)
                            port_part = "" if p in (80, 443) else f":{p}"
                            seed_cmd = f"curl -s -I http://{ip}{port_part}/"
                            seed_out = "HTTP/1.1 200 OK\nServer: nginx/1.18"
                        elif ps & {445, 139}:
                            seed_cmd = f"smbclient -N -L //{ip}/"
                            seed_out = "Sharename  Type  Comment\nshare  Disk"
                        elif 53 in ps:
                            seed_cmd = f"dig @{ip} version.bind chaos txt"
                            seed_out = "status: NOERROR\nversion.bind"
                        elif 22 in ps:
                            seed_cmd = f"nmap -sV -T2 -p 22 {ip}"
                            seed_out = "22/tcp open ssh OpenSSH 9.2p1"
                        elif ps & {3306, 5432, 1433, 27017, 6379}:
                            p = min(ps & {3306, 5432, 1433, 27017, 6379})
                            seed_cmd = f"nmap -sV -T2 -p {p} {ip}"
                            seed_out = f"{p}/tcp open database"
                        elif ps & {2375, 2376}:
                            seed_cmd = f"curl -s http://{ip}:2375/version"
                            seed_out = '{"Version":"20.10.17"}'
                        else:
                            seed_cmd = f"nmap -sV -T2 -p {ports[0]} {ip}"
                            seed_out = f"{ports[0]}/tcp open unknown"
                    seed_reason = f"Probe {ip} port {ports[0]}."
                else:
                    seed_cmd = "curl -s -I http://192.168.1.2/"
                    seed_out = "HTTP/1.1 200 OK\nServer: lighttpd/1.4.53"
                    seed_reason = "Check HTTP on known host."
            except Exception:
                seed_cmd = "curl -s -I http://192.168.1.2/"
                seed_out = "HTTP/1.1 200 OK\nServer: lighttpd/1.4.53"
                seed_reason = "Check HTTP on known host."

            if is_exploit:
                self.context.append_user("Mix exploitation with enumeration. Try creds on known hosts AND scan new ones.")
            else:
                self.context.append_user("Enumerate services on discovered hosts.")
            self.context.append_assistant(
                f"REASONING: {seed_reason}\n"
                f"COMMAND: {seed_cmd}"
            )
            self.context.append_user(
                f"[STATUS]: success\n[OUTPUT]:\n{seed_out}\n\n"
                "Good. Now probe a DIFFERENT host with an appropriate tool."
            )

        # Initial health check
        health = await self.llm.health_check()
        self.ui.update_backend_status(health)

        while not self.planner.mission_complete:
            phase = self.planner.current_phase
            self.ui.update_phase(self.planner.phase_name)
            self.ui.update_stats(
                uptime=self.watchdog.format_elapsed(),
                commands=self.total_commands,
                blocked=self.total_blocked,
                errors=self.consecutive_errors,
            )

            # Push state to web UI
            findings = self.mission_log.get_findings_summary()
            update_state({
                "phase": self.planner.phase_name,
                "mode": "THOR" if self.llm.use_thor else "LOCAL",
                "uptime": self.watchdog.format_elapsed(),
                "thor_online": self.llm.use_thor,
                "wifi_up": findings.get("wifi_connected", False),
                "commands_total": self.total_commands,
                "commands_blocked": self.total_blocked,
                "errors": self.consecutive_errors,
                "iteration": self.iteration,
                "findings": {
                    "hosts": findings.get("live_hosts", 0),
                    "ports": findings.get("open_ports", 0),
                    "creds": findings.get("credentials", 0),
                    "vulns": findings.get("vulnerabilities", 0),
                },
                "hosts": findings.get("hosts", []),
                "creds": findings.get("creds", []),
                "vulns": findings.get("vulns", []),
            })

            try:
                # C2 control state checks
                ctrl = await self._check_control_state()
                if ctrl == "killed":
                    break
                if isinstance(ctrl, tuple) and ctrl[0] == "manual_cmd":
                    # Execute manually injected command
                    manual_cmd = ctrl[1]
                    self.ui.render_command(f"[MANUAL] {manual_cmd}")
                    push_feed("command", f"[MANUAL] {manual_cmd}")
                    result = await self._execute(manual_cmd)
                    self.context.append_tool_result(manual_cmd, result)
                    self.mission_log.record(manual_cmd, result, "[MANUAL]")
                    self.ui.render_result(result)
                    self.total_commands += 1
                    continue

                # Fix #3: Time-based stuck detection — if no command
                # executed in 5 min, force context reset regardless of
                # streak count. Catches all stuck patterns.
                if time.time() - self.last_command_time > self.STUCK_TIMEOUT_SEC:
                    self.ui.render_warning("5min stuck — forcing context reset")
                    push_feed("warning", "Stuck >5min, auto-reset")
                    self._reset_context_with_fewshot()
                    self.last_command_time = time.time()  # prevent rapid-fire resets

                # Build system prompt with phase context
                system = self._build_system_prompt()

                # Add starred host priority to prompt
                starred = db.get_state("starred_hosts", [])
                if starred:
                    priority_ips = [s["ip"] for s in starred if s.get("remaining", 0) > 0]
                    if priority_ips:
                        system += f"\n\nPRIORITY TARGET: Focus on {', '.join(priority_ips[:3])}"

                # Add tool preferences to prompt
                tool_prefs = db.get_state("tool_preferences", {})
                if tool_prefs:
                    disabled = tool_prefs.get("disabled", [])
                    preferred = tool_prefs.get("preferred", [])
                    if disabled:
                        system += f"\nAVOID tools: {', '.join(disabled)}"
                    if preferred:
                        system += f"\nPREFER tools: {', '.join(preferred)}"

                # Add blacklisted hosts to prompt (skip these entirely)
                blacklist = db.get_state("blacklisted_hosts", [])
                if blacklist:
                    bl_ips = [b["ip"] for b in blacklist if b.get("ip")]
                    if bl_ips:
                        system += f"\nBLACKLIST (do NOT scan): {', '.join(bl_ips)}"

                # Add host notes from analyst (context for the model)
                host_notes = db.get_state("host_notes", {})
                if host_notes:
                    notes_text = ""
                    for mac, note in host_notes.items():
                        if note.strip():
                            # Resolve MAC to IP for the model
                            ip = mac  # fallback
                            try:
                                for h in db.get_hosts():
                                    if h["mac"] == mac:
                                        ip = h["ip"]
                                        break
                            except Exception:
                                pass
                            notes_text += f"\n  {ip}: {note[:100]}"
                    if notes_text:
                        system += f"\nANALYST NOTES:{notes_text}"

                # Add host memory (compact — max 200 tokens)
                memory_ctx = host_memory.build_prompt_context(max_tokens=200)
                if memory_ctx:
                    system += f"\n{memory_ctx}"

                # Add network memory (scanned IPs, observations)
                net_id = getattr(self.mission_log, 'network_id', '')
                if net_id:
                    net_ctx = host_memory.build_network_prompt_context(net_id, max_tokens=100)
                    if net_ctx:
                        system += f"\n{net_ctx}"

                # Attack planner — cache plan, refresh every ~50 commands
                if (self.total_commands % 50 == 0 or
                        not hasattr(self, '_cached_plan')):
                    self._cached_plan = attack_planner.generate_plan(max_tokens=150)
                if self._cached_plan:
                    system += f"\n{self._cached_plan}"

                messages = self.context.get_messages()

                # Call LLM
                self.ui.render_thinking()
                response = await self.llm.chat(system, messages)

                # Detect garbage / degenerate output
                if self._is_garbage(response):
                    self.garbage_streak += 1
                    self.total_garbage += 1
                    self.ui.render_warning(
                        f"Garbage output (streak {self.garbage_streak})")
                    push_feed("warning",
                              f"Garbage output #{self.total_garbage}")

                    # After 5 consecutive garbage outputs, full reset
                    if self.garbage_streak >= 5:
                        self.ui.render_warning("5 garbage streak — resetting context")
                        push_feed("warning", "Context reset after 5 garbage")
                        self._reset_context_with_fewshot()
                    else:
                        self.context.append_user(
                            "Your output was malformed. "
                            "Respond ONLY as:\n"
                            "REASONING: [1-2 sentences]\n"
                            "COMMAND: [single linux command]"
                        )
                    continue

                # Good output — reset garbage streak
                self.garbage_streak = 0

                # Parse response
                reasoning = self._parse_reasoning(response)
                command = self._parse_command(response)

                # Always append the assistant response to context
                self.context.append_assistant(response)

                if reasoning:
                    self.ui.render_agent_thought(reasoning)
                    push_feed("thought", reasoning)

                if command:
                    # Skip commands targeting known dead-end hosts
                    import re as _re
                    target_ips = _re.findall(r'192\.168\.1\.\d+', command)
                    if target_ips and all(self._should_skip_host(ip) for ip in target_ips):
                        push_feed("warning", f"Skipped dead-end host: {target_ips[0]}")
                        self.context.append_user(
                            f"{target_ips[0]} is a dead-end (firewalled/down). "
                            "Pick a DIFFERENT, LIVE host. "
                            "REASONING: [text] COMMAND: [command]"
                        )
                        continue

                    # Same-host enforcement — one action per host per turn,
                    # UNLESS multi-turn exploitation is active on this host.
                    if self.last_executed_ip and target_ips:
                        if target_ips[0] == self.last_executed_ip:
                            if (self.multi_turn_remaining > 0 and
                                    target_ips[0] == self.multi_turn_ip):
                                # Multi-turn: allow consecutive on same host
                                self.multi_turn_remaining -= 1
                            else:
                                self.garbage_streak += 1
                                push_feed("warning",
                                          f"Same host rejected: {target_ips[0]}")
                                self.context.append_user(
                                    f"You just scanned {target_ips[0]}. "
                                    "ROTATE to a DIFFERENT host for stealth. "
                                    "REASONING: [text] COMMAND: [different host]"
                                )
                                continue

                    # Validate command before execution
                    if not self._is_valid_command(command):
                        self.garbage_streak += 1
                        push_feed("warning", f"Invalid cmd rejected: {command[:40]}")
                        self.context.append_user(
                            "Invalid command. Use real tools like: nmap, curl, dig, smbclient. "
                            "REASONING: [text] COMMAND: [single valid command]"
                        )
                        continue

                    # Duplicate command detection
                    if self._is_duplicate(command):
                        self.garbage_streak += 1
                        self.ui.render_warning(
                            f"Dup command (streak {self.garbage_streak}): {command[:60]}")
                        push_feed("warning",
                                  f"Dup #{self.garbage_streak}: {command[:40]}")
                        if self.garbage_streak >= 5:
                            self.ui.render_warning("5 dup streak — resetting context")
                            push_feed("warning", "Context reset: dup loop")
                            self._reset_context_with_fewshot()
                        else:
                            self.context.append_user(
                                f"You already ran '{command[:80]}'. "
                                "Try a DIFFERENT approach or tool. "
                                "REASONING: [new analysis] COMMAND: [different command]"
                            )
                        continue

                    self.recent_commands.append(command)
                    if len(self.recent_commands) > 5:
                        self.recent_commands.pop(0)

                    # Fix #4: Deduplicate ports in nmap -p lists
                    if 'nmap' in command and '-p' in command:
                        command = self._dedup_ports(command)

                    self.ui.render_command(command)
                    push_feed("command", command)
                    result = await self._execute(command)
                    # tool_result is appended as a user message,
                    # keeping the alternating user/assistant pattern
                    self.context.append_tool_result(command, result)
                    self.mission_log.record(command, result, reasoning)
                    self.ui.render_result(result)

                    status = result.get("status", "unknown")
                    if status == "blocked":
                        push_feed("blocked", result.get("error", ""))
                    elif status == "error":
                        push_feed("error", result.get("error", ""))
                    else:
                        output = result.get("output", "")
                        preview = output[:800] + "..." if len(output) > 800 else output
                        push_feed("result", preview)

                    self.total_commands += 1
                    self.last_command_time = time.time()
                    if target_ips:
                        self.last_executed_ip = target_ips[0]
                    if result.get("status") == "blocked":
                        self.total_blocked += 1

                    # Capture successful interactions for finetuning
                    if status == "success" and reasoning and command:
                        try:
                            training_capture.capture_successful_interaction(
                                system_prompt=system,
                                messages=messages,
                                response=response,
                                reasoning=reasoning,
                                command=command,
                                result=result,
                                phase=self.planner.phase_name,
                                network_id=getattr(self.mission_log, 'network_id', ''),
                            )
                        except Exception:
                            pass  # never let training capture crash the agent

                    # Auto-extract host observations + track scanned IPs
                    try:
                        import re as _re
                        ips_in_cmd = _re.findall(r'192\.168\.1\.\d+', command)
                        net_id = getattr(self.mission_log, 'network_id', '')
                        for ip in set(ips_in_cmd):
                            # Track IP as scanned at network level
                            if net_id:
                                host_memory.mark_ip_scanned(net_id, ip)
                            # Look up MAC for this IP
                            mac = ""
                            try:
                                for h in db.get_hosts():
                                    if h["ip"] == ip:
                                        mac = h["mac"]
                                        break
                            except Exception:
                                pass
                            if mac:
                                host_memory.auto_extract_observations(
                                    ip, mac, command,
                                    result.get("output", ""),
                                    result.get("status", "")
                                )
                    except Exception:
                        pass

                    # Output parser — extract structured intelligence
                    try:
                        if target_ips:
                            _parse_mac = ""
                            for _h in db.get_hosts():
                                if _h["ip"] == target_ips[0]:
                                    _parse_mac = _h.get("mac", "")
                                    break
                            parsed = output_parser.parse_output(
                                target_ips[0], _parse_mac, command,
                                result.get("output", ""),
                                result.get("status", ""))
                            # Use parser's suggested next command for multi-turn
                            if (parsed.get("next_command") and
                                    self.multi_turn_remaining > 0):
                                # Override the hint with parser's suggestion
                                pass  # hint will be set in context below
                    except Exception:
                        parsed = {}

                    # WiFi connect detection
                    if ("wpa_supplicant" in command or
                            "dhclient" in command):
                        if result.get("status") == "success":
                            self.llm.notify_wifi_connected()
                            self.mission_log.set_finding("wifi_connected", True)

                    output_summary = result.get("output", "")[:500]

                    # Multi-turn: keep context if exploiting a high-priority host
                    if (self.multi_turn_remaining > 0 and
                            target_ips and target_ips[0] == self.multi_turn_ip):
                        # Stay on this host — use playbook step if available
                        next_hint = "Go DEEPER on this same host."
                        if (self.active_playbook and
                                self.playbook_step < len(self.active_playbook.get("steps", []))):
                            self.playbook_step += 1
                            if self.playbook_step < len(self.active_playbook["steps"]):
                                next_cmd = self.active_playbook["steps"][self.playbook_step]
                                next_hint = f"Next step: {next_cmd}"
                        elif parsed and parsed.get("next_command"):
                            next_hint = f"Next step: {parsed['next_command']}"

                        self.context.append_user(
                            f"[RESULT]: {output_summary}\n\n"
                            f"{next_hint} "
                            "REASONING: [text] COMMAND: [next step]"
                        )
                        continue

                    # Mark playbook completion if one was active
                    if self.active_playbook and self.multi_turn_ip:
                        pb_id = self.active_playbook.get("id", "")
                        _pb_mac = ""
                        for _h in db.get_hosts():
                            if _h["ip"] == self.multi_turn_ip:
                                _pb_mac = _h.get("mac", "")
                                break
                        if _pb_mac and pb_id:
                            host_memory.mark_playbook_done(
                                _pb_mac, pb_id, ip=self.multi_turn_ip)
                        self.active_playbook = None
                        self.playbook_step = 0

                    # Normal: reset context, suggest random host
                    self.context.clear()

                    # Suggest a random host — weighted by attack surface
                    import random as _random
                    try:
                        all_hosts = db.get_hosts()
                        excluded = set(self.config["mission"]["scope"].get(
                            "excluded_hosts", []))
                        all_memories = host_memory.get_all_memories()
                        dead_end_ips = set()
                        for mac, mem in all_memories.items():
                            if mem.get("status") == "dead-end":
                                dead_end_ips.add(mem.get("ip", ""))

                        recent_ips = set(re.findall(r'192\.168\.1\.\d+',
                                         " ".join(self.recent_commands[-3:])))

                        from agent.planner import Phase as _Ph
                        _is_exploit = self.planner.current_phase >= _Ph.EXPLOIT

                        if _is_exploit:
                            # EXPLOIT: weight by attack surface priority
                            high, medium, low = [], [], []
                            for h in all_hosts:
                                ip = h["ip"]
                                if ip in excluded or ip in dead_end_ips or ip in recent_ips:
                                    continue
                                if not h.get("ports"):
                                    continue
                                pri = host_memory.get_host_priority(h.get("mac", ""))
                                if pri == "high":
                                    high.append(ip)
                                elif pri == "exhausted":
                                    pass  # skip exhausted hosts
                                elif pri == "low":
                                    low.append(ip)
                                else:
                                    medium.append(ip)

                            # Heavy weight toward high-priority (confirmed access)
                            r = _random.random()
                            if high and r < 0.50:
                                suggested = _random.choice(high)
                            elif medium and r < 0.85:
                                suggested = _random.choice(medium)
                            elif low:
                                suggested = _random.choice(low)
                            elif high:
                                suggested = _random.choice(high)
                            elif medium:
                                suggested = _random.choice(medium)
                            else:
                                suggested = ""
                        else:
                            # ENUMERATE: original logic
                            interesting = [h["ip"] for h in all_hosts
                                           if h["ip"] not in excluded
                                           and h["ip"] not in dead_end_ips
                                           and h["ip"] not in recent_ips
                                           and len(h.get("ports", [])) > 0]
                            others = [h["ip"] for h in all_hosts
                                      if h["ip"] not in excluded
                                      and h["ip"] not in dead_end_ips
                                      and h["ip"] not in recent_ips
                                      and len(h.get("ports", [])) == 0]

                            if interesting and _random.random() < 0.7:
                                suggested = _random.choice(interesting)
                            elif others:
                                suggested = _random.choice(others)
                            elif interesting:
                                suggested = _random.choice(interesting)
                            else:
                                suggested = ""
                        if suggested:
                            # Check if we know this host's ports
                            try:
                                host_ports = []
                                for h in all_hosts:
                                    if h["ip"] == suggested:
                                        host_ports = h.get("ports", [])
                                        break
                                from agent.planner import Phase as _Phase
                                _exploiting = self.planner.current_phase >= _Phase.EXPLOIT
                                if not host_ports:
                                    hint = f"Try: nmap -sS -T2 --top-ports 20 {suggested} (unknown ports). "
                                elif _exploiting:
                                    # EXPLOIT: use depth hints for hosts with findings,
                                    # standard hints for others
                                    import random as _rh
                                    # Look up MAC for this IP
                                    _mac = ""
                                    for _h in all_hosts:
                                        if _h["ip"] == suggested:
                                            _mac = _h.get("mac", "")
                                            break
                                    _pri = host_memory.get_host_priority(_mac) if _mac else "medium"
                                    if _pri == "high" and _mac:
                                        # High-priority: check for playbook first
                                        pb = self._get_playbook(suggested, _mac, set(host_ports))
                                        if pb and pb["steps"]:
                                            hint = f"Try: {pb['steps'][0]} ({pb['desc']}). "
                                            self.active_playbook = pb
                                            self.playbook_step = 0
                                            self.multi_turn_remaining = min(len(pb["steps"]), 3)
                                        else:
                                            hint = self._depth_hint(suggested, _mac, set(host_ports))
                                            self.multi_turn_remaining = 2
                                        self.multi_turn_ip = suggested
                                    elif _rh.random() < 0.5:
                                        hint = self._exploit_hint(suggested, set(host_ports))
                                    else:
                                        hint = self._enumerate_hint(suggested, set(host_ports))
                                else:
                                    # ENUMERATE phase: probe services
                                    hint = self._enumerate_hint(suggested, set(host_ports))
                            except Exception:
                                hint = f"Try probing {suggested} next. "
                        else:
                            hint = ""
                    except Exception:
                        hint = ""

                    self.context.append_user(
                        f"[LAST COMMAND]: {command}\n"
                        f"[RESULT]: {output_summary}\n\n"
                        f"{hint}"
                        "REASONING: [text] COMMAND: [command]"
                    )
                else:
                    # No command produced — track streak
                    self.garbage_streak += 1
                    if self.garbage_streak >= 5:
                        self.ui.render_warning("5 no-command streak — resetting context")
                        push_feed("warning", "Context reset: no commands")
                        self._reset_context_with_fewshot()
                    else:
                        self.context.append_user(
                            "You MUST include a COMMAND line. "
                            "REASONING: [text] COMMAND: [single command]"
                        )

                # Phase transition check
                changed = self.planner.evaluate(self.mission_log)
                if changed:
                    self.ui.render_phase_transition(self.planner.phase_name)
                    push_feed("phase", f"ENTERING: {self.planner.phase_name}")

                # Periodic Thor health check
                self.iteration += 1
                if self.iteration % self.HEARTBEAT_INTERVAL == 0:
                    health = await self.llm.health_check()
                    self.ui.update_backend_status(health)

                self.consecutive_errors = 0

            except Exception as e:
                self.consecutive_errors += 1
                self.mission_log.record_error(e)
                self.ui.render_error(
                    f"Error #{self.consecutive_errors}: {e}"
                )
                if self.consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                    self.ui.render_error(
                        f"{self.MAX_CONSECUTIVE_ERRORS} errors. "
                        "Resetting context and pausing 60s."
                    )
                    # Reset context instead of long pause
                    self.context.clear()
                    self.context.append_user(
                        "Context reset. Scan the next host. "
                        "REASONING: [text] COMMAND: [command]"
                    )
                    await asyncio.sleep(60)
                    self.consecutive_errors = 0
                else:
                    delay = self.RETRY_DELAYS[
                        min(self.consecutive_errors - 1,
                            len(self.RETRY_DELAYS) - 1)
                    ]
                    await asyncio.sleep(delay)

        self.ui.render_mission_complete()
        self.mission_log.finalize()

    async def _execute(self, command: str, max_retries: int = 2) -> dict:
        """Send command through scope proxy.

        Only retries on transport errors (proxy unreachable).
        Command execution errors (exit code != 0) are NOT retried — the
        command ran, it just failed, and retrying won't help (plus the
        proxy audit-logs each attempt, causing triple entries).
        """
        import httpx
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    resp = await client.post(
                        f"{self.proxy_url}/execute",
                        json={"command": command},
                    )
                    return resp.json()
            except Exception as e:
                last_error = str(e)
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
        return {"status": "error", "error": last_error, "output": ""}

    def _build_system_prompt(self) -> str:
        """Load system prompt from file and inject phase context."""
        scope = self.config["mission"]["scope"]
        stealth = self.config["stealth"]
        template = _load_system_prompt().format(
            scope_networks=", ".join(scope["networks"]),
            excluded_hosts=", ".join(scope.get("excluded_hosts", [])),
            excluded_ports=", ".join(str(p) for p in scope.get("excluded_ports", [])),
            cred_spray_rate=stealth["cred_spray_rate_per_min"],
            phase_context=self.planner.phase_context,
        )
        # Append mission state summary
        summary = self.context.get_summary()
        if summary:
            template += f"\n\n## MISSION STATE\n{summary}"
        return template

    async def _check_control_state(self):
        """Check C2 control state: kill, pause, phase, manual queue, starred hosts."""
        # Kill switch
        if db.get_state("kill_switch", False):
            self.ui.render_warning("KILL SWITCH activated")
            push_feed("warning", "KILL SWITCH — mission terminated")
            self.planner.mission_complete = True
            return "killed"

        # Pause
        while db.get_state("paused", False):
            update_state({"phase": "PAUSED"})
            await asyncio.sleep(1)

        # Forced phase change
        forced = db.get_state("forced_phase")
        if forced:
            self.planner.force_phase(forced)
            db.set_state("forced_phase", None)
            self.ui.render_phase_transition(self.planner.phase_name)
            push_feed("phase", f"FORCED: {self.planner.phase_name}")
            # Reset context for new phase
            self.context.clear()
            self.context.append_user(
                f"Phase changed to {self.planner.phase_name}. "
                "REASONING: [text] COMMAND: [command]"
            )

        # Live config override
        config_override = db.get_state("agent_config", {})
        if config_override:
            if "temperature" in config_override:
                self.llm._temperature = config_override["temperature"]
            if "max_tokens" in config_override:
                self.llm._max_tokens = config_override["max_tokens"]

        # Manual command queue (FIFO)
        queue = db.get_state("command_queue", [])
        if queue:
            cmd = queue.pop(0)
            db.set_state("command_queue", queue)
            return ("manual_cmd", cmd)

        # Decrement starred host counters
        starred = db.get_state("starred_hosts", [])
        if starred:
            updated = []
            for s in starred:
                s["remaining"] = s.get("remaining", 0) - 1
                if s["remaining"] > 0:
                    updated.append(s)
            db.set_state("starred_hosts", updated)

        return "continue"

    @staticmethod
    def _is_valid_command(command: str) -> bool:
        """Reject obviously invalid commands before they hit the executor."""
        if not command or len(command) < 2:
            return False
        cmd_lower = command.lower().strip()
        # Reject format tags
        if cmd_lower in ('command:', 'reasoning:', 'command', 'reasoning'):
            return False
        # Reject backtick-prefixed
        if command.startswith('`'):
            return False
        # Must start with a letter or /
        if not command[0].isalpha() and command[0] != '/':
            return False
        # Reject fake file paths that don't exist
        if '/path/to/' in command or '/home/user/' in command:
            return False
        # Reject placeholder tokens (model failed to substitute a real value)
        if re.search(r'<(ip|target|host|port|url|user|password|domain)>', cmd_lower):
            return False
        # Fix #2: Network tools MUST have a target IP — model sometimes
        # omits it, wasting a turn on "nmap --top-ports 20" with no target
        net_tools = ('nmap', 'curl', 'dig', 'smbclient', 'ssh', 'nxc',
                     'gobuster', 'nikto', 'hydra', 'enum4linux', 'sqlmap',
                     'dirb', 'impacket-samrdump', 'impacket-rpcdump',
                     'impacket-secretsdump', 'impacket-wmiexec')
        first_word = cmd_lower.split()[0] if cmd_lower.split() else ''
        if first_word in net_tools:
            if not re.search(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', command):
                return False
        # Reject invalid nmap timing (only -T0 through -T5)
        timing = re.search(r'-T(\d+)', command)
        if timing and int(timing.group(1)) > 5:
            return False
        # Enforce stealth: reject -T3, -T4, -T5 for nmap
        if 'nmap' in cmd_lower and timing and int(timing.group(1)) > 2:
            return False
        return True

    @staticmethod
    def _dedup_ports(command: str) -> str:
        """Fix #4: Remove duplicate ports from nmap -p lists."""
        match = re.search(r'(-p\s*)([\d,]+)', command)
        if match:
            prefix = match.group(1)
            ports_str = match.group(2)
            # Preserve order, remove duplicates
            seen = set()
            unique = []
            for p in ports_str.split(','):
                p = p.strip()
                if p and p not in seen:
                    seen.add(p)
                    unique.append(p)
            if unique:
                deduped = prefix + ','.join(unique)
                command = command[:match.start()] + deduped + command[match.end():]
        return command

    @staticmethod
    def _depth_hint(ip: str, mac: str, ports: set) -> str:
        """Generate exploit-depth hint based on what's already been found.

        Instead of re-running the same probe, suggest the NEXT logical
        step deeper into confirmed access points.
        """
        import random as _r
        findings = host_memory.get_access_findings(mac)
        failed = host_memory.get_failed_attacks(mac)
        tried = host_memory.get_tried_actions(mac)
        all_obs = [o["text"] for o in host_memory.get_memory(mac).get("observations", [])]

        hints = []

        # CVE DB lookup — version-specific exploits (highest priority)
        cve_hint = cve_db.get_exploit_hint(all_obs, ip)
        if cve_hint:
            hints.append(cve_hint)

        # SMB depth: if shares are known, READ them (not re-list!)
        if any("shares accessible" in f for f in findings):
            share_match = None
            for f in findings:
                if "shares accessible" in f:
                    share_match = f
            if share_match:
                # Extract share names
                shares = [s.strip() for s in
                          share_match.replace("SMB shares accessible:", "").split(",")]
                for share in shares:
                    share = share.strip()
                    if share and share != "IPC$":
                        hints.extend([
                            f"Try: smbclient -N //{ip}/{share} -c 'ls' (read share contents). ",
                            f"Try: smbclient -N //{ip}/{share} -c 'recurse ON; ls' (deep list). ",
                        ])
            hints.extend([
                f"Try: nxc smb {ip} -u '' -p '' --rid-brute (enumerate users). ",
                f"Try: enum4linux -a {ip} (full SMB enumeration). ",
            ])

        # Pi-hole depth: actual login mechanism (POST, not basic auth)
        if any("Pi-hole" in f or "pi-hole" in f for f in findings):
            hints.extend([
                f"Try: curl -s -X POST http://{ip}/admin/index.php -d 'pw=admin' (Pi-hole login). ",
                f"Try: curl -s -X POST http://{ip}/admin/index.php -d 'pw=raspberry' (Pi-hole login). ",
                f"Try: curl -s http://{ip}/admin/api.php?status (Pi-hole API). ",
                f"Try: gobuster dir -u http://{ip} -w /usr/share/wordlists/dirb/common.txt -q -t 5. ",
            ])

        # HTTP depth: if server responds, probe deeper + web exploits
        if any("HTTP server" in f or "HTTP 403" in f for f in findings):
            hints.extend([
                f"Try: curl -s http://{ip}/robots.txt (hidden paths). ",
                f"Try: curl -s http://{ip}/.env (leaked config). ",
                f"Try: curl -s http://{ip}/server-status (Apache status). ",
                f"Try: dirb http://{ip} /usr/share/wordlists/dirb/small.txt -S (dir scan). ",
                f"Try: nikto -h http://{ip} -Tuning 1 -maxtime 60s (web vuln scan). ",
            ])

        # VNC depth: if VNC is open, try different approaches
        if 5900 in ports:
            if not any("VNC" in f and "FAILED" in f for f in failed):
                hints.extend([
                    f"Try: nxc vnc {ip} -p password (VNC default). ",
                    f"Try: nxc vnc {ip} -p admin (VNC admin). ",
                    f"Try: nxc vnc {ip} -p raspberry (VNC pi). ",
                ])

        # Samba version known: try specific exploits
        if any("Samba version" in f for f in findings):
            hints.extend([
                f"Try: impacket-samrdump {ip} (enumerate SAM users). ",
                f"Try: impacket-rpcdump {ip} (RPC endpoints). ",
            ])

        # DNS depth: try zone transfer, reverse lookups
        if any("DNS resolver" in f or "dnsmasq" in f for f in findings):
            hints.extend([
                f"Try: dig axfr @{ip} (zone transfer). ",
                f"Try: dig @{ip} -x 192.168.1.2 (reverse lookup). ",
                f"Try: dig @{ip} any (all records). ",
            ])

        # Impacket on any SMB host (even without specific findings)
        if ports & {445, 139}:
            hints.extend([
                f"Try: impacket-samrdump {ip} (enumerate users via SAM). ",
                f"Try: impacket-rpcdump {ip} (RPC endpoints). ",
            ])

        # Filter out hints for tools we already tried on this host
        if tried and hints:
            filtered = []
            for h in hints:
                skip = False
                for t in tried:
                    action = t.replace("TRIED ", "").lower()
                    if action in h.lower():
                        skip = True
                        break
                if not skip:
                    filtered.append(h)
            if filtered:
                hints = filtered

        if hints:
            return _r.choice(hints)

        # Fallback to standard exploit hint if no depth available
        return AgentLoop._exploit_hint(ip, ports)

    @staticmethod
    def _enumerate_hint(ip: str, ports: set) -> str:
        """Generate enumerate-phase hint: service probing matched to ports."""
        hp = ports
        http_ports = hp & {80, 443, 8080, 8443, 8000, 8888, 3000, 9000, 9200}
        if http_ports:
            p = min(http_ports)
            scheme = "https" if p in (443, 8443, 9443) else "http"
            port_part = "" if p in (80, 443) else f":{p}"
            return f"Try: curl -s -I {scheme}://{ip}{port_part}/ (port {p}). "
        if hp & {445, 139}:
            return f"Try: smbclient -N -L //{ip}/ (has SMB). "
        if 53 in hp:
            return f"Try: dig @{ip} version.bind chaos txt (has DNS). "
        if 22 in hp:
            return f"Try: nmap -sV -T2 -p 22 {ip} (has SSH). "
        if hp & {3306, 5432, 1433, 27017, 6379}:
            p = min(hp & {3306, 5432, 1433, 27017, 6379})
            return f"Try: nmap -sV -T2 -p {p} {ip} (database). "
        if hp & {5900, 5901}:
            return f"Try: nmap -sV -T2 -p 5900 {ip} (VNC). "
        if 21 in hp:
            return f"Try: nmap -sV -T2 -p 21 {ip} (FTP). "
        return f"Try: nmap -sV -T2 -p {min(hp)} {ip}. "

    @staticmethod
    def _exploit_hint(ip: str, ports: set) -> str:
        """Generate exploit-phase hint: smart, varied attacks matched to ports.

        Includes: default creds, searchsploit, nmap vuln scripts, impacket,
        web probing, directory busting. Randomized to prevent repetition.
        """
        import random as _r
        hints = []

        # Version-based CVE lookup (always useful)
        if 22 in ports:
            hints.extend([
                f"Try: nmap -T2 --script=ssh-auth-methods -p 22 {ip} (SSH auth check). ",
                f"Try: nmap -T2 --script=vulners -p 22 {ip} (CVE scan SSH). ",
                f"Try: nxc ssh {ip} -u pi -p raspberry (Pi default). ",
                f"Try: nxc ssh {ip} -u root -p root (SSH default). ",
                f"Try: nxc ssh {ip} -u admin -p admin (SSH default). ",
                f"Try: sshpass -p 'raspberry' ssh -o StrictHostKeyChecking=no pi@{ip}. ",
                f"Try: nxc ssh {ip} -u root -p /usr/share/wordlists/nmap.lst (wordlist). ",
            ])
        if 23 in ports:
            hints.extend([
                f"Try: nxc telnet {ip} -u admin -p admin (telnet default). ",
                f"Try: nxc telnet {ip} -u root -p root (telnet default). ",
            ])
        if ports & {445, 139}:
            hints.extend([
                f"Try: nmap -T2 --script=smb-vuln* -p 445 {ip} (SMB vuln scan). ",
                f"Try: nxc smb {ip} -u '' -p '' --shares (null session). ",
                f"Try: nxc smb {ip} -u guest -p '' --shares --rid-brute (guest+RID). ",
                f"Try: enum4linux -a {ip} (full SMB enum). ",
                f"Try: impacket-samrdump {ip} (SAM user dump). ",
                f"Try: impacket-rpcdump {ip} (RPC endpoints). ",
                f"Try: impacket-samrdump {ip} (enumerate SAM users). ",
            ])
        if ports & {80, 443, 8080, 8888}:
            p = min(ports & {80, 443, 8080, 8888})
            pp = "" if p == 80 else f":{p}"
            hints.extend([
                f"Try: curl -s http://{ip}{pp}/robots.txt (hidden paths). ",
                f"Try: curl -s http://{ip}{pp}/.env (leaked config). ",
                f"Try: curl -s http://{ip}{pp}/admin/ -u admin:admin (admin panel). ",
                f"Try: curl -s http://{ip}{pp}/server-status (Apache status). ",
                f"Try: gobuster dir -u http://{ip}{pp} -w /usr/share/wordlists/dirb/common.txt -q -t 5. ",
                f"Try: dirb http://{ip}{pp} /usr/share/wordlists/dirb/small.txt -S (dir brute). ",
                f"Try: nmap -T2 --script=http-vuln* -p {p} {ip} (HTTP vuln scan). ",
            ])
        if 5900 in ports:
            hints.extend([
                f"Try: nxc vnc {ip} -p password (VNC default). ",
                f"Try: nmap -T2 --script=vnc-info -p 5900 {ip} (VNC info). ",
            ])
        if 53 in ports:
            hints.extend([
                f"Try: dig axfr @{ip} (DNS zone transfer). ",
                f"Try: dig axfr @{ip} (DNS zone transfer). ",
            ])
        if 21 in ports:
            hints.extend([
                f"Try: nxc ftp {ip} -u anonymous -p anonymous (FTP anon). ",
                f"Try: nmap -T2 --script=ftp-anon -p 21 {ip} (FTP anon check). ",
            ])
        if ports & {3306, 5432, 6379}:
            if 6379 in ports:
                hints.append(f"Try: redis-cli -h {ip} INFO (Redis unauthenticated). ")
            if 3306 in ports:
                hints.append(f"Try: nxc mysql {ip} -u root -p root (MySQL default). ")

        if not hints:
            hints = [
                f"Try: nmap -T2 --script=vulners -p {min(ports)} {ip} (CVE scan). ",
                f"Try: nmap -T2 --script=vulners -p 22 {ip} (CVE scan). ",
            ]
        return _r.choice(hints)

    def _get_playbook(self, ip: str, mac: str, ports: set) -> dict:
        """Find a matching playbook for this host based on observations."""
        import json, os
        try:
            pb_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data", "playbooks.json")
            with open(pb_path) as f:
                playbooks = json.load(f).get("playbooks", [])
        except Exception:
            return None

        obs = [o["text"] for o in
               host_memory.get_memory(mac).get("observations", [])]
        tried = host_memory.get_tried_actions(mac)
        obs_text = " ".join(obs).lower()

        for pb in playbooks:
            pb_id = pb.get("id", "")
            trigger = pb.get("trigger_obs", "").lower()
            trigger_ports = set(pb.get("trigger_ports", []))

            # Skip completed playbooks (unless repeatable, max 3 times) and failed ones
            repeatable = pb.get("repeatable", False)
            if host_memory.is_playbook_done(mac, pb_id):
                if not repeatable:
                    continue
                # Count how many times this playbook ran
                done_count = sum(1 for o in obs if o == f"PLAYBOOK_DONE {pb_id}")
                if done_count >= 3:
                    continue  # max 3 repeats
            if host_memory.is_playbook_failed(mac, pb_id):
                continue

            if trigger in obs_text and (not trigger_ports or
                                         trigger_ports & ports):
                # Extract actual share names from observations
                actual_shares = ["share"]  # fallback
                for o in obs:
                    if "shares accessible:" in o:
                        parts = o.split("shares accessible:")[-1]
                        actual_shares = [s.strip() for s in parts.split(",")
                                        if s.strip() and s.strip() != "IPC$"]
                        break

                # Filter out already-tried steps
                steps = []
                for step in pb.get("steps", []):
                    share_name = actual_shares[0] if actual_shares else "share"
                    step_cmd = step.format(ip=ip, share=share_name)
                    # Check if we already tried this tool
                    skip = False
                    for t in tried:
                        action = t.replace("TRIED ", "").lower()
                        if action in step_cmd.lower():
                            skip = True
                            break
                    if not skip:
                        steps.append(step_cmd)

                if steps:
                    return {"id": pb["id"], "steps": steps,
                            "desc": pb.get("desc", "")}
        return None

    def _reset_context_with_fewshot(self):
        """Shared context reset with few-shot example.

        Used by: time-based stuck detection, dup-loop reset, no-command reset.
        Injects a concrete nmap example on a random host so the 2B model
        copies the pattern instead of repeating its stuck reasoning.
        """
        import random as _rand
        self.context.clear()
        try:
            _hosts = [h for h in db.get_hosts()
                      if len(h.get("ports", [])) > 0]
            reset_ip = _rand.choice(_hosts)["ip"] if _hosts else "192.168.1.2"
        except Exception:
            reset_ip = "192.168.1.2"
        self.context.append_user(f"Scan {reset_ip}.")
        self.context.append_assistant(
            f"REASONING: Probe ports on {reset_ip}.\n"
            f"COMMAND: nmap -sS -T2 --top-ports 20 {reset_ip}"
        )
        self.context.append_user(
            "[STATUS]: success\n[OUTPUT]:\n22/tcp open ssh\n\n"
            "Good. Now probe a DIFFERENT host. "
            "REASONING: [text] COMMAND: [command]"
        )
        self.garbage_streak = 0
        self.recent_commands.clear()
        self.last_executed_ip = ""
        self.multi_turn_remaining = 0
        self.multi_turn_ip = ""
        self.active_playbook = None
        self.playbook_step = 0

    def _should_skip_host(self, ip: str) -> bool:
        """Check if a host is known dead-end and should be skipped."""
        try:
            for h in db.get_hosts():
                if h["ip"] == ip:
                    mem = host_memory.get_memory(h["mac"])
                    if mem.get("status") == "dead-end":
                        return True
                    break
        except Exception:
            pass
        return False

    def _is_duplicate(self, command: str) -> bool:
        """Check if exact same command was run in the last 5 turns."""
        return command in self.recent_commands

    @staticmethod
    def _is_garbage(response: str) -> bool:
        """Detect degenerate output (number sequences, repetition)."""
        if not response or len(response) < 10:
            return True
        # Mostly digits/commas (number sequence degeneration)
        non_alnum = response.replace(",", "").replace(" ", "").replace("\n", "")
        if len(non_alnum) > 50 and sum(c.isdigit() for c in non_alnum) / len(non_alnum) > 0.7:
            return True
        # Extreme repetition: same 10-char chunk repeated many times
        if len(response) > 100:
            chunk = response[10:20]
            if chunk and response.count(chunk) > len(response) // (len(chunk) * 2):
                return True
        return False

    # _is_refusal removed — abliterated model (Qwen3.5-2B-Unredacted-MAX) doesn't refuse

    @staticmethod
    def _parse_reasoning(response: str) -> str:
        # Strip markdown heading prefixes (### REASONING:)
        cleaned = re.sub(r'^#+\s*', '', response.strip())
        match = re.search(r'REASONING:\s*(.+?)(?=COMMAND:|$)',
                          cleaned, re.DOTALL)
        if match:
            text = match.group(1).strip()
            # Strip markdown bold markers
            text = text.strip("*").strip()
            return text if text else None
        # If no REASONING tag, treat entire non-command text as reasoning
        if "COMMAND:" not in response.upper():
            text = response.strip()
            return text if text else None
        return None

    @staticmethod
    def _parse_command(response: str) -> str:
        match = re.search(r'COMMAND:\s*(.+)', response, re.IGNORECASE)
        if match:
            cmd = match.group(1).strip()
            # Strip markdown formatting: bold (**), code fences, backticks
            cmd = re.sub(r'^```\w*\s*', '', cmd)
            cmd = re.sub(r'\s*```$', '', cmd)
            # Strip leading/trailing backticks and asterisks (markdown)
            cmd = re.sub(r'^[`*]+', '', cmd)
            cmd = re.sub(r'[`*]+$', '', cmd)
            cmd = cmd.strip()
            # Remove leading shell prompts, markdown headers
            cmd = re.sub(r'^[#>$\s]+', '', cmd).strip()
            return cmd if cmd else None
        return None


def _load_system_prompt() -> str:
    """Load system prompt from prompts/system.md (hot-reloadable)."""
    import os
    for base in [os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "/root/nightcrawler", "/opt/nightcrawler"]:
        path = os.path.join(base, "prompts", "system.md")
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
    # Fallback
    return ("You are NIGHTCRAWLER, a pentest agent. "
            "SCOPE: {scope_networks}. {phase_context}\n"
            "REASONING: [text] COMMAND: [command]")
