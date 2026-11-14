import asyncio
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import httpx
from protego import Protego

from .models import Page
from .shopping import adapter_for, has_type, objects
from .network import AccessError, SafeHTTP, host_matches, validate_url


def extract(html, url=None):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    nodes = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        nodes.extend(data if isinstance(data, list) else [data] if isinstance(data, dict) else [])
    if url and not any(has_type(node, "Product") for node in objects(nodes)):
        nodes.extend(adapter_for(url).public_products(soup, url))
    for tag in soup(["script", "style", "noscript", "nav", "footer", "noframes", "template"]):
        tag.decompose()
    return title, soup.get_text("\n", strip=True), nodes


def blocked_reason(status, title, text):
    content = (title + "\n" + text[:1500]).lower()
    if any(
        x in content
        for x in (
            "подтвердите, что вы не робот",
            "доступ ограничен",
            "verify you are human",
            "captcha challenge",
            "докажите, что вы не робот",
            "checking your browser",
            "нужна небольшая проверка браузера",
        )
    ):
        return "captcha_or_access_challenge"
    if status in {401, 407}:
        return "authentication_required"
    if status == 403:
        return "http_403"
    if status == 429:
        return "http_429"
    if status == 451 or any(
        x in content for x in ("not available in your region", "недоступен в вашем регионе")
    ):
        return "region_blocked"
    if status >= 400:
        return f"http_{status}"
    return None


class Fetcher:
    def __init__(self, settings, store, http=None):
        self.settings = settings
        self.store = store
        self.http = http or SafeHTTP(settings)
        self.robots = {}
        self.robots_locks = {}
        self.cooldowns = {}
        self.page_locks = {}

    async def allowed(self, url, region=None):
        u, _ = validate_url(url)
        origin = f"{u.scheme}://{u.netloc}"
        key = (origin, self.http.router.choose(url, region))
        async with self.robots_locks.setdefault(key, asyncio.Lock()):
            cached = self.robots.get(key)
            if cached and cached[0] > time.monotonic():
                if cached[2]:
                    raise AccessError(cached[2])
                parser = cached[1]
            else:
                try:
                    response = await self.fetch_robots(origin + "/robots.txt", key[1])
                    if response.status_code in {404, 410}:
                        parser = None
                    elif response.status_code != 200:
                        raise AccessError(f"robots_unavailable_http_{response.status_code}")
                    elif re.search(r"<\s*(?:!doctype\s+html|html|form)\b", response.text, re.I):
                        raise AccessError("robots_access_challenge")
                    else:
                        parser = Protego.parse(response.text)
                except (AccessError, TimeoutError) as error:
                    reason = str(error) if isinstance(error, AccessError) else "robots_timeout"
                    self.robots[key] = (time.monotonic() + self.settings.error_ttl, None, reason)
                    raise AccessError(reason) from None
                self.robots[key] = (time.monotonic() + 3600, parser, None)
        if parser is not None:
            if not parser.can_fetch(url, self.settings.user_agent):
                raise AccessError("robots_denied")
            crawl_delay = parser.crawl_delay(self.settings.user_agent)
            if crawl_delay:
                # A separate per-host cooldown preserves a source's requested crawl delay.
                self.http.host_intervals[urlsplit(url).hostname] = max(
                    self.settings.per_host_interval, crawl_delay
                )

    async def fetch_robots(self, url, region):
        # RFC 9309 permits redirects for robots. Every hop retains DNS/SSRF checks.
        for redirect in range(6):
            validate_url(url)
            response = await self.http.get(url, region)
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            if redirect == 5:
                raise AccessError("robots_redirect_limit")
            target = response.headers.get("location")
            if not target:
                raise AccessError("robots_invalid_redirect")
            url = urljoin(url, target)
        raise AccessError("robots_redirect_limit")

    async def raw(self, url, region=None, *, robots=True):
        current = url
        for hop in range(self.settings.max_redirects + 1):
            validate_url(current)
            if not self.marketplace_enabled(current):
                raise AccessError("source_policy_denied")
            selected = self.http.router.choose(current, region)
            await self.http.require_region(selected)
            host = urlsplit(current).hostname
            if self.cooldowns.get(host, 0) > time.monotonic():
                raise AccessError("source_cooldown")
            if robots:
                await self.allowed(current, region)
            response = await self.http.get(current, region)
            if response.status_code in {301, 302, 303, 307, 308}:
                if hop == self.settings.max_redirects:
                    raise AccessError("redirect_limit")
                if not response.headers.get("location"):
                    raise AccessError("invalid_redirect")
                current = urljoin(current, response.headers["location"])
                continue
            content_type = response.headers.get("content-type", "").split(";")[0]
            if content_type and content_type not in {
                "text/html",
                "text/plain",
                "application/xhtml+xml",
                "application/json",
                "text/css",
                "application/javascript",
                "text/javascript",
            }:
                raise AccessError("unsupported_content_type")
            title, text, _ = extract(response.text)
            error = blocked_reason(response.status_code, title, text)
            if error:
                if response.status_code in {403, 429, 439} or "challenge" in error:
                    wait = 300
                    retry = response.headers.get("retry-after", "")
                    if retry.isdigit():
                        wait = min(3600, max(wait, int(retry)))
                    self.cooldowns[host] = time.monotonic() + wait
                raise AccessError(error)
            return response, current
        raise AccessError("redirect_limit")

    async def read(self, url, region=None, *, dynamic=False, refresh=False):
        selected = self.http.router.choose(url, region)
        key = f"page:{selected}:{dynamic}:{url}"
        async with self.page_locks.setdefault(key, asyncio.Lock()):
            cached = self.store.get(key) if not refresh else None
            if cached:
                return Page.model_validate({**cached, "cached": True})
            page = Page(url=url, source="public_page", status="unavailable", region=selected)
            try:
                # Includes robots, redirects and DNS in the total wall-clock budget.
                from .browser_capture import challenge_policy

                budget = self.settings.timeout * 3
                if challenge_policy(self.settings, url):
                    budget += self.settings.browser_timeout
                async with asyncio.timeout(budget):
                    try:
                        response, final_url = await self.raw(url, region)
                    except AccessError as initial_error:
                        retryable = str(initial_error) in {
                            "captcha_or_access_challenge",
                            "http_403",
                            "http_439",
                        }
                        if not (
                            retryable and self.settings.browser_url and challenge_policy(self.settings, url)
                        ):
                            raise
                        payload = await self.worker(
                            "/capture", {"url": url, "region": selected}, self.settings.browser_url
                        )
                        final_url = payload["final_url"]
                        validate_url(final_url)
                        response = httpx.Response(payload["http_status"], text=payload["html"])
                        page.extractor = payload["extractor"]
                        self.cooldowns.pop(urlsplit(url).hostname, None)
                    page.http_status = response.status_code
                    page.final_url = final_url
                    page.title, page.text, page.structured_data = extract(response.text, final_url)
                    page.html = response.text
                    soup = BeautifulSoup(page.html, "html.parser")
                    page.variant_labels = {
                        str(option.get("value")): option.get_text(" ", strip=True)[:300]
                        for option in soup.select('select[name="id"] option[value]')[:100]
                    }
                    page.paragraphs = [
                        p.get_text(" ", strip=True)[:1800] for p in soup.select("p, blockquote")
                    ][:100]
                    if not page.text.strip():
                        raise AccessError("empty_page")
                    # Ordinary dynamic rendering remains limited to opt-in thin JS shells.
                    if dynamic and len(page.text) < 250 and not page.structured_data:
                        if not self.settings.browser_url:
                            raise AccessError("dynamic_page_browser_not_configured")
                        payload = await self.worker(
                            "/render", {"url": final_url, "region": selected}, self.settings.browser_url
                        )
                        page.html = payload["html"]
                        validate_url(payload.get("final_url", final_url))
                        page.title, page.text, page.structured_data = extract(
                            page.html, payload.get("final_url", final_url)
                        )
                        page.final_url = payload.get("final_url", final_url)
                        page.extractor = "playwright+safehttp"
                    error = blocked_reason(page.http_status or 200, page.title, page.text)
                    if error:
                        raise AccessError(error)
                    page.status = "verified"
                    has_offers = any(
                        has_type(node, "Product") and node.get("offers")
                        for node in objects(page.structured_data)
                    )
                    if self.settings.extraction_url and page.html and not has_offers:
                        try:
                            payload = await self.worker(
                                "/extract", {"html": page.html}, self.settings.extraction_url
                            )
                            page.text = payload["text"]
                            page.extractor += "+crawl4ai"
                        except AccessError:
                            page.extractor += "+extraction_fallback"
                    error = blocked_reason(page.http_status or 200, page.title, page.text)
                    if error:
                        raise AccessError(error)
            except (AccessError, TimeoutError) as error:
                if isinstance(error, TimeoutError) and page.status == "verified":
                    page.extractor += "+extraction_timeout_fallback"
                else:
                    page.status = "unavailable"
                    page.error = str(error) if isinstance(error, AccessError) else "timeout"
                    match = re.search(r"http_(\d{3})", page.error)
                    if match:
                        page.http_status = int(match[1])
                    page.html, page.text, page.structured_data = "", "", []
                    page.paragraphs, page.variant_labels = [], {}
            if page.status == "verified":
                page.text = page.text[:30000]
                page.excerpts = [p[:600] for p in re.split(r"\n\s*\n|\n", page.text) if len(p.strip()) > 30][
                    :8
                ]
            self.store.put(
                key,
                page.model_dump(mode="json"),
                self.settings.page_ttl if page.status == "verified" else self.settings.error_ttl,
            )
            self.store.record(page)
            return page

    async def worker(self, path, payload, base):
        if not self.settings.worker_token:
            raise AccessError("worker_auth_not_configured")
        try:
            timeout = self.settings.browser_timeout + 3 if path == "/capture" else self.settings.timeout * 2
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                async with client.stream(
                    "POST",
                    base + path,
                    json=payload,
                    headers={"Authorization": "Bearer " + self.settings.worker_token},
                ) as response:
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.settings.max_bytes * 2:
                            raise AccessError("worker_response_too_large")
                        chunks.append(chunk)
                    data = json.loads(b"".join(chunks))
                    if isinstance(data, dict) and re.fullmatch(
                        r"[a-z0-9_]{1,80}", str(data.get("error", ""))
                    ):
                        raise AccessError(data["error"])
                    response.raise_for_status()
                    return data
        except (httpx.HTTPError, ValueError):
            raise AccessError("worker_unavailable") from None

    def marketplace_enabled(self, url):
        host = (urlsplit(url).hostname or "").lower()
        for name, domain in self.settings.marketplaces.items():
            if host_matches(host, domain):
                return name in self.settings.enabled_marketplaces
        return True
