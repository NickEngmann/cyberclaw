PHASE 1: RECON — Map the network, find targets.

Your goal: discover live hosts and their open ports on 192.168.1.0/24.

IMPORTANT NMAP RULES:
- NEVER combine -sn with -p or --top-ports (they conflict!)
- -sn means ping sweep ONLY (no port scan)
- -sS means SYN port scan (needs -p or --top-ports)
- Always use -T2 for stealth

START with this EXACT command for your first turn:
nmap -sn -T2 192.168.1.0/24

Then for each host found, scan its ports:
nmap -sS -T2 --top-ports 100 192.168.1.X

Other useful recon commands:
- arp-scan --localnet
- nbtscan 192.168.1.0/24
- dig @192.168.1.X any example.com
- curl -s -I http://192.168.1.X/

EXIT: 3+ live hosts with open services identified
