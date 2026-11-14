# Инструменты Research

Экспорт текущих MCP-описаний. Полные схемы параметров и результатов находятся в `tools.json`.

## `search_hacker_news`

```text
search_hacker_news(query, max_results=6)
```

```text
Find relevant Hacker News stories and comments with links to individual comments.
```

## `search_github_issues`

```text
search_github_issues(query, repository=null, max_results=4)
```

```text
Search public GitHub issues and sample their comments. For GitHub Discussions use search_web.
```

## `search_web`

```text
search_web(query, site=null, max_results=4, region=null)
```

```text
Search and read public web pages. Use a domain filter for a specific site.

Product listings require verified in-stock offers with a positive price and currency.
Excluded product pages are counted in excluded_product_pages.
```

## `research`

```text
research(query, max_threads=4, mode=null, domains=null, model=null, city=null)
```

```text
Research multiple sources with mode=quick (30s) or deep (180s).

Pass an exact product model for prices/reviews and a city for delivery.
Omitting mode preserves the original Reddit-only indexed-copy response.
```

## `search_products`

```text
search_products(query, model=null, marketplaces=null, min_price=null, max_price=null, currency=null, condition=null, city=null, max_results=9, region=null, specifications=null)
```

```text
Find public product offers. Exact model prevents merging adjacent models.

Marketplaces are ozon, avito, yandex_market or explicit shop domains.
Offers require verified in-stock availability, a positive price and currency.
Pass delivery city. Discovery candidates are diagnostic sources.
```

## `compare_prices`

```text
compare_prices(model, marketplaces=null, city=null, condition=null, max_results=12, region=null)
```

```text
Compare exact-model offers by currency, condition and variants.

Offers require verified in-stock availability, a positive price and currency.
Best offer requires public price, stock and confirmed city delivery cost.
```

## `get_product_details`

```text
get_product_details(url=null, model=null, region=null)
```

```text
Read public product specifications, variants and evidence from URL or exact model.

Offers require verified in-stock availability, a positive price and currency.
```

## `get_product_reviews`

```text
get_product_reviews(model, domains=null, max_results=8, region=null)
```

```text
Find reviews and model-local discussion fragments with dates and source attribution.
```

## `read_page`

```text
read_page(url, region=null, dynamic=false, refresh=false)
```

```text
Read an allowed public HTTP(S) page. Dynamic rendering is opt-in for thin JS shells.

Access challenges, authorization requirements and robots denials are returned as errors.
```

## `diagnose_egress`

```text
diagnose_egress()
```

```text
Check actual external IP and country of the RU and EU routes.
```
