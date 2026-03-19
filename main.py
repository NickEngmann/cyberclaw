#!/usr/bin/env python3
"""Nightcrawler — Mobile Autonomous Pentest Agent entry point."""

import asyncio
import os
import subprocess
import sys
import yaml

from agent.llm_client import LLMClient
from agent.loop import AgentLoop
from agent.planner import Phase
from ui.colors import C
from ui.terminal import TerminalUI


def check_network_connectivity() -> bool:
    """Check if we already have network connectivity."""
    # Method 1: default route
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            return True
    except Exception:
        pass

    # Method 2: check if wlan0 has an IP
    try:
        result = subprocess.run(
            ["ip", "addr", "show", "wlan0"],
            capture_output=True, text=True, timeout=5,
        )
        if "inet " in result.stdout:
            return True
    except Exception:
        pass

    # Method 3: can we reach the LLM? (shared network namespace)
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:8080/health", timeout=3)
        if r.status_code == 200:
            return True
    except Exception:
        pass

    # Method 4: can we ping the gateway?
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", "192.168.1.1"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return True
    except Exception:
        pass

    return False


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


async def main():
    config_path = os.environ.get("NC_CONFIG", "config.yaml")
    config = load_config(config_path)

    dry_run = (
        os.environ.get("NC_DRY_RUN", "0") == "1"
        or config.get("dry_run", {}).get("enabled", False)
    )

    ui = TerminalUI()

    # Boot sequence display
    proxy_port = config.get("scope_proxy", {}).get("port", 8800)
    llm_port = config["model"]["local"]["port"]

    services = [
        {"name": "Loading config", "status": "OK", "detail": config_path},
        {"name": "LLM backend", "status": "OK",
         "detail": f"llama.cpp :{llm_port}"},
        {"name": "Scope proxy", "status": "OK",
         "detail": f":{proxy_port}"},
    ]

    if dry_run:
        services.append(
            {"name": "Mode", "status": "DRY RUN", "detail": "simulation"}
        )
    else:
        services.append(
            {"name": "Mode", "status": "AUTONOMOUS", "detail": "live"}
        )

    # Thor is optional — check if reachable
    thor_cfg = config["model"].get("thor", {})
    if thor_cfg.get("enabled"):
        services.append(
            {"name": "Thor", "status": "CHECKING",
             "detail": thor_cfg.get("endpoint", "?")}
        )
    else:
        services.append(
            {"name": "Thor", "status": "SKIP", "detail": "disabled"}
        )

    # Start Web UI
    webui_port = config.get("webui", {}).get("port", 8888)
    services.append(
        {"name": "Web UI", "status": "OK", "detail": f":{webui_port}"}
    )

    ui.render_boot_sequence(services)

    from webui.server import run_webui, get_tailscale_ip
    webui_host = get_tailscale_ip()
    run_webui(port=webui_port, host=webui_host)
    print(f"  {C.GREEN}[INIT]{C.RESET} Web UI running on http://{webui_host}:{webui_port} (Tailscale only)")

    # Initialize components
    llm = LLMClient(config["model"])
    proxy_url = f"http://127.0.0.1:{proxy_port}"

    loop = AgentLoop(
        config=config,
        llm_client=llm,
        proxy_url=proxy_url,
        ui=ui,
    )

    # If already connected, skip WiFi breach — go straight to recon
    if check_network_connectivity():
        loop.planner.current_phase = Phase.RECON
        loop.mission_log.set_finding("wifi_connected", True)
        ui.wifi_up = True
        print(f"  {C.GREEN}[INIT]{C.RESET} Network detected — skipping WiFi breach, starting at RECON")

    try:
        await loop.run()
    except KeyboardInterrupt:
        print(f"\n  [STOP] Interrupted by operator.")
    finally:
        await llm.close()


if __name__ == "__main__":
    asyncio.run(main())
