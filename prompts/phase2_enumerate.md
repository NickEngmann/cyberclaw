PHASE 2: ENUMERATION — Probe services on discovered hosts.

Known hosts:
- 192.168.1.2: ports 22,53,80,139,443,445,5900 (Raspberry Pi)
- 192.168.1.15: ports 22,53
- 192.168.1.20: port 22 (Raspberry Pi)
- 192.168.1.25: port 8888 (Amazon device)

Enumerate ONE service at a time. Good next commands:
- curl -s -I http://192.168.1.2/
- curl -s -I https://192.168.1.2/ -k
- curl -s http://192.168.1.25:8888/
- nmap -sV -T2 -p 22,53,80,443,445,5900 192.168.1.2
- smbclient -N -L //192.168.1.2/
- nmap -sV -T2 -p 22 192.168.1.15

EXIT: 1+ vulnerability or credential found
