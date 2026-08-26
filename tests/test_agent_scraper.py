import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import re
from bs4 import BeautifulSoup
import agent

def test_scraper():
    a = agent.TavilyHardwareAgent()
    passed = 0
    total = 0

    def assert_eq(actual, expected, msg):
        nonlocal passed, total
        total += 1
        assert actual == expected, f"FAILED [{msg}]: expected {expected}, got {actual}"
        passed += 1

    print("=================================================================")
    print("RUNNING AGENT.PY LIVE PARSER & SEMANTIC MATCHER TEST SUITE")
    print("=================================================================")

    # 1. Best Buy Schema.org JSON-LD Test
    bb_html = """
    <html><head>
    <script type="application/ld+json">
    {
        "@context": "http://schema.org",
        "@type": "Product",
        "name": "GIGABYTE - NVIDIA GeForce RTX 5080 WINDFORCE OC 16GB GDDR7 Graphics Card - Black",
        "image": "https://pisces.bbystatic.com/image2/BestBuy_US/images/products/6616/6616102_sd.jpg",
        "offers": {
            "@type": "Offer",
            "price": 1199.99,
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
            "itemCondition": "https://schema.org/NewCondition"
        }
    }
    </script>
    </head><body></body></html>
    """
    res_bb = asyncio.run(a.parse_page_content(bb_html, "", "https://bestbuy.com/product/123", "Best Buy", "GPU", "RTX 5080"))
    assert_eq(res_bb['price'], 1199.99, "Best Buy JSON-LD price")
    assert_eq(res_bb['inStock'], True, "Best Buy JSON-LD inStock")
    assert_eq(res_bb['source'], "json-ld", "Best Buy source")

    # 2. B&H Photo Video Schema.org JSON-LD Test
    bh_html = """
    <html><head>
    <script type="application/ld+json">
    [
      {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Gigabyte GeForce RTX 5070 AERO OC 12GB Graphics Card",
        "image": "https://static.bhphoto.com/images/item.jpg",
        "offers": [
          {
            "@type": "Offer",
            "price": "849.99",
            "priceCurrency": "USD",
            "availability": "http://schema.org/InStock"
          }
        ]
      }
    ]
    </script>
    </head><body></body></html>
    """
    res_bh = asyncio.run(a.parse_page_content(bh_html, "", "https://bhphotovideo.com/c/product/456", "B&H", "GPU", "RTX 5070"))
    assert_eq(res_bh['price'], 849.99, "B&H JSON-LD price")
    assert_eq(res_bh['inStock'], True, "B&H JSON-LD inStock")

    # 3. Micro Center OpenGraph Meta Tags Test
    mc_html = """
    <html><head>
    <meta property="og:title" content="NVIDIA GeForce RTX 5080 Overclocked Triple Fan 16GB GDDR7 PCIe 5.0 Graphics Card" />
    <meta property="og:price:amount" content="1699.99" />
    <meta property="og:availability" content="instock" />
    <meta property="og:image" content="https://microcenter.com/img.jpg" />
    </head><body></body></html>
    """
    res_mc = asyncio.run(a.parse_page_content(mc_html, "", "https://microcenter.com/product/789", "Micro Center", "GPU", "RTX 5080"))
    assert_eq(res_mc['price'], 1699.99, "Micro Center OpenGraph price")
    assert_eq(res_mc['inStock'], True, "Micro Center OpenGraph inStock")

    # 4. Newegg Clean DOM Buybox Test (ignoring sponsored & recommended boxes)
    newegg_html = """
    <html><body>
    <div class="recommended-box">
      <div class="price-current"><strong>$209</strong><sup>.99</sup></div>
    </div>
    <div class="product-pane">
      <h1 class="product-title">ASUS TUF Gaming GeForce RTX 5080 16GB GDDR7 PCI Express 5.0 Video Card</h1>
      <div class="price-current"><strong>$1,349</strong><sup>.99</sup></div>
      <div class="product-inventory">In Stock</div>
    </div>
    </body></html>
    """
    res_ne = asyncio.run(a.parse_page_content(newegg_html, "", "https://newegg.com/p/N82E168123", "Newegg", "GPU", "RTX 5080"))
    assert_eq(res_ne['price'], 1349.99, "Newegg DOM buybox price")
    assert_eq(res_ne['inStock'], True, "Newegg inStock")

    # 4b. Newegg 2026 Layout with Sponsored Headline Ad Banner & price-current_2026
    newegg_2026_html = """
    <html><body>
    <div class="product-headline is-new">
      <div class="item-container">
        <a class="item-title">GIGABYTE 27 inch QHD Monitor</a>
        <li class="price-current">$<strong>129</strong><sup>.99</sup></li>
        <div class="item-sponsored">Sponsored</div>
      </div>
    </div>
    <div class="product-buy-box is-product-blackfriday-first">
      <h1 class="product-title">ASUS PRIME GeForce RTX 5080 16GB GDDR7 Graphics Card</h1>
      <div class="product-price">
        <div class="price-current_2026">$<strong>1,745</strong><sup>.99</sup></div>
      </div>
    </div>
    <div class="product-sellers">
      <div class="seller-price">
        <li class="price-current">$<strong>2,450</strong><sup>.00</sup></li>
      </div>
    </div>
    </body></html>
    """
    res_ne26 = asyncio.run(a.parse_page_content(newegg_2026_html, "", "https://newegg.com/asus-prime-rtx5080-16g/p/1FT-000Y-00BS2", "Newegg", "GPU", "RTX 5080"))
    assert_eq(res_ne26['price'], 1745.99, "Newegg 2026 buybox correctly ignores headline ad and sellers")
    assert_eq(res_ne26['inStock'], True, "Newegg 2026 inStock")

    # 5. Out of stock item test
    oos_html = """
    <html><head>
    <script type="application/ld+json">
    {
        "@type": "Product",
        "name": "NVIDIA GeForce RTX 5080 Founders Edition",
        "offers": {
            "price": 999.99,
            "availability": "https://schema.org/OutOfStock"
        }
    }
    </script>
    </head><body></body></html>
    """
    res_oos = asyncio.run(a.parse_page_content(oos_html, "", "https://bestbuy.com/p/999", "Best Buy", "GPU", "RTX 5080"))
    assert_eq(res_oos['inStock'], False, "Out of Stock detected accurately")

    # 5b. B&H Discontinued / No Longer Available Page Test (from real user screenshot)
    bh_discontinued_html = """
    <html><head>
    <script type="application/ld+json">
    {
        "@type": "Product",
        "name": "EVGA GeForce GTX 1070 GAMING ACX 3.0 Black Edition Graphics Card",
        "offers": {
            "price": "599.00",
            "priceCurrency": "USD",
            "availability": "http://schema.org/InStock"
        }
    }
    </script>
    </head><body>
    <h1>EVGA GeForce GTX 1070 GAMING ACX 3.0 Black Edition Graphics Card</h1>
    <div data-selenium="stockStatus">No Longer Available</div>
    </body></html>
    """
    res_bh_disc = asyncio.run(a.parse_page_content(bh_discontinued_html, "", "https://bhphotovideo.com/c/product/123", "B&H", "GPU", "GTX 1070"))
    assert_eq(res_bh_disc['inStock'], False, "B&H No Longer Available item detected as out of stock")

    # 6. Semantic Entity Matcher Tests
    assert_eq(a.is_semantic_product_match("GIGABYTE AORUS RTX 5080 16GB", "RTX 5080", "GPU"), True, "Valid GPU match")
    assert_eq(a.is_semantic_product_match("12VHPWR Power Cable for RTX 5080 5090", "RTX 5080", "GPU"), False, "Accessory cable rejected")
    assert_eq(a.is_semantic_product_match("RTX 5080 Water Block Backplate Cooler", "RTX 5080", "GPU"), False, "Waterblock accessory rejected")
    assert_eq(a.is_semantic_product_match("NVIDIA GEFORCE 50 SERIES RTX 5070 Ti 5080 5090 ...", "RTX 5070", "GPU"), False, "Multi-model keyword spam rejected")
    assert_eq(a.is_semantic_product_match("ASUS ROG Strix RTX 5080 Gaming Laptop", "RTX 5080", "GPU"), False, "Laptop rejected for GPU query")

    # 7. Adaptability across various categories (No hardcoded floors)
    assert_eq(a.is_semantic_product_match("AMD Ryzen 7 7800X3D 8-Core Processor", "Ryzen 7 7800X3D", "CPU"), True, "CPU match")
    assert_eq(a.is_semantic_product_match("Corsair Vengeance RGB 32GB (2x16GB) DDR5 6000MHz", "Corsair Vengeance DDR5 32GB", "RAM"), True, "RAM match")
    assert_eq(a.is_semantic_product_match("Samsung 990 Pro 2TB NVMe SSD", "Samsung 990 Pro 2TB", "Storage"), True, "Storage match")
    assert_eq(a.is_semantic_product_match("Logitech G PRO X SUPERLIGHT 2 Wireless Gaming Mouse", "Logitech G Pro X Superlight", "Peripherals"), True, "Peripheral mouse match")

    # 8. Older budget hardware & used listings
    old_gpu_html = """
    <html><head>
    <script type="application/ld+json">
    {
        "@type": "Product",
        "name": "EVGA GeForce GTX 1080 SC GAMING 8GB GDDR5X",
        "offers": {
            "price": 94.99,
            "availability": "https://schema.org/InStock",
            "itemCondition": "https://schema.org/UsedCondition"
        }
    }
    </script>
    </head><body></body></html>
    """
    res_old = asyncio.run(a.parse_page_content(old_gpu_html, "", "https://ebay.com/itm/1080", "eBay", "GPU", "GTX 1080"))
    assert_eq(res_old['price'], 94.99, "Older/budget hardware supported without artificial floors")
    assert_eq(res_old['isRefurbished'], True, "Used condition detected")

    # 9. Ensure Zero-Snippet-Guessing rule in agent.py
    assert not hasattr(a, 'extract_clean_snippet_price'), "Snippet regex extraction function must be removed"
    assert not hasattr(a, 'is_price_sanity_valid'), "Hardcoded sanity floor table must be removed"

    print(f"\n=================================================================")
    print(f"🎉 ALL {passed}/{total} AGENT SCRAPER TESTS PASSED WITH 100% ACCURACY!")
    print("=================================================================")

if __name__ == "__main__":
    test_scraper()
