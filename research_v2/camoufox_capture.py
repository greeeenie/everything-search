"""Anonymous marketplace capture in an isolated Camoufox environment.

The worker sends one bounded JSON request on stdin. Only the final result is
written to stdout. Browser state is private and separated by egress region.
"""

import asyncio
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit, urlunsplit

from .browser_capture import capture_timeout, challenge_policy, is_challenge_asset, is_challenge_protocol
from .browser_network import BrowserNetwork
from .config import Settings
from .fetching import Fetcher, blocked_reason, extract
from .network import AccessError, host_matches, validate_url
from .shopping import adapter_for, has_type, objects
from .store import Store


def canonical_browser_url(url, domain):
    parsed, host = validate_url(url)
    if host not in {domain, "www." + domain}:
        raise AccessError("source_policy_denied")
    expected_port = 443 if parsed.scheme == "https" else 80
    if parsed.port not in {None, expected_port}:
        raise AccessError("unsupported_port")
    authority = "www." + domain if domain in {"ozon.ru", "avito.ru"} else host
    return urlunsplit(("https", authority, parsed.path or "/", parsed.query, ""))


async def capture(settings, url, region):
    from camoufox import DefaultAddons
    from camoufox.async_api import AsyncCamoufox
    from playwright.async_api import Error as BrowserError

    configured = challenge_policy(settings, url)
    fetcher = Fetcher(settings, Store(":memory:"))
    if not configured or configured[1].get("backend") != "camoufox" or not fetcher.marketplace_enabled(url):
        raise AccessError("source_policy_denied")
    url = canonical_browser_url(url, configured[0])
    _, main_host = validate_url(url)
    policy = configured[1]
    selected = fetcher.http.router.choose(url, region)
    await fetcher.http.require_region(selected)
    await fetcher.allowed(url, selected)
    directory = Path(settings.database).parent / "browser" / adapter_for(url).name / selected
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    last_navigation = directory / "last-navigation"
    if last_navigation.exists():
        delay = settings.per_host_interval - (time.time() - last_navigation.stat().st_mtime)
        if delay > 0:
            await asyncio.sleep(delay)
    last_navigation.touch(mode=0o600)
    blocked_navigation = None
    proof_passed = asyncio.Event()
    requests, navigations, status = 0, 0, None
    async with BrowserNetwork(settings, selected, policy["domains"]) as network:
        async with AsyncCamoufox(
            browser=policy.get("browser_version"),
            headless="virtual",
            os="windows",
            locale="ru-RU",
            geoip=False,
            persistent_context=True,
            user_data_dir=str(directory / "profile"),
            block_webrtc=True,
            exclude_addons=[DefaultAddons.UBO],
            accept_downloads=False,
            proxy={"server": network.url},
            firefox_user_prefs={
                "network.http.redirection-limit": settings.max_redirects,
                "network.proxy.no_proxies_on": "",
                "network.trr.mode": 5,
                "browser.startup.page": 0,
                "browser.sessionstore.resume_from_crash": False,
            },
        ) as context:
            await context.route_web_socket("**", lambda socket: socket.close())
            previous_pages = list(context.pages)
            page = await context.new_page()
            for previous_page in previous_pages:
                await previous_page.close()

            async def route_request(route):
                nonlocal requests, navigations, blocked_navigation
                request = route.request
                try:
                    _, host = validate_url(request.url)
                    requests += 1
                    if requests > 240 or not any(host_matches(host, d) for d in policy["domains"]):
                        raise AccessError("browser_request_limit")
                    protocol = is_challenge_protocol(request.url, main_host, policy)
                    if request.method not in {"GET", "HEAD"} and not (protocol and request.method == "POST"):
                        raise AccessError("browser_method_denied")
                    if request.is_navigation_request():
                        navigations += 1
                        if host != main_host or navigations > settings.max_redirects + 1:
                            raise AccessError("redirect_limit")
                    if not (protocol or is_challenge_asset(request.url, policy)):
                        await fetcher.allowed(request.url, selected)
                    await route.continue_()
                except (AccessError, TimeoutError) as error:
                    if request.is_navigation_request():
                        blocked_navigation = str(error) or "timeout"
                    await route.abort()
                except BrowserError:
                    pass

            await context.route("**/*", route_request)

            async def on_response(response):
                nonlocal status
                if response.request.is_navigation_request() and response.frame == page.main_frame:
                    status = response.status
                if urlsplit(response.url).path == "/abt/result" and response.status == 200:
                    try:
                        if (await response.json()).get("ok") is True:
                            proof_passed.set()
                    except (ValueError, BrowserError):
                        pass

            page.on("response", on_response)

            async def navigate():
                nonlocal status
                status = None
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                except BrowserError:
                    pass  # A successful challenge can replace the document during goto.

            await navigate()
            clean_navigation = False
            deadline = asyncio.get_running_loop().time() + 40
            reason = "browser_no_product_data"
            while asyncio.get_running_loop().time() < deadline:
                if proof_passed.is_set() and not clean_navigation:
                    clean_navigation = True
                    blocked_navigation = None
                    # The challenge appends a tracking query. Reopen the allowed canonical URL.
                    await navigate()
                if blocked_navigation:
                    raise AccessError(blocked_navigation)
                if status is None:
                    await asyncio.sleep(0.5)
                    continue
                try:
                    html = await page.content()
                    if len(html.encode()) > settings.max_bytes:
                        raise AccessError("render_too_large")
                    title, text, nodes = extract(html, page.url)
                    reason = blocked_reason(status, title, text)
                    product = any(has_type(node, "Product") and node.get("name") for node in objects(nodes))
                except BrowserError:
                    await asyncio.sleep(0.5)
                    continue
                if not reason and product:
                    await fetcher.allowed(page.url, selected)
                    return {
                        "html": html,
                        "final_url": page.url,
                        "http_status": status,
                        "extractor": "camoufox+guarded-egress+automatic-check",
                    }
                if status in {401, 407, 429, 451}:
                    raise AccessError(reason or f"http_{status}")
                await asyncio.sleep(0.5)
            raise AccessError(reason or "browser_no_product_data")


async def main():
    try:
        payload = json.loads(sys.stdin.buffer.read(10001))
        settings = Settings.load()
        async with asyncio.timeout(capture_timeout(settings, payload["url"]) - 5):
            result = await capture(settings, payload["url"], payload.get("region"))
    except AccessError as error:
        result = {"error": str(error)}
    except TimeoutError:
        result = {"error": "browser_timeout"}
    except Exception:
        result = {"error": "camoufox_unavailable"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
