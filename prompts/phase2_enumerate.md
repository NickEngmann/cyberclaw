PHASE 2: ENUMERATION — Deep-dive discovered services.

For each discovered host and service, enumerate thoroughly:

SMB (445):
- nxc smb <target> --shares -u '' -p ''
- nxc smb <target> --users -u '' -p ''
- enum4linux -a <target>
- smbclient -N -L //<target>/

HTTP (80/443/8080):
- curl -s -I http://<target>/ (grab headers, server version)
- gobuster dir -u http://<target>/ -w /usr/share/wordlists/dirb/common.txt -q
- nikto -h http://<target>/ -Tuning 1

SSH (22):
- Check for banner: curl -s telnet://<target>:22 or nmap -sV -p 22 <target>

DNS (53):
- dig @<target> any <domain>
- dig @<target> axfr <domain>

MySQL (3306) / PostgreSQL (5432):
- nxc mysql <target> -u root -p ''
- nxc postgres <target> -u postgres -p ''

Try null sessions, anonymous access, and default credentials.
Respect lockout thresholds — if you see lockouts, STOP spraying.

EXIT: 1+ vulnerability or credential found, OR all services enumerated
