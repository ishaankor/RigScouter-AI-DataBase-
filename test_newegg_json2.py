import requests
import re
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}
url = "https://www.newegg.com/intel-core-ultra-7-270k-plus-core-ultra-7-series-2-arrow-lake-refresh-lga-1851-desktop-cpu-processor/p/N82E16819118628"
res = requests.get(url, headers=headers)

# Find context around the main buy box
matches = re.findall(r'(.{0,100}class="price-current".{0,100})', res.text)
for m in matches[:5]:
    print(m)
