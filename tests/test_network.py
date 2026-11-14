import socket

import httpx
import pytest

from research_v2.config import Settings
from research_v2.network import AccessError, EgressRouter, resolve_public, validate_url
from conftest import fetcher_for


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest",
        "http://10.0.0.1",
        "file:///etc/passwd",
        "ftp://example.com",
        "https://user:secret@example.com",
        "http://localhost/",
        "http://printer.local/",
        "http://[::ffff:127.0.0.1]/",
        "https://example.com:8443",
        "http://example.com\\@127.0.0.1/",
    ],
)
def test_ssrf_literals(url):
    with pytest.raises(AccessError):
        validate_url(url)


def test_domain_boundary_and_override(settings):
    router = EgressRouter(settings)
    assert router.choose("https://www.ozon.ru/product/x") == "RU"
    assert router.choose("https://ozon.ru.evil.com") == "EU"
    assert router.choose("https://reddit.com", "RU") == "RU"


def test_untrusted_proxy_rejected():
    with pytest.raises(ValueError):
        Settings(ru_proxy="socks5://external.example:1080")


async def test_private_dns_answer_rejected(monkeypatch):
    import asyncio

    async def addresses(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", addresses)
    with pytest.raises(AccessError, match="ssrf_blocked"):
        await resolve_public("public-looking.example", 443)


async def test_redirect_private_destination_blocked(settings, store):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secrets"})

    page = await fetcher_for(settings, store, handler).read("https://example.com/page")
    assert page.status == "unavailable" and page.error == "ssrf_blocked"
    assert len(calls) == 2


async def test_pin_destination_and_host_header(settings, store):
    def handler(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        return (
            httpx.Response(404)
            if request.url.path == "/robots.txt"
            else httpx.Response(200, text="<p>Public content</p>")
        )

    page = await fetcher_for(settings, store, handler).read("https://example.com/page")
    assert page.status == "verified"


async def test_ru_does_not_fallback(settings, store):
    import httpx

    def handler(request):
        raise httpx.ConnectError("failed")

    page = await fetcher_for(settings, store, handler).read("https://ozon.ru/product/123")
    assert page.status == "unavailable" and page.error == "ru_egress_unavailable"


async def test_compressed_response_is_decoded_once(settings, store):
    import gzip

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "text/html"},
            content=gzip.compress(b"<p>Readable compressed public content</p>"),
        )

    page = await fetcher_for(settings, store, handler).read("https://example.com/page")
    assert page.status == "verified" and "compressed public content" in page.text
