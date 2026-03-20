# Nightcrawler — Architecture

```
 ░█▄░█ █ █▀▀ █░█ ▀█▀ █▀▀ █▀█ ▄▀█ █░█░█ █░░ █▀▀ █▀█
 ░█░▀█ █ █▄█ █▀█ ░█░ █▄▄ █▀▄ █▀█ ▀▄▀▄▀ █▄▄ ██▄ █▀▄  v0.1.0
```

## Overview

Nightcrawler is a shell-based autonomous penetration testing agent that runs entirely within a Kali NetHunter chroot on an OnePlus 8. It uses a local Qwen3.5-2B Instruct model (Q8_0 quantization) as its reasoning engine and the official Kali Linux MCP server (`mcp-kali-server`) as its tool interface, with a scope enforcement proxy sitting between the agent and the MCP to prevent out-of-scope actions.

The model constructs raw CLI commands (e.g., `nmap -sS -T2 192.168.1.0/24`) rather than calling pre-defined tool schemas. This gives the agent access to every tool in Kali without hand-built wrappers.

The agent can operate fully standalone on local inference. If an NVIDIA AGX Thor (128GB) is reachable over Tailscale, the agent offloads orchestration to a larger model. If Thor is unavailable or connectivity is lost, the agent continues autonomously.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     ONEPLUS 8  ·  NETHUNTER CHROOT                  │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  TMUX SESSION "nightcrawler"                                  │  │
│  │  Detachable · Survives screen-off · Remote-attachable via SSH │  │
│  │                                                               │  │
│  │  ┌─────────────┐                                             │  │
│  │  │  QWEN 3.5   │  Constructs raw Kali commands               │  │
│  │  │  2B Q8_0    │  e.g. "nmap -sS 192.168.1.0/24"            │  │
│  │  │  llama.cpp  │                                             │  │
│  │  │  :8080      │                                             │  │
│  │  └──────┬──────┘                                             │  │
│  │         │                                                     │  │
│  │         ▼                                                     │  │
│  │  ┌─────────────────┐      ┌──────────────────────────────┐   │  │
│  │  │  NIGHTCRAWLER   │      │  SCOPE ENFORCEMENT PROXY     │   │  │
│  │  │  AGENT LOOP     │─────▶│  :8800                       │   │  │
│  │  │  + WATCHDOG     │      │                              │   │  │
│  │  │  Python 3       │      │  ✓ Validates target IPs      │   │  │
│  │  │  main.py        │      │  ✓ Blocks excluded hosts     │   │  │
│  │  │                 │      │  ✓ Blocks excluded ports     │   │  │
│  │  └────────┬────────┘      │  ✓ Enforces rate limits      │   │  │
│  │           │               │  ✓ Logs all commands         │   │  │
│  │           │               │  ✓ Adds jitter delays        │   │  │
│  │  ┌────────▼────────┐      │  ✗ Rejects destructive cmds  │   │  │
│  │  │  WEB UI         │      └──────────┬───────────────────┘   │  │
│  │  │  :8888          │                 │                       │  │
│  │  │  (Tailscale     │                 ▼                       │  │
│  │  │   IP only)      │      ┌──────────────────────────────┐   │  │
│  │  └─────────────────┘      │  mcp-kali-server (official)  │   │  │
│  │                           │  :5000                       │   │  │
│  │                           │  Raw terminal execution      │   │  │
│  │                           │  nmap, aircrack-ng, hydra,   │   │  │
│  │                           │  nxc, gobuster, sqlmap, ...  │   │  │
│  │                           └──────────────────────────────┘   │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  MISSION LOG: logs/findings.json, timeline.jsonl,            │  │
│  │               commands.jsonl, creds.enc                      │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  TAILSCALE DAEMON — mesh VPN for Thor + remote SSH access    │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  ANDROID PERSISTENCE LAYER (Magisk service.sh)              │  │
│  │  +10s: SSH (9022) · +12s: /vendor mount · +14s: Kali SSH    │  │
│  │  +20m: llama-server watchdog (30s health, 20m crash cool)   │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
                              │
               ┌──────────────▼──────────────┐
               │  THOR (AGX 128GB)           │
               │  Qwen3.5-27B+              │
               │  OPTIONAL — agent works     │
               │  fully standalone without   │
               └─────────────────────────────┘
```

---

## Three-Layer Command Stack

```
Agent (constructs commands)
   │
   ▼
Scope Enforcement Proxy (:8800)    ← OUR CODE — the safety layer
   │  Validates targets, rate limits, blocks destructive cmds
   │  Logs every command to SQLite + commands.jsonl
   │  Translates response format (stdout/stderr → status/output)
   ▼
kali-server-mcp (:5000)            ← OFFICIAL KALI PACKAGE (apt install mcp-kali-server)
   │  POST /api/command — shlex.split execution (no shell=True)
   │  Returns: {stdout, stderr, return_code, success, timed_out}
   ▼
Kali Linux (chroot)
   │  nmap, aircrack-ng, hydra, nxc, gobuster, sqlmap, ...
```

**Why this layering:** The official `mcp-kali-server` gives the model access to every Kali tool. The model constructs actual CLI commands. The scope proxy is the guardrail — it sits between the agent and the Kali MCP, intercepting every command before execution.

### Scope Proxy Enforcement

| Check | Description |
|-------|-------------|
| Scope validation | Extracts all IPs/CIDRs, verifies each is within `config.scope.networks` |
| Host exclusion | Blocks commands targeting `excluded_hosts` (e.g., gateway) |
| Port exclusion | Blocks commands targeting `excluded_ports` (e.g., SCADA 502/503) |
| Destructive filter | Regex blocklist: `rm -rf`, `mkfs`, `dd if=`, `reboot`, `shutdown`, etc. |
| Rate limiting | Enforces `scan_rate_per_min`, injects random jitter |
| Audit logging | Every command (allowed or blocked) → `commands.jsonl` |

---

## Phase State Machine

```
┌─────────┐              ┌─────────┐              ┌─────────┐
│ PHASE 0 │  WiFi up     │ PHASE 1 │  3+ hosts    │ PHASE 2 │
│ WiFi    │─────────────▶│ Recon   │─────────────▶│ Enum    │
│ Breach  │              │ & Map   │              │ & Probe │
│ LOCAL   │              │         │              │         │
└─────────┘              └─────────┘              └────┬────┘
                                                       │ vuln/cred found
                         ┌─────────┐              ┌────▼────┐
                         │ PHASE 4 │              │ PHASE 3 │
                         │ Cleanup │◀─────────────│ Exploit │
                         │ & Report│              │ & Pivot │
                         └─────────┘              └─────────┘
```

- **Phase 0 (WiFi Breach):** Air-gapped, local only. Crack WiFi, connect, bring up Tailscale. **Auto-skipped** if network already detected.
- **Phase 1 (Recon):** nmap stealth scans, map subnet, identify targets.
- **Phase 2 (Enumerate):** Deep-dive services. SMB, HTTP, databases. Null sessions, default creds.
- **Phase 3 (Exploit):** Demonstrate impact. Validate access, enumerate sensitive data.
- **Phase 4 (Cleanup):** Verify findings logged, disconnect, sync to Thor.

---

## Directory Structure

```
/opt/nightcrawler/                   (or /root/nightcrawler/ for dev)
├── main.py                          # Entry point — boots everything
├── config.yaml                      # Mission scope, model config, stealth params
├── scope_proxy.py                   # Scope enforcement proxy (Flask)
├── agent/
│   ├── loop.py                      # Core decision loop + error handling
│   ├── planner.py                   # Phase state machine
│   ├── llm_client.py                # llama.cpp / Thor API client w/ fallback
│   ├── context.py                   # Context window manager + summarizer
│   ├── watchdog.py                  # Mission timer + runtime enforcement
│   └── mission_log.py               # Structured findings + timeline
├── proxy/
│   ├── scope.py                     # IP/port/host validation
│   ├── rate_limiter.py              # Command rate limiting + jitter
│   ├── command_filter.py            # Destructive command blocklist
│   └── logger.py                    # Command audit logging
├── ui/
│   ├── terminal.py                  # Terminal TUI renderer
│   ├── matrix.py                    # Matrix rain + glitch effects
│   ├── panels.py                    # Status panels
│   └── colors.py                    # ANSI color definitions
├── webui/
│   ├── server.py                    # Flask web dashboard (Tailscale only)
│   └── templates/index.html         # Hacker terminal aesthetic UI
├── simulation/
│   ├── mock_kali_server.py          # Fake kali-server-mcp for dry-run
│   ├── scenarios/basic_wpa2.json    # Test scenarios
│   └── runner.py                    # Simulation driver
├── scripts/
│   ├── launch.sh                    # tmux session launcher (4 windows)
│   ├── start.sh                     # Boot all services + agent
│   ├── start-llm.sh                 # Start llama-server on GPU (Android side)
│   ├── stop.sh                      # Graceful shutdown
│   ├── wipe.sh                      # Secure delete all mission data
│   ├── install.sh                   # Install to /opt/nightcrawler
│   └── webui-daemon.sh              # WebUI daemon {start|stop|status|restart}
├── certs/                           # (gitignored) self-signed SSL certs
├── logs/                            # (gitignored) mission data
│   ├── findings.json
│   ├── timeline.jsonl
│   ├── commands.jsonl
│   └── creds.enc
├── playbooks/                       # Fallback/recovery decision trees
├── models/                          # (gitignored) .gguf model files
├── patches/                         # llama.cpp OpenCL patches
├── backups/                         # Magisk module configs
└── docs/
    ├── ARCHITECTURE.md              # This file
    ├── COMMANDS.md                  # GPU inference + service management
    ├── README-GPU.md                # GPU/OpenCL setup reference
    └── INSTALL-GPU.sh               # GPU/Termux build script
```

---

## LLM Command Format

The agent produces structured output for every turn:

```
REASONING: [1-2 sentences explaining analysis and next step]
COMMAND: [single Linux command to execute]
```

The system prompt is injected with phase context, scope constraints, and mission state summary. The model is instructed to never chain commands (`&&`, `;`), always stay in scope, and use stealth scan rates.

---

## LLM Backend (llama-server)

llama-server runs on the **Android side as root** (not in the Kali chroot, not as Termux user). It uses the Adreno 650 GPU via OpenCL with Qualcomm's vendor driver. The Kali chroot shares the network namespace with Android, so the agent reaches it at `http://127.0.0.1:8080`.

**Why root, not Termux user?** The Termux UID (10393) can't bind network ports when invoked via `su`. Running as root with Termux's `LD_LIBRARY_PATH` works.

**Auto-start:** Magisk watchdog starts llama-server 20 min after boot. If it crashes, 20 min cooldown before restart. Health checked every 30s.

**Manual start:** `ssh -p 9022 shell@127.0.0.1 "bash /data/local/nhsystem/kalifs/root/nightcrawler/scripts/start-llm.sh"`

**Logs:** `/data/local/tmp/var/log/llama-server.log`, `/data/local/tmp/var/log/llama-watchdog.log`

---

## Web UI

Hacker terminal aesthetic dashboard at `:8888`, served over HTTPS with a self-signed cert. Bound to Tailscale IP only (not exposed on target network).

**URL:** `https://kali.taileba694.ts.net:8888` (accept self-signed cert warning)

**Management:** `scripts/webui-daemon.sh {start|stop|status|restart}`

Shows:

- Phase, mode, uptime, watchdog timer
- Live agent feed (thoughts, commands, results, blocked actions)
- Discovered hosts with ports
- Credentials and vulnerabilities
- Findings summary counters

Polls `/api/state` every second. Also supports SSE streaming at `/api/stream`.

---

## Configuration

See `config.yaml` for full reference. Key sections:

- `mission.scope` — allowed networks, excluded hosts/ports
- `model.local` — llama.cpp server config (port, ctx, threads)
- `model.thor` — optional AGX endpoint
- `stealth` — scan rate, credential spray rate, jitter, deauth settings
- `wifi` — interface names, wordlists, multi-SSID config
- `webui.port` — web dashboard port (default 8888)

---

## Security Model

1. **System prompt** — first line of defense. Instructs model to stay in scope.
2. **Scope proxy** — second line. Validates every command before it reaches kali-server-mcp.
3. **Audit trail** — third line. Every command logged for operational review.
4. **Credential encryption** — AES-256-GCM, key from `NC_CRED_KEY` env var.
5. **Secure wipe** — `wipe.sh` performs multi-pass shred + fstrim.

---

## Memory Footprint

~2.74GB model + ~0.6GB KV cache @ 8192 ctx = **~3.34GB** on GPU.
Android system uses ~5GB. Agent Python processes use ~50MB total.
Remaining ~3.5GB for Kali tools, nmap processes, and OS cache.

**CRITICAL:** Never run two llama-server processes simultaneously.
Dual instances caused an OOM crash (2× ~3GB = phone reboot).

---

## Data Storage (SQLite v2)

All findings stored in `logs/nightcrawler.db` with WAL mode:
- **Hosts** keyed by MAC address (survives DHCP changes)
- **Network-scoped** — all data tagged with network CIDR
- **Networks table** — tracks discovered networks with SSID/gateway
- **Backward compat** — still writes findings.json + timeline.jsonl

Export for Thor: `GET /api/export/<network>` returns full JSON.

---

## 2B Model Tuning Lessons (from 36h test)

The Qwen3.5-2B model required extensive tuning to produce reliable commands:

| Parameter | Final Value | Why |
|-----------|-------------|-----|
| Temperature | 0.2 | Higher values increase garbage rate |
| max_tokens | 200 | Less = truncated commands, more = verbose garbage |
| Context budget | 6000 tokens | Of 8192 total, leaves room for system prompt |
| Few-shot seed | Phase-aware | RECON: nmap example, ENUMERATE: curl example |
| Garbage reset | 5-streak | Clears context after 5 consecutive failures |
| Dup detection | 2-in-5 | Forces tool/target diversification |

**Key insight:** The 2B model follows few-shot examples, not instructions.
System prompt text ("DO NOT run nmap") is ignored, but a curl example in
the conversation seed causes it to produce curl commands.

---

## Roadmap

```
v0.1  ✅ Core agent loop + TUI + web UI + scope proxy + real execution
v0.2  ✅ Full recon/enum phases + SQLite + multi-network + MAC-keyed hosts
v0.3  Exploit phase + credential spraying + multi-SSID
v0.4  Passive capture (tcpdump/tshark) for MAC/hostname discovery
v0.5  Thor-side report generation pipeline
v0.6  Larger model support (4B Q4_0 at 2.0 t/s)
v1.0  Field-tested, stable release
```
