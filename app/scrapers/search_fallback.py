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
    Filters out discounts (e.g. 'Save $30', '$50 off') and distinguishes
    original MSRP from current selling prices.
    """
    if not txt:
        return None, None

    working_txt = txt

    # 1. Identify and strip discount/savings amounts, delivery minimums, monthly plans, gift cards, etc.
    discount_patterns = [
        r"(?:save|off|discount|instant rebate|rebate|coupon)\s*(?:up to\s*)?:?\s*\$\s*[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?",
        r"\$\s*[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?\s*(?:off|rebate|discount|coupon|instant savings)",
        r"(?:free\s+(?:delivery|shipping)|shipping\s+on|orders?\s+over)\s*(?:orders?\s*)?(?:of\s*)?\$\s*[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?",
        r"\$\s*[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?\s*(?:delivery|shipping|flat\s*rate)",
        r"(?:pay|as\s+low\s+as|starting\s+at)\s*\$\s*[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?\s*(?:/\s*mo|/\s*month|per\s+month|a\s+month)?",
        r"\$\s*[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?\s*(?:/\s*mo|/\s*month|per\s+month|a\s+month)",
        r"(?:gift\s*card|visa|rewards|statement\s+credit|trade[\s-]in).*?\$\s*[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?",
        r"\$\s*[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?\s*(?:gift\s*card|rewards|credit|back|trade[\s-]in)",
        r"(?:add[\s-]ons?|duster|air\s*duster|compressed\s*air|thermal\s*paste|protection\s*plan|tech\s*support|extended\s*warranty|geek\s*squad|in[\s-]store\s*only).*?\$\s*[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?",
        r"\$\s*[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?\s*(?:air\s*duster|compressed\s*air|thermal\s*paste|protection\s*plan|tech\s*support|extended\s*warranty|geek\s*squad)"
    ]
    for pat in discount_patterns:
        working_txt = re.sub(pat, " ", working_txt, flags=re.IGNORECASE)
    # Re-apply once in case multiple add-ons or promos were listed sequentially
    working_txt = re.sub(
        r"(?:duster|air\s*duster|compressed\s*air|thermal\s*paste|protection\s*plan|tech\s*support|free\s+delivery).*?\$\s*[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?",
        " ",
        working_txt,
        flags=re.IGNORECASE
    )

    # 2. Extract original / was / regular / MSRP price
    orig_pattern = r"(?:original(?:ly)?(?:\s+price)?|reg(?:ular)?(?:\s+price)?|list\s+price|msrp|previous(?:ly)?(?:\s+price)?|was(?:\s+the\s+price)?)\s*:?\s*\$\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?)"
    orig_match = re.search(
        orig_pattern,
        working_txt,
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

    # Strip the was/original price substring from working text
    working_txt = re.sub(
        orig_pattern,
        " ",
        working_txt,
        flags=re.IGNORECASE
    )

    # 3. Check for explicit active price indicators ('today $XX', 'now $XX', 'sale $XX', 'todays price $XX')
    active_match = re.search(
        r"(?:today(?:'s)?(?:\s+price)?|now|sale(?:\s+price)?|current(?:\s*price)?|our\s*price|special\s*price|deal(?:\s+price)?)\s*:?\s*\$\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?)",
        working_txt,
        re.IGNORECASE
    )
    if active_match:
        try:
            val = float(active_match.group(1).replace(",", ""))
            if 5.0 <= val <= 9999.0:
                return val, original_price
        except ValueError:
            pass

    # 4. Find all remaining dollar amounts
    raw_matches = re.findall(r"\$\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?)", working_txt)
    prices: List[float] = []
    for m in raw_matches:
        try:
            val = float(m.replace(",", ""))
            if 5.0 <= val <= 9999.0:
                prices.append(val)
        except ValueError:
            pass

    if not prices:
        return None, original_price

    # Filter out original_price from prices if present
    candidate_prices = [p for p in prices if original_price is None or abs(p - original_price) > 0.01]
    if candidate_prices:
        current_price = candidate_prices[0]
    else:
        current_price = prices[0]

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
