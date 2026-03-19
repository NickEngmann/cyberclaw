PHASE 1: RECON — Map the network, find targets.

Your goal is to discover all live hosts and their open services on the target subnet.

Recommended approach:
1. Start with a ping sweep: nmap -sn 192.168.1.0/24
2. Follow up with a stealth SYN scan on discovered hosts: nmap -sS -T2 --top-ports 1000 <target>
3. For interesting hosts, do deeper scans: nmap -sV -T2 -p- <target>
4. Use nbtscan for NetBIOS: nbtscan 192.168.1.0/24
5. Use arp-scan for layer 2: arp-scan --localnet

Look for:
- Domain Controllers: ports 88 (Kerberos) + 445 (SMB) + 389 (LDAP)
- Web servers: ports 80, 443, 8080, 8443
- Databases: ports 3306 (MySQL), 5432 (PostgreSQL), 1433 (MSSQL)
- SSH: port 22
- File shares: ports 445, 139, 2049 (NFS)
- Printers: port 9100, 631 (skip unless they offer pivot)

EXIT: 3+ live hosts with open services identified, OR 30 min timeout
