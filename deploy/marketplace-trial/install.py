#!/usr/bin/env python3
"""Install a pinned upstream checkout and isolated trial services on the known Pi."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess

COMMIT = "f019e9ac41555c8509d50777cbafa0ab37fec8c5"
SOURCE = Path("/opt/ru-marketplace-mcp")
ASSETS = Path(__file__).resolve().parent


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


if os.geteuid() != 0:
    raise SystemExit("Run as root")
backup = Path("/var/backups/marketplace-trial") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
backup.mkdir(parents=True, mode=0o700)
hashes = {}
for name in ("/etc/xray-multitool/config.json", "/etc/sing-box/config.json"):
    path = Path(name)
    hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
(backup / "vpn-hashes.json").write_text(json.dumps(hashes))
for command, name in ((["ip", "rule"], "ip-rule.txt"), (["nft", "list", "ruleset"], "nft.txt")):
    (backup / name).write_text(run(*command, capture_output=True).stdout)
try:
    account = pwd.getpwnam("marketplace-trial")
except KeyError:
    run("useradd", "--system", "--create-home", "--home-dir", "/var/lib/marketplace-trial", "--shell", "/usr/sbin/nologin", "marketplace-trial")
    account = pwd.getpwnam("marketplace-trial")
if not SOURCE.exists():
    SOURCE.mkdir()
    run("git", "init", str(SOURCE))
    run("git", "-C", str(SOURCE), "remote", "add", "origin", "https://github.com/Vladimir-Human/ru-marketplace-mcp.git")
    run("git", "-C", str(SOURCE), "fetch", "--depth", "1", "origin", COMMIT)
    run("git", "-C", str(SOURCE), "checkout", "--detach", "FETCH_HEAD")
actual = run("git", "-C", str(SOURCE), "rev-parse", "HEAD", capture_output=True).stdout.strip()
if actual != COMMIT or run("git", "-C", str(SOURCE), "status", "--porcelain", capture_output=True).stdout:
    raise SystemExit("Existing checkout differs from the pinned clean upstream")
tools = Path("/opt/marketplace-trial-tools")
if not (tools / "bin/uv").exists():
    run("python3", "-m", "venv", str(tools))
    run(str(tools / "bin/pip"), "install", "--disable-pip-version-check", "uv==0.11.25")
run(str(tools / "bin/uv"), "sync", "--frozen", "--all-packages", "--all-extras", "--group", "dev", "--python", "/usr/bin/python3", "--directory", str(SOURCE))
configuration = Path("/etc/marketplace-trial")
configuration.mkdir(mode=0o750, exist_ok=True)
os.chown(configuration, 0, account.pw_gid)
environment = configuration / "server.env"
if not environment.exists():
    lines = {
        "MCP_TRANSPORT": "http", "MCP_HTTP_HOST": "127.0.0.1", "MCP_HTTP_PORT": "8790",
        "MCP_HTTP_AUTH_TOKEN": secrets.token_urlsafe(36), "MCP_HTTP_TENANT_ID": "marketplace-trial",
        "MARKETPLACE_SOURCES": "wildberries,ozon,yandex_market,avito,aliexpress,dns,citilink,detsky_mir,lamoda,megamarket,compare",
        "CHROME_CDP_HOST": "127.0.0.1", "CHROME_CDP_PORT": "9223",
        "CHROME_BINARY": "/usr/bin/chromium", "CHROME_SCRAPING_PROFILE": "/var/lib/marketplace-trial/chrome",
        "CHROME_HEADLESS": "0", "CHROME_CDP_MAX_NAVIGATIONS": "1",
        "CHROME_NAV_TIMEOUT_MS": "45000",
        "CHROME_CDP_PER_HOST_NAVIGATIONS": "1", "OZON_CHROME_COMPAT": "windows",
        "COMPARE_SOURCE_TIMEOUT": "120", "OTEL_SDK_DISABLED": "true",
    }
    environment.write_text("".join(f"{key}={value}\n" for key, value in lines.items()))
    environment.chmod(0o640)
    os.chown(environment, 0, account.pw_gid)
Path("/usr/local/libexec").mkdir(exist_ok=True)
shutil.copy2(ASSETS / "egress.py", "/usr/local/libexec/marketplace-trial-egress.py")
shutil.copy2(ASSETS / "ready.py", "/usr/local/libexec/marketplace-trial-ready.py")
for name in ("marketplace-trial-egress", "marketplace-trial-browser", "marketplace-trial"):
    target = Path("/etc/systemd/system") / (name + ".service")
    if target.exists():
        shutil.copy2(target, backup / target.name)
    shutil.copy2(ASSETS / target.name, target)
run("systemctl", "daemon-reload")
run("systemctl", "enable", "--now", "marketplace-trial-egress", "marketplace-trial-browser", "marketplace-trial")
print("Installed ru-marketplace-mcp", COMMIT, "on loopback port 8790")
