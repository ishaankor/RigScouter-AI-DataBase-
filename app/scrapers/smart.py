import json
import logging
import re
from typing import List, Dict, Optional, Any
import httpx
from app.core.config import settings
from app.models.offer import Offer

logger = logging.getLogger("rigscouter.scrapers.smart")

SMART_PARSER_SYSTEM_PROMPT = (
    "You are an expert PC hardware procurement analyst and data extraction specialist. "
    "Your job is to read raw web snippet text, search result snippets, or product text from hardware retailers "
    "(such as Newegg, Micro Center, Best Buy, Amazon, eBay, B&H) and accurately extract structured product offers.\n\n"
    "CRITICAL RULES:\n"
    "1. ACTIVE PRICE: Extract the actual current checkout price in USD (e.g. 219.99). "
    "Do NOT confuse 'Save $50', '$50 off', 'Instant Rebate $30', or 'Was $299' with the current price! "
    "Do NOT pick up prices of recommended items, carousel products (e.g. 'Customers also viewed $559'), "
    "or add-on accessories (e.g. 'Compressed Air Duster $29.99', 'Ultra Duster $9.99', 'Thermal Paste', 'Protection Plan'). "
    "Extract ONLY the price of the standalone component itself.\n"
    "2. ORIGINAL PRICE: If there is a clear previous/original price ('Was $XXX', 'Regular Price $XXX', 'List Price $XXX'), "
    "extract it; otherwise null.\n"
    "3. STOCK STATUS: Determine if the product is in stock (true) or out of stock (false). "
    "Words like '0 in stock', 'sold out', 'out of stock', 'currently unavailable', 'backorder', 'not available online' mean in_stock: false.\n"
    "4. STANDALONE COMPONENT MATCH: The user is looking for a standalone PC rig part. "
    "If the item is a full desktop PC prebuilt, laptop, motherboard bundle, upgrade kit, or accessory (air duster, sticker, empty box, toy, poster, cable), "
    "set is_standalone_match: false. If it is the genuine standalone hardware component, set is_standalone_match: true.\n"
    "5. CONDITION: One of: 'new', 'open_box', 'refurbished', 'used'. Default is 'new'.\n"
    "6. CLEAN TITLE: Extract the accurate, clean product title without retailer branding noise (e.g. strip '| Newegg.com', 'Best Buy:').\n\n"
    "Return JSON format:\n"
    "{\n"
    "  \"offers\": [\n"
    "    {\n"
    "      \"title\": string,\n"
    "      \"price\": number or null,\n"
    "      \"original_price\": number or null,\n"
    "      \"in_stock\": boolean,\n"
    "      \"condition\": string,\n"
    "      \"is_standalone_match\": boolean,\n"
    "      \"rejection_reason\": string or null\n"
    "    }\n"
    "  ]\n"
    "}"
)


async def parse_result_text(
    text: str,
    retailer_name: str,
    url: str,
    query: str,
    category: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Smart LLM-powered extraction from retailer snippets, titles, or page contents.
    Converts unstructured web snippet text into accurate structured offers without fragile regexes.
    """
    if not text or not text.strip():
        return []

    groq_key = settings.GROQ_API_KEY
    if not groq_key:
        return _fallback_heuristic_parse(text, retailer_name, url, query)

    user_prompt = (
        f"Retailer: {retailer_name}\n"
        f"Target Search Query: \"{query}\"\n"
        f"Category: {category or 'PC Hardware'}\n"
        f"Page URL: {url}\n\n"
        f"Raw Text Content:\n\"\"\"\n{text.strip()[:2000]}\n\"\"\"\n\n"
        "Extract the product offer if it matches the target search query. Return JSON."
    )

    models_to_try = [
        settings.GROQ_MODEL,
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant"
    ]

    for model_name in models_to_try:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {groq_key}"},
                    json={
                        "model": model_name,
                        "messages": [
                            {"role": "system", "content": SMART_PARSER_SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt}
                        ],
                        "temperature": 0.0,
                        "response_format": {"type": "json_object"}
                    }
                )
                if res.status_code == 200:
                    data = res.json()
                    content = data["choices"][0]["message"]["content"]
                    parsed = json.loads(content)
                    raw_offers = parsed.get("offers", [])

                    valid_listings = []
                    for o in raw_offers:
                        price = o.get("price")
                        if price is not None:
                            try:
                                price_val = float(price)
                            except (ValueError, TypeError):
                                continue

                            if price_val > 5.0 and o.get("is_standalone_match", True):
                                orig_val = None
                                if o.get("original_price"):
                                    try:
                                        orig_val = float(o["original_price"])
                                    except (ValueError, TypeError):
                                        pass

                                valid_listings.append({
                                    "title": o.get("title") or query,
                                    "price": price_val,
                                    "original_price": orig_val,
                                    "in_stock": bool(o.get("in_stock", True)),
                                    "condition": str(o.get("condition", "new")).lower(),
                                    "url": url,
                                    "retailer": retailer_name,
                                    "source": f"{retailer_name.lower().replace(' ', '')}-smart"
                                })

                    if valid_listings:
                        return valid_listings
                    elif raw_offers:
                        # LLM determined this was not a standalone match or invalid
                        for o in raw_offers:
                            reason = o.get("rejection_reason") or "Not a standalone component match"
                            logger.info(f"[{retailer_name} Smart Filtered] '{o.get('title', '')[:50]}': {reason}")
                        return []
        except Exception as e:
            logger.debug(f"[Smart Parser Model Notice ({model_name})] {e}")
            continue

    # Graceful fallback to regex heuristics if LLM endpoint fails
    return _fallback_heuristic_parse(text, retailer_name, url, query)


def _fallback_heuristic_parse(
    text: str,
    retailer_name: str,
    url: str,
    query: str
) -> List[Dict[str, Any]]:
    """Deterministic fallback in case of network or API outage."""
    from app.scrapers.search_fallback import parse_tavily_snippet_prices
    price, orig = parse_tavily_snippet_prices(text)
    if not price or price <= 5.0:
        return []

    t_lower = text.lower()
    is_sold_out = any(
        phrase in t_lower for phrase in [
            "0 in stock", "sold out", "out of stock", "currently unavailable",
            "not in stock", "backorder", "pick up in store only - 0"
        ]
    )

    is_refurb = "refurbished" in t_lower
    is_open_box = any(w in t_lower for w in ["open-box", "open box"])
    cond = "refurbished" if is_refurb else ("open_box" if is_open_box else "new")

    first_line = text.split("\n")[0].strip()
    clean_title = re.sub(r'\s*[|\-]\s*(?:Newegg\.com|Best Buy|Micro Center).*$', '', first_line, flags=re.I).strip()

    return [{
        "title": clean_title or query,
        "price": price,
        "original_price": orig,
        "in_stock": not is_sold_out,
        "condition": cond,
        "url": url,
        "retailer": retailer_name,
        "source": f"{retailer_name.lower().replace(' ', '')}-fallback"
    }]
