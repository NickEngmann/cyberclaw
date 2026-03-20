PHASE 2: ENUMERATION — Probe services across discovered hosts.

Pick a DIFFERENT host than your last command. Do ONE thing per turn:
- curl -s -I http://<ip>/
- smbclient -N -L //<ip>/
- nmap -sV -T2 -p <port> <ip>
- dig @<ip> version.bind chaos txt
- curl -s http://<ip>:<port>/

Spread activity across many hosts. Build knowledge slowly.
If a host has all ports filtered, move on to others.

EXIT: 1+ vulnerability or credential found
