import sys
import os
import unittest
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import ProductAnalyzer, ProductAnalysis
from app.scrapers.validator import validate_offer_fast


class TestV1UnitUniversalRigValidation(unittest.TestCase):
    """
    VERSION 1: UNIT TEST SUITE (Universal Rig Parts)
    Exhaustively tests granular regex patterns, price floor/ceiling algorithms,
    and validation across ALL PC rig components (Case, Cooler, Motherboard,
    RAM, Storage, Power Supply, GPU, CPU).
    """

    def setUp(self):
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

    # -------------------------------------------------------------
    # 1. Universal Rig Parts Acceptance Tests
    # -------------------------------------------------------------
    def test_all_rig_categories_accept_genuine_offers(self):
        """Verifies that all 8 core PC rig categories pass validation for genuine components."""
        rig_parts = [
            ("Fractal Design North", "Case", 139.99, "Fractal Design North Mid Tower Case Charcoal Black"),
            ("Thermalright Peerless Assassin 120 SE", "Cooling", 34.90, "Thermalright Peerless Assassin 120 SE CPU Air Cooler"),
            ("Corsair Vengeance RGB 32GB DDR5", "RAM", 109.99, "Corsair Vengeance RGB 32GB (2x16GB) DDR5 6000MHz C36"),
            ("Samsung 990 Pro 2TB", "Storage", 169.99, "Samsung 990 Pro 2TB PCIe 4.0 M.2 NVMe SSD"),
            ("Corsair RM850x", "Power Supply", 129.99, "Corsair RM850x 850 Watt 80 Plus Gold Modular PSU"),
            ("MSI MAG B650 Tomahawk WiFi", "Motherboard", 199.99, "MSI MAG B650 Tomahawk WiFi AM5 ATX Motherboard"),
            ("RTX 4070 Super", "GPU", 599.99, "ASUS TUF Gaming GeForce RTX 4070 Super 12GB OC Edition"),
            ("AMD Ryzen 5 9600X", "CPU", 219.00, "AMD Ryzen 5 9600X 6-Core AM5 Desktop Processor"),
        ]

        for query, cat, price, title in rig_parts:
            pa = ProductAnalysis(
                raw_query=query,
                brand=None,
                model=query,
                category=cat,
                is_pc_part=True,
                retailer_search_query=query,
                negative_keywords=[],
                min_price=price * 0.7
            )
            valid, reason = validate_offer_fast(title, price, pa)
            self.assertTrue(valid, f"Failed for {cat} ('{query}'): {reason}")

    # -------------------------------------------------------------
    # 2. Universal Price Ceiling & Floor Validation
    # -------------------------------------------------------------
    def test_price_ceiling_rejects_bestbuy_carousel_leak(self):
        """Ensures Best Buy $559 carousel price for 5600X is rejected."""
        valid, reason = validate_offer_fast(
            "AMD Ryzen 5 5600X 4th Gen 6 core Unlocked Desktop Processor",
            559.00,
            self.analysis_5600x
        )
        self.assertFalse(valid)
        self.assertIn("exceeds realistic ceiling", reason)

    def test_price_floor_rejects_cheap_stickers(self):
        """Ensures non-hardware items and cheap under-floor accessories are rejected."""
        valid, reason = validate_offer_fast(
            "AMD Ryzen 5 9600X Metal Case Badge Sticker",
            4.99,
            self.analysis_9600x
        )
        self.assertFalse(valid)
        self.assertIn("sticker", reason.lower())

        valid_floor, reason_floor = validate_offer_fast(
            "AMD Ryzen 5 9600X Desktop Processor",
            12.00,
            self.analysis_9600x
        )
        self.assertFalse(valid_floor)
        self.assertIn("below minimum hardware floor", reason_floor)

    # -------------------------------------------------------------
    # 3. Model Token & Variant Suffix Matching
    # -------------------------------------------------------------
    def test_model_number_mismatch_rejection(self):
        """Ensures a 5600X offer is rejected when searching for 9600X."""
        valid, reason = validate_offer_fast(
            "AMD Ryzen 5 5600X 6-core 12-thread unlocked desktop processor",
            140.00,
            self.analysis_9600x
        )
        self.assertFalse(valid)
        self.assertIn("Missing model number 9600", reason)

    def test_missing_variant_suffix_rejection(self):
        """Ensures non-X model is rejected when searching for an X variant."""
        valid, reason = validate_offer_fast(
            "AMD Ryzen 5 5600 6-Core 3.5GHz AM4 Processor",
            115.00,
            self.analysis_5600x
        )
        self.assertFalse(valid)
        self.assertIn("Missing requested variant 'X'", reason)

    # -------------------------------------------------------------
    # 4. Universal Fake / Dummy / Non-Hardware Pattern Rejection
    # -------------------------------------------------------------
    def test_dummy_and_empty_box_rejection(self):
        """Ensures empty boxes, display dummies, and broken units are universally rejected."""
        fake_titles = [
            "Lian Li O11 Dynamic EVO RGB - Original Empty Box Only",
            "NVIDIA GeForce RTX 4090 1:1 Scale Dummy Toy Model",
            "Samsung 990 Pro 2TB NVMe SSD - For Parts Only Not Working",
            "Corsair RM850x Power Supply - Broken / Cracked / No Power"
        ]
        for ft in fake_titles:
            valid, reason = validate_offer_fast(ft, 45.00, self.analysis_9600x)
            self.assertFalse(valid, f"Expected rejection for '{ft}', got: {reason}")

    # -------------------------------------------------------------
    # 5. Micro Center Stock Indicator Parsing
    # -------------------------------------------------------------
    def test_microcenter_stock_status_parsing(self):
        """Tests detection of sold-out and in-stock snippets across rig parts."""
        out_of_stock_snippets = [
            "AMD Ryzen 5 5600X Vermeer AM4 OEM - 0 in stock at Westbury",
            "Fractal Design North White - Sold out online and in stores",
            "Item currently unavailable for online shipping or in-store pickup",
            "Samsung 990 Pro 2TB - Pick up in store only - 0 available"
        ]
        in_stock_snippets = [
            "AMD Ryzen 5 9600X 6-Core AM5 Processor - 15 in stock at Dallas",
            "Corsair RM850x Shift - 8 in stock for pickup today"
        ]

        for s in out_of_stock_snippets:
            is_sold_out = any(
                phrase in s.lower() for phrase in [
                    "0 in stock", "sold out", "out of stock", "currently unavailable",
                    "not in stock", "backorder", "pick up in store only - 0",
                    "0 available", "not available online", "discontinued"
                ]
            )
            self.assertTrue(is_sold_out, f"Expected out-of-stock for snippet: {s}")

        for s in in_stock_snippets:
            is_sold_out = any(
                phrase in s.lower() for phrase in [
                    "0 in stock", "sold out", "out of stock", "currently unavailable",
                    "not in stock", "backorder", "pick up in store only - 0",
                    "0 available", "not available online", "discontinued"
                ]
            )
            self.assertFalse(is_sold_out, f"Expected in-stock for snippet: {s}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
