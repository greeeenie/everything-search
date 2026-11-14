import asyncio

import httpx
import pytest

from conftest import fetcher_for, public_resolver
from research_v2.browser_capture import challenge_policy, is_challenge_asset, is_challenge_protocol
from research_v2.browser_network import BrowserNetwork
from research_v2.network import AccessError, resolve_public


@pytest.mark.parametrize("authority", ["127.0.0.1:443", "[::1]:443", "avito.ru:22", "evil.example:443"])
async def test_browser_proxy_rejects_unapproved_destinations(settings, authority):
    network = BrowserNetwork(settings, "RU", ["avito.ru"], resolver=public_resolver)
    with pytest.raises(AccessError):
        await network.destination(authority)


async def test_browser_proxy_pins_public_dns(settings, monkeypatch):
    async def addresses(*args, **kwargs):
        return [(2, 1, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", addresses)
    network = BrowserNetwork(settings, "RU", ["avito.ru"], resolver=resolve_public)
    with pytest.raises(AccessError, match="ssrf_blocked"):
        await network.destination("www.avito.ru:443")


async def test_browser_proxy_only_listens_on_loopback(settings):
    async with BrowserNetwork(settings, "RU", ["avito.ru"]) as network:
        assert all(sock.getsockname()[0] == "127.0.0.1" for sock in network.server.sockets)


def test_challenge_protocol_is_separate_and_explicit(settings):
    settings.automatic_challenges = {
        "avito.ru": {"domains": ["avito.ru", "avito.st"], "protocol_paths": ["/web/3/firewallPow/"]}
    }
    configured = challenge_policy(settings, "https://www.avito.ru/all?q=model")
    assert configured[0] == "avito.ru"
    policy = configured[1]
    assert is_challenge_protocol("https://www.avito.ru/web/3/firewallPow/get", "www.avito.ru", policy)
    for url in [
        "https://www.avito.ru/web/3/items",
        "https://www.avito.ru/account/",
        "https://evil.example/web/3/firewallPow/get",
    ]:
        assert not is_challenge_protocol(url, "www.avito.ru", policy)
    assert challenge_policy(settings, "https://avito.ru.evil.example/") is None


def test_challenge_assets_are_scoped_to_configured_hosts_and_paths():
    policy = {"domains": ["ozon.ru", "ozone.ru"], "asset_paths": ["/s3/abt-challenge/"]}
    assert is_challenge_asset("https://cdn2.ozone.ru/s3/abt-challenge/script.js", policy)
    assert not is_challenge_asset("https://evil.example/s3/abt-challenge/script.js", policy)
    assert not is_challenge_asset("https://www.ozon.ru/account/", policy)
    policy["protocol_paths"] = ["/abt/result"]
    assert is_challenge_protocol("https://www.ozon.ru/abt/result", "www.ozon.ru", policy)
    assert not is_challenge_protocol("https://www.ozon.ru/abt/result-other", "www.ozon.ru", policy)


async def test_authorized_challenge_uses_one_browser_attempt(settings, store, monkeypatch):
    settings.automatic_challenges = {"avito.ru": {"domains": ["avito.ru"], "protocol_paths": []}}
    settings.browser_url = "http://127.0.0.1:8770"
    calls = []

    def handler(request):
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(439)

    async def worker(path, payload, base):
        calls.append(path)
        return {
            "html": "<h1>Available product</h1>",
            "final_url": payload["url"],
            "http_status": 200,
            "extractor": "playwright+guarded-egress+automatic-check",
        }

    fetcher = fetcher_for(settings, store, handler)
    monkeypatch.setattr(fetcher, "worker", worker)
    page = await fetcher.read("https://www.avito.ru/all?q=test")
    cached = await fetcher.read("https://www.avito.ru/all?q=test")
    assert page.status == "verified" and cached.cached
    assert calls == ["/capture"]


async def test_failed_browser_is_bounded_and_cached(settings, store, monkeypatch):
    settings.automatic_challenges = {"avito.ru": {"domains": ["avito.ru"], "protocol_paths": []}}
    settings.browser_url = "http://127.0.0.1:8770"
    calls = []

    def handler(request):
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(439)

    async def worker(*args):
        calls.append(True)
        raise AccessError("captcha_unresolved")

    fetcher = fetcher_for(settings, store, handler)
    monkeypatch.setattr(fetcher, "worker", worker)
    first = await fetcher.read("https://www.avito.ru/all?q=test")
    second = await fetcher.read("https://www.avito.ru/all?q=test")
    third = await fetcher.read("https://www.avito.ru/all?q=other")
    assert first.error == "captcha_unresolved" and second.cached
    assert third.error == "source_cooldown" and len(calls) == 1
