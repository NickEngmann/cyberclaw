PHASE 2: ENUMERATION — Probe services on discovered hosts.

Known hosts with open ports:
- 192.168.1.2 (Raspberry Pi): 22/ssh, 53/dns, 80/http, 139/netbios, 443/https, 445/smb, 5900/vnc
- 192.168.1.15: 22/ssh, 53/dns
- 192.168.1.20 (Raspberry Pi): 22/ssh
- 192.168.1.25 (Amazon): 8888/http

DO NOT run nmap again. Instead probe the services directly:
- curl -s -I http://192.168.1.2/
- curl -sk https://192.168.1.2/
- curl -s http://192.168.1.25:8888/
- smbclient -N -L //192.168.1.2/
- nmap -sV -T2 -p 22,80,445 192.168.1.2 (ALWAYS use -T2, never -T4)
- dig @192.168.1.2 version.bind chaos txt

EXIT: 1+ vulnerability or credential found
