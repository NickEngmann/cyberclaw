"""Core agent decision loop."""

import asyncio
import re

from agent.llm_client import LLMClient
from agent.planner import PhasePlanner
from agent.context import ContextManager
from agent.watchdog import Watchdog
from agent.mission_log import MissionLog

try:
    from webui.server import update_state, push_feed
except ImportError:
    def update_state(u): pass
    def push_feed(t, c): pass


class AgentLoop:
    """Main decision engine — calls LLM, executes commands through proxy."""

    MAX_CONSECUTIVE_ERRORS = 5
    RETRY_DELAYS = [2, 5, 15, 30, 60]
    HEARTBEAT_INTERVAL = 10

    def __init__(self, config: dict, llm_client: LLMClient,
                 proxy_url: str, ui):
        self.config = config
        self.llm = llm_client
        self.proxy_url = proxy_url
        self.ui = ui
        self.context = ContextManager(max_tokens=3400)
        self.planner = PhasePlanner(config)
        self.mission_log = MissionLog(
            config.get("logging", {}).get("dir", "logs")
        )
        self.watchdog = Watchdog(
            config["mission"]["max_runtime_hours"]
        )
        self.consecutive_errors = 0
        self.iteration = 0
        self.total_commands = 0
        self.total_blocked = 0

    async def run(self):
        """Main loop — runs until mission complete or watchdog expires."""
        self.ui.render_boot_complete()
        self.watchdog.start()

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
                # Build system prompt with phase context
                system = self._build_system_prompt()
                messages = self.context.get_messages()

                # Call LLM
                self.ui.render_thinking()
                response = await self.llm.chat(system, messages)

                # Parse response
                reasoning = self._parse_reasoning(response)
                command = self._parse_command(response)

                if reasoning:
                    self.context.append_assistant(response)
                    self.ui.render_agent_thought(reasoning)
                    push_feed("thought", reasoning)

                if command:
                    self.ui.render_command(command)
                    push_feed("command", command)
                    result = await self._execute(command)
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
                        preview = output[:300] + "..." if len(output) > 300 else output
                        push_feed("result", preview)

                    self.total_commands += 1
                    if result.get("status") == "blocked":
                        self.total_blocked += 1

                    # WiFi connect detection
                    if ("wpa_supplicant" in command or
                            "dhclient" in command):
                        if result.get("status") == "success":
                            self.llm.notify_wifi_connected()
                            self.mission_log.set_finding("wifi_connected", True)

                elif not reasoning:
                    # Empty response — nudge the model
                    self.context.append_user(
                        "No command or reasoning received. "
                        "What is your next step?"
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
                        "5 consecutive errors. Pausing 5 min."
                    )
                    await asyncio.sleep(300)
                    self.consecutive_errors = 0
                else:
                    delay = self.RETRY_DELAYS[
                        min(self.consecutive_errors - 1, 4)
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
        """Load system prompt template and inject phase context."""
        scope = self.config["mission"]["scope"]
        stealth = self.config["stealth"]
        template = SYSTEM_PROMPT_TEMPLATE.format(
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

    @staticmethod
    def _parse_reasoning(response: str) -> str:
        match = re.search(r'REASONING:\s*(.+?)(?=COMMAND:|$)',
                          response, re.DOTALL)
        if match:
            return match.group(1).strip()
        # If no REASONING tag, treat entire non-command text as reasoning
        if "COMMAND:" not in response:
            return response.strip() if response.strip() else None
        return None

    @staticmethod
    def _parse_command(response: str) -> str:
        match = re.search(r'COMMAND:\s*(.+)', response)
        if match:
            cmd = match.group(1).strip()
            # Strip markdown code fences if present
            cmd = re.sub(r'^```\w*\s*', '', cmd)
            cmd = re.sub(r'\s*```$', '', cmd)
            return cmd if cmd else None
        return None


SYSTEM_PROMPT_TEMPLATE = """You are NIGHTCRAWLER, an autonomous penetration testing agent deployed on a mobile device inside a target facility. You operate by issuing Linux terminal commands through the Kali MCP server. You have access to every tool installed in Kali Linux.

## RULES — NEVER VIOLATE THESE

1. **SCOPE IS LAW.** Only target: {scope_networks}
   Excluded hosts: {excluded_hosts}
   Excluded ports: {excluded_ports}

2. **STEALTH MATTERS.** Use -T2 or lower for nmap. Credential spray max {cred_spray_rate}/min. Space out commands.

3. **LOG EVERYTHING.** State your reasoning before each command in 1-2 sentences.

4. **ONE COMMAND AT A TIME.** Single command, wait for output, then decide next action.

5. **PRIORITIZE IMPACT.** Focus on real business risk. Skip printers unless they offer pivot.

6. **NO DESTRUCTIVE COMMANDS.** Never: rm -rf, mkfs, dd, reboot, shutdown, iptables -F.

7. **KNOW YOUR LIMITS.** You are a 2B model on a phone. Be conservative. Defer to Thor when available.

## COMMAND FORMAT

REASONING: [1-2 sentences]
COMMAND: [single Linux command]

## CURRENT PHASE

{phase_context}"""
