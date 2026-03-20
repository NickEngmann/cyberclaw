# Nightcrawler - Mobile Autonomous Pentest Agent

## Project
Nightcrawler is an autonomous penetration testing agent running on a OnePlus 8 with Kali NetHunter. It uses a local Qwen3.5-2B model as its reasoning engine and a real command executor as its tool interface, with a scope enforcement proxy as the safety layer.

- GitHub: github.com/NickEngmann/nightcrawler
- Install: `/opt/nightcrawler/` (production), `/root/nightcrawler/` (dev)
- Architecture doc: `docs/ARCHITECTURE.md`

## CRITICAL: llama-server Rules
- **NEVER start a second llama-server process** — always `pgrep llama-server` first
- **NEVER kill llama-server from Kali chroot** — only from Android shell (port 9022)
- **Context window: 8192 tokens** — do not increase without explicit user approval
- Dual llama-server processes caused OOM crash on 2026-03-20 (~3GB × 2 = phone reboot)
- Auto-starts 20 min after boot via Magisk watchdog (30s health check, 20 min crash cooldown)
- Manual start: `ssh -p 9022 shell@<ip> "bash /data/local/nhsystem/kalifs/root/nightcrawler/scripts/start-llm.sh"`

## Device Info
- Phone: OnePlus 8 (kebab), Snapdragon 865, Adreno 650 GPU
- Kernel: 4.19.157-perf+ (Nameless AOSP, Android 12)
- RAM: 12GB (shared between CPU and GPU)
- GPU Driver: Qualcomm v819.2, Compiler E031.50.02.00 (Magisk module)
- Chroot: Kali Linux at /data/local/nhsystem/kalifs
- Termux: installed (used for OpenCL GPU builds)

## SSH Access
```bash
ssh -p 9022 shell@192.168.1.53   # Android shell (Magisk openssh)
ssh root@<tailscale-ip>           # Kali root shell (port 22)
```

## Service Ports
| Port | Service | Description |
|------|---------|-------------|
| 22   | SSH     | Kali root shell |
| 9022 | SSH     | Android shell (Magisk) |
| 5000 | kali-server-mcp | Official Kali MCP server (shlex, no shell=True) |
| 8080 | llama-server | Qwen3.5-2B-Unredacted-MAX Q8_0 (abliterated) via llama.cpp (ctx=8192) |
| 8800 | scope-proxy | Scope enforcement + rate limit + audit |
| 8888 | web UI | Dashboard (Tailscale IP only, HTTPS) |

## Nightcrawler Stack
```
Agent (main.py) → LLM (llama.cpp :8080) → REASONING + COMMAND
    ↓
Scope Proxy (:8800) → validates IPs, ports, destructive cmds → /api/command
    ↓
kali-server-mcp (:5000) → shlex.split + subprocess (official Kali package)
    ↓
Kali Linux tools (nmap, curl, smbclient, nxc, gobuster, dig, ...)
```

## Running
```bash
# Start all services manually (after llama-server is healthy)
kali-server-mcp --port 5000 &
python3 scope_proxy.py --config config.yaml --port 8800 --upstream http://127.0.0.1:5000 &
bash scripts/webui-daemon.sh start
python3 main.py &

# Or use the 36h tmux launcher
bash scripts/run-36h.sh

# Dry-run (mock kali server, no real commands)
NC_DRY_RUN=1 python3 main.py
```

## GPU Inference (OpenCL)
llama.cpp compiled in Termux with Adreno-optimized OpenCL kernels. Runs as root on Android side (not in chroot). From Kali chroot, agent reaches it at http://127.0.0.1:8080 (shared network namespace).

### Key constraints
- **Context: 8192 tokens** — do not change without user approval
- First run after reboot: ~3 min kernel JIT (cached after)
- Q8_0 is fastest on GPU. Never use Q4_K_M on GPU (10x slower)
- 4B Q8_0 fails: exceeds 1GB per-allocation limit. Use Q4_0 for 4B.

## Performance
| Model | Quant | Backend | Prompt | Generation |
|-------|-------|---------|--------|------------|
| Qwen3.5-2B | Q8_0 | **OpenCL GPU** | **23.3 t/s** | **4.8 t/s** |
| Qwen3.5-0.8B | Q8_0 | OpenCL GPU | 30.5 t/s | 6.3 t/s |
| Qwen3.5-4B | Q4_0 | OpenCL GPU | 10.1 t/s | 2.0 t/s |

## Data Storage
- **SQLite DB**: `logs/nightcrawler.db` — primary store (WAL mode, MAC-keyed hosts, multi-network)
- **Host memories**: `host_memories` in SQLite state — auto-generated observations + analyst edits
- **JSON compat**: `logs/findings.json`, `logs/timeline.jsonl`, `logs/commands.jsonl`
- **Prompts**: `prompts/*.md` — hot-reloadable, edit to tune model behavior
- **Export for Thor**: `GET /api/export/<network>` — full JSON dump per network (includes memories)
- **Memory export**: `GET /api/hosts/memories/export` — all host observations for Thor

## kali-server-mcp vs kali_executor.py
Switched from custom `kali_executor.py` to the official `kali-server-mcp` package:
- **API**: `POST /api/command` (not `/execute`) — proxy translates the response format
- **Execution**: Uses `shlex.split` (not `shell=True`) — eliminates `/bin/sh` backtick errors
- **Response**: Returns `{stdout, stderr, return_code, success, timed_out}`
- **Proxy translation**: Maps to `{status, output, return_code}` for the agent
- `kali_executor.py` still exists as a fallback but is not used in production

## Host Memory System
The agent auto-generates observations from scan results and injects them
into the system prompt (capped at 200 tokens) to prevent repeating dead-end
approaches. Red teamers can add/edit observations via the web UI.

- **Auto-extracted**: HTTP servers, SSH versions, SMB shares, filtered ports
- **Status**: unknown → interesting → compromised (or dead-end)
- **Tags**: auto-generated (pi-hole, ssh, smb, dns)
- **Avoid tools**: per-host tool exclusions
- **Exportable**: `GET /api/hosts/memories/export` for Thor
- **Editable**: Click host → MEMORY section → add observations, set status

## C2 Interactive Features (Web UI)
The web UI at `:8888` has full C2 controls:
- **⭐ Star hosts**: Prioritize scanning for N iterations (PRIORITY TARGET in prompt)
- **⛔ Blacklist hosts**: Skip entirely (BLACKLIST in prompt, strikethrough in UI)
- **✎ Network edit**: Custom names and notes per network
- **Host notes → LLM**: Analyst notes injected as ANALYST NOTES in system prompt
- **Pause/Resume**: Toggle agent operation
- **Force phase**: RECON/ENUMERATE/EXPLOIT buttons
- **Command injection**: Manual command text input
- **Tool preferences**: Enable/disable specific tools
- **Kill switch**: Emergency stop
- **Config panel**: Live temperature/max_tokens editing
- **Command search**: Search history by keyword

## Key Architecture Decisions (from 36h test run)
- **Few-shot prompting** is essential — the 2B model follows examples, not instructions
- **Phase-aware seed**: RECON uses nmap example, ENUMERATE uses curl example
- **Garbage detection** with 5-streak context reset prevents model spiral
- **Duplicate command detection** forces tool/target diversification
- **SQLite backend** (not JSON files) for memory efficiency and concurrent access
- **MAC-keyed hosts** survive DHCP changes, tagged by network_id (gateway MAC hash)
- **Network discrimination**: Same CIDR on different routers = separate data
- **kali-server-mcp** eliminates shell interpretation errors vs subprocess shell=True
- **Agent RSS stays ~35-50MB** — no memory leak when not restarting repeatedly
- Repeated agent restarts DO leak memory (Python process accumulation) — avoid

## 2B Model Behavior Notes (Qwen3.5-2B-Unredacted-MAX Q8_0)
- **Abliterated** — no safety refusals, no re-prompting needed
- ~50% command success rate (inherent limit of 2B parameters)
- Produces garbage/number sequences ~25% of turns
- Follows few-shot examples more than system prompt instructions
- Verbose reasoning eats tokens — "10 words max" in system prompt helps
- Temperature 0.2 gives best format compliance
- max_tokens 200 balances completeness vs garbage
- Stealth: system prompt enforces -T2 for nmap (never -T4/-T5)

## Development Notes
- Agent auto-detects network: resumes at correct phase based on existing findings
- Thor (AGX 128GB) is optional — agent operates fully standalone
- Web UI binds to Tailscale IP only (not exposed on target network)
- All commands audited to SQLite + commands.jsonl regardless of allow/block
- Health check monitors: services, agent RSS, dual llama-server, stale timeline, disk, memory

## Boot Sequence (Magisk service.sh)
1. +10s: Android SSH (9022)
2. +12s: Mount /vendor in Kali chroot
3. +14s: Kali SSH (22)
4. +20min: llama-server watchdog starts (health check every 30s, 20min crash cooldown)

## After Reboot — Manual Steps
1. Wait ~23min for llama-server: `curl -s http://127.0.0.1:8080/health`
2. Start kali-server-mcp: `kali-server-mcp --port 5000 &`
3. Start proxy: `python3 scope_proxy.py --config config.yaml --port 8800 --upstream http://127.0.0.1:5000 &`
4. Start webui: `bash scripts/webui-daemon.sh start`
5. Start agent: `python3 main.py &`

## Magisk Modules
| Module | Purpose |
|--------|---------|
| openssh (v9.9p2) | Persistent SSH on ports 9022 + 22 |
| adreno-650_819v2 | GPU driver v819.2 (E031.50) |
| nethunter (v1.4.0) | Kali chroot + tools |
| tailscaled | Tailscale VPN |

## Web UI
- URL: https://kali.taileba694.ts.net:8888 (self-signed cert)
- Daemon: `bash scripts/webui-daemon.sh {start|stop|status|restart}`
- Features: clickable host cards, port details, scan history, network selector, Thor export
- Reads from SQLite + JSON files (survives agent crashes)

## IMPORTANT: Clean Up Test Artifacts
After ANY testing that creates data in the DB (test networks, test hosts, demo data):
- **Delete test networks**: `DELETE FROM networks WHERE network_id NOT IN ('<real_id>')`
- **Delete test hosts**: `DELETE FROM hosts WHERE network NOT IN (SELECT network_id FROM networks)`
- **Delete test memories**: Check `host_memories` state for test MAC addresses
- **Verify**: `python3 -c "from agent.db import *; init_db('logs'); print(get_networks()); print(len(get_hosts()))"`
- Never leave fake data (HomeWifi, ClientOffice, FF:EE:DD:CC:BB:AA, etc.) in production DB

## Known Issues
- Q4_K_M on GPU: extremely slow (falls back to generic kernels)
- 4B Q8_0: fails to load (exceeds 1GB per-allocation limit)
- Vulkan: dead end (vendor=1.1, Mesa Turnip=DeviceLostError)
- OpenCL embedded kernels: 60+ min JIT (use non-embedded)
- 2B model garbage rate ~50% — handled by garbage detection + context reset
- Context overflow at old 4096 limit — fixed with 8192
