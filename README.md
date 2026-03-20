# Nightcrawler

Autonomous mobile penetration testing agent running on Kali NetHunter.

```
 ░█▄░█ █ █▀▀ █░█ ▀█▀ █▀▀ █▀█ ▄▀█ █░█░█ █░░ █▀▀ █▀█
 ░█░▀█ █ █▄█ █▀█ ░█░ █▄▄ █▀▄ █▀█ ▀▄▀▄▀ █▄▄ ██▄ █▀▄  v0.1.0

 AUTONOMOUS MOBILE PENTEST AGENT
 OnePlus 8 · NetHunter · Qwen3.5-2B · OpenCL GPU
```

## What It Does

Nightcrawler is a drop box that thinks for itself. Deploy the phone, walk away, and it:

1. **Cracks WiFi** autonomously (WPA2-PSK, targeted deauth, wordlist attack)
2. **Maps the network** with stealth nmap scans — discovers hosts, ports, MACs
3. **Enumerates services** — SMB shares, web apps (found a Pi-hole!), SSH versions, DNS
4. **Probes with diverse tools** — nmap, curl, smbclient, dig, netcat, telnet
5. **Reports findings** with full command audit trail + exportable JSON for Thor

All reasoning is done by a local Qwen3.5-2B model running on the phone's GPU via llama.cpp + OpenCL. No cloud, no API keys, no cellular needed.

## Hardware

- **Phone:** OnePlus 8 (Snapdragon 865, Adreno 650 GPU, 12GB RAM)
- **OS:** Android 12 + Kali NetHunter chroot
- **Model:** Qwen3.5-2B-Unredacted-MAX Q8_0 (abliterated) — 4.8 t/s on GPU, 8192 ctx
- **Optional:** NVIDIA AGX Thor (128GB) for advanced reasoning over Tailscale

## Quick Start

```bash
# Install (inside Kali chroot)
bash INSTALL.sh

# Wait for llama-server (~23min after boot)
curl -s http://127.0.0.1:8080/health

# Start services
kali-server-mcp --port 5000 &
python3 scope_proxy.py --config config.yaml --port 8800 --upstream http://127.0.0.1:5000 &
bash scripts/webui-daemon.sh start
python3 main.py &

# Or use the 36h tmux launcher
bash scripts/run-36h.sh

# Web UI (from any device on Tailscale)
# https://kali.taileba694.ts.net:8888
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
  │  Official Kali MCP — shlex command execution (no shell=True)
  ▼
Kali Linux
    nmap, curl, smbclient, nxc, gobuster, dig, hydra, ...
```

See `docs/ARCHITECTURE.md` for the full system design.

## Key Features

- **Fully autonomous** — no human in the loop during operation
- **Scope-enforced** — two-layer defense prevents out-of-scope actions
- **Multi-network** — data tagged by network CIDR, persists across deployments
- **MAC-keyed hosts** — tracks devices by MAC address, survives DHCP changes
- **SQLite backend** — efficient storage with WAL mode for concurrent access
- **Interactive web dashboard** — clickable hosts, port details, scan history, network selector
- **Thor export** — `/api/export/<network>` JSON endpoint for offloading to AGX
- **Self-healing** — garbage detection, context reset, duplicate command detection
- **Auditable** — every command logged to SQLite + JSONL with reasoning
- **Memory efficient** — agent RSS ~35-50MB, no memory leak
- **C2 controls** — star hosts, blacklist, force phase, inject commands, kill switch

## Tested Results (36h autonomous run)

From the first 36-hour test on a home network (192.168.1.0/24):

- **28 hosts discovered** via ping sweep
- **5 hosts with open ports** enumerated in detail
- **Key finding:** 192.168.1.2 is a Raspberry Pi running:
  - **Pi-hole** DNS sinkhole (identified from HTTP response)
  - **Samba 4.17.12-Debian** with null-session accessible shares
  - **OpenSSH 9.2p1 Debian**
  - 7 open ports: SSH, DNS, HTTP, HTTPS, SMB, NetBIOS, VNC
- **132+ commands** executed autonomously
- **Tools used:** nmap, curl, smbclient, dig, telnet, netcat, ssh
- Agent RSS stable at 35-50MB throughout (no memory leak)

## Project Structure

```
nightcrawler/
├── main.py              # Entry point (phase-aware startup)
├── config.yaml          # Mission scope + model config
├── scope_proxy.py       # Scope enforcement proxy
├── kali_executor.py     # Real command executor (subprocess)
├── INSTALL.sh           # Nightcrawler installer
├── agent/
│   ├── loop.py          # Core decision loop + error recovery
│   ├── planner.py       # Phase state machine
│   ├── llm_client.py    # llama.cpp / Thor API client
│   ├── context.py       # Sliding window context manager
│   ├── watchdog.py      # Mission timer
│   ├── mission_log.py   # Findings tracking (SQLite-backed)
│   └── db.py            # SQLite backend (MAC-keyed, multi-network)
├── proxy/               # IP validation, rate limiting, command filter
├── prompts/             # Hot-reloadable prompt files (edit to tune)
│   ├── system.md        # System prompt template
│   ├── phase1_recon.md  # Recon phase guidance
│   ├── phase2_enumerate.md  # Enumeration guidance
│   └── ...
├── ui/                  # Terminal TUI (matrix rain, panels)
├── webui/               # Web dashboard (Flask, SQLite-backed)
├── scripts/
│   ├── run-36h.sh       # Sustained run with crash recovery
│   ├── health-check.sh  # Cron health monitor
│   ├── start-llm.sh     # Start llama-server on GPU
│   ├── webui-daemon.sh  # WebUI daemon management
│   ├── launch.sh        # tmux session launcher
│   └── ...
├── logs/                # Mission data (gitignored)
├── models/              # .gguf files (gitignored)
├── patches/             # llama.cpp OpenCL patches
└── docs/                # Architecture, GPU setup
```

## Configuration

Edit `config.yaml` before deployment:

```yaml
mission:
  scope:
    networks: ["192.168.1.0/24"]
    excluded_hosts: ["192.168.1.1", "192.168.1.53"]  # gateway + self
    excluded_ports: [502, 503]
  max_runtime_hours: 36

model:
  local:
    ctx_size: 8192
    port: 8080
```

## GPU Performance

| Model | Quant | Prompt | Generation |
|-------|-------|--------|------------|
| Qwen3.5-2B | Q8_0 | 23.3 t/s | **4.8 t/s** |
| Qwen3.5-0.8B | Q8_0 | 30.5 t/s | 6.3 t/s |
| Qwen3.5-4B | Q4_0 | 10.1 t/s | 2.0 t/s |

All via OpenCL on Adreno 650 with Qualcomm driver v819.2.
See `docs/README-GPU.md` for GPU setup and `docs/INSTALL-GPU.sh` for the build script.

## License

Authorized penetration testing use only. Requires valid Rules of Engagement.
