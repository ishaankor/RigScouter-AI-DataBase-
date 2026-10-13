from abc import ABC, abstractmethod
from typing import List, Optional
from app.models.offer import Offer, ProductAnalysis

class BaseScraper(ABC):
    """
    Abstract Base Class for retailer scrapers.
    Each retailer adapter implements search and direct price lookup.
    """
    retailer_name: str = "Unknown"

    @abstractmethod
    async def search(
        self,
        query: str,
        analysis: Optional[ProductAnalysis] = None
    ) -> List[Offer]:
        """
        Search retailer for candidates matching the hardware component.
        """
        pass

    @abstractmethod
    async def get_price(self, url: str) -> Optional[Offer]:
        """
        Look up current price and stock status directly from a product URL.
        """
        pass
