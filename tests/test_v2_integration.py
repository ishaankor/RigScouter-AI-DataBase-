import sys
import os
import unittest
from unittest.mock import patch, AsyncMock
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import ProductAnalysis, BestBuyClient
from app.scrapers.microcenter import MicroCenterScraper
from app.models.offer import Offer


class TestV2IntegrationScrapersAndPipelines(unittest.TestCase):
    """
    VERSION 2: INTEGRATION TEST SUITE
    Tests multi-component scraper flows, simulated Tavily search payloads,
    Best Buy priceBlocks integration, and Micro Center stock evaluation.
    """

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.mc_scraper = MicroCenterScraper()
        self.bb_client = BestBuyClient()

        self.analysis_9600x = ProductAnalysis(
            raw_query="AMD Ryzen 5 9600X",
            brand="AMD",
            model="Ryzen 5 9600X",
            category="CPU",
            is_pc_part=True,
            retailer_search_query="AMD Ryzen 5 9600X",
            negative_keywords=[],
            min_price=180.0,
            product_exists=True
        )

        self.analysis_5600x = ProductAnalysis(
            raw_query="AMD Ryzen 5 5600X",
            brand="AMD",
            model="Ryzen 5 5600X",
            category="CPU",
            is_pc_part=True,
            retailer_search_query="AMD Ryzen 5 5600X",
            negative_keywords=[],
            min_price=100.0,
            product_exists=True
        )

    def tearDown(self):
        self.loop.close()

    # -------------------------------------------------------------
    # 1. Micro Center Scraper Integration
    # -------------------------------------------------------------
    @patch("app.scrapers.microcenter.query_tavily_search")
    def test_microcenter_scraper_marks_out_of_stock_accurately(self, mock_tavily):
        """Tests that MicroCenterScraper accurately identifies sold-out stock and marks in_stock=False."""
        mock_tavily.return_value = [
            {
                "url": "https://www.microcenter.com/product/674512/amd-ryzen-5-5600x-vermeer-oem",
                "title": "AMD Ryzen 5 5600X Vermeer 3.7GHz 6-Core AM4 OEM Processor",
                "content": "Original price $249.99 Todays price $219.99. 0 in stock at your local store. Sold out online."
            }
        ]

        offers = self.loop.run_until_complete(
            self.mc_scraper.search("AMD Ryzen 5 5600X", self.analysis_5600x)
        )

        self.assertEqual(len(offers), 1)
        self.assertFalse(offers[0].in_stock)
        self.assertEqual(offers[0].price, 219.99)
        self.assertEqual(offers[0].retailer, "Micro Center")

    @patch("app.scrapers.microcenter.query_tavily_search")
    def test_microcenter_scraper_prioritizes_in_stock_offers(self, mock_tavily):
        """Tests that MicroCenterScraper ranks in-stock items ahead of out-of-stock items."""
        mock_tavily.return_value = [
            {
                "url": "https://www.microcenter.com/product/682001/amd-ryzen-5-9600x",
                "title": "AMD Ryzen 5 9600X 6-Core AM5 Processor",
                "content": "Price: $219.99. Sold out online, 0 in stock."
            },
            {
                "url": "https://www.microcenter.com/product/682002/amd-ryzen-5-9600x-boxed",
                "title": "AMD Ryzen 5 9600X 6-Core AM5 Boxed Processor",
                "content": "Price: $229.99. 14 in stock for immediate in-store pickup."
            }
        ]

        offers = self.loop.run_until_complete(
            self.mc_scraper.search("AMD Ryzen 5 9600X", self.analysis_9600x)
        )

        self.assertEqual(len(offers), 2)
        # First offer must be the in-stock offer
        self.assertTrue(offers[0].in_stock)
        self.assertEqual(offers[0].price, 229.99)
        # Second offer is out of stock
        self.assertFalse(offers[1].in_stock)

    # -------------------------------------------------------------
    # 2. Best Buy Fallback Shield Integration
    # -------------------------------------------------------------
    @patch("agent.query_tavily_search")
    @patch.object(BestBuyClient, "_lookup_priceblocks_sku")
    def test_bestbuy_fallback_drops_inactive_sku_carousel_prices(self, mock_priceblocks, mock_tavily):
        """
        Simulates discontinued Best Buy page (5600X) where priceBlocks is None
        and search snippet contains $559 from AM5 carousel.
        Verifies that candidates list is empty (shielded).
        """
        mock_tavily.return_value = [
            {
                "url": "https://www.bestbuy.com/site/amd-ryzen-5-5600x-4th-gen-6-core/6438943.p",
                "title": "AMD - Ryzen 5 5600X 4th Gen 6 core Unlocked Desktop Processor - Best Buy",
                "content": "AMD Ryzen 5 5600X ... Customers also viewed: Socket AM5 Ryzen 9 7900X $559.00 New in stock."
            }
        ]
        # Inactive SKU returns None
        mock_priceblocks.return_value = None

        candidates = self.loop.run_until_complete(
            self.bb_client._fallback_tavily_search(self.analysis_5600x)
        )

        # Must return empty list, NOT $559.00
        self.assertEqual(candidates, [])

    @patch("agent.query_tavily_search")
    @patch.object(BestBuyClient, "_lookup_priceblocks_sku")
    def test_bestbuy_fallback_accepts_active_priceblocks_sku(self, mock_priceblocks, mock_tavily):
        """
        Simulates active Best Buy SKU where priceBlocks returns live pricing and stock status.
        """
        mock_tavily.return_value = [
            {
                "url": "https://www.bestbuy.com/site/amd-ryzen-5-9600x-zen-5-6-core/6591234.p",
                "title": "AMD Ryzen 5 9600X - Best Buy",
                "content": "AMD Ryzen 5 9600X Desktop Processor"
            }
        ]
        mock_priceblocks.return_value = {
            "title": "AMD Ryzen 5 9600X 6-Core 12-Thread AM5 Processor",
            "price": 229.99,
            "originalPrice": 249.99,
            "inStock": True,
            "url": "https://www.bestbuy.com/site/amd-ryzen-5-9600x/6591234.p",
            "imageUrl": "https://pisces.bbystatic.com/image2/6591234.jpg",
            "sku": "6591234"
        }

        candidates = self.loop.run_until_complete(
            self.bb_client._fallback_tavily_search(self.analysis_9600x)
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["price"], 229.99)
        self.assertEqual(candidates[0]["retailer"], "Best Buy")
        self.assertTrue(candidates[0]["inStock"])
        self.assertEqual(candidates[0]["condition"], "new")


if __name__ == "__main__":
    unittest.main(verbosity=2)
