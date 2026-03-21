# Nightcrawler — Feature Reference

Cross-referenced from `CLAUDE.md` (operational guide) and `docs/ARCHITECTURE.md` (system design).

## Exploit Pipeline

### CVE Database (`agent/cve_db.py` + `data/cve_exploits.json`)
- **24,956 entries** across 62 services, built from exploit-db CSV + manual curation
- Version-aware regex matching: "OpenSSH 9.2p1" → CVE-2024-6387 (regreSSHion)
- Returns ready-to-run commands, not just CVE IDs
- Replaces searchsploit entirely — instant lookups (2.3ms) vs slow CLI with wrong syntax
- 5.1MB on disk, ~50MB RAM, CPU-only
- Loaded once at startup, queried by hint system on every host selection

### Exploit Playbooks (`data/playbooks.json`)
- 11 multi-step attack sequences triggered by host memory observations
- `repeatable: false` = one-and-done (recon/enumeration playbooks)
- `repeatable: true` = can retry up to 3 times (credential attacks)
- Completion persisted in SQLite (survives phone restarts)
- Steps fed via multi-turn mode with specific next-step commands
- Share names extracted from actual observations (not hardcoded)
- Playbooks: smb_share_read, pihole_exploit, dns_zone_transfer, samba_deep, http_deep, vnc_attack, ssh_full_attack, telnet_attack, ftp_attack, redis_attack, mysql_attack

### Output Parser (`agent/output_parser.py`)
- Extracts structured intelligence from raw command output
- nmap vulners → CVE IDs → vulnerability DB + trigger CVE DB for next command
- smbclient ls → interesting files (.conf, .env, .bak) → host memory
- dig axfr → hostnames → new scan targets
- nxc [+] → credentials → DB + suggest post-exploit follow-up
- nikto → findings → vulnerability DB
- curl robots.txt → disallowed paths → suggest probing
- impacket-samrdump → usernames → suggest credential testing
- Service fingerprints → structured port→service cache in SQLite
- Checks for NT_STATUS errors before extracting (no false positives)

### Attack Planner (`agent/attack_planner.py`)
- Strategic directives injected into system prompt (cached, refreshed every ~50 commands)
- Identifies: no-follow-through hosts, untested SSH, unexplored ports
- Highlights confirmed access points and suggests priorities
- Answers: "Given everything we know, what should we focus on?"
- The 2B model can't strategize — the planner strategizes for it

### Smart Host Targeting
- **Priority weighting**: high (50%) for confirmed access, medium (35%) untested, low (15%) failed, exhausted (0%) for 5+ failures
- **Failure memory**: records every failed credential attempt (e.g., "FAILED SSH admin:admin")
- **Tried-action dedup**: records searchsploit/axfr/enum4linux/nikto/impacket attempts, hints filter them out
- **Multi-turn mode**: 2-3 consecutive commands on high-priority hosts without rotation
- Max 30 observations per host (auto-prunes old agent observations)

### Exploit Chain Tracking
- Vulnerabilities stored with `chain` column: the sequence of commands that found them
- Example: "smbclient -N -L //192.168.1.2/ → shares: share, nobody"
- Used in report generation to show clients how to reproduce/patch

## Pipeline Validation

Commands pass through 6 validation gates before execution:
1. **Dead-end skip**: hosts marked dead-end in host_memory are skipped
2. **Same-host enforcement**: rejects if target IP == last executed IP (unless multi-turn)
3. **_is_valid_command()**: rejects fake paths, placeholders, -T3+, missing target IP
4. **_is_duplicate()**: exact match against last 5 commands
5. **_dedup_ports()**: cleans duplicate ports in nmap -p lists
6. **Time-based stuck detection**: 5min without a command = force context reset

All rejection paths increment `garbage_streak`. At streak 5, `_reset_context_with_fewshot()` fires.

## Web UI & C2

### Dashboard (`:8888`)
- Separate process from agent (agent/ui_bridge.py writes to SQLite, webui reads)
- Stealth middleware: rejects connections from target network (192.168.x), allows localhost + Tailscale
- Server header spoofed as "nginx" — blue team scanners see dead-looking nginx
- Responsive: ~90ms API calls (was 10s+ when in-process with agent)

### C2 Controls
- Star/blacklist hosts, force phase, pause/resume, kill switch
- Command injection, tool preferences, config panel
- Host memory editing, network observations

### Report Generation (`/api/report`)
- REPORT button downloads formatted pentest report
- Includes: executive summary, vulnerabilities with exploit chains, remediation advice, credentials, host inventory
- Auto-generated remediation per finding type (SMB null session → disable anonymous access, etc.)
- Severity breakdown: critical/high/medium/low counts

### Vulnerability & Credential Tracking
- Auto-recorded from command output (SMB shares, Pi-hole, NSE VULNERABLE, nxc [+])
- Deduplication: same host+vuln won't double-insert
- Visible in web UI Credentials & Vulnerabilities section
- Exported in reports with exploit chains

## Security

### Port Binding
| Port | Service | Binding | Exposure |
|------|---------|---------|----------|
| 8080 | llama-server | 127.0.0.1 | Localhost only |
| 5000 | kali-mcp | 127.0.0.1 | Localhost only |
| 8800 | scope-proxy | 127.0.0.1 | Localhost only |
| 8888 | webui | 0.0.0.0 | Stealth filtered (localhost + Tailscale only) |
| 22 | Kali SSH | 0.0.0.0 | Exposed (auth required) |
| 9022 | Android SSH | 0.0.0.0 | Exposed (auth required) |

### Stealth
- nmap timing enforced: -T2 only (never -T3+)
- Patient rotation: one action per host per turn
- Host rotation: same-host enforcement prevents scanning bursts
- Web UI spoofs nginx headers, returns empty 404 to target network

## Deferred to Thor (`docs/THOR_DEFERRED.md`)
- Full NVD/Vulners offline mirror (too much RAM for phone)
- Thinking/reasoning mode for exploit planning (2B token budget too small)
- Complex metasploit integration (msfconsole syntax too complex for 2B)
