#!/usr/bin/env python3
"""Configure new Research services; generate secrets directly on the host."""

from pathlib import Path
import os
import pwd
import secrets
import shutil
import subprocess

root = Path("/opt/research-v2")
for directory in ("scripts", "research_v2", "deploy", "config"):
    base = root / directory
    for path in [base, *base.rglob("*")]:
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)
config = Path("/etc/research-v2")
config.mkdir(mode=0o755, exist_ok=True)
for source in (root / "deploy").glob("*.service"):
    shutil.copy2(source, Path("/etc/systemd/system") / source.name)
for source in (root / "deploy").glob("*.timer"):
    shutil.copy2(source, Path("/etc/systemd/system") / source.name)
for name, group in [("research.env", "research-v2"), ("searxng.env", "searxng-research")]:
    path = config / name
    if path.exists():
        continue
    if name == "research.env":
        text = (
            "\n".join(
                [
                    "RESEARCH_CONFIG=/etc/research-v2/egress.yaml",
                    "RESEARCH_DATABASE=/var/lib/research-v2/research.sqlite3",
                    "RESEARCH_EU_PROXY=socks5://127.0.0.1:10808",
                    "RESEARCH_RU_PROXY=",
                    "RESEARCH_WORKER_TOKEN=" + secrets.token_urlsafe(32),
                    "RESEARCH_EXTRACTION_URL=http://127.0.0.1:8770",
                    "RESEARCH_BROWSER_URL=http://127.0.0.1:8770",
                    # This MCP is reached via loopback SSH relay and the existing authenticated tunnel.
                    "RESEARCH_TRUSTED_TUNNEL=1",
                ]
            )
            + "\n"
        )
    else:
        text = "SEARXNG_SECRET=" + secrets.token_urlsafe(32) + "\n"
    path.write_text(text)
    path.chmod(0o640)
    os.chown(path, 0, pwd.getpwnam(group).pw_gid)
shutil.copy2(root / "config/egress.yaml", config / "egress.yaml")
shutil.copy2(root / "deploy/searxng.yml", config / "searxng.yml")
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(
    [
        "systemctl",
        "enable",
        "--now",
        "research-discovery",
        "research-worker",
        "research-v2",
        "research-maintenance.timer",
    ],
    check=True,
)
