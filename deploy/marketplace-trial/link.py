#!/usr/bin/env python3
"""Connect the trial to the existing authenticated OpenAI tunnel, with rollback."""

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import pwd
import shutil
import subprocess


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def private_copy(source, destination, owner=0, group=0):
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o600)
    os.chown(destination, owner, group)


parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["pi-export", "vps-prepare", "pi-link", "vps-activate", "vps-rollback"])
args = parser.parse_args()
if os.geteuid() != 0:
    raise SystemExit("Run as root")

if args.action == "pi-export":
    account = pwd.getpwnam("marketplace-trial")
    state = Path(account.pw_dir)
    if not (state / "link_key").exists():
        run("sudo", "-u", account.pw_name, "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(state / "link_key"))
    shutil.copyfile(state / "link_key.pub", "/tmp/marketplace-link.pub")
    values = dict(line.split("=", 1) for line in Path("/etc/marketplace-trial/server.env").read_text().splitlines())
    exported = Path("/tmp/marketplace-bearer")
    exported.write_text("Bearer " + values["MCP_HTTP_AUTH_TOKEN"])
    exported.chmod(0o600)
    operator = pwd.getpwnam("multitool")
    os.chown(exported, operator.pw_uid, operator.pw_gid)

elif args.action == "vps-prepare":
    backup = Path("/var/backups/marketplace-trial") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True, mode=0o700)
    try:
        account = pwd.getpwnam("marketplace-link")
    except KeyError:
        run("useradd", "--system", "--create-home", "--home-dir", "/var/lib/marketplace-link", "--shell", "/usr/sbin/nologin", "marketplace-link")
        account = pwd.getpwnam("marketplace-link")
    ssh = Path(account.pw_dir) / ".ssh"
    ssh.mkdir(mode=0o700, exist_ok=True)
    os.chown(ssh, account.pw_uid, account.pw_gid)
    authorized = ssh / "authorized_keys"
    key = Path("/tmp/marketplace-link.pub").read_text().strip()
    if not key.startswith("ssh-ed25519 ") or "\n" in key:
        raise SystemExit("Expected one Ed25519 public key")
    line = 'restrict,port-forwarding,permitlisten="127.0.0.1:8791" ' + key + "\n"
    if authorized.exists():
        private_copy(authorized, backup / "authorized_keys")
    authorized.write_text(line)
    authorized.chmod(0o600)
    os.chown(authorized, account.pw_uid, account.pw_gid)
    policy = Path("/etc/ssh/sshd_config.d/marketplace-trial.conf")
    if policy.exists():
        private_copy(policy, backup / policy.name)
    policy.write_text("""Match User marketplace-link
 AuthenticationMethods publickey
 PasswordAuthentication no
 AllowTcpForwarding remote
 PermitListen 127.0.0.1:8791
 GatewayPorts no
 PermitTTY no
 X11Forwarding no
 AllowAgentForwarding no
 MaxSessions 0
Match all
""")
    run("sshd", "-t")
    run("systemctl", "reload", "ssh")
    configuration = Path("/etc/marketplace-trial")
    configuration.mkdir(mode=0o700, exist_ok=True)
    encrypted = configuration / "marketplace-auth.cred"
    if encrypted.exists():
        private_copy(encrypted, backup / encrypted.name)
    temporary = configuration / "marketplace-auth.new"
    run("systemd-creds", "encrypt", "--name=marketplace-auth", "/tmp/marketplace-bearer", str(temporary))
    temporary.chmod(0o600)
    temporary.replace(encrypted)
    shutil.copyfile("/etc/ssh/ssh_host_ed25519_key.pub", "/tmp/marketplace-vps-host.pub")

elif args.action == "pi-link":
    account = pwd.getpwnam("marketplace-trial")
    known = Path(account.pw_dir) / "known_hosts"
    key = Path("/tmp/marketplace-vps-host.pub").read_text().split()
    if len(key) < 2 or key[0] != "ssh-ed25519":
        raise SystemExit("Expected verified VPS host key")
    known.write_text("216.173.68.140 " + " ".join(key[:2]) + "\n")
    known.chmod(0o600)
    os.chown(known, account.pw_uid, account.pw_gid)
    Path("/etc/systemd/system/marketplace-trial-link.service").write_text("""[Unit]
Description=Reverse SSH transport for the marketplace trial
Requires=marketplace-trial-egress.service
After=network-online.target marketplace-trial-egress.service marketplace-trial.service
Wants=network-online.target

[Service]
User=marketplace-trial
Group=marketplace-trial
ExecStart=/usr/bin/ssh -N -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/var/lib/marketplace-trial/known_hosts -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -i /var/lib/marketplace-trial/link_key -R 127.0.0.1:8791:127.0.0.1:8790 marketplace-link@216.173.68.140
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
""")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "marketplace-trial-link")

elif args.action == "vps-activate":
    override = Path("/etc/systemd/system/reddit-research-tunnel.service.d/marketplace-trial.conf")
    override.parent.mkdir(exist_ok=True)
    if override.exists():
        raise SystemExit("Trial tunnel override already exists")
    wrapper = Path("/opt/reddit-research-mcp/run-marketplace-trial")
    wrapper.write_text("""#!/bin/sh
set -eu
CONTROL_PLANE_API_KEY=$(cat "$CREDENTIALS_DIRECTORY/control-plane-api-key")
export CONTROL_PLANE_API_KEY
exec /opt/reddit-research-mcp/tunnel-client run \\
 --mcp.server-url http://127.0.0.1:8791/mcp \\
 --mcp.extra-headers "Authorization: file:$CREDENTIALS_DIRECTORY/marketplace-auth" \\
 --mcp.extra-headers "X-MCP-Tenant: marketplace-trial" \\
 --mcp.startup-wait-timeout 20s \\
 --health.listen-addr 127.0.0.1:0 \\
 --health.url-file /run/reddit-research-tunnel/health-url
""")
    wrapper.chmod(0o755)
    override.write_text("""[Service]
LoadCredentialEncrypted=marketplace-auth:/etc/marketplace-trial/marketplace-auth.cred
ExecStart=
ExecStart=/opt/reddit-research-mcp/run-marketplace-trial
""")
    run("systemctl", "daemon-reload")
    run("systemctl", "restart", "reddit-research-tunnel")

elif args.action == "vps-rollback":
    Path("/etc/systemd/system/reddit-research-tunnel.service.d/marketplace-trial.conf").unlink(missing_ok=True)
    run("systemctl", "daemon-reload")
    run("systemctl", "restart", "reddit-research-tunnel")

print(args.action, "complete")
