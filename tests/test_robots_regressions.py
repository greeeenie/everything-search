import httpx

from conftest import fetcher_for


async def test_yandex_empty_query_rule_does_not_deny_product_cards(settings, store):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /?\nDisallow: /api/\n")
        return httpx.Response(200, text="<h1>Wanbo Cube 2 Pro</h1>")

    page = await fetcher_for(settings, store, handler).read("https://market.yandex.ru/card/wanbo/12345")
    assert page.status == "verified" and calls[-1] == "/card/wanbo/12345"


async def test_robots_query_and_wildcard_restrictions_are_respected(settings, store):
    def handler(request):
        return httpx.Response(200, text="User-agent: *\nDisallow: /?\nDisallow: /*?private=*\n")

    fetcher = fetcher_for(settings, store, handler)
    for url in ("https://example.com/?search=x", "https://example.com/card?private=1"):
        page = await fetcher.read(url)
        assert page.error == "robots_denied"


async def test_most_specific_robot_rule_wins(settings, store):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\nAllow: /public/\n")
        return httpx.Response(200, text="<p>Allowed public content</p>")

    assert (
        await fetcher_for(settings, store, handler).read("https://example.com/public/page")
    ).status == "verified"


async def test_robots_redirect_can_be_followed(settings, store):
    def handler(request):
        if request.url.path == "/robots.txt" and not request.url.query:
            return httpx.Response(307, headers={"location": "/robots.txt?revision=1"})
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text="<p>Public product content</p>")

    assert (
        await fetcher_for(settings, store, handler).read("https://example.com/product")
    ).status == "verified"


async def test_robots_redirect_to_private_ip_is_blocked(settings, store):
    def handler(request):
        return httpx.Response(307, headers={"location": "http://127.0.0.1/robots.txt"})

    page = await fetcher_for(settings, store, handler).read("https://example.com/product")
    assert page.error == "ssrf_blocked"


async def test_failed_robots_fetch_is_cached_for_the_host(settings, store):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(403)

    fetcher = fetcher_for(settings, store, handler)
    await fetcher.read("https://example.com/one")
    await fetcher.read("https://example.com/two")
    assert count == 1


async def test_html_challenge_is_not_parsed_as_empty_robots(settings, store):
    def handler(request):
        return httpx.Response(200, text="<html><form>Verify you are human</form></html>")

    page = await fetcher_for(settings, store, handler).read("https://example.com/product")
    assert page.error == "robots_access_challenge"
