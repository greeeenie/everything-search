from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    domains: dict[str, str] = field(default_factory=dict)
    default_region: str = "EU"
    marketplaces: dict[str, str] = field(default_factory=dict)
    enabled_marketplaces: list[str] = field(default_factory=list)
    native_search_marketplaces: list[str] = field(default_factory=list)
    automatic_challenges: dict[str, dict] = field(default_factory=dict)
    browser_timeout: float = 30
    camoufox_python: str = ""
    condition_detail_limit: int = 4
    review_domains: list[str] = field(default_factory=list)
    ru_proxy: str = ""
    eu_proxy: str = "socks5://127.0.0.1:10808"
    verify_egress: bool = True
    expected_countries: dict[str, str] = field(default_factory=lambda: {"RU": "RU", "EU": "LV"})
    database: str = "data/research.sqlite3"
    searxng_url: str = "http://127.0.0.1:8088"
    discovery_backend: str = "searxng"
    timeout: float = 12
    concurrency: int = 4
    per_host_interval: float = 1.5
    page_ttl: int = 300
    error_ttl: int = 120
    search_ttl: int = 180
    max_bytes: int = 2_000_000
    max_redirects: int = 4
    extraction_url: str = ""
    browser_url: str = ""
    worker_token: str = ""
    user_agent: str = "ResearchV2/2.0 (+public-page research; respects robots.txt)"
    blocked_domains: list[str] = field(default_factory=list)
    allow_internal_hosts: bool = False
    public_base_url: str = ""
    api_tokens: list[str] = field(default_factory=list)
    oauth_enabled: bool = True
    http_host: str = "127.0.0.1"
    cdp_url: str = ""
    native_connectors: bool = False
    cdp_max_concurrent: int = 3
    cdp_per_host_interval: float = 1.2
    challenge_wait_s: float = 15

    def _internal_host_allowed(self, host):
        return host in {"127.0.0.1", "::1"} or (
            self.allow_internal_hosts and bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", host or ""))
        )

    def __post_init__(self):
        for proxy in (self.ru_proxy, self.eu_proxy):
            if not proxy:
                continue
            p = urlsplit(proxy)
            if (
                p.scheme != "socks5"
                or not self._internal_host_allowed(p.hostname)
                or not p.port
                or p.username is not None
                or p.password is not None
            ):
                raise ValueError("Egress proxies must use SOCKS5 and an allowed internal host")
        if self.default_region not in {"EU", "RU"} or any(
            x not in {"EU", "RU"} for x in self.domains.values()
        ):
            raise ValueError("Regions must be EU or RU")
        for endpoint in (self.searxng_url, self.extraction_url, self.browser_url):
            if endpoint:
                u = urlsplit(endpoint)
                if (
                    u.scheme != "http"
                    or not self._internal_host_allowed(u.hostname)
                    or u.username is not None
                    or u.password is not None
                ):
                    raise ValueError("Internal endpoints must use HTTP and an allowed internal host")
        if self.cdp_url:
            cdp = urlsplit(self.cdp_url)
            host = cdp.hostname or ""
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == "localhost"
            if (
                cdp.scheme != "http"
                or not (loopback or self._internal_host_allowed(host))
                or cdp.username is not None
                or cdp.password is not None
                or cdp.port == 0
                or cdp.query
                or cdp.fragment
                or any(ord(char) <= 32 or char == "\\" for char in self.cdp_url)
            ):
                raise ValueError("CDP endpoint must be an internal HTTP base URL")
        self.cdp_max_concurrent = min(6, max(1, self.cdp_max_concurrent))

    @classmethod
    def load(cls):
        path = Path(os.getenv("RESEARCH_CONFIG", ROOT / "config/egress.yaml"))
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for key in (
            "ru_proxy",
            "eu_proxy",
            "database",
            "searxng_url",
            "discovery_backend",
            "extraction_url",
            "browser_url",
            "worker_token",
            "camoufox_python",
            "cdp_url",
            "public_base_url",
            "http_host",
        ):
            if "RESEARCH_" + key.upper() in os.environ:
                data[key] = os.environ["RESEARCH_" + key.upper()]
        for key in ("allow_internal_hosts", "oauth_enabled", "verify_egress", "native_connectors"):
            value = os.getenv("RESEARCH_" + key.upper(), "").strip().lower()
            if value in {"1", "true", "yes", "0", "false", "no"}:
                data[key] = value in {"1", "true", "yes"}
        if (concurrency := os.getenv("RESEARCH_CDP_MAX_CONCURRENT")) is not None:
            data["cdp_max_concurrent"] = int(concurrency)
        if (tokens := os.getenv("RESEARCH_API_TOKENS")) is not None:
            data["api_tokens"] = [token.strip() for token in tokens.split(",") if token.strip()]
        if countries := os.getenv("RESEARCH_EXPECTED_COUNTRIES", "").strip():
            entries = (
                json.loads(countries)
                if countries.startswith("{")
                else dict(entry.split("=", 1) for entry in countries.split(","))
            )
            data["expected_countries"] = {
                region.strip().upper(): country.strip().upper() for region, country in entries.items()
            }
        return cls(**data)
