import json
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest
import yaml

from research_v2 import browser_capture, worker
from research_v2.config import Settings


DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "docker-hub"


@pytest.mark.parametrize(
    ("env_host", "args", "host", "port"),
    [
        (None, [], "127.0.0.1", 8770),
        ("0.0.0.0", [], "0.0.0.0", 8770),
        ("0.0.0.0", ["--host", "127.0.0.1", "--port", "9000"], "127.0.0.1", 9000),
    ],
)
def test_worker_cli_host(monkeypatch, env_host, args, host, port):
    if env_host is None:
        monkeypatch.delenv("RESEARCH_WORKER_HOST", raising=False)
    else:
        monkeypatch.setenv("RESEARCH_WORKER_HOST", env_host)
    monkeypatch.setattr(sys, "argv", ["research_v2.worker", *args])
    app = object()
    monkeypatch.setattr(worker, "create_app", lambda: app)
    run = Mock()
    monkeypatch.setattr(worker.uvicorn, "run", run)

    worker.main()

    run.assert_called_once_with(app, host=host, port=port, log_level="warning", access_log=False)


@pytest.mark.parametrize(
    ("docker", "proxy", "expected_countries"),
    [
        (True, "socks5://egress-xray:1080", {"RU": "RU", "EU": "SE"}),
        (False, "socks5://127.0.0.1:10808", {"RU": "RU", "EU": "LV"}),
    ],
)
async def test_camoufox_subprocess_inherits_egress_settings(monkeypatch, docker, proxy, expected_countries):
    settings = Settings(
        camoufox_python=sys.executable,
        automatic_challenges={"ozon.ru": {"backend": "camoufox"}},
        eu_proxy=proxy,
        allow_internal_hosts=docker,
        verify_egress=docker,
        expected_countries=expected_countries,
    )
    monkeypatch.setenv("RESEARCH_ALLOW_INTERNAL_HOSTS", "0" if docker else "1")
    monkeypatch.setenv("RESEARCH_VERIFY_EGRESS", "0" if docker else "1")
    monkeypatch.setenv("RESEARCH_EXPECTED_COUNTRIES", "RU=RU,EU=GB")
    monkeypatch.setenv("RESEARCH_WORKER_TOKEN", "worker-secret")
    monkeypatch.setenv("RESEARCH_CONFIG", str(DEPLOY.parents[1] / "config" / "egress.yaml"))

    result = {"html": "<p>Product</p>", "http_status": 200}
    process = Mock(returncode=0)
    process.stdin.drain = AsyncMock()
    process.stdout.read = AsyncMock(side_effect=[json.dumps(result).encode(), b""])
    process.wait = AsyncMock()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(browser_capture.asyncio, "create_subprocess_exec", spawn)

    assert await browser_capture.capture_with_camoufox(settings, "https://www.ozon.ru/", "RU") == result

    assert spawn.await_args.args == (sys.executable, "-m", "research_v2.camoufox_capture")
    env = spawn.await_args.kwargs["env"]
    assert env["RESEARCH_ALLOW_INTERNAL_HOSTS"] == ("1" if docker else "0")
    assert env["RESEARCH_VERIFY_EGRESS"] == ("1" if docker else "0")
    assert env["RESEARCH_EXPECTED_COUNTRIES"] == json.dumps(expected_countries)
    assert env["RESEARCH_EU_PROXY"] == proxy
    assert "RESEARCH_WORKER_TOKEN" not in env
    with patch.dict(os.environ, env, clear=True):
        child = Settings.load()
    assert child.eu_proxy == proxy
    assert child.allow_internal_hosts is docker
    assert child.verify_egress is docker
    assert child.expected_countries == expected_countries


def test_docker_hub_compose_wiring():
    compose = yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"research-api", "browser-cdp", "research-worker", "searxng"}
    assert all("ports" not in service for service in services.values())
    assert compose["networks"]["ingress"]["external"] is True
    assert compose["networks"]["egress"]["external"] is True
    assert compose["networks"]["search-net"]["internal"] is True

    api = services["research-api"]
    assert api["build"] == {"context": "../..", "target": "core"}
    assert api["networks"]["ingress"]["aliases"] == ["research-api"]
    assert set(api["networks"]) == {"ingress", "egress", "search-net"}
    assert api["environment"]["RESEARCH_HTTP_HOST"] == "0.0.0.0"
    assert api["environment"]["RESEARCH_SEARXNG_URL"] == "http://searxng:8080"
    for key in ("RESEARCH_EXTRACTION_URL", "RESEARCH_BROWSER_URL"):
        assert api["environment"][key] == "http://research-worker:8770"
    assert api["environment"]["RESEARCH_NATIVE_CONNECTORS"] == "1"
    assert not any(key.startswith("RESEARCH_MARKETPLACE_BACKEND_") for key in api["environment"])

    browser_worker = services["research-worker"]
    assert browser_worker["build"] == {"context": "../..", "target": "worker"}
    assert "networks" not in browser_worker
    assert browser_worker["network_mode"] == "service:browser-cdp"
    assert browser_worker["depends_on"]["browser-cdp"] == {"condition": "service_healthy", "restart": True}
    assert browser_worker["environment"]["RESEARCH_WORKER_HOST"] == "0.0.0.0"
    assert browser_worker["environment"]["RESEARCH_WORKER_TOKEN"] == api["environment"]["RESEARCH_WORKER_TOKEN"]
    assert browser_worker["environment"]["RESEARCH_CDP_URL"] == "http://127.0.0.1:9222"
    assert browser_worker["environment"]["RESEARCH_CDP_MAX_CONCURRENT"] == "3"

    chrome = services["browser-cdp"]
    assert set(chrome["networks"]) == {"egress", "search-net", "marketplace-outbound"}
    assert chrome["networks"]["search-net"]["aliases"] == ["research-worker"]
    assert chrome["healthcheck"]["test"][-1] == "http://127.0.0.1:9222/json/version"

    searxng = services["searxng"]
    assert set(searxng["networks"]) == {"egress", "search-net"}
    assert "./searxng.yml:/etc/searxng/settings.yml:ro" in searxng["volumes"]


def test_docker_hub_searxng_routes_search_over_socks():
    searxng = yaml.safe_load((DEPLOY / "searxng.yml").read_text(encoding="utf-8"))
    assert searxng["server"]["bind_address"] == "0.0.0.0"
    assert searxng["server"]["port"] == 8080
    assert searxng["search"]["formats"] == ["html", "json"]
    assert searxng["outgoing"]["proxies"]["all://"] == "socks5h://egress-xray:1080"


def test_local_compose_is_self_contained():
    compose = yaml.safe_load((DEPLOY / "compose.local.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"research-api", "browser-cdp", "research-worker", "searxng"}
    assert compose["networks"] == {"local-net": {"driver": "bridge"}}
    assert all("env_file" not in service for service in services.values())

    api = services["research-api"]
    assert api["ports"] == ["127.0.0.1:8765:8765"]
    assert all("ports" not in service for name, service in services.items() if name != "research-api")
    assert api["networks"] == ["local-net"]
    assert api["environment"]["RESEARCH_NO_AUTH"] == "1"
    assert "RESEARCH_API_TOKENS" not in api["environment"]
    assert api["environment"]["RESEARCH_SEARXNG_URL"] == "http://searxng:8080"
    assert api["environment"]["RESEARCH_EXTRACTION_URL"] == "http://research-worker:8770"
    assert api["environment"]["RESEARCH_BROWSER_URL"] == "http://research-worker:8770"

    worker_service = services["research-worker"]
    assert worker_service["network_mode"] == "service:browser-cdp"
    assert worker_service["depends_on"]["browser-cdp"]["condition"] == "service_healthy"
    assert worker_service["environment"]["RESEARCH_WORKER_TOKEN"] == api["environment"]["RESEARCH_WORKER_TOKEN"]
    for service in (api, worker_service):
        assert service["environment"]["RESEARCH_VERIFY_EGRESS"] == "0"
        assert service["environment"]["RESEARCH_RU_PROXY"] == service["environment"]["RESEARCH_EU_PROXY"] == ""
    assert services["browser-cdp"]["networks"] == {"local-net": {"aliases": ["research-worker"]}}
    assert services["searxng"]["networks"] == ["local-net"]
    assert services["searxng"]["volumes"] == ["./searxng.local.yml:/etc/searxng/settings.yml:ro"]


def test_local_searxng_uses_direct_search():
    production = yaml.safe_load((DEPLOY / "searxng.yml").read_text(encoding="utf-8"))
    local = (DEPLOY / "searxng.local.yml").read_text(encoding="utf-8")
    del production["outgoing"]["proxies"]
    assert yaml.safe_load(local) == production
    assert "egress-xray" not in local
