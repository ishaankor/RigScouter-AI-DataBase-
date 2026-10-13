import logging
import uuid
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import httpx
from app.core.config import settings
from app.core.database import supabase

logger = logging.getLogger("rigscouter.services.notification")

class NotificationService:
    async def send_resend_email(
        self,
        to: str,
        subject: str,
        html: str,
        from_address: Optional[str] = None
    ) -> Optional[str]:
        """Sends an HTML email through Resend API. Returns email ID if successful."""
        api_key = settings.RESEND_API_KEY
        if not api_key:
            logger.warning("[NotificationService] No RESEND_API_KEY set, skipping email dispatch.")
            return None

        from_addr = from_address or f'"RigScouter AI" <{settings.RESEND_DOMAIN}>'

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"from": from_addr, "to": to, "subject": subject, "html": html}
                )
                if res.status_code in [200, 201]:
                    data = res.json()
                    email_id = data.get("id")
                    logger.info(f"✉️ [Email Sent] Successfully dispatched to {to} (ID: {email_id})")
                    return email_id
                else:
                    logger.error(f"[Resend API Error] ({res.status_code}): {res.text}")
        except Exception as e:
            logger.error(f"[Email Dispatch Exception] {to}: {e}")

        return None

    async def send_discord_webhook(self, webhook_url: str, payload: Dict[str, Any]) -> bool:
        """Sends a rich embedded notification to a Discord webhook."""
        if not webhook_url or not webhook_url.startswith("https://discord.com/api/webhooks/"):
            return False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(webhook_url, json=payload)
                if res.status_code in [200, 204]:
                    logger.info("🎮 [Discord Webhook Sent] Successfully posted notification.")
                    return True
                else:
                    logger.warning(f"[Discord Webhook Error] ({res.status_code}): {res.text}")
        except Exception as e:
            logger.error(f"[Discord Webhook Exception] {e}")

        return False

    async def record_digest_audit(
        self,
        user_id: str,
        headline: str,
        summary: str,
        report_data: Dict[str, Any],
        saved_opportunity: float = 0.0
    ) -> bool:
        """
        CRITICAL: Records the digest dispatch into `daily_digests` and updates `user_preferences`.
        This guarantees idempotency and prevents double-sends.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        digest_id = str(uuid.uuid4())

        # 1. Insert into daily_digests
        try:
            await asyncio.to_thread(
                supabase.table("daily_digests").insert({
                    "id": digest_id,
                    "user_id": user_id,
                    "headline": headline,
                    "executive_summary": summary,
                    "report_data": report_data,
                    "total_saved_opportunity": round(saved_opportunity, 2),
                    "generated_at": now_iso
                }).execute
            )
            logger.info(f"✅ [Digest Audit Recorded] user_id={user_id}, digest_id={digest_id}")
        except Exception as db_err:
            logger.error(f"❌ [Digest Audit Insert Failed] user_id={user_id}: {db_err}")
            return False

        # 2. Update user_preferences delivery_channels with last_sent_at
        try:
            pref_res = await asyncio.to_thread(
                supabase.table("user_preferences").select("delivery_channels").eq("user_id", user_id).limit(1).execute
            )
            existing_channels = {}
            if pref_res.data:
                raw_ch = pref_res.data[0].get("delivery_channels")
                if isinstance(raw_ch, str):
                    try:
                        import json
                        existing_channels = json.loads(raw_ch)
                    except Exception:
                        pass
                elif isinstance(raw_ch, dict):
                    existing_channels = raw_ch

            existing_channels["last_sent_at"] = now_iso

            await asyncio.to_thread(
                supabase.table("user_preferences").update({
                    "delivery_channels": existing_channels,
                    "updated_at": now_iso
                }).eq("user_id", user_id).execute
            )
            logger.info(f"✅ [User Preferences Updated] last_sent_at set to {now_iso} for user {user_id}")
            return True
        except Exception as pref_err:
            logger.error(f"❌ [User Preferences Update Failed] user_id={user_id}: {pref_err}")
            return False

notification_service = NotificationService()
