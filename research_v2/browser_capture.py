"""Automatic anonymous browser checks for explicitly configured sources."""

import asyncio
import json
import os
from pathlib import Path
import signal

from .browser_network import BrowserNetwork
from .fetching import blocked_reason, extract
from .network import AccessError, host_matches, validate_url
from .shopping import has_type, objects

MAX_CAPTURE_JSON_BYTES = 512 * 1024
MAX_CAPTURE_JSON_RESPONSES = 12
# JSON escaping can expand control characters or UTF-16 surrogate pairs.
CAPTURE_JSON_OUTPUT_LIMIT = MAX_CAPTURE_JSON_BYTES * MAX_CAPTURE_JSON_RESPONSES * 3 + 65536


def capture_patterns(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 8 or any(
        not isinstance(pattern, str) or not pattern for pattern in value
    ):
        raise AccessError("invalid_capture_json_patterns")
    return value


class JsonResponseCapture:
    """Collect a bounded set of matching JSON responses from one page."""

    def __init__(self, patterns: list[str] | None):
        self.patterns = patterns or ()
        self.results = []
        self.tasks = set()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    def on_response(self, response):
        if (
            self.closed
            or len(self.results) + len(self.tasks) >= MAX_CAPTURE_JSON_RESPONSES
            or not any(pattern in response.url for pattern in self.patterns)
        ):
            return
        task = asyncio.create_task(self.collect(response))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def collect(self, response):
        from playwright.async_api import Error as BrowserError

        try:
            validate_url(response.url)
            if len(response.url) > 4096:
                return
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"", "application/json", "text/plain"} and not content_type.endswith("+json"):
                return
            size = int(response.headers.get("content-length", 0))
            if size > MAX_CAPTURE_JSON_BYTES:
                return
            body = await response.body()
            if len(body) > MAX_CAPTURE_JSON_BYTES:
                return
            data = json.loads(body)
            # Starlette emits strict JSON. Validate nested values before returning them.
            json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self.results.append({"url": response.url, "status": response.status, "data": data})
        except (AccessError, ValueError, TypeError, RecursionError, BrowserError):
            pass

    async def settle(self):
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=1)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def challenge_policy(settings, url):
    _, host = validate_url(url)
    return next(
        (
            (domain, policy)
            for domain, policy in settings.automatic_challenges.items()
            if host_matches(host, domain)
        ),
        None,
    )


def capture_timeout(settings, url):
    configured = challenge_policy(settings, url)
    timeout = (
        configured[1].get("timeout", settings.browser_timeout) if configured else settings.browser_timeout
    )
    return min(120, max(5, float(timeout)))


async def capture_with_camoufox(
    settings, url, region, *, allow_search: bool = False, capture_json_patterns: list[str] | None = None,
):
    """The optional Firefox environment has its own compatible Playwright version."""
    configured = challenge_policy(settings, url)
    if not configured or configured[1].get("backend") != "camoufox":
        raise AccessError("source_policy_denied")
    if not settings.camoufox_python or not Path(settings.camoufox_python).is_file():
        raise AccessError("camoufox_not_installed")
    if not isinstance(allow_search, bool):
        raise AccessError("invalid_allow_search")
    patterns = capture_patterns(capture_json_patterns)
    options = {"url": url, "region": region}
    if allow_search:
        options["allow_search"] = True
    if patterns is not None:
        options["capture_json_patterns"] = patterns
    payload = json.dumps(options, ensure_ascii=False).encode()
    if len(payload) > 16384:
        raise AccessError("request_too_large")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "LANG", "XDG_CACHE_HOME", "RESEARCH_CONFIG", "RESEARCH_DATABASE"}
    }
    environment.update(
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
        PYTHONUTF8="1",
        RESEARCH_EU_PROXY=settings.eu_proxy,
        RESEARCH_RU_PROXY=settings.ru_proxy,
        RESEARCH_ALLOW_INTERNAL_HOSTS="1" if settings.allow_internal_hosts else "0",
        RESEARCH_VERIFY_EGRESS="1" if settings.verify_egress else "0",
        RESEARCH_EXPECTED_COUNTRIES=json.dumps(settings.expected_countries),
        RESEARCH_DATABASE=settings.database,
    )
    process = await asyncio.create_subprocess_exec(
        settings.camoufox_python,
        "-m",
        "research_v2.camoufox_capture",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=environment,
        start_new_session=True,
    )
    try:
        process.stdin.write(payload)
        await process.stdin.drain()
        process.stdin.close()
        chunks, size = [], 0
        limit = settings.max_bytes * 2 + 65536 + (CAPTURE_JSON_OUTPUT_LIMIT if patterns else 0)
        while chunk := await process.stdout.read(65536):
            size += len(chunk)
            if size > limit:
                raise AccessError("worker_response_too_large")
            chunks.append(chunk)
        await process.wait()
        try:
            result = json.loads(b"".join(chunks))
        except ValueError:
            raise AccessError("camoufox_unavailable") from None
        if result.get("error"):
            raise AccessError(result["error"])
        if process.returncode:
            raise AccessError("camoufox_unavailable")
        return result
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()


def is_challenge_protocol(url, main_host, policy):
    parsed, host = validate_url(url)
    return host == main_host and any(
        path != "/"
        and path.startswith("/")
        and (parsed.path == path or (path.endswith("/") and parsed.path.startswith(path)))
        for path in policy.get("protocol_paths", [])
    )


def is_challenge_asset(url, policy):
    parsed, host = validate_url(url)
    return any(host_matches(host, domain) for domain in policy["domains"]) and any(
        path != "/" and path.startswith("/") and parsed.path.startswith(path)
        for path in policy.get("asset_paths", [])
    )


async def capture_page(fetcher, url, region):
    from playwright.async_api import Error as BrowserError, async_playwright

    settings = fetcher.settings
    configured = challenge_policy(settings, url)
    if not configured or not fetcher.marketplace_enabled(url):
        raise AccessError("automatic_challenge_not_enabled")
    _, policy = configured
    _, main_host = validate_url(url)
    selected = fetcher.http.router.choose(url, region)
    await fetcher.http.require_region(selected)
    await fetcher.allowed(url, selected)
    requests, navigations, status = 0, 0, 200
    async with BrowserNetwork(settings, selected, policy["domains"]) as network, async_playwright() as pw:
        executable = "/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None
        browser = await pw.chromium.launch(
            executable_path=executable,
            headless=True,
            chromium_sandbox=True,
            proxy={"server": network.url, "bypass": "<-loopback>"},
            args=[
                "--disable-background-networking",
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ],
        )
        try:
            context = await browser.new_context(service_workers="block", accept_downloads=False)
            await context.route_web_socket("**", lambda socket: socket.close())

            page = await context.new_page()
            session = await context.new_cdp_session(page)

            async def intercept(event):
                nonlocal requests, navigations
                request = event["request"]
                request_id = event["requestId"]
                try:
                    _, host = validate_url(request["url"])
                    requests += 1
                    if requests > 240 or not any(host_matches(host, d) for d in policy["domains"]):
                        raise AccessError("browser_request_limit")
                    protocol = is_challenge_protocol(request["url"], main_host, policy)
                    if request["method"] not in {"GET", "HEAD"} and not (
                        protocol and request["method"] == "POST"
                    ):
                        raise AccessError("browser_method_denied")
                    if event.get("resourceType") == "Document":
                        navigations += 1
                        if host != main_host or navigations > settings.max_redirects + 1:
                            raise AccessError("redirect_limit")
                    # These exact paths exchange challenge proofs, never catalogue data.
                    # A user-authorized browser may execute that access-check protocol.
                    if not (protocol or is_challenge_asset(request["url"], policy)):
                        await fetcher.allowed(request["url"], selected)
                    await session.send("Fetch.continueRequest", {"requestId": request_id})
                except (AccessError, TimeoutError):
                    try:
                        await session.send(
                            "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
                        )
                    except BrowserError:
                        pass
                except BrowserError:
                    pass  # The context can close while an intercepted request is pending.

            # CDP pauses each redirect hop, including redirects inside an existing TLS tunnel.
            await session.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
            session.on("Fetch.requestPaused", intercept)

            def on_response(response):
                nonlocal status
                if response.request.is_navigation_request() and response.frame == page.main_frame:
                    status = response.status

            page.on("response", on_response)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except BrowserError:
                # A challenge can replace its document while the first goto is pending.
                pass
            # Allow one browser/PoW exchange, bounded by the worker's total timeout.
            # No refresh loop, button guessing or account state is involved.
            reason = "browser_no_product_data"
            for _ in range(24):
                try:
                    html = await page.content()
                    if len(html.encode()) > settings.max_bytes:
                        raise AccessError("render_too_large")
                    title, text, nodes = extract(html, page.url)
                    reason = blocked_reason(status, title, text)
                    has_product = any(
                        has_type(node, "Product") and node.get("offers") for node in objects(nodes)
                    )
                except BrowserError:
                    await asyncio.sleep(0.5)
                    continue
                if not reason and has_product:
                    final_url = page.url
                    validate_url(final_url)
                    await fetcher.allowed(final_url, selected)
                    return {
                        "html": html,
                        "final_url": final_url,
                        "http_status": status,
                        "extractor": "playwright+guarded-egress+automatic-check",
                    }
                if status in {401, 407, 429, 451}:
                    raise AccessError(reason or f"http_{status}")
                await asyncio.sleep(0.5)
            raise AccessError(reason or "browser_no_product_data")
        finally:
            await browser.close()
