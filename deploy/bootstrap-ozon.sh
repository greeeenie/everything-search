#!/bin/sh
set -eu
test "$(id -u)" = 0
id research-v2 >/dev/null
# Camoufox requires its own Playwright version. Keep that dependency in this venv.
python3 -m venv /opt/research-ozon
/opt/research-ozon/bin/python -m pip install --disable-pip-version-check 'camoufox==0.5.9' /opt/research-v2
/opt/research-ozon/bin/python -m playwright install-deps firefox
apt-get install -y --no-install-recommends xvfb xauth
/opt/research-ozon/bin/python -c 'from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()'
runuser -u research-v2 -- env HOME=/var/lib/research-v2 /opt/research-ozon/bin/python -m camoufox fetch official/158.0.1-beta.41
