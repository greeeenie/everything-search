import asyncio
import json
import os
import signal
import sys

import httpx
import pytest

from conftest import fetcher_for
from research_v2.browser_capture import capture_with_camoufox, capture_timeout
from research_v2.network import AccessError
from research_v2.camoufox_capture import canonical_browser_url


def enable(settings):
    settings.browser_url = "http://127.0.0.1:8770"
    settings.automatic_challenges = {
        "ozon.ru": {
            "backend": "camoufox",
            "timeout": 75,
            "domains": ["ozon.ru", "ozone.ru"],
            "protocol_paths": ["/abt/result"],
            "asset_paths": ["/s3/abt-challenge/"],
        }
    }


def test_ozon_normalizes_public_links_to_https_www():
    assert canonical_browser_url("http://ozon.ru/product/model-123456/?variant=2#reviews", "ozon.ru") == (
        "https://www.ozon.ru/product/model-123456/?variant=2"
    )


@pytest.mark.parametrize(
    "url", ["https://ozon.ru.evil.example/", "https://127.0.0.1/", "https://www.ozon.ru:80/"]
)
def test_ozon_canonicalization_rejects_other_destinations(url):
    with pytest.raises(AccessError):
        canonical_browser_url(url, "ozon.ru")


async def test_ozon_uses_configured_browser_directly(settings, store, monkeypatch):
    enable(settings)

    def handler(request):
        raise AssertionError("The configured source already requires a browser")

    async def worker(path, payload, base):
        assert payload["region"] == "RU"
        return {
            "html": "<p>Verified public product page</p>",
            "final_url": payload["url"],
            "http_status": 200,
            "extractor": "camoufox+guarded-egress+automatic-check",
        }

    fetcher = fetcher_for(settings, store, handler)
    monkeypatch.setattr(fetcher, "worker", worker)
    page = await fetcher.read("https://www.ozon.ru/product/wanbo-123456/")
    assert page.status == "verified" and page.region == "RU"
    assert page.extractor.startswith("camoufox")


async def test_disabled_ozon_never_launches_browser(settings, store, monkeypatch):
    enable(settings)
    settings.enabled_marketplaces.remove("ozon")
    fetcher = fetcher_for(settings, store, lambda request: httpx.Response(200))

    async def worker(*args):
        raise AssertionError("Disabled source")

    monkeypatch.setattr(fetcher, "worker", worker)
    page = await fetcher.read("https://www.ozon.ru/product/model-123456/")
    assert page.error == "source_policy_denied"


async def test_removed_ozon_listing_does_not_pause_other_listings(settings, store, monkeypatch):
    enable(settings)
    fetcher = fetcher_for(settings, store, lambda request: httpx.Response(200))
    calls = []

    async def worker(path, payload, base):
        calls.append(payload["url"])
        if "removed" in payload["url"]:
            raise AccessError("robots_denied")
        return {
            "html": "<p>Public evidence</p>",
            "final_url": payload["url"],
            "http_status": 200,
            "extractor": "camoufox",
        }

    monkeypatch.setattr(fetcher, "worker", worker)
    removed = await fetcher.read("https://www.ozon.ru/product/removed-123456/")
    available = await fetcher.read("https://www.ozon.ru/product/available-234567/")
    assert removed.status == "unavailable" and available.status == "verified" and len(calls) == 2


def test_ozon_timeout_is_explicit_and_bounded(settings):
    enable(settings)
    assert capture_timeout(settings, "https://www.ozon.ru/product/example-123456/") == 75
    settings.automatic_challenges["ozon.ru"]["timeout"] = 10000
    assert capture_timeout(settings, "https://www.ozon.ru/") == 120
    assert capture_timeout(settings, "https://www.avito.ru/all") == 30


class InputPipe:
    def write(self, data):
        self.data = data

    async def drain(self):
        pass

    def close(self):
        pass


class Process:
    def __init__(self, output=None):
        self.stdin = InputPipe()
        self.stdout = asyncio.StreamReader()
        self.returncode = None
        self.pid = 987654
        if output is not None:
            self.stdout.feed_data(output)
            self.stdout.feed_eof()

    async def wait(self):
        self.returncode = 0


@pytest.mark.parametrize("domain", ["ozon.ru", "avito.ru"])
async def test_browser_subprocess_does_not_receive_worker_secret(settings, monkeypatch, domain):
    enable(settings)
    settings.automatic_challenges["avito.ru"] = {"backend": "camoufox", "domains": ["avito.ru", "avito.st"]}
    settings.camoufox_python = sys.executable
    monkeypatch.setenv("RESEARCH_WORKER_TOKEN", "private-worker-token")
    process = Process(json.dumps({"html": "<p>Source</p>", "http_status": 200}).encode())

    async def spawn(*args, **kwargs):
        assert kwargs["start_new_session"] and "RESEARCH_WORKER_TOKEN" not in kwargs["env"]
        assert args[0] == sys.executable and args[2] == "research_v2.camoufox_capture"
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await capture_with_camoufox(settings, "https://www." + domain + "/product/example-123456/", "RU")
    assert result["http_status"] == 200
    assert json.loads(process.stdin.data)["region"] == "RU"


async def test_cancelled_browser_process_is_terminated(settings, monkeypatch):
    enable(settings)
    settings.camoufox_python = sys.executable
    process = Process()
    killed = []

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)), raising=False)
    task = asyncio.create_task(
        capture_with_camoufox(settings, "https://www.ozon.ru/product/test-123456/", "RU")
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [(process.pid, 9)]


def test_avito_browser_url_preserves_query():
    assert canonical_browser_url("http://avito.ru/all?q=Wanbo+Cube+2+Pro", "avito.ru") == (
        "https://www.avito.ru/all?q=Wanbo+Cube+2+Pro"
    )


async def test_shopping_skips_review_urls_before_browser(settings, store):
    from research_v2.engine import Engine
    from research_v2.models import Page
    from test_engine import FakeDiscovery, FakeFetcher

    review = Page(
        url="https://www.ozon.ru/product/wanbo-cube-2-pro-123456/reviews/",
        title="Wanbo Cube 2 Pro отзывы",
        source="public_page",
        status="verified",
        region="RU",
    )
    engine = Engine(settings, store, FakeFetcher([]), FakeDiscovery([review]))
    result = await engine.search_products("Wanbo Cube 2 Pro", model="Wanbo Cube 2 Pro", marketplaces=["ozon"])
    assert result["offers"] == [] and result["candidates"][0]["fetch_error"] == "not_product_offer_url"


async def test_universal_web_search_keeps_review_urls(settings, store):
    from research_v2.engine import Engine
    from research_v2.models import Page
    from test_engine import FakeDiscovery, FakeFetcher

    review = Page(
        url="https://www.ozon.ru/product/wanbo-cube-2-pro-123456/reviews/",
        title="Wanbo Cube 2 Pro отзывы",
        source="public_page",
        status="verified",
        region="RU",
    )
    engine = Engine(settings, store, FakeFetcher([review]), FakeDiscovery([review]))
    result = await engine.search_web("Wanbo Cube 2 Pro отзывы", site="ozon.ru")
    assert len(result["results"]) == 1 and result["results"][0]["fetch_status"] == "verified"


async def test_ozon_profile_requests_are_serialized(settings, store, monkeypatch):
    enable(settings)
    fetcher = fetcher_for(settings, store, lambda request: httpx.Response(200))
    active, peak = 0, 0

    async def worker(path, payload, base):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {
            "html": "<p>Public offer evidence</p>",
            "final_url": payload["url"],
            "http_status": 200,
            "extractor": "camoufox",
        }

    monkeypatch.setattr(fetcher, "worker", worker)
    pages = await asyncio.gather(
        *(fetcher.read(f"https://www.ozon.ru/product/item-{number}/") for number in [123456, 234567, 345678])
    )
    assert peak == 1 and all(page.status == "verified" for page in pages)


async def test_browser_timeout_does_not_mark_ozon_as_blocked(settings, store, monkeypatch):
    enable(settings)
    fetcher = fetcher_for(settings, store, lambda request: httpx.Response(200))

    async def worker(path, payload, base):
        if "timeout" in payload["url"]:
            raise AccessError("browser_timeout")
        return {
            "html": "<p>Public evidence</p>",
            "final_url": payload["url"],
            "http_status": 200,
            "extractor": "camoufox",
        }

    monkeypatch.setattr(fetcher, "worker", worker)
    first = await fetcher.read("https://www.ozon.ru/product/timeout-123456/")
    second = await fetcher.read("https://www.ozon.ru/product/available-234567/")
    assert first.error == "browser_timeout" and second.status == "verified"


async def test_legacy_web_tool_exposes_fetched_product_price(settings, store):
    from conftest import product_page
    from research_v2.engine import Engine
    from test_engine import FakeDiscovery, FakeFetcher

    page = product_page(price="12990")
    engine = Engine(settings, store, FakeFetcher([page]), FakeDiscovery([page]))
    result = await engine.search_web("Wanbo Cube 2 Pro", site="ozon.ru")
    row = result["results"][0]
    assert "Index price 1 RUB" in row["search_excerpt"]
    assert row["offers"][0]["price"] == "12990"
    assert row["offers"][0]["evidence"]["status"] == "verified"


async def test_product_parser_failure_preserves_web_result(settings, store, monkeypatch):
    from conftest import product_page
    from research_v2.engine import Engine
    from test_engine import FakeDiscovery, FakeFetcher

    class BrokenAdapter:
        def parse(self, page):
            raise ValueError("malformed product")

    page = product_page()
    engine = Engine(settings, store, FakeFetcher([page]), FakeDiscovery([page]))
    monkeypatch.setattr("research_v2.engine.adapter_for", lambda url: BrokenAdapter())
    result = await engine.search_web("Wanbo Cube 2 Pro", site="ozon.ru")
    assert result["pages_read"] == 1
    assert result["results"][0]["product_extraction_error"] == "parsing_error"


async def test_ozon_block_in_one_region_does_not_pause_another(settings, store, monkeypatch):
    enable(settings)
    fetcher = fetcher_for(settings, store, lambda request: httpx.Response(200))

    async def worker(path, payload, base):
        if payload["region"] == "EU":
            raise AccessError("http_403")
        return {
            "html": "<p>Public Russian offer</p>",
            "final_url": payload["url"],
            "http_status": 200,
            "extractor": "camoufox",
        }

    monkeypatch.setattr(fetcher, "worker", worker)
    url = "https://www.ozon.ru/product/item-123456/"
    blocked = await fetcher.read(url, "EU")
    available = await fetcher.read(url, "RU")
    assert blocked.error == "http_403" and available.status == "verified"
    assert available.region == "RU"
