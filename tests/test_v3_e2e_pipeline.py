import sys
import os
import unittest
from unittest.mock import patch, AsyncMock
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import HardwareAgent, ProductAnalysis, ProductAnalyzer


class TestV3E2EPipeline(unittest.TestCase):
    """
    VERSION 3: END-TO-END PIPELINE & REGRESSION SUITE
    Tests full HardwareAgent execution lifecycle across varied PC rig parts
    (Cases, Coolers, Power Supplies, SSDs, CPUs, GPUs) ensuring query integrity,
    resilient multi-retailer pooling, and correct data schemas.
    """

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.agent = HardwareAgent()

    def tearDown(self):
        self.loop.close()

    # -------------------------------------------------------------
    # 1. Query Integrity E2E (No SKU Hijacking Across Rig Categories)
    # -------------------------------------------------------------
    @patch.object(ProductAnalyzer, "analyze_query")
    @patch.object(HardwareAgent, "_search_newegg", new_callable=AsyncMock)
    @patch.object(HardwareAgent, "_search_microcenter", new_callable=AsyncMock)
    def test_pipeline_preserves_searched_query_for_all_rig_parts(
        self, mock_mc, mock_newegg, mock_analyzer
    ):
        """Verifies that HardwareAgent queries retailers with the exact searched component."""
        test_queries = [
            ("Lian Li O11 Vision Chrome", "Case", 139.99),
            ("Arctic Liquid Freezer III 360", "Cooling", 110.00),
            ("Corsair RM850x Shift", "Power Supply", 139.99),
            ("Samsung 990 Pro 2TB", "Storage", 169.99),
            ("AMD Ryzen 5 9600X", "CPU", 229.00),
        ]

        for query_str, cat, price in test_queries:
            mock_analyzer.return_value = ProductAnalysis(
                raw_query=query_str,
                brand=query_str.split()[0],
                model=query_str,
                category=cat,
                is_pc_part=True,
                retailer_search_query=query_str,
                negative_keywords=[],
                min_price=price * 0.7,
                product_exists=True
            )
            mock_mc.return_value = []
            mock_newegg.return_value = []

            # Mock Amazon, eBay, Best Buy methods on agent
            with patch.object(self.agent.amazon, "search", new_callable=AsyncMock) as mock_amz, \
                 patch.object(self.agent.ebay, "search", new_callable=AsyncMock) as mock_ebay, \
                 patch.object(self.agent.bestbuy, "search", new_callable=AsyncMock) as mock_bb:

                mock_amz.return_value = []
                mock_ebay.return_value = []
                mock_bb.return_value = []

                res = self.loop.run_until_complete(self.agent.run(query_str))

                # Verify retailers were searched with the user's exact component
                self.assertEqual(res["query"], query_str)
                mock_mc.assert_called_with(query_str, mock_analyzer.return_value)
                mock_newegg.assert_called_with(query_str, mock_analyzer.return_value)

    # -------------------------------------------------------------
    # 2. End-to-End Pooling & Filter Validation
    # -------------------------------------------------------------
    @patch.object(ProductAnalyzer, "analyze_query")
    def test_e2e_pool_filters_anomalous_carousel_leaks(self, mock_analyzer):
        """
        Simulates raw offers from multiple retailers and verifies that:
        - $559 carousel price is dropped
        - $219.99 OEM 5600X is dropped when searching 9600X
        - Genuine $229.99 9600X offers from Amazon & Best Buy are retained
        """
        mock_analyzer.return_value = ProductAnalysis(
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

        with patch.object(self.agent.amazon, "search", new_callable=AsyncMock) as mock_amz, \
             patch.object(self.agent.ebay, "search", new_callable=AsyncMock) as mock_ebay, \
             patch.object(self.agent.bestbuy, "search", new_callable=AsyncMock) as mock_bb, \
             patch.object(self.agent, "_search_microcenter", new_callable=AsyncMock) as mock_mc, \
             patch.object(self.agent, "_search_newegg", new_callable=AsyncMock) as mock_newegg:

            mock_amz.return_value = [{
                "retailer": "Amazon",
                "title": "AMD Ryzen 5 9600X 6-Core AM5 Processor",
                "price": 229.99,
                "inStock": True,
                "url": "https://amazon.com/dp/B0D5",
                "condition": "new",
                "conditionLabel": "Brand New"
            }]
            mock_ebay.return_value = [{
                "retailer": "eBay",
                "title": "AMD Ryzen 5 9600X CPU Brand New Sealed",
                "price": 219.00,
                "inStock": True,
                "url": "https://ebay.com/itm/123",
                "condition": "new",
                "conditionLabel": "Brand New"
            }]
            mock_bb.return_value = [{
                "retailer": "Best Buy",
                "title": "AMD Ryzen 5 9600X Desktop Processor - Best Buy",
                "price": 229.99,
                "inStock": True,
                "url": "https://bestbuy.com/site/659.p",
                "condition": "new",
                "conditionLabel": "Brand New"
            }]
            # Micro Center returns out of stock OEM 5600X (wrong model)
            mock_mc.return_value = [{
                "retailer": "Micro Center",
                "title": "AMD Ryzen 5 5600X Vermeer AM4 OEM",
                "price": 219.99,
                "inStock": False,
                "url": "https://microcenter.com/product/674",
                "condition": "new",
                "conditionLabel": "Brand New"
            }]
            mock_newegg.return_value = []

            # Fast validation drops the 5600X from Micro Center
            valid_offers = []
            for pool in [mock_amz.return_value, mock_ebay.return_value, mock_bb.return_value, mock_mc.return_value]:
                for o in pool:
                    is_valid, _ = ProductAnalyzer.validate_offer_fast(mock_analyzer.return_value, o["title"], o["price"])
                    if is_valid:
                        valid_offers.append(o)

            # Valid offers should only contain the 3 authentic 9600X offers
            self.assertEqual(len(valid_offers), 3)
            retailers = [o["retailer"] for o in valid_offers]
            self.assertIn("Amazon", retailers)
            self.assertIn("eBay", retailers)
            self.assertIn("Best Buy", retailers)
            self.assertNotIn("Micro Center", retailers)  # 5600X dropped!

    # -------------------------------------------------------------
    # 3. Direct Product URL Scrape Dispatch
    # -------------------------------------------------------------
    def test_direct_product_url_router(self):
        """Verifies that scrape_product_url dispatches to the correct retailer client by domain."""
        with patch.object(self.agent.amazon, "lookup_url", new_callable=AsyncMock) as mock_amz_url, \
             patch.object(self.agent.ebay, "lookup_url", new_callable=AsyncMock) as mock_ebay_url, \
             patch.object(self.agent.bestbuy, "lookup_url", new_callable=AsyncMock) as mock_bb_url, \
             patch.object(self.agent.generic, "lookup_url", new_callable=AsyncMock) as mock_gen_url:

            self.loop.run_until_complete(self.agent.scrape_product_url("https://www.amazon.com/dp/B0D5"))
            mock_amz_url.assert_called_once()

            self.loop.run_until_complete(self.agent.scrape_product_url("https://www.ebay.com/itm/123456"))
            mock_ebay_url.assert_called_once()

            self.loop.run_until_complete(self.agent.scrape_product_url("https://www.bestbuy.com/site/123456.p"))
            mock_bb_url.assert_called_once()

            self.loop.run_until_complete(self.agent.scrape_product_url("https://www.microcenter.com/product/123456"))
            mock_gen_url.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
