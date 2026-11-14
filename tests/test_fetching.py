import httpx

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
