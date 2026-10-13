import re
import logging
from typing import List, Dict, Tuple, Optional
import httpx
from app.core.config import settings

logger = logging.getLogger("rigscouter.fallback")

async def query_tavily_search(query: str, max_results: int = 4) -> List[Dict]:
    """
    Executes search against Tavily across available API keys.
    """
    active_keys = settings.TAVILY_API_KEYS
    if not active_keys:
        return []

    for key in active_keys:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    "https://api.tavily.com/search",
                    json={"api_key": key, "query": query, "max_results": max_results}
                )
                if res.status_code == 200:
                    results = res.json().get("results", [])
                    if results:
                        return results
        except Exception as e:
            logger.debug(f"[Tavily Search Key Notice] {e}")
            continue

    return []

def parse_tavily_snippet_prices(txt: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Extracts current price and original/was price from search snippets.
    Supports formats like:
      - '$549.99'
      - 'Was $699.99' / 'Reg. $799.99' / 'Save $100'
      - '$549.99 (Save $150)'
    """
    if not txt:
        return None, None

    # Find all dollar amounts
    raw_matches = re.findall(r"\$\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?)", txt)
    prices: List[float] = []
    for m in raw_matches:
        try:
            val = float(m.replace(",", ""))
            if 5.0 <= val <= 9999.0:
                prices.append(val)
        except ValueError:
            pass

    if not prices:
        return None, None

    # Check for original/was price patterns
    orig_match = re.search(
        r"(?:was|reg(?:ular)?\.?|original(?:ly)?|list price)\s*:?\s*\$\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?)",
        txt,
        re.IGNORECASE
    )
    original_price: Optional[float] = None
    if orig_match:
        try:
            val = float(orig_match.group(1).replace(",", ""))
            if 5.0 <= val <= 9999.0:
                original_price = val
        except ValueError:
            pass

    # The current selling price is typically the lowest non-trivial price found
    current_price = min(prices)
    if original_price and original_price <= current_price:
        original_price = None

    return current_price, original_price

async def fetch_with_zenrows(url: str) -> Tuple[int, str]:
    """
    Fetches URL through ZenRows residential proxy when direct fetch is blocked.
    """
    api_key = settings.ZENROWS_API_KEY
    if not api_key:
        return 0, ""

    try:
        zenrows_url = "https://api.zenrows.com/v1/"
        params = {
            "apikey": api_key,
            "url": url,
            "premium_proxy": "true",
            "proxy_country": "us"
        }
        async with httpx.AsyncClient(timeout=25.0) as client:
            res = await client.get(zenrows_url, params=params)
            return res.status_code, res.text
    except Exception as e:
        logger.warning(f"[ZenRows Fetch Exception] {e}")
        return 0, ""
