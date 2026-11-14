#!/usr/bin/env python3
"""Prepare a constrained reverse SSH listener and a loopback socket relay.

Preparation is separate from activation so the Pi can be tested first.
Public key and known_hosts contain no credentials.
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import pwd
import shutil
import subprocess


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "activate", "rollback"])
    parser.add_argument("--public-key")
    args = parser.parse_args()
    state = Path("/var/lib/research-relay")
    if args.action == "prepare":
        if not args.public_key:
            raise SystemExit("--public-key file required")
        key = Path(args.public_key).read_text().strip()
        if not key.startswith("ssh-ed25519 ") or "\n" in key:
            raise SystemExit("Expected a single ed25519 public key")
        try:
            pwd.getpwnam("research-relay")
        except KeyError:
            run(
                "useradd",
                "--system",
                "--create-home",
                "--home-dir",
                str(state),
                "--shell",
                "/usr/sbin/nologin",
                "research-relay",
            )
            run("usermod", "-p", "*", "research-relay")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = Path("/var/backups/research-v2-relay") / stamp
        backup.mkdir(mode=0o700, parents=True)
        for name in (
            "/etc/systemd/system/reddit-research-mcp.service",
            "/etc/systemd/system/reddit-research-tunnel.service",
        ):
            if Path(name).exists():
                shutil.copy2(name, backup)
        ssh = state / ".ssh"
        ssh.mkdir(mode=0o700, exist_ok=True)
        authorized = ssh / "authorized_keys"
        if authorized.exists():
            shutil.copy2(authorized, backup / "authorized_keys")
        authorized.write_text(
            'restrict,port-forwarding,permitlisten="127.0.0.1:8766",command="/bin/false" ' + key + "\n"
        )
        authorized.chmod(0o600)
        run("chown", "-R", "research-relay:research-relay", str(state))
        ssh_config = Path("/etc/ssh/sshd_config.d/90-research-relay.conf")
        if ssh_config.exists():
            shutil.copy2(ssh_config, backup / ssh_config.name)
        ssh_config.write_text("""Match User research-relay
    AllowTcpForwarding remote
    GatewayPorts no
    PermitListen 127.0.0.1:8766
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    PasswordAuthentication no
    KbdInteractiveAuthentication no
Match all
""")
        run("sshd", "-t")
        run("systemctl", "reload", "ssh")
        Path("/etc/systemd/system/research-relay.socket").write_text("""[Unit]
Description=Loopback MCP relay to home Raspberry Pi

[Socket]
ListenStream=127.0.0.1:8765
NoDelay=true

[Install]
WantedBy=sockets.target
""")
        executable = next(
            (
                p
                for p in ("/usr/lib/systemd/systemd-socket-proxyd", "/lib/systemd/systemd-socket-proxyd")
                if Path(p).exists()
            ),
            None,
        )
        if not executable:
            raise SystemExit("systemd-socket-proxyd unavailable")
        Path("/etc/systemd/system/research-relay.service").write_text(f"""[Unit]
Description=SSH-backed MCP transport relay
Requires=research-relay.socket
After=network.target

[Service]
ExecStart={executable} 127.0.0.1:8766
DynamicUser=true
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
""")
        run("systemctl", "daemon-reload")
        print("Relay prepared. Existing Research still serves port 8765.")
    elif args.action == "activate":
        # Fail before switching if the Pi endpoint is not reachable.
        run("curl", "--fail", "--max-time", "10", "http://127.0.0.1:8766/health")
        unit = Path("/etc/systemd/system/reddit-research-tunnel.service")
        original = Path("/var/backups/research-v2-relay/original-tunnel.service")
        if not original.exists():
            shutil.copy2(unit, original)
        # systemd does not remove Requires= dependencies through an empty drop-in.
        # Edit only the old backend dependency in the backed-up owning unit.
        content = original.read_text()
        content = content.replace("Requires=reddit-research-mcp.service", "Requires=research-relay.socket")
        content = content.replace("After=reddit-research-mcp.service", "After=research-relay.socket")
        unit.write_text(content)
        dropin = Path("/etc/systemd/system/reddit-research-tunnel.service.d/research-relay.conf")
        dropin.unlink(missing_ok=True)
        run("systemctl", "daemon-reload")
        run("systemctl", "stop", "reddit-research-tunnel")
        run("systemctl", "disable", "--now", "reddit-research-mcp")
        run("systemctl", "enable", "--now", "research-relay.socket")
        run("systemctl", "start", "reddit-research-tunnel")
        print("Existing authenticated MCP tunnel now relays to Raspberry Pi.")
    else:
        run("systemctl", "stop", "reddit-research-tunnel")
        run("systemctl", "disable", "--now", "research-relay.socket")
        subprocess.run(["systemctl", "stop", "research-relay.service"], capture_output=True)
        Path("/etc/systemd/system/reddit-research-tunnel.service.d/research-relay.conf").unlink(
            missing_ok=True
        )
        original = Path("/var/backups/research-v2-relay/original-tunnel.service")
        if original.exists():
            shutil.copy2(original, "/etc/systemd/system/reddit-research-tunnel.service")
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", "reddit-research-mcp")
        run("systemctl", "start", "reddit-research-tunnel")
        print("Original VPS Research restored.")


if __name__ == "__main__":
    main()
