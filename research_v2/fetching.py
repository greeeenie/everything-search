import asyncio
import json
import re
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import httpx

from .models import Page
from .network import AccessError, SafeHTTP, host_matches, validate_url


def extract(html):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    nodes = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        nodes.extend(data if isinstance(data, list) else [data] if isinstance(data, dict) else [])
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
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
        self.cooldowns = {}
        self.page_locks = {}

    async def allowed(self, url, region=None):
        u, _ = validate_url(url)
        origin = f"{u.scheme}://{u.netloc}"
        key = (origin, self.http.router.choose(url, region))
        cached = self.robots.get(key)
        if not cached or cached[0] < time.monotonic():
            response = await self.http.get(origin + "/robots.txt", region)
            if response.status_code in {404, 410}:
                parser = None
            elif response.status_code != 200:
                raise AccessError(f"robots_unavailable_http_{response.status_code}")
            else:
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
            self.robots[key] = (time.monotonic() + 3600, parser)
        else:
            parser = cached[1]
        if parser is not None:
            if not parser.can_fetch(self.settings.user_agent, url):
                raise AccessError("robots_denied")
            crawl_delay = parser.crawl_delay(self.settings.user_agent) or parser.crawl_delay("*")
            if crawl_delay:
                # A separate per-host cooldown preserves a source's requested crawl delay.
                self.http.host_intervals[urlsplit(url).hostname] = max(
                    self.settings.per_host_interval, crawl_delay
                )

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
                if response.status_code in {403, 429} or "challenge" in error:
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
                async with asyncio.timeout(self.settings.timeout * 3):
                    response, final_url = await self.raw(url, region)
                    page.http_status = response.status_code
                    page.final_url = final_url
                    page.title, page.text, page.structured_data = extract(response.text)
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
                    # Never launch Chromium after denial/challenge. Only opt-in thin JS shells.
                    if dynamic and len(page.text) < 250 and not page.structured_data:
                        if not self.settings.browser_url:
                            raise AccessError("dynamic_page_browser_not_configured")
                        payload = await self.worker(
                            "/render", {"url": final_url, "region": selected}, self.settings.browser_url
                        )
                        page.html = payload["html"]
                        validate_url(payload.get("final_url", final_url))
                        page.title, page.text, page.structured_data = extract(page.html)
                        page.final_url = payload.get("final_url", final_url)
                        page.extractor = "playwright+safehttp"
                    if self.settings.extraction_url and page.html:
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
                    page.status = "verified"
                    page.text = page.text[:30000]
                    page.excerpts = [
                        p[:600] for p in re.split(r"\n\s*\n|\n", page.text) if len(p.strip()) > 30
                    ][:8]
            except (AccessError, TimeoutError) as error:
                page.error = str(error) if isinstance(error, AccessError) else "timeout"
                match = re.search(r"http_(\d{3})", page.error)
                if match:
                    page.http_status = int(match[1])
                page.html, page.text, page.structured_data = "", "", []
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
            async with httpx.AsyncClient(timeout=self.settings.timeout * 2, trust_env=False) as client:
                async with client.stream(
                    "POST",
                    base + path,
                    json=payload,
                    headers={"Authorization": "Bearer " + self.settings.worker_token},
                ) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.settings.max_bytes * 2:
                            raise AccessError("worker_response_too_large")
                        chunks.append(chunk)
                    return json.loads(b"".join(chunks))
        except (httpx.HTTPError, ValueError):
            raise AccessError("worker_unavailable") from None

    def marketplace_enabled(self, url):
        host = (urlsplit(url).hostname or "").lower()
        for name, domain in self.settings.marketplaces.items():
            if host_matches(host, domain):
                return name in self.settings.enabled_marketplaces
        return True
