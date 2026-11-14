import httpx
import asyncio

from conftest import fetcher_for


async def test_robots_denial(settings, store):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /private")

    page = await fetcher_for(settings, store, handler).read("https://example.com/private")
    assert page.error == "robots_denied" and calls == ["/robots.txt"]


async def test_error_cache_and_cooldown(settings, store):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(429)

    fetcher = fetcher_for(settings, store, handler)
    first = await fetcher.read("https://example.com/page")
    second = await fetcher.read("https://example.com/page")
    third = await fetcher.read("https://example.com/another")
    assert first.error == "http_429" and second.cached
    assert third.error == "source_cooldown" and len(calls) == 2


async def test_verified_cache_and_refresh(settings, store):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return (
            httpx.Response(404)
            if request.url.path == "/robots.txt"
            else httpx.Response(200, text="<p>Readable evidence content</p>")
        )

    fetcher = fetcher_for(settings, store, handler)
    first = await fetcher.read("https://example.com/page")
    second = await fetcher.read("https://example.com/page")
    third = await fetcher.read("https://example.com/page", refresh=True)
    assert first.status == second.status == third.status == "verified"
    assert not first.cached and second.cached and not third.cached
    assert len(calls) == 3


async def test_challenge_is_not_rendered(settings, store, monkeypatch):
    def handler(request):
        return (
            httpx.Response(404)
            if request.url.path == "/robots.txt"
            else httpx.Response(200, text="<title>Verify you are human</title>")
        )

    fetcher = fetcher_for(settings, store, handler)

    async def worker(*args, **kwargs):
        raise AssertionError("Browser must not bypass a challenge")

    monkeypatch.setattr(fetcher, "worker", worker)
    page = await fetcher.read("https://example.com/page", dynamic=True)
    assert page.status == "unavailable" and page.error == "captcha_or_access_challenge"


async def test_response_size_limit(settings, store):
    settings.max_bytes = 64

    def handler(request):
        return (
            httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(200, text="x" * 100)
        )

    page = await fetcher_for(settings, store, handler).read("https://example.com/page")
    assert page.error == "response_too_large"


async def test_optional_extraction_timeout_preserves_verified_html(settings, store, monkeypatch):
    settings.timeout = 0.03
    settings.extraction_url = "http://127.0.0.1:8770"

    def handler(request):
        return (
            httpx.Response(404)
            if request.url.path == "/robots.txt"
            else httpx.Response(200, text="<p>Successfully retrieved public evidence remains available.</p>")
        )

    async def slow_worker(*args):
        await asyncio.sleep(1)

    fetcher = fetcher_for(settings, store, handler)
    monkeypatch.setattr(fetcher, "worker", slow_worker)
    page = await fetcher.read("https://example.com/page")
    assert page.status == "verified" and page.error is None
    assert "public evidence" in page.text and page.excerpts
    assert "extraction_timeout_fallback" in page.extractor


async def test_structured_offers_skip_secondary_text_extraction(settings, store, monkeypatch):
    settings.extraction_url = "http://127.0.0.1:8770"

    def handler(request):
        return (
            httpx.Response(404)
            if request.url.path == "/robots.txt"
            else httpx.Response(
                200,
                text='<p>Available product evidence</p><script type="application/ld+json">'
                '{"@type":"Product","name":"Example","offers":{"@type":"Offer","price":"100","priceCurrency":"RUB"}}'
                "</script>",
            )
        )

    async def worker(*args):
        raise AssertionError("Complete structured offers are already extracted")

    fetcher = fetcher_for(settings, store, handler)
    monkeypatch.setattr(fetcher, "worker", worker)
    page = await fetcher.read("https://example.com/product")
    assert page.status == "verified" and page.structured_data[0]["offers"]["price"] == "100"
