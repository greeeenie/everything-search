from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    domains: dict[str, str] = field(default_factory=dict)
    default_region: str = "EU"
    marketplaces: dict[str, str] = field(default_factory=dict)
    enabled_marketplaces: list[str] = field(default_factory=list)
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

    def __post_init__(self):
        for proxy in (self.ru_proxy, self.eu_proxy):
            if not proxy:
                continue
            p = urlsplit(proxy)
            if (
                p.scheme != "socks5"
                or p.hostname not in {"127.0.0.1", "::1"}
                or not p.port
                or p.username
                or p.password
            ):
                raise ValueError("Egress proxies must be fixed loopback SOCKS5 endpoints")
        if self.default_region not in {"EU", "RU"} or any(
            x not in {"EU", "RU"} for x in self.domains.values()
        ):
            raise ValueError("Regions must be EU or RU")
        for endpoint in (self.searxng_url, self.extraction_url, self.browser_url):
            if endpoint:
                u = urlsplit(endpoint)
                if u.scheme != "http" or u.hostname not in {"127.0.0.1", "::1"} or u.username or u.password:
                    raise ValueError("Workers and discovery must use fixed loopback HTTP endpoints")

    @classmethod
    def load(cls):
        path = Path(os.getenv("RESEARCH_CONFIG", ROOT / "config/egress.yaml"))
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for key in (
            "ru_proxy",
            "eu_proxy",
            "database",
            "searxng_url",
            "discovery_backend",
            "extraction_url",
            "browser_url",
            "worker_token",
        ):
            if "RESEARCH_" + key.upper() in os.environ:
                data[key] = os.environ["RESEARCH_" + key.upper()]
        return cls(**data)
