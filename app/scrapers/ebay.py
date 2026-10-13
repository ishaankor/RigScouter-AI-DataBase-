import base64
import time
import logging
from typing import List, Optional
import httpx
from app.core.config import settings
from app.models.offer import Offer, ProductAnalysis
from app.scrapers.base import BaseScraper
from app.scrapers.validator import validate_offer_fast

logger = logging.getLogger("rigscouter.scrapers.ebay")

class EbayScraper(BaseScraper):
    retailer_name = "eBay"

    def __init__(self):
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0

    async def _get_access_token(self) -> Optional[str]:
        client_id = settings.EBAY_CLIENT_ID
        client_secret = settings.EBAY_CLIENT_SECRET
        if not client_id or not client_secret:
            return None

        if self._access_token and time.time() < (self._token_expires_at - 60):
            return self._access_token

        credentials = f"{client_id}:{client_secret}"
        encoded_creds = base64.b64encode(credentials.encode()).decode()
        oauth_url = "https://api.ebay.com/identity/v1/oauth2/token"

        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    oauth_url,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Authorization": f"Basic {encoded_creds}"
                    },
                    data={
                        "grant_type": "client_credentials",
                        "scope": "https://api.ebay.com/oauth/api_scope"
                    },
                    timeout=10.0
                )
                if res.status_code == 200:
                    data = res.json()
                    self._access_token = data.get("access_token")
                    expires_in = data.get("expires_in", 7200)
                    self._token_expires_at = time.time() + expires_in
                    return self._access_token
        except Exception as e:
            logger.warning(f"[eBay OAuth Exception] {e}")
        return None

    async def search(
        self,
        query: str,
        analysis: Optional[ProductAnalysis] = None
    ) -> List[Offer]:
        token = await self._get_access_token()
        if not token:
            return []

        clean_query = query.replace('"', '').strip()
        browse_url = "https://api.ebay.com/buy/browse/v1/item_summary/search"

        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(
                    browse_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
                    },
                    params={
                        "q": clean_query,
                        "limit": "6",
                        "filter": "buyingOptions:{FIXED_PRICE}"
                    },
                    timeout=12.0
                )
                if res.status_code == 200:
                    items = res.json().get("itemSummaries", [])
                    offers: List[Offer] = []
                    for it in items:
                        title = it.get("title", "")
                        price_obj = it.get("price", {})
                        price_val = float(price_obj.get("value", 0)) if price_obj.get("value") else 0
                        if price_val <= 0:
                            continue

                        condition = it.get("condition", "New")
                        if any(w in condition.lower() for w in ["parts", "not working", "broken"]):
                            continue

                        is_valid, _ = validate_offer_fast(title, price_val, analysis)
                        if is_valid:
                            item_url = it.get("itemWebUrl") or f"https://www.ebay.com/itm/{it.get('itemId')}"
                            img = (it.get("image") or {}).get("imageUrl")
                            offers.append(Offer(
                                retailer=self.retailer_name,
                                price=price_val,
                                url=item_url,
                                image_url=img,
                                title=title,
                                condition=condition,
                                in_stock=True
                            ))
                            if len(offers) >= 2:
                                break
                    return offers
        except Exception as e:
            logger.debug(f"[eBay Search Exception] {e}")

        return []

    async def get_price(self, url: str) -> Optional[Offer]:
        # Item lookup via browse API if itemId present
        return None
