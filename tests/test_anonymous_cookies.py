import httpx

from conftest import fetcher_for, public_resolver
from research_v2.network import SafeHTTP


async def test_anonymous_redirect_cookie_retains_original_host_scope(settings, store):
    def handler(request):
        if request.url.path == "/robots.txt":
            if request.headers.get("cookie") != "region=RU":
                return httpx.Response(
                    307,
                    headers={
                        "location": "/robots.txt?region=RU",
                        "set-cookie": "region=RU; Domain=example.com; Path=/; Secure",
                    },
                )
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        assert request.headers["cookie"] == "region=RU"
        return httpx.Response(200, text="<p>Public offer</p>")

    page = await fetcher_for(settings, store, handler).read("https://example.com/product")
    assert page.status == "verified"


async def test_cookie_is_not_sent_to_another_host_or_region(settings):
    calls = []

    def handler(request):
        calls.append(dict(request.headers))
        return httpx.Response(
            200, headers={"set-cookie": "session=anonymous; Domain=example.com; Path=/; Secure"}
        )

    http = SafeHTTP(settings, resolver=public_resolver, transport=httpx.MockTransport(handler))
    await http.get("https://example.com/", "RU")
    await http.get("https://other.example/", "RU")
    await http.get("https://example.com/", "EU")
    assert all("cookie" not in headers for headers in calls)
