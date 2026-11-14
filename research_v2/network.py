import asyncio
import ipaddress
import re
import socket
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Settings


class AccessError(Exception):
    """Machine-readable safe error; never contains response bodies or secrets."""


def host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


class EgressRouter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def choose(self, url: str, region=None) -> str:
        if region is not None:
            if region not in {"RU", "EU"}:
                raise ValueError("region must be EU or RU")
            return region
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        for domain in sorted(self.settings.domains, key=len, reverse=True):
            if host_matches(host, domain):
                return self.settings.domains[domain]
        return self.settings.default_region

    def proxy(self, region):
        return (self.settings.ru_proxy if region == "RU" else self.settings.eu_proxy) or None


async def resolve_public(host, port):
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise AccessError("dns_error") from None
    ips = list(dict.fromkeys(x[4][0] for x in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise AccessError("ssrf_blocked")
    # Pin the exact numeric destination used by HTTPX and SSH SOCKS; no second DNS lookup.
    return sorted(ips, key=lambda ip: ":" in ip)[0]


def validate_url(url):
    try:
        u = urlsplit(url)
        host = (u.hostname or "").encode("idna").decode().lower().rstrip(".")
        if (
            u.scheme not in {"http", "https"}
            or not host
            or u.username is not None
            or u.password is not None
            or u.port not in {None, 80, 443}
            or "\\" in url
            or any(ord(c) <= 32 for c in url)
            or "%" in host
            or host == "localhost"
            or host.endswith((".localhost", ".local", ".internal", ".onion"))
        ):
            raise AccessError("ssrf_blocked")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9.-]+", host):
                raise AccessError("ssrf_blocked")
        else:
            if not ip.is_global or (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None):
                raise AccessError("ssrf_blocked")
        return u, host
    except (ValueError, UnicodeError):
        raise AccessError("ssrf_blocked") from None


class SafeHTTP:
    def __init__(self, settings: Settings, resolver=resolve_public, transport=None):
        self.settings = settings
        self.router = EgressRouter(settings)
        self.resolver = resolver
        self.transport = transport
        self.semaphore = asyncio.Semaphore(settings.concurrency)
        self.locks = {}
        self.last_request = {}
        self.host_intervals = {}
        self.health = {}
        self.health_lock = asyncio.Lock()
        self.cookies = {}

    async def diagnose(self, region):
        try:
            async with asyncio.timeout(self.settings.timeout * 2):
                response = await self.get("https://www.cloudflare.com/cdn-cgi/trace", region)
            values = dict(line.split("=", 1) for line in response.text.splitlines() if "=" in line)
            expected = self.settings.expected_countries.get(region)
            good = response.status_code == 200 and bool(values.get("ip")) and values.get("loc") == expected
            return {
                "region": region,
                "status": "verified" if good else "unavailable",
                "ip": values.get("ip"),
                "country": values.get("loc"),
                "expected_country": expected,
                "error": None if good else "egress_country_mismatch",
                "source": "https://www.cloudflare.com/cdn-cgi/trace",
            }
        except (AccessError, TimeoutError):
            return {"region": region, "status": "unavailable", "error": "egress_unavailable"}

    async def require_region(self, region):
        if not self.settings.verify_egress:
            return
        async with self.health_lock:
            entry = self.health.get(region)
            if not entry or entry[0] < time.monotonic():
                result = await self.diagnose(region)
                self.health[region] = (
                    time.monotonic() + (60 if result["status"] == "verified" else 15),
                    result,
                )
            else:
                result = entry[1]
            if result["status"] != "verified":
                raise AccessError(region.lower() + "_egress_unavailable")

    async def get(self, url, region=None, *, redirects=0):
        """One bounded GET; caller handles redirects and robots for each hop."""
        u, host = validate_url(url)
        if any(host_matches(host, d) for d in self.settings.blocked_domains):
            raise AccessError("source_policy_denied")
        ip = await asyncio.wait_for(
            self.resolver(host, u.port or (443 if u.scheme == "https" else 80)), timeout=self.settings.timeout
        )
        selected = self.router.choose(url, region)
        authority = f"[{ip}]" if ":" in ip else ip
        if u.port:
            authority += f":{u.port}"
        pinned_url = urlunsplit((u.scheme, authority, u.path or "/", u.query, ""))
        lock = self.locks.setdefault(host, asyncio.Lock())
        async with self.semaphore, lock:
            loop = asyncio.get_running_loop()
            delay = self.host_intervals.get(host, self.settings.per_host_interval) - (
                loop.time() - self.last_request.get(host, 0)
            )
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_request[host] = loop.time()
            cookie_jar = self.cookies.setdefault((selected, host), httpx.Cookies())
            cookie_request = httpx.Request("GET", url)
            cookie_jar.set_cookie_header(cookie_request)
            request_headers = {"Host": u.netloc}
            if "cookie" in cookie_request.headers:
                request_headers["Cookie"] = cookie_request.headers["cookie"]
            try:
                async with httpx.AsyncClient(
                    proxy=self.router.proxy(selected) if self.transport is None else None,
                    transport=self.transport,
                    timeout=self.settings.timeout,
                    trust_env=False,
                    follow_redirects=False,
                    headers={"User-Agent": self.settings.user_agent},
                ) as client:
                    async with client.stream(
                        "GET", pinned_url, headers=request_headers, extensions={"sni_hostname": host}
                    ) as response:
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self.settings.max_bytes:
                                raise AccessError("response_too_large")
                            chunks.append(chunk)
                        headers = [
                            (key, value)
                            for key, value in response.headers.multi_items()
                            if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
                        ]
                        result = httpx.Response(
                            response.status_code,
                            headers=headers,
                            content=b"".join(chunks),
                            request=httpx.Request("GET", url),
                        )
                        cookie_jar.extract_cookies(result)
                        return result
            except httpx.TimeoutException:
                raise AccessError("timeout") from None
            except httpx.RequestError:
                raise AccessError("ru_egress_unavailable" if selected == "RU" else "network_error") from None
