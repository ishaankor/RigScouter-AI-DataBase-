import os
import requests
from dotenv import load_dotenv

load_dotenv()

TAVILY_API_KEY = "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"

def investigate_tavily():
    print("Testing Tavily Search for Newegg GTX 1070...")
    res = requests.post('https://api.tavily.com/search', json={
        "api_key": TAVILY_API_KEY,
        "query": "buy GTX 1070 price",
        "search_depth": "advanced",
        "include_domains": ["newegg.com"],
        "include_raw_content": True,
        "max_results": 1
    }, timeout=15)
    
    if res.ok:
        data = res.json()
        results = data.get('results', [])
        if results:
            hit = results[0]
            raw_content = hit.get('raw_content', '')
            print(f"URL: {hit.get('url')}")
            print(f"Raw content length: {len(raw_content)}")
            print("First 1000 characters of raw_content:")
            print("---")
            print(raw_content[:1000])
            print("---")
            
            with open("scratch.html", "w") as f:
                f.write(raw_content)
            print("Saved full raw_content to scratch.html")
    else:
        print("Tavily search failed", res.text)

if __name__ == "__main__":
    investigate_tavily()
