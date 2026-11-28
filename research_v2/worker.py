"""Extraction and bounded browser worker with a loopback default.

Text extraction, HTTP-backed rendering and regional browser capture have
separate bounded paths. Native browser capture uses a loopback CONNECT guard.
"""

import argparse
import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re

from bs4 import BeautifulSoup
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from .auth import BearerGate
from .browser_capture import JsonResponseCapture, capture_patterns
from .config import Settings
from .fetching import Fetcher, blocked_reason
from .network import AccessError, host_matches, resolve_public, validate_url
from .store import Store

WINDOWS_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
)
CHALLENGE_STATUS_CODES = {401, 403, 429, 439}
CHALLENGE_TEXT = (
    "qrator", "cloudflare", "checking if the site connection is secure", "just a moment",
    "ozon antibot", "avito pow", "firewallpow", "проверка браузера", "почти готово",
)


@dataclass
class CdpFetchOptions:
    url: str
    region: str = "RU"
    wait_selector: str | None = None
    wait_ms: int = 2500
    wait_for_challenge: bool = True
    scroll_steps: int = 1
    capture_json_patterns: list[str] | None = None
    timeout_s: float = 35
    block_media: bool = True
    user_agent_compat: str | None = None

    @classmethod
    def parse(cls, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
            raise AccessError("invalid_url")
        options = cls(**{key: payload[key] for key in cls.__dataclass_fields__ if key in payload})
        if options.region not in ("RU", "EU"):
            raise AccessError("invalid_region")
        if options.wait_selector is not None and not (
            isinstance(options.wait_selector, str) and options.wait_selector.strip()
        ):
            raise AccessError("invalid_wait_selector")
        for key, minimum, maximum in (("wait_ms", 0, 15000), ("scroll_steps", 0, 5), ("timeout_s", 5, 60)):
            value = getattr(options, key)
            types = (int, float) if key == "timeout_s" else (int,)
            if type(value) not in types or not minimum <= value <= maximum:
                raise AccessError(f"invalid_{key}")
        for key in ("wait_for_challenge", "block_media"):
            if not isinstance(getattr(options, key), bool):
                raise AccessError(f"invalid_{key}")
        if options.user_agent_compat not in (None, "windows"):
            raise AccessError("invalid_user_agent_compat")
        capture_patterns(options.capture_json_patterns)
        return options


@dataclass
class CdpNavigation:
    status: int | None = None
    denied: str | None = None

    def on_response(self, response, page):
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            self.status = response.status


async def bounded_json(request, limit):
    import json

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise AccessError("request_too_large")
        body.extend(chunk)
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        raise AccessError("invalid_json") from None


class Worker:
    def __init__(self, settings):
        self.settings = settings
        self.fetcher = Fetcher(settings, Store(":memory:"))
        self.semaphore = asyncio.Semaphore(1)
        self.extraction_semaphore = asyncio.Semaphore(2)
        self.cdp_semaphore = asyncio.Semaphore(settings.cdp_max_concurrent)
        self.cdp_host_locks = {}
        self.cdp_last_navigation = {}

    async def extract(self, request):
        try:
            payload = await bounded_json(request, self.settings.max_bytes * 2)
            html = payload.get("html")
            if not isinstance(html, str) or len(html.encode()) > self.settings.max_bytes:
                raise AccessError("invalid_html")
            async with asyncio.timeout(15), self.extraction_semaphore:
                text = await asyncio.to_thread(self.extract_html, html)
            return JSONResponse({"text": text[:30000], "extractor": "crawl4ai-lxml"})
        except (AccessError, TimeoutError) as error:
            return JSONResponse({"error": str(error) or "timeout"}, status_code=422)
        except Exception:
            return JSONResponse({"error": "extraction_unavailable"}, status_code=422)

    @staticmethod
    def extract_html(html):
        from crawl4ai import LXMLWebScrapingStrategy

        soup = BeautifulSoup(html, "html.parser")
        for hidden in soup(["script", "style", "noframes", "template"]):
            hidden.decompose()
        result = LXMLWebScrapingStrategy().scrap("https://source.invalid/", str(soup))
        cleaned = result.get("cleaned_html", "") if isinstance(result, dict) else result.cleaned_html
        return BeautifulSoup(cleaned, "html.parser").get_text("\n", strip=True)

    async def render(self, request):
        try:
            payload = await bounded_json(request, 10000)
            url, region = payload["url"], payload.get("region")
            validate_url(url)
            async with asyncio.timeout(35), self.semaphore:
                result = await self.render_page(url, region)
            return JSONResponse(result)
        except (AccessError, TimeoutError, KeyError, ValueError):
            return JSONResponse({"error": "render_unavailable"}, status_code=422)
        except Exception as error:
            logging.getLogger(__name__).warning("Browser render failed (%s)", type(error).__name__)
            return JSONResponse({"error": "browser_unavailable"}, status_code=422)

    async def capture(self, request):
        from .browser_capture import capture_with_camoufox, capture_page, capture_timeout, challenge_policy

        try:
            payload = await bounded_json(request, 16384)
            url, region = payload["url"], payload.get("region")
            configured = challenge_policy(self.settings, url)
            options = {}
            if "allow_search" in payload:
                if not isinstance(payload["allow_search"], bool):
                    raise AccessError("invalid_allow_search")
                options["allow_search"] = payload["allow_search"]
            if "capture_json_patterns" in payload:
                options["capture_json_patterns"] = capture_patterns(payload["capture_json_patterns"])
            if not self.fetcher.marketplace_enabled(url):
                raise AccessError("source_policy_denied")
            async with asyncio.timeout(capture_timeout(self.settings, url)), self.semaphore:
                if configured and configured[1].get("backend") == "camoufox":
                    result = await capture_with_camoufox(self.settings, url, region, **options)
                else:
                    result = await capture_page(self.fetcher, url, region)
            return JSONResponse(result)
        except AccessError as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        except TimeoutError:
            return JSONResponse({"error": "browser_timeout"}, status_code=422)
        except Exception as error:
            logging.getLogger(__name__).warning("Browser capture failed (%s)", type(error).__name__)
            return JSONResponse({"error": "browser_unavailable"}, status_code=422)

    async def cdp_health(self, request):
        cdp_url = self.settings.cdp_url
        if not cdp_url:
            return JSONResponse({"reachable": False, "reason": "cdp_not_configured"})
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                response = await client.get(cdp_url.rstrip("/") + "/json/version")
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("invalid CDP version response")
                return JSONResponse({"reachable": True, "browser": data.get("Browser"), "cdp_url": cdp_url})
        except (httpx.HTTPError, ValueError):
            return JSONResponse({"reachable": False, "reason": "cdp_unreachable"})

    async def cdp_fetch(self, request):
        navigation = CdpNavigation()
        try:
            options = CdpFetchOptions.parse(await bounded_json(request, 16384))
            _, host = validate_url(options.url)
            if not self.settings.cdp_url:
                raise AccessError("cdp_not_configured")
            # Acquire the host lock first so queued tabs for one shop cannot exhaust the pool.
            async with asyncio.timeout(options.timeout_s), self.cdp_host_locks.setdefault(host, asyncio.Lock()):
                delay = self.settings.cdp_per_host_interval - (
                    asyncio.get_running_loop().time() - self.cdp_last_navigation.get(host, float("-inf"))
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                async with self.cdp_semaphore:
                    result = await self.cdp_page(options, host, navigation)
            return JSONResponse(result)
        except AccessError as error:
            reason = str(error)
        except TimeoutError:
            reason = "browser_timeout"
        except Exception as error:
            logging.getLogger(__name__).warning("CDP fetch failed (%s)", type(error).__name__)
            reason = "cdp_unavailable"
        return JSONResponse({"error": reason, "http_status": navigation.status}, status_code=422)

    def cdp_destination(self, url, main_domain=None):
        parsed, host = validate_url(url)
        if (
            (main_domain and not host_matches(host, main_domain))
            or any(host_matches(host, domain) for domain in self.settings.blocked_domains)
            or not self.fetcher.marketplace_enabled(url)
        ):
            raise AccessError("source_policy_denied")
        return host, parsed.port or (443 if parsed.scheme == "https" else 80)

    async def cdp_page(self, options, host, navigation):
        from playwright.async_api import Error as BrowserError, TimeoutError as BrowserTimeout, async_playwright

        main_domain = next(
            (domain for domain in sorted(self.settings.marketplaces.values(), key=len, reverse=True)
             if host_matches(host, domain)),
            host,
        )
        destination = self.cdp_destination(options.url, main_domain)
        await resolve_public(*destination)
        checked = {destination}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(self.settings.cdp_url.rstrip("/"), timeout=5000)
            page = None
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context(
                    service_workers="block", accept_downloads=False,
                )
                page = await context.new_page()
                await page.route_web_socket("**", lambda socket: socket.close())
                session = await context.new_cdp_session(page)
                await session.send("Network.setBypassServiceWorker", {"bypass": True})
                frame_tree = await session.send("Page.getFrameTree")
                main_frame_id = frame_tree["frameTree"]["frame"]["id"]

                async def intercept(event):
                    request_id = event["requestId"]
                    resource = event.get("resourceType")
                    main = resource == "Document" and event.get("frameId") == main_frame_id
                    try:
                        if options.block_media and resource in {"Image", "Media", "Font"}:
                            raise AccessError("media_blocked")
                        target = self.cdp_destination(event["request"]["url"], main_domain if main else None)
                        if target not in checked:
                            async with asyncio.timeout(3):
                                await resolve_public(*target)
                            checked.add(target)
                        await session.send("Fetch.continueRequest", {"requestId": request_id})
                    except (AccessError, TimeoutError) as error:
                        if main:
                            navigation.denied = str(error) or "dns_error"
                        try:
                            await session.send(
                                "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
                            )
                        except BrowserError:
                            pass
                    except BrowserError:
                        pass  # The page can close while a request is paused.

                # CDP intercepts each redirect hop, including hops skipped by Playwright's page.route.
                session.on("Fetch.requestPaused", intercept)
                await session.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
                if options.user_agent_compat == "windows":
                    version = re.search(r"\d+", browser.version)
                    await session.send("Network.setUserAgentOverride", {
                        "userAgent": WINDOWS_CHROME_UA.format(major=version[0] if version else "131"),
                        "acceptLanguage": "ru-RU,ru;q=0.9",
                        "platform": "Win32",
                    })
                    await session.send("Emulation.setLocaleOverride", {"locale": "ru_RU"})

                page.on("response", lambda response: navigation.on_response(response, page))
                async with JsonResponseCapture(options.capture_json_patterns) as captured:
                    if captured.patterns:
                        page.on("response", captured.on_response)
                    self.cdp_last_navigation[host] = asyncio.get_running_loop().time()
                    try:
                        response = await page.goto(options.url, wait_until="domcontentloaded")
                        if response is not None and navigation.status is None:
                            navigation.status = response.status
                    except BrowserTimeout:
                        raise TimeoutError from None
                    except BrowserError:
                        if navigation.denied or navigation.status is None:
                            raise AccessError(navigation.denied or "navigation_failed") from None
                        # A challenge may start a second navigation before goto finishes.
                    if options.wait_for_challenge:
                        await self.wait_cdp_challenge(page, navigation)
                    if options.wait_selector:
                        await page.locator("css=" + options.wait_selector).first.wait_for(state="visible")
                    for _ in range(options.scroll_steps):
                        await page.evaluate("window.scrollBy({top: innerHeight, behavior: 'smooth'})")
                        await page.wait_for_timeout(350)
                    if options.wait_ms:
                        await page.wait_for_timeout(options.wait_ms)
                    await captured.settle()
                    if navigation.denied:
                        raise AccessError(navigation.denied)
                    final_url, status = page.url, navigation.status
                    self.cdp_destination(final_url, main_domain)
                    title = await page.title()
                    html = await page.content()
                    if final_url != page.url or status != navigation.status:
                        self.cdp_destination(page.url, main_domain)
                        raise AccessError("navigation_changed")
                    if len(html.encode()) > self.settings.max_bytes:
                        raise AccessError("render_too_large")
                    return {
                        "html": html, "final_url": final_url, "http_status": status,
                        "title": title, "captured_json": captured.results, "extractor": "cdp-chromium",
                    }
            except BrowserTimeout:
                raise TimeoutError from None
            finally:
                try:
                    if page is not None:
                        await page.close()
                except BrowserError:
                    pass
                finally:
                    # Closing a connected Playwright Browser disconnects this client.
                    try:
                        await browser.close()
                    except BrowserError:
                        pass

    async def wait_cdp_challenge(self, page, navigation):
        from playwright.async_api import Error as BrowserError

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.challenge_wait_s
        while True:
            if navigation.denied:
                raise AccessError(navigation.denied)
            try:
                title, text = await page.evaluate(
                    "() => [document.title, document.body?.innerText.slice(0, 1500) || '']"
                )
            except BrowserError:
                # The execution context can be replaced during a challenge redirect.
                unsettled = True
            else:
                status = navigation.status or 200
                unsettled = (
                    status in CHALLENGE_STATUS_CODES
                    or blocked_reason(status, title, text) == "captcha_or_access_challenge"
                    or any(marker in (title + "\n" + text).casefold() for marker in CHALLENGE_TEXT)
                )
            if not unsettled:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AccessError("challenge_unresolved")
            await page.wait_for_timeout(min(500, remaining * 1000))

    async def render_page(self, url, region):
        from playwright.async_api import async_playwright

        # A failed HTTP fetch/denial is never retried with a browser.
        initial, final_url = await self.fetcher.raw(url, region)
        _, main_host = validate_url(final_url)
        count, size = 0, 0
        async with async_playwright() as playwright:
            executable = "/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None
            browser = await playwright.chromium.launch(
                executable_path=executable,
                headless=True,
                chromium_sandbox=True,
                proxy={"server": "http://127.0.0.1:9", "bypass": "<-loopback>"},
                args=[
                    "--disable-background-networking",
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
            try:
                context = await browser.new_context(service_workers="block", accept_downloads=False)
                await context.route_web_socket("**", lambda socket: socket.close())

                async def route_request(route):
                    nonlocal count, size
                    try:
                        count += 1
                        _, host = validate_url(route.request.url)
                        if host != main_host or route.request.method != "GET" or count > 40:
                            await route.abort()
                            return
                        if route.request.resource_type in {"image", "media", "font"}:
                            await route.abort()
                            return
                        if route.request.url == final_url:
                            response = initial
                        else:
                            response, _ = await self.fetcher.raw(route.request.url, region)
                        size += len(response.content)
                        if size > self.settings.max_bytes * 5:
                            await route.abort()
                            return
                        headers = {
                            k: v
                            for k, v in response.headers.items()
                            if k.lower()
                            not in {"content-encoding", "content-length", "transfer-encoding", "set-cookie"}
                        }
                        await route.fulfill(
                            status=response.status_code, headers=headers, body=response.content
                        )
                    except (AccessError, TimeoutError):
                        await route.abort()

                await context.route("**/*", route_request)
                page = await context.new_page()
                await page.goto(final_url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(1000)
                html = await page.content()
                validate_url(page.url)
                if len(html.encode()) > self.settings.max_bytes:
                    raise AccessError("render_too_large")
                return {"html": html, "final_url": page.url}
            finally:
                await browser.close()


def create_app(settings=None):
    settings = settings or Settings.load()
    if not settings.worker_token:
        raise RuntimeError("RESEARCH_WORKER_TOKEN is required")
    worker = Worker(settings)

    async def health(request):
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/extract", worker.extract, methods=["POST"]),
            Route("/render", worker.render, methods=["POST"]),
            Route("/capture", worker.capture, methods=["POST"]),
            Route("/cdp-fetch", worker.cdp_fetch, methods=["POST"]),
            Route("/cdp-health", worker.cdp_health),
            Route("/health", health),
        ]
    )
    return BearerGate(app, settings.worker_token)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("RESEARCH_WORKER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
