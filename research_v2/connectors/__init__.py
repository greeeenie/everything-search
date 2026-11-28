"""Native marketplace connector contracts and shared transport."""

from .base import (
    ConnectorCardResult,
    ConnectorSearchResult,
    ConnectorTransport,
    MarketplaceConnector,
    SourceOutcome,
)
from .registry import NativeMarketplaceHub

__all__ = [
    "ConnectorCardResult",
    "ConnectorSearchResult",
    "ConnectorTransport",
    "MarketplaceConnector",
    "NativeMarketplaceHub",
    "SourceOutcome",
]
