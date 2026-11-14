from typing import Annotated, Literal
from typing import Any

from mcp.types import ToolAnnotations
from pydantic import Field

from .engine import get_engine

Query = Annotated[str, Field(min_length=1, max_length=300)]
Region = Literal["RU", "EU"] | None
Limit = Annotated[int, Field(ge=1, le=20)]


def register_tools(mcp):
    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

    @mcp.tool(annotations=read_only)
    async def search_products(
        query: Query,
        model: Query | None = None,
        marketplaces: list[str] | None = None,
        min_price: Annotated[float | None, Field(ge=0)] = None,
        max_price: Annotated[float | None, Field(ge=0)] = None,
        currency: Annotated[str | None, Field(pattern=r"^[A-Za-z]{3}$")] = None,
        condition: Literal["new", "used", "refurbished"] | None = None,
        city: str | None = None,
        max_results: Limit = 9,
        region: Region = None,
        specifications: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Find public product offers. Exact model prevents merging adjacent models.

        Marketplaces are ozon, avito, yandex_market or explicit shop domains.
        Offers require verified in-stock availability, a positive price and currency.
        Pass delivery city. Discovery candidates are diagnostic sources.
        """
        return await get_engine().search_products(
            query,
            model,
            marketplaces,
            min_price,
            max_price,
            currency,
            condition,
            city,
            max_results,
            region,
            specifications=specifications,
        )

    @mcp.tool(annotations=read_only)
    async def compare_prices(
        model: Query,
        marketplaces: list[str] | None = None,
        city: str | None = None,
        condition: Literal["new", "used", "refurbished"] | None = None,
        max_results: Limit = 12,
        region: Region = None,
    ) -> dict[str, Any]:
        """Compare exact-model offers by currency, condition and variants.

        Offers require verified in-stock availability, a positive price and currency.
        Best offer requires public price, stock and confirmed city delivery cost.
        """
        return await get_engine().compare_prices(model, marketplaces, city, condition, max_results, region)

    @mcp.tool(annotations=read_only)
    async def get_product_details(
        url: str | None = None, model: Query | None = None, region: Region = None
    ) -> dict[str, Any]:
        """Read public product specifications, variants and evidence from URL or exact model.

        Offers require verified in-stock availability, a positive price and currency.
        """
        return await get_engine().get_product_details(url, model, region)

    @mcp.tool(annotations=read_only)
    async def get_product_reviews(
        model: Query,
        domains: list[str] | None = None,
        max_results: Limit = 8,
        region: Region = None,
    ) -> dict[str, Any]:
        """Find reviews and model-local discussion fragments with dates and source attribution."""
        return await get_engine().get_product_reviews(model, domains, max_results, region)

    @mcp.tool(annotations=read_only)
    async def read_page(
        url: str,
        region: Region = None,
        dynamic: bool = False,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Read an allowed public HTTP(S) page. Dynamic rendering is opt-in for thin JS shells.

        Access challenges, authorization requirements and robots denials are returned as errors.
        """
        return await get_engine().read_page(url, region, dynamic, refresh)

    @mcp.tool(annotations=read_only)
    async def diagnose_egress() -> dict[str, Any]:
        """Check actual external IP and country of the RU and EU routes."""
        return await get_engine().diagnose_egress()
