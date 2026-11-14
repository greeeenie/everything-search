"""Read-only checks on Pi after deployment, including original VPN hashes."""

import hashlib
import json
from pathlib import Path
import subprocess


def output(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout.strip()


report = {"services": {}, "listeners": []}
for service in (
    "research-egress",
    "research-v2",
    "research-worker",
    "research-discovery",
    "research-link",
    "research-maintenance.timer",
):
    report["services"][service] = {
        "active": output("systemctl", "is-active", service),
        "enabled": output("systemctl", "is-enabled", service),
    }
for line in output("ss", "-lnt").splitlines()[1:]:
    if any(":" + str(port) in line for port in (8765, 8770, 8088, 10808)):
        report["listeners"].append(line.split()[3])
snapshots = sorted(Path("/var/backups/research-v2").glob("*/vpn-hashes.json"))
if snapshots:
    expected = json.loads(snapshots[0].read_text())
    report["vpn_files_unchanged"] = {
        name: hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest
        for name, digest in expected.items()
    }
report["vpn_services"] = output(
    "systemctl", "show", "xray-multitool", "sing-box", "-p", "MainPID", "-p", "ActiveState"
)
print(json.dumps(report, indent=2))
