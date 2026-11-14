#!/usr/bin/env python3
"""Install an independent UID-scoped bypass for the existing sing-box mark.

No existing VPN file or firewall table is edited. Run as root after inventory.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import pwd
import shutil
import subprocess


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "start", "stop"])
    parser.add_argument("--user", default="research-v2")
    parser.add_argument("--bypass-mark", default="0x2024")
    args = parser.parse_args()
    uid = pwd.getpwnam(args.user).pw_uid
    mark = int(args.bypass_mark, 0)
    if args.action == "stop":
        subprocess.run(["nft", "delete", "table", "inet", "research_egress"], capture_output=True)
        subprocess.run(
            ["ip", "rule", "del", "pref", "50", "uidrange", f"{uid}-{uid}", "lookup", "main"],
            capture_output=True,
        )
        return
    if args.action == "install":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = Path("/var/backups/research-v2") / stamp
        backup.mkdir(parents=True, mode=0o700)
        hashes = {}
        for name in ("/etc/xray-multitool/config.json", "/etc/sing-box/config.json"):
            path = Path(name)
            if path.exists():
                shutil.copy2(path, backup / (path.parent.name + "-config.json"))
                hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        (backup / "vpn-hashes.json").write_text(json.dumps(hashes, indent=2))
        for command, filename in (
            (["nft", "list", "ruleset"], "nft.txt"),
            (["ip", "rule"], "ip-rule.txt"),
            (["ip", "route", "show", "table", "all"], "routes.txt"),
        ):
            (backup / filename).write_text(run(*command, capture_output=True).stdout)
        shutil.copy2(__file__, "/usr/local/sbin/research-egress")
        Path("/usr/local/sbin/research-egress").chmod(0o755)
        unit = f"""[Unit]
Description=Research UID-specific direct RU routing
After=network-online.target sing-box.service
Before=research-v2.service research-worker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/research-egress start --user {args.user} --bypass-mark {args.bypass_mark}
ExecStop=/usr/local/sbin/research-egress stop --user {args.user}

[Install]
WantedBy=multi-user.target
"""
        Path("/etc/systemd/system/research-egress.service").write_text(unit)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "research-egress.service")
    rules = run("ip", "rule", capture_output=True).stdout
    expected = f"uidrange {uid}-{uid} lookup main"
    if any(line.startswith("50:") and expected not in line for line in rules.splitlines()):
        raise SystemExit("Routing priority 50 already used; inspect before installing")
    existing = run("nft", "list", "ruleset", capture_output=True).stdout
    if "table inet sing-box" in existing and f"0x{mark:08x}" not in existing:
        raise SystemExit("Requested bypass mark absent from existing sing-box rules")
    ruleset = f"""table inet research_egress {{
 chain mark_output {{
  type route hook output priority -160; policy accept;
  meta skuid {uid} meta mark set {mark} ct mark set meta mark
 }}
}}
"""
    # Only replace the table this installer owns, atomically.
    if (
        subprocess.run(["nft", "list", "table", "inet", "research_egress"], capture_output=True).returncode
        == 0
    ):
        ruleset = "delete table inet research_egress\n" + ruleset
    run("nft", "-c", "-f", "-", input=ruleset)
    run("nft", "-f", "-", input=ruleset)
    if expected not in rules:
        run("ip", "rule", "add", "pref", "50", "uidrange", f"{uid}-{uid}", "lookup", "main")
    print("Research egress installed for UID", uid)


if __name__ == "__main__":
    main()
