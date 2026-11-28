# Research v3 on Docker Compose

The stack runs the API, the browser worker, Chromium, and SearXNG.
The API runs all ten native marketplace connectors in-process. Connectors
use the local worker for Chromium CDP and Camoufox when browser rendering is needed.

## Mode 1. Quick local start without tokens or keys

Run from the repository root with Docker and Compose v2:

```sh
docker compose -f deploy/docker-hub/compose.local.yaml up -d --build
```

The local stack needs no `.env` file. It creates a bridge network, uses direct
internet access, and stores data in `deploy/docker-hub/data-local`. It uses
built-in values for the internal worker token and SearXNG secret. The API is
published on loopback only.

Connect a local Streamable HTTP client to `http://127.0.0.1:8765/mcp` with no
authentication headers. Verify REST access:

```sh
curl -fsS 'http://127.0.0.1:8765/api/v1/status?quick=1'
```

To run the Python server directly with uv, use `uv run python server.py` for stdio
or `uv run python server.py --http --no-auth` for HTTP. The Compose command also
starts the search and browser services.

## Mode 2. Production server with HTTPS, multiple API tokens, and OAuth 2.1

Use [compose.yaml](compose.yaml) for remote ChatGPT and Claude connectors. Replace
`https://search.example.com` with your public HTTPS origin. Run the production
commands below from `deploy/docker-hub` in your checkout on a Linux host.

### Prepare the host

Use Docker Engine with Compose v2. The existing external `ingress` network must
contain Caddy. The external `egress` bridge must have normal outbound routing
and a SOCKS5 service reachable as `egress-xray:1080`. On `home-docker`, direct
traffic exits in Russia and SOCKS5 traffic exits in Sweden.

```sh
docker network inspect ingress egress >/dev/null
umask 077
cp -n .env.example .env
chmod 600 .env
for key in RESEARCH_API_TOKENS RESEARCH_WORKER_TOKEN SEARXNG_SECRET; do
  sed -i "s/^${key}=$/${key}=$(openssl rand -hex 32)/" .env
done
mkdir -p -m 0700 data/api data/worker
sudo install -d -m 0700 -o 10001 -g 10001 data/chrome-profile
```

Each secret has a separate role. `RESEARCH_API_TOKENS` accepts comma-separated
client tokens. The API and worker share `RESEARCH_WORKER_TOKEN`. SearXNG uses
`SEARXNG_SECRET` for its own signing.

On another host, update `RESEARCH_PUBLIC_BASE_URL` and the site name in the
Caddy block together. Set `RESEARCH_EXPECTED_COUNTRIES` to the country codes for
the direct and proxied routes. If your SOCKS5 address differs, update
`RESEARCH_EU_PROXY` in `.env` and `outgoing.proxies` in `searxng.yml`. Keep the
SearXNG settings file readable by its container user.

### Start the stack and enable HTTPS

```sh
docker compose config --quiet
docker compose up -d --build --wait --remove-orphans
docker compose ps
```

Add [Caddyfile.snippet](Caddyfile.snippet) to the configuration used by
`ingress-caddy`. For a container using the standard config path, validate and
reload it after saving the change.

```sh
docker exec ingress-caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker exec ingress-caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

Caddy resolves `research-api:8765` on `ingress` and flushes streamed MCP
responses immediately. The stack publishes no host ports. The API reaches
the worker at `research-worker:8770` and SearXNG at `searxng:8080` on
`search-net`. The `research-worker` alias belongs to `browser-cdp` on this network.

Chromium and the worker share a network namespace. CDP listens on
`127.0.0.1:9222` within that namespace. Direct RU traffic can leave through
the outbound bridge. The shared namespace and SearXNG also join `egress`
to reach the EU SOCKS5 service. Chromium runs as UID/GID 10001 under Xvfb with its
sandbox disabled for compatibility with Docker's default namespace
restrictions. The container drops all capabilities and sets
`no-new-privileges`.

The API database is in `data/api`, Camoufox's regional profiles are in
`data/worker/browser`, and Chromium's profile is in `data/chrome-profile`.
Recreate Chromium and the worker together when rebuilding the browser so the
worker joins the replacement namespace.

```sh
docker compose up -d --build --force-recreate --wait browser-cdp research-worker
```

### Smoke-test the deployment

From the Compose directory, check the API through Caddy, the worker and
SearXNG from the API container, and CDP from the worker.

```sh
. ./.env
curl -fsS "$RESEARCH_PUBLIC_BASE_URL/health"
docker compose exec -T research-api python - <<'PY'
import asyncio
import json
import urllib.request

from research_v2.config import Settings
from research_v2.connectors import NativeMarketplaceHub
from research_v2.store import Store

with urllib.request.urlopen("http://research-worker:8770/health", timeout=5) as response:
    assert json.load(response)["status"] == "ok"
with urllib.request.urlopen("http://searxng:8080/search?q=python&format=json", timeout=15) as response:
    assert json.load(response)["results"], "SearXNG returned no results"

store = Store(":memory:")
try:
    health = asyncio.run(NativeMarketplaceHub(Settings.load(), store).health())
    assert health["status"] == "ok" and health["cdp"]["reachable"], health
    assert health["engine"] == "native" and len(health["connectors"]) == 10, health
finally:
    store.close()
PY
docker compose exec -T research-worker python - <<'PY'
import asyncio

from playwright.async_api import async_playwright

async def check_cdp():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=5000)
        print(browser.version)

asyncio.run(check_cdp())
PY
```

Check the public MCP endpoint with the first configured API token. Expect
HTTP 200 and a JSON-RPC initialization result in the response.

```sh
curl -isS --fail-with-body --max-time 10 "$RESEARCH_PUBLIC_BASE_URL/mcp" \
  -H "Authorization: Bearer ${RESEARCH_API_TOKENS%%,*}" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"compose-smoke","version":"1"}}}'
```
