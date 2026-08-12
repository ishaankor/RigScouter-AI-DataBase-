import requests
from bs4 import BeautifulSoup

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
}
url = "https://www.newegg.com/asus-dual-rtx2060-o6g-evo-geforce-rtx-2060-6gb-graphics-card-double-fans/p/N82E16814126349"
res = requests.get(url, headers=headers)
soup = BeautifulSoup(res.text, 'lxml')

print(f"Title: {soup.select_one('.product-title').text if soup.select_one('.product-title') else 'No title'}")

for p in soup.select('.price-current'):
    print(f"Price current: {p.text.strip()} | parent classes: {p.parent.get('class', [])}")
    
# Let's search for 484 to see what class it has
import re
for m in re.findall(r'<div[^>]*>.*?484.*?</div>', res.text)[:5]:
    pass # this might be too noisy
