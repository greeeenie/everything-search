"""Read-only inventory. Never print credentials or complete Xray configs."""

import json
import pathlib
import subprocess

path = pathlib.Path("/usr/local/x-ui/bin/config.json")
if path.exists():
    config = json.loads(path.read_text())
    print(
        json.dumps(
            {
                "routing": config.get("routing"),
                "outbounds": [
                    {k: x.get(k) for k in ("tag", "protocol", "sendThrough")}
                    for x in config.get("outbounds", [])
                ],
                "inbounds": [
                    {k: x.get(k) for k in ("tag", "protocol", "listen", "port")}
                    for x in config.get("inbounds", [])
                ],
            },
            indent=2,
        )
    )
for command in [
    ["ip", "rule", "show"],
    ["ip", "route", "show"],
    ["systemctl", "show", "x-ui", "-p", "MainPID", "-p", "ActiveState", "-p", "ExecMainStartTimestamp"],
    ["ss", "-lnt"],
    ["sshd", "-T"],
]:
    output = subprocess.run(command, capture_output=True, text=True).stdout
    if command[0] == "sshd":
        output = "\n".join(
            x
            for x in output.splitlines()
            if x.startswith(("gatewayports ", "allowtcpforwarding ", "permitlisten ", "permitrootlogin "))
        )
    print(" ".join(command), output)
