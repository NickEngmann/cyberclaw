# Nightcrawler

Autonomous mobile penetration testing agent running on Kali NetHunter.

```
 ░█▄░█ █ █▀▀ █░█ ▀█▀ █▀▀ █▀█ ▄▀█ █░█░█ █░░ █▀▀ █▀█
 ░█░▀█ █ █▄█ █▀█ ░█░ █▄▄ █▀▄ █▀█ ▀▄▀▄▀ █▄▄ ██▄ █▀▄  v0.1.0

 AUTONOMOUS MOBILE PENTEST AGENT
 OnePlus 8 · NetHunter · Qwen3.5-2B · mcp-kali-server
```

## What It Does

Nightcrawler is a drop box that thinks for itself. Deploy the phone, walk away, and it:

1. **Cracks WiFi** autonomously (WPA2-PSK, targeted deauth, wordlist attack)
2. **Maps the network** with stealth nmap scans
3. **Enumerates services** — SMB shares, web apps, databases, default creds
4. **Exploits vulnerabilities** and documents impact
5. **Reports findings** with full command audit trail

All reasoning is done by a local Qwen3.5-2B model running on the phone's GPU via llama.cpp + OpenCL. No cloud, no API keys, no cellular needed.

## Hardware

- **Phone:** OnePlus 8 (Snapdragon 865, Adreno 650 GPU, 12GB RAM)
- **OS:** Android 12 + Kali NetHunter chroot
- **Model:** Qwen3.5-2B Q8_0 — 4.8 tokens/sec generation on GPU
- **Optional:** NVIDIA AGX Thor (128GB) for advanced reasoning over Tailscale

## Quick Start

```bash
# Install (inside Kali chroot)
bash INSTALL.sh

# Dry-run test (no real commands executed)
cd /opt/nightcrawler
NC_DRY_RUN=1 python3 main.py

# Full launch in tmux session
bash /opt/nightcrawler/scripts/launch.sh

# Web UI (from any device on Tailscale)
# Open http://<tailscale-ip>:8888
```

## Architecture

```
Agent Loop (main.py)
  │  Calls Qwen3.5-2B via llama.cpp (:8080)
  │  Produces: REASONING + COMMAND
  ▼
Scope Enforcement Proxy (:8800)
  │  Validates IPs, blocks destructive cmds, rate limits, audit logs
  ▼
kali-server-mcp (:5000)
  │  Official Kali MCP — raw terminal execution
  ▼
Kali Linux
    nmap, aircrack-ng, hydra, nxc, gobuster, sqlmap, ...
```

See `docs/ARCHITECTURE.md` for the full system design.

## Key Features

- **Fully autonomous** — no human in the loop during operation
- **Scope-enforced** — two-layer defense prevents out-of-scope actions
- **Stealth-aware** — configurable scan rates, jitter, targeted deauths
- **Air-gap capable** — operates with zero connectivity until WiFi is cracked
- **Persistent** — tmux session survives screen-off, reconnect via SSH
- **Auditable** — every command logged to `commands.jsonl` with reasoning
- **Web dashboard** — real-time view via browser (Tailscale only)
- **Thor offload** — optional, agent works standalone without it

## Project Structure

```
nightcrawler/
├── main.py              # Entry point
├── config.yaml          # Mission scope + model config
├── scope_proxy.py       # Scope enforcement proxy
├── INSTALL.sh           # Nightcrawler installer
├── agent/               # Decision loop, planner, LLM client, watchdog
├── proxy/               # IP validation, rate limiting, command filter
├── ui/                  # Terminal TUI (matrix rain, panels)
├── webui/               # Web dashboard (Flask + SSE)
├── simulation/          # Mock server + dry-run scenarios
├── scripts/             # launch, start, stop, wipe, install
├── models/              # .gguf files (gitignored)
├── logs/                # Mission data (gitignored)
├── patches/             # llama.cpp OpenCL patches for Adreno 650
├── docs/                # Architecture, GPU commands, GPU setup reference
│   ├── ARCHITECTURE.md
│   ├── COMMANDS.md
│   ├── README-GPU.md    # GPU inference docs (OpenCL/Adreno setup)
│   └── INSTALL-GPU.sh   # GPU/OpenCL setup script (Termux + llama.cpp)
└── backups/             # Magisk module configs
```

## Configuration

Edit `config.yaml` before deployment:

```yaml
mission:
  scope:
    networks: ["192.168.0.0/16", "10.0.0.0/8"]
    excluded_hosts: ["192.168.1.1"]      # gateway
    excluded_ports: [502, 503]            # SCADA
  max_runtime_hours: 8

stealth:
  scan_rate_per_min: 50
  cred_spray_rate_per_min: 10
  jitter_range_ms: [200, 2000]
```

## GPU Performance

| Model | Quant | Prompt | Generation |
|-------|-------|--------|------------|
| Qwen3.5-2B | Q8_0 | 23.3 t/s | **4.8 t/s** |
| Qwen3.5-0.8B | Q8_0 | 30.5 t/s | 6.3 t/s |
| Qwen3.5-4B | Q4_0 | 10.1 t/s | 2.0 t/s |

All via OpenCL on Adreno 650 with Qualcomm driver v819.2.
See `docs/README-GPU.md` for the full GPU inference story and `docs/INSTALL-GPU.sh` for the OpenCL/Termux setup.

## License

Authorized penetration testing use only. Requires valid Rules of Engagement.
