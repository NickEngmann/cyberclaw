PHASE 1: RECON — Discover hosts and ports slowly.

Start with a ping sweep, then probe ONE host per turn:
- nmap -sn -T2 192.168.1.0/24
- nmap -sS -T2 --top-ports 100 <single ip>
- arp-scan --localnet

Do NOT scan all hosts at once. One host per turn, rotate.

EXIT: 3+ live hosts with open services
