#!/usr/bin/env python3
import argparse
import ipaddress
import os
from pathlib import Path
import pwd
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument("--vps-ip", required=True)
parser.add_argument("--host-public-key", required=True)
args = parser.parse_args()
address = str(ipaddress.IPv4Address(args.vps_ip))
key = Path(args.host_public_key).read_text().strip()
if not key.startswith("ssh-ed25519 ") or "\n" in key:
    raise SystemExit("A verified single-line host public key is required")
home = Path("/var/lib/research-v2")
known = home / "known_hosts"
known.write_text(address + " " + key + "\n")
known.chmod(0o600)
account = pwd.getpwnam("research-v2")
os.chown(known, account.pw_uid, account.pw_gid)
Path("/etc/research-v2/link.env").write_text("RESEARCH_VPS=research-relay@" + address + "\n")
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(["systemctl", "enable", "--now", "research-link"], check=True)
