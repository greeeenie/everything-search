"""Loopback-only extraction and bounded browser worker.

Text extraction, HTTP-backed rendering and regional browser capture have
separate bounded paths. Native browser capture uses a loopback CONNECT guard.
"""

import argparse
import asyncio
import logging
from pathlib import Path

from bs4 import BeautifulSoup
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from .auth import BearerGate
from .config import Settings
from .fetching import Fetcher
from .network import AccessError, validate_url
from .store import Store


async def bounded_json(request, limit):
    import json

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise AccessError("request_too_large")
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
            payload = await bounded_json(request, 10000)
            url, region = payload["url"], payload.get("region")
            configured = challenge_policy(self.settings, url)
            if not self.fetcher.marketplace_enabled(url):
                raise AccessError("source_policy_denied")
            async with asyncio.timeout(capture_timeout(self.settings, url)), self.semaphore:
                if configured and configured[1].get("backend") == "camoufox":
                    result = await capture_with_camoufox(self.settings, url, region)
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
            Route("/health", health),
        ]
    )
    return BearerGate(app, settings.worker_token)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
