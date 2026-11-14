#!/bin/sh
set -eu
# Source must already be copied into /opt/research-v2. Existing VPN stays in its own services.
test "$(id -u)" = 0
command -v python3
if ! id research-v2 >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/research-v2 --shell /usr/sbin/nologin research-v2
fi
python3 -m venv /opt/research-v2/.venv
/opt/research-v2/.venv/bin/pip install --disable-pip-version-check '/opt/research-v2[extract,browser]'
if ! id searxng-research >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/searxng-research --shell /usr/sbin/nologin searxng-research
fi
if ! test -d /opt/searxng-research/.git; then
    git clone --depth 1 https://github.com/searxng/searxng.git /opt/searxng-research
fi
python3 -m venv /opt/searxng-research/.venv
/opt/searxng-research/.venv/bin/pip install --disable-pip-version-check -U pip setuptools wheel
/opt/searxng-research/.venv/bin/pip install --disable-pip-version-check -r /opt/searxng-research/requirements.txt
/opt/searxng-research/.venv/bin/pip install --disable-pip-version-check --no-build-isolation /opt/searxng-research granian
git -C /opt/searxng-research rev-parse HEAD > /opt/searxng-research/DEPLOYED_COMMIT
echo 'Python environments installed'
