"""Read-only, credential-free inventory of the existing VPN."""

import json
from pathlib import Path
import subprocess

for path in (Path("/etc/xray-multitool/config.json"), Path("/etc/sing-box/config.json")):
    data = json.loads(path.read_text())
    print(path)
    print(
        json.dumps(
            {
                "inbounds": [
                    {k: x.get(k) for k in ("tag", "type", "protocol", "listen", "listen_port", "port")}
                    for x in data.get("inbounds", [])
                ],
                "outbounds": [
                    {
                        **{k: x.get(k) for k in ("tag", "type", "protocol")},
                        "sockopt": x.get("streamSettings", {}).get("sockopt"),
                    }
                    for x in data.get("outbounds", [])
                ],
                "routing": data.get("routing"),
                "route_options": {
                    k: data.get("route", {}).get(k)
                    for k in ("auto_detect_interface", "default_mark", "default_interface")
                },
            },
            indent=2,
        )
    )
for args in (
    ["ss", "-lntp"],
    ["docker", "version", "--format", "{{.Server.Version}}"],
    ["systemctl", "show", "xray-multitool", "sing-box", "-p", "MainPID", "-p", "ActiveState"],
):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        print(" ".join(args), result.stdout[:5000], result.stderr[:300])
    except FileNotFoundError:
        print(args[0], "not installed")
