#!/usr/bin/env python3
"""UID-scoped direct egress for the separate marketplace trial on this Pi."""

import pwd
import subprocess
import sys


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


uid = pwd.getpwnam("marketplace-trial").pw_uid
expected = f"uidrange {uid}-{uid} lookup main"
rules = run("ip", "rule", capture_output=True).stdout
if any(line.startswith("51:") and expected not in line for line in rules.splitlines()):
    raise SystemExit("Routing priority 51 is already occupied")
if sys.argv[1] == "stop":
    subprocess.run(["nft", "delete", "table", "inet", "marketplace_trial"], capture_output=True)
    if expected in rules:
        run("ip", "rule", "del", "pref", "51", "uidrange", f"{uid}-{uid}", "lookup", "main")
    raise SystemExit(0)
existing = run("nft", "list", "ruleset", capture_output=True).stdout
if "0x00002024" not in existing:
    raise SystemExit("Expected sing-box bypass mark was not found")
prefix = "delete table inet marketplace_trial\n" if "table inet marketplace_trial" in existing else ""
configuration = prefix + f"""table inet marketplace_trial {{
 chain output_mark {{
  type route hook output priority -159; policy accept;
  meta skuid {uid} meta mark set 0x2024 ct mark set meta mark
 }}
}}
"""
run("nft", "-c", "-f", "-", input=configuration)
run("nft", "-f", "-", input=configuration)
if expected not in rules:
    run("ip", "rule", "add", "pref", "51", "uidrange", f"{uid}-{uid}", "lookup", "main")
print("Marketplace trial RU egress ready for UID", uid)
