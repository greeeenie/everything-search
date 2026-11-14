import asyncio
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .models import SearchHit
from .network import AccessError, EgressRouter, host_matches, validate_url


def canonical_url(url):
    u, host = validate_url(url)
    query = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid", "yclid"}
    ]
    authority = f"[{host}]" if ":" in host else host
    if u.port and u.port != (443 if u.scheme == "https" else 80):
        authority += f":{u.port}"
    return urlunsplit((u.scheme, authority, u.path or "/", urlencode(sorted(query)), ""))


class Discovery:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.router = EgressRouter(settings)
        self.semaphore = asyncio.Semaphore(settings.concurrency)

    async def search(self, query, domains=None, limit=8, exact=False, region=None):
        query = query.strip()
        if not query or len(query) > 500 or not 1 <= limit <= 20:
            raise ValueError("query must contain 1..500 characters; limit 1..20")
        domains = list(dict.fromkeys((d.lower().rstrip(".") for d in domains or [])))
        if len(domains) > 8 or any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,63}", d) for d in domains):
            raise ValueError("Use at most eight plain domains")
        phrase = '"' + query.replace('"', "") + '"' if exact else query
        queries = [(phrase + (" site:" + d if d else ""), [d] if d else []) for d in domains or [None]]
        rows = await asyncio.gather(
            *(self._query(q, allowed, limit, region) for q, allowed in queries), return_exceptions=True
        )
        hits, errors, seen = [], [], set()
        for (q, _), result in zip(queries, rows):
            if isinstance(result, Exception):
                errors.append(
                    {
                        "query": q,
                        "error": str(result) if isinstance(result, AccessError) else "discovery_error",
                    }
                )
                continue
            for hit in result:
                if hit.url not in seen:
                    hits.append(hit)
                    seen.add(hit.url)
        # Round-robin query results so a single shop cannot consume the whole budget.
        hits = sorted(
            hits, key=lambda h: next((i for i, r in enumerate(rows) if isinstance(r, list) and h in r), 0)
        )
        if domains:
            buckets = [[h for h in hits if host_matches(urlsplit(h.url).hostname or "", d)] for d in domains]
            hits = [
                h for batch in __import__("itertools").zip_longest(*buckets) for h in batch if h is not None
            ]
        return hits[:limit], errors

    async def _query(self, query, domains, limit, region):
        backend = self.settings.discovery_backend
        key = "search:" + hashlib.sha256(repr((backend, query, domains, limit, region)).encode()).hexdigest()
        cached = self.store.get(key)
        if cached:
            if isinstance(cached, dict) and "error" in cached:
                raise AccessError(cached["error"])
            return [SearchHit(**x) for x in cached]
        try:
            async with (
                self.semaphore,
                httpx.AsyncClient(timeout=self.settings.timeout, trust_env=False) as client,
            ):
                if backend == "searxng":
                    response = await client.get(
                        self.settings.searxng_url + "/search",
                        params={
                            "q": query,
                            "format": "json",
                            "language": "ru-RU" if region == "RU" else "auto",
                        },
                    )
                elif backend == "legacy-index":
                    # Explicit compatibility option only, never an automatic fallback.
                    response = await client.post(
                        "https://api.keenable.ai/v1/search/public",
                        json={"query": query, "max_results": limit},
                    )
                else:
                    raise AccessError("unknown_discovery_backend")
                if response.status_code != 200:
                    self.store.put(
                        key, {"error": f"discovery_http_{response.status_code}"}, self.settings.error_ttl
                    )
                    raise AccessError(f"discovery_http_{response.status_code}")
                payload = response.json()
                data = payload.get("results", [])
                if not data and payload.get("unresponsive_engines"):
                    self.store.put(key, {"error": "discovery_upstream_unavailable"}, self.settings.error_ttl)
                    raise AccessError("discovery_upstream_unavailable")
        except httpx.TimeoutException:
            self.store.put(key, {"error": "discovery_timeout"}, self.settings.error_ttl)
            raise AccessError("discovery_timeout") from None
        except (httpx.RequestError, ValueError):
            self.store.put(key, {"error": "discovery_unavailable"}, self.settings.error_ttl)
            raise AccessError("discovery_unavailable") from None
        hits = []
        for row in data:
            try:
                url = canonical_url(row["url"])
            except (KeyError, AccessError):
                continue
            if domains and not any(host_matches(urlsplit(url).hostname or "", d) for d in domains):
                continue
            hit = SearchHit(
                url=url,
                title=str(row.get("title", ""))[:500],
                search_excerpt=str(row.get("content") or row.get("snippet") or row.get("description") or "")[
                    :1200
                ],
                source=backend,
                status="indexed_only",
                region=self.router.choose(url, region),
            )
            hits.append(hit)
            self.store.record(hit)
        self.store.put(key, [h.model_dump(mode="json") for h in hits], self.settings.search_ttl)
        return hits
