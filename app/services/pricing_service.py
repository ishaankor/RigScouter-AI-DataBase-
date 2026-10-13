import re
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from app.core.database import supabase
from app.models.offer import Offer, PriceSnapshot
from app.services.sse_service import sse_service

logger = logging.getLogger("rigscouter.services.pricing")

class PricingService:
    async def record_snapshot(self, snapshot: PriceSnapshot):
        """
        Persists price snapshot across `components`, `retailer_products`, and `price_snapshots`.
        """
        if not snapshot.retailer_product_id or snapshot.price <= 0:
            return

        now_iso = snapshot.recorded_at or datetime.now(timezone.utc).isoformat()
        clean_comp_id = snapshot.component_id or snapshot.retailer_product_id.split("-")[0]

        # 1. Upsert components table
        try:
            canonical = snapshot.title or f"{snapshot.brand or 'Hardware'} {snapshot.model or clean_comp_id}"
            comp_payload = {
                "id": clean_comp_id,
                "category": snapshot.category or "Other",
                "brand": snapshot.brand or "Hardware",
                "model": snapshot.model or clean_comp_id,
                "canonical_name": canonical[:255]
            }
            await asyncio.to_thread(
                supabase.table("components").upsert(comp_payload).execute
            )
        except Exception as e:
            logger.debug(f"[Components Upsert Notice] {e}")

        # 2. Upsert retailer_products table
        try:
            rp_payload = {
                "id": snapshot.retailer_product_id,
                "component_id": clean_comp_id,
                "retailer": snapshot.retailer,
                "title": (snapshot.title or snapshot.retailer_product_id)[:255],
                "product_url": snapshot.product_url or "#",
                "image_url": snapshot.image_url
            }
            await asyncio.to_thread(
                supabase.table("retailer_products").upsert(rp_payload).execute
            )
        except Exception as e:
            logger.debug(f"[RetailerProducts Upsert Notice] {e}")

        # 3. Insert into price_snapshots
        try:
            snap_payload = {
                "retailer_product_id": snapshot.retailer_product_id,
                "price": round(float(snapshot.price), 2),
                "shipping": round(float(snapshot.shipping or 0.0), 2),
                "total_price": round(float(snapshot.price) + float(snapshot.shipping or 0.0), 2),
                "currency": "USD",
                "availability": "in_stock" if snapshot.in_stock else "out_of_stock",
                "condition": snapshot.condition or "new",
                "scraped_at": now_iso
            }
            await asyncio.to_thread(
                supabase.table("price_snapshots").insert(snap_payload).execute
            )
            logger.info(f"📸 [Snapshot Recorded] {snapshot.retailer_product_id}: ${snapshot.price:.2f} ({snapshot.retailer})")
        except Exception as e:
            logger.warning(f"⚠️ [Snapshot Insert Error] {snapshot.retailer_product_id}: {e}")

    async def update_component_and_watchlist(
        self,
        comp_id: str,
        comp_name: str,
        offers: List[Offer]
    ) -> Dict[str, Any]:
        """
        Updates hardware_components and all corresponding watchlist_items with new pricing,
        calculating all-time-low (ATL) and 24h, 7d, 30d history.
        """
        if not offers:
            return {"updated": False}

        best = offers[0]
        new_price = float(best.price)
        now_iso = datetime.now(timezone.utc).isoformat()

        # Fetch existing record for baseline ATL and price history
        res = await asyncio.to_thread(
            supabase.table("hardware_components").select("*").eq("id", comp_id).limit(1).execute
        )
        existing = res.data[0] if res.data else {}

        old_price = float(existing.get("current_price") or 0.0)
        old_atl = float(existing.get("lowest_price_90d") or old_price or new_price)
        new_atl = min(old_atl if old_atl > 0 else new_price, new_price)

        # Merge price history
        raw_specs = existing.get("specs") or {}
        specs_dict = json.loads(raw_specs) if isinstance(raw_specs, str) else raw_specs
        history = list(specs_dict.get("price_history") or [])
        history.append({"price": new_price, "timestamp": now_iso})
        if len(history) > 60:
            history = history[-60:]

        specs_dict["RetailerOffers"] = [o.dict() for o in offers]
        specs_dict["price_history"] = history

        # Save hardware component
        hw_payload = {
            "current_price": new_price,
            "lowest_price_90d": new_atl,
            "retailer": best.retailer,
            "product_url": best.url,
            "image_url": best.image_url or existing.get("image_url"),
            "specs": json.dumps(specs_dict),
            "updated_at": now_iso
        }
        await asyncio.to_thread(
            supabase.table("hardware_components").update(hw_payload).eq("id", comp_id).execute
        )

        # Record snapshots
        for off in offers:
            ret_slug = re.sub(r'[^a-zA-Z0-9]+', '-', off.retailer.lower()).strip('-')
            row_id = f"{comp_id}-{ret_slug}"
            await self.record_snapshot(PriceSnapshot(
                retailer_product_id=row_id,
                price=off.price,
                component_id=comp_id,
                retailer=off.retailer,
                title=off.title or comp_name,
                product_url=off.url,
                image_url=off.image_url,
                in_stock=off.in_stock,
                condition=off.condition
            ))

        # Update watchlist items
        wl_updates: Dict[str, Any] = {"all_time_low": new_atl}
        if old_price > 0:
            wl_updates["previous_price_24h"] = old_price

        base_comp_id = re.sub(r'-(amazon|ebay|micro-center|microcenter|newegg|best-buy|bestbuy|bh|b-h)$', '', comp_id.lower())
        filter_clause = f"component_id.eq.{comp_id},component_id.eq.{base_comp_id}"

        await asyncio.to_thread(
            supabase.table("watchlist_items").update(wl_updates).or_(filter_clause).execute
        )

        # Broadcast real-time SSE event if price changed
        price_changed = abs(new_price - old_price) >= 0.01
        if price_changed:
            sse_service.broadcast("price_update", {
                "componentId": comp_id,
                "componentName": comp_name,
                "oldPrice": old_price,
                "newPrice": new_price,
                "retailer": best.retailer,
                "timestamp": now_iso
            })

        return {
            "updated": True,
            "comp_id": comp_id,
            "old_price": old_price,
            "new_price": new_price,
            "price_changed": price_changed,
            "best_retailer": best.retailer
        }

pricing_service = PricingService()
