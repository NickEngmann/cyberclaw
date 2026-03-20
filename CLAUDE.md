# Nightcrawler - Mobile Autonomous Pentest Agent

## Project
Nightcrawler is an autonomous penetration testing agent running on a OnePlus 8 with Kali NetHunter. It uses a local Qwen3.5-2B model as its reasoning engine and the official `mcp-kali-server` as its tool interface, with a scope enforcement proxy as the safety layer.

- GitHub: github.com/NickEngmann/nightcrawler
- Install: `/opt/nightcrawler/` (production), `/root/nightcrawler/` (dev)
- Architecture doc: `docs/ARCHITECTURE.md`

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
| 5000 | kali-server-mcp | Official Kali MCP (raw command execution) |
| 8080 | llama-server | Qwen3.5-2B Q8_0 via llama.cpp |
| 8800 | scope-proxy | Scope enforcement + rate limit + audit |
| 8888 | web UI | Dashboard (Tailscale IP only) |

## GPU Inference (OpenCL)
llama.cpp compiled in Termux with Adreno-optimized OpenCL kernels. Must run from Android root shell as Termux user (UID 10393), NOT from Kali chroot. See `docs/COMMANDS.md` for all commands.

```bash
TERMUX_HOME=/data/data/com.termux/files/home
su 10393 -c "export LD_LIBRARY_PATH=/vendor/lib64; \
  export GGML_OPENCL_PLATFORM=0; export GGML_OPENCL_DEVICE=0; \
  cd $TERMUX_HOME/llama.cpp/ggml/src/ggml-opencl/kernels; \
  $TERMUX_HOME/llama.cpp/build-fast/bin/llama-server \
  -m $TERMUX_HOME/models/Qwen3.5-2B-Q8_0.gguf \
  -ngl 99 -c 4096 -t 4 --port 8080 --jinja \
  --chat-template-kwargs '{\"enable_thinking\":false}' --log-disable"
```

### Key constraints
- **Must run as root** (not su 10393) — Termux UID can't bind network ports via su
- First run after reboot: ~3 min kernel JIT (cached after)
- Context: 4096 tokens (works fine with 2B Q8_0)
- Q8_0 is fastest on GPU. Never use Q4_K_M on GPU (10x slower, generic kernels)
- 4B Q8_0 fails: exceeds 1GB per-allocation limit. Use Q4_0 for 4B.
- llama.cpp patches: `patches/llama-cpp-opencl-adreno650.patch`

## Performance
| Model | Quant | Backend | Prompt | Generation |
|-------|-------|---------|--------|------------|
| Qwen3.5-2B | Q8_0 | **OpenCL GPU** | **23.3 t/s** | **4.8 t/s** |
| Qwen3.5-0.8B | Q8_0 | OpenCL GPU | 30.5 t/s | 6.3 t/s |
| Qwen3.5-4B | Q4_0 | OpenCL GPU | 10.1 t/s | 2.0 t/s |

## Nightcrawler Stack
```
Agent (main.py) → LLM (llama.cpp :8080) → REASONING + COMMAND
    ↓
Scope Proxy (:8800) → validates IPs, ports, destructive cmds
    ↓
kali-server-mcp (:5000) → raw terminal execution
    ↓
Kali Linux tools (nmap, aircrack-ng, hydra, nxc, gobuster, ...)
```

## Running
```bash
# Dry-run (mock kali server, no real commands)
NC_DRY_RUN=1 python3 main.py

# Full launch in tmux
bash scripts/launch.sh

# Install to /opt/nightcrawler
bash scripts/install.sh
```

## Development Notes
- Agent auto-detects network: skips WiFi breach phase if already connected
- Thor (AGX 128GB) is optional — agent operates fully standalone
- Web UI binds to Tailscale IP only (not exposed on target network)
- All commands audited to `logs/commands.jsonl` regardless of allow/block

## Magisk Modules
| Module | Purpose |
|--------|---------|
| openssh (v9.9p2) | Persistent SSH on ports 9022 + 22 |
| adreno-650_819v2 | GPU driver v819.2 (E031.50) |
| nethunter (v1.4.0) | Kali chroot + tools |
| tailscaled | Tailscale VPN |

## CRITICAL: llama-server Rules
- **NEVER start a second llama-server process** — check `pgrep llama-server` first
- **NEVER kill llama-server from Kali chroot** — only from Android shell (port 9022)
- **Context window: 8192 tokens** — do not increase without user approval
- Dual llama-server processes caused OOM crash on 2026-03-20 (~6GB × 2 = phone reboot)
- Auto-starts 20 min after boot via Magisk watchdog (with 20 min crash cooldown)
- Manual start: SSH to port 9022 and run `bash /data/local/nhsystem/kalifs/root/nightcrawler/scripts/start-llm.sh`

## Known Issues
- Q4_K_M on GPU: extremely slow (falls back to generic kernels)
- 4B Q8_0: fails to load (exceeds 1GB per-allocation limit)
- Vulkan: dead end (vendor=1.1, Mesa Turnip=DeviceLostError)
- OpenCL embedded kernels: 60+ min JIT (use non-embedded)
- `llama-server` runs as root on Android side (not in chroot, not as Termux user)
- From Kali chroot, the agent reaches it at http://127.0.0.1:8080 (shared network namespace)
- WebUI daemon: `bash /root/nightcrawler/scripts/webui-daemon.sh start`
- WebUI HTTPS: https://kali.taileba694.ts.net:8888 (self-signed cert in certs/)
