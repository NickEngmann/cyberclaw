"""Core agent decision loop."""

import asyncio
import re

from agent.llm_client import LLMClient
from agent.planner import PhasePlanner
from agent.context import ContextManager
from agent.watchdog import Watchdog
from agent.mission_log import MissionLog

from agent import db
from agent import host_memory
from agent import training_capture

try:
    from webui.server import update_state, push_feed
except ImportError:
    def update_state(u): pass
    def push_feed(t, c): pass


class AgentLoop:
    """Main decision engine — calls LLM, executes commands through proxy."""

    MAX_CONSECUTIVE_ERRORS = 8
    RETRY_DELAYS = [2, 3, 5, 8, 10, 15, 20, 30]
    HEARTBEAT_INTERVAL = 10

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
        self.watchdog = Watchdog(
            config["mission"]["max_runtime_hours"]
        )
        self.consecutive_errors = 0
        self.garbage_streak = 0
        self.recent_commands = []  # last N commands for dup detection
        self.iteration = 0
        self.total_commands = 0
        self.total_blocked = 0
        self.total_garbage = 0

    async def run(self):
        """Main loop — runs until mission complete or watchdog expires."""
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
            # ENUMERATE/EXPLOIT: teach curl/service probing
            self.context.append_user("Enumerate services on discovered hosts.")
            self.context.append_assistant(
                "REASONING: Check HTTP on the Raspberry Pi.\n"
                "COMMAND: curl -s -I http://192.168.1.2/"
            )
            self.context.append_user(
                "[STATUS]: success\n[OUTPUT]:\n"
                "HTTP/1.1 200 OK\nServer: lighttpd/1.4.53\n"
                "Content-Type: text/html\n\n"
                "Good. Now probe other services. Try: smbclient, curl on other hosts, dig."
            )

        # Initial health check
        health = await self.llm.health_check()
        self.ui.update_backend_status(health)

        while not self.planner.mission_complete:
            # Watchdog check
            wdg = self.watchdog.check()
            if wdg == "expired":
                self.ui.render_warning("WATCHDOG: Max runtime reached.")
                self.planner.force_phase("cleanup")
            elif wdg in ("warn_90", "warn_75"):
                pct = wdg[-2:]
                self.ui.render_warning(
                    f"WATCHDOG: {pct}% runtime. "
                    f"{self.watchdog.format_remaining()} left."
                )

            phase = self.planner.current_phase
            self.ui.update_phase(self.planner.phase_name)
            self.ui.update_stats(
                uptime=self.watchdog.format_elapsed(),
                watchdog=self.watchdog.format_remaining(),
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
                "watchdog": self.watchdog.format_remaining(),
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
                        self.context.clear()
                        self.context.append_user(
                            f"Context was reset. You are in {self.planner.phase_name}. "
                            "REASONING: [analysis] COMMAND: [single command]"
                        )
                        self.garbage_streak = 0
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
                        self.ui.render_warning(f"Duplicate command: {command[:60]}")
                        push_feed("warning", f"Dup cmd: {command[:60]}")
                        self.context.append_user(
                            f"You already ran '{command[:80]}'. "
                            "Try a DIFFERENT approach or tool. "
                            "REASONING: [new analysis] COMMAND: [different command]"
                        )
                        continue

                    self.recent_commands.append(command)
                    if len(self.recent_commands) > 5:
                        self.recent_commands.pop(0)

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

                    # WiFi connect detection
                    if ("wpa_supplicant" in command or
                            "dhclient" in command):
                        if result.get("status") == "success":
                            self.llm.notify_wifi_connected()
                            self.mission_log.set_finding("wifi_connected", True)

                    # Reset context after each command — host memory provides
                    # persistent knowledge, so we don't need to carry context
                    # about previous hosts. Fresh context = better rotation.
                    self.context.clear()
                    output_summary = result.get("output", "")[:500]

                    # Suggest a random host — weighted toward interesting hosts,
                    # excluding dead-ends and recently probed hosts
                    import random as _random
                    try:
                        all_hosts = db.get_hosts()
                        excluded = set(self.config["mission"]["scope"].get(
                            "excluded_hosts", []))
                        # Get dead-end MACs from host memory
                        all_memories = host_memory.get_all_memories()
                        dead_end_ips = set()
                        for mac, mem in all_memories.items():
                            if mem.get("status") == "dead-end":
                                dead_end_ips.add(mem.get("ip", ""))

                        # Filter out excluded, dead-end, and just-scanned hosts
                        recent_ips = set(re.findall(r'192\.168\.1\.\d+',
                                         " ".join(self.recent_commands[-3:])))

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
                        hint = f"Try probing {suggested} next. " if suggested else ""
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
                        self.context.clear()
                        self.context.append_user(
                            "Enumerate services on 192.168.1.2. "
                            "REASONING: [text] COMMAND: [command]"
                        )
                        self.garbage_streak = 0
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
        """Send command through scope proxy."""
        import httpx
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=300) as client:
                    resp = await client.post(
                        f"{self.proxy_url}/execute",
                        json={"command": command},
                    )
                    result = resp.json()
                    if result.get("status") == "blocked":
                        return result
                    if (result.get("status") == "error" and
                            attempt < max_retries):
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return result
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
        # Reject invalid nmap timing (only -T0 through -T5)
        import re
        timing = re.search(r'-T(\d+)', command)
        if timing and int(timing.group(1)) > 5:
            return False
        # Enforce stealth: reject -T3, -T4, -T5 for nmap
        if 'nmap' in cmd_lower and timing and int(timing.group(1)) > 2:
            return False
        return True

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
        """Check if same command was run in last 3 turns."""
        return self.recent_commands.count(command) >= 2

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
