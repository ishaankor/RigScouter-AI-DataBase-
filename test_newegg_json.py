import requests
import json
import re

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
}
url = "https://www.newegg.com/intel-core-ultra-7-270k-plus-core-ultra-7-series-2-arrow-lake-refresh-lga-1851-desktop-cpu-processor/p/N82E16819118628"
res = requests.get(url, headers=headers)
html = res.text

# Try to find application/ld+json
scripts = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
for s in scripts:
    try:
        data = json.loads(s)
        if isinstance(data, dict) and data.get('@type') == 'Product':
            offers = data.get('offers', {})
            print(f"JSON-LD Price: {offers.get('price')}")
    except Exception as e:
        pass

# Also let's see what prices are in the page
prices = re.findall(r'class="price-current"(?:.*?)>.*?<strong>(.*?)</strong>(?:.*?)<sup>(.*?)</sup>', html, re.DOTALL)
print(f"Prices found: {prices[:5]}")
