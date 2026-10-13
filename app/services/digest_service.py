import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import httpx
from app.core.config import settings
from app.core.database import supabase
from app.services.notification_service import notification_service

logger = logging.getLogger("rigscouter.services.digest")

class DigestService:
    async def generate_ai_summary(self, watchlist: List[Dict[str, Any]], frequency: str = "daily") -> Dict[str, Any]:
        """
        Uses Groq LLM to analyze hardware price movements and generate a clean briefing.
        """
        if not watchlist:
            return {
                "headline": "Hardware Watchlist Briefing",
                "executiveSummary": "All monitored PC hardware components are currently stable at standard pricing.",
                "totalSavedOpportunity": 0.0,
                "items": []
            }

        # Calculate metrics
        drops = []
        total_savings = 0.0
        for item in watchlist:
            curr = float(item.get("current_price") or 0.0)
            prev24 = float(item.get("previous_price_24h") or curr)
            atl = float(item.get("all_time_low") or curr)
            target = float(item.get("target_price") or 0.0)

            drop_amount = max(0.0, prev24 - curr)
            if drop_amount > 0:
                total_savings += drop_amount
                drops.append({
                    "name": item.get("component_name"),
                    "current": curr,
                    "previous": prev24,
                    "drop": drop_amount,
                    "retailer": item.get("retailer", "Amazon"),
                    "is_atl": curr <= atl,
                    "target_met": target > 0 and curr <= target
                })

        # LLM prompt
        system_prompt = (
            "You are RigScouter AI's chief hardware pricing analyst. "
            "Write a concise, high-impact headline and 2-sentence executive summary for the user's daily hardware briefing. "
            "Highlight the biggest price drops or all-time lows. Output valid JSON only: "
            "{\"headline\": \"...\", \"executive_summary\": \"...\"}"
        )
        user_content = f"Frequency: {frequency}\nItems with drops: {json.dumps(drops[:5])}\nTotal savings opportunity: ${total_savings:.2f}"

        headline = f"⚡ RigScouter AI {frequency.upper()} BRIEFING"
        if drops:
            top_drop = max(drops, key=lambda x: x["drop"])
            headline = f"📉 {top_drop['name'][:30]} dropped ${top_drop['drop']:.2f} to ${top_drop['current']:.2f} • ⚡ LIVE BRIEFING"
        summary = f"Identified ${total_savings:.2f} in active savings opportunities across your monitored components." if total_savings > 0 else "All monitored hardware components are at stable market pricing."

        if settings.GROQ_API_KEY:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
                        json={
                            "model": settings.GROQ_MODEL,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_content}
                            ],
                            "temperature": 0.2,
                            "response_format": {"type": "json_object"}
                        }
                    )
                    if res.status_code == 200:
                        data = json.loads(res.json()["choices"][0]["message"]["content"])
                        if data.get("headline"):
                            headline = data["headline"]
                        if data.get("executive_summary"):
                            summary = data["executive_summary"]
            except Exception as e:
                logger.warning(f"[Groq Digest Summary Warning] {e}")

        return {
            "headline": headline,
            "executiveSummary": summary,
            "totalSavedOpportunity": total_savings,
            "items": watchlist
        }

    def build_email_html(self, report: Dict[str, Any], date_str: str) -> str:
        """Constructs a responsive, light/dark compatible email digest."""
        headline = report.get("headline", "Daily Hardware Digest")
        summary = report.get("executiveSummary", "")
        items = report.get("items", [])
        total_saved = report.get("totalSavedOpportunity", 0.0)

        item_rows = ""
        for it in items[:6]:
            name = it.get("component_name", "Component")
            price = float(it.get("current_price") or 0.0)
            prev24 = float(it.get("previous_price_24h") or price)
            ret = it.get("retailer", "Store")
            url = it.get("product_url", "https://rigscouter.ishaankoradia.com/dashboard/watchlist")
            drop = prev24 - price

            drop_badge = f'<span style="color: #16a34a; font-weight: bold; font-size: 13px;">-${drop:.2f}</span>' if drop > 0 else '<span style="color: #64748b; font-size: 13px;">Stable</span>'

            item_rows += f"""
            <tr style="border-bottom: 1px solid #f1f5f9;">
              <td style="padding: 14px 12px;">
                <div style="font-weight: 700; color: #0f172a; font-size: 14px;">{name}</div>
                <div style="font-size: 12px; color: #64748b; margin-top: 2px;">Retailer: <strong>{ret}</strong></div>
              </td>
              <td style="padding: 14px 12px; text-align: right;">
                <div style="font-weight: 800; color: #0f172a; font-size: 16px;">${price:.2f}</div>
                <div>{drop_badge}</div>
              </td>
              <td style="padding: 14px 12px; text-align: right;">
                <a href="{url}" style="background-color: #0284c7; color: #ffffff; text-decoration: none; padding: 6px 12px; border-radius: 6px; font-size: 12px; font-weight: 600; display: inline-block;">View Deal</a>
              </td>
            </tr>
            """

        return f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"><title>{headline}</title></head>
        <body style="background-color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 24px 0; color: #0f172a;">
          <table width="100%" cellpadding="0" cellspacing="0" style="max-width: 620px; margin: 0 auto; background: #ffffff; border-radius: 12px; border: 1px solid #e2e8f0; overflow: hidden;">
            <tr>
              <td style="background-color: #0f172a; padding: 24px; text-align: center;">
                <div style="color: #38bdf8; font-size: 20px; font-weight: 900; letter-spacing: -0.5px;">⚡ RigScouter AI</div>
                <div style="color: #94a3b8; font-size: 12px; margin-top: 4px; text-transform: uppercase; letter-spacing: 1px;">Hardware Price Intelligence • {date_str}</div>
              </td>
            </tr>
            <tr>
              <td style="padding: 24px;">
                <h2 style="font-size: 18px; color: #0f172a; margin-top: 0;">{headline}</h2>
                <p style="color: #475569; font-size: 14px; line-height: 1.5; margin-bottom: 20px;">{summary}</p>
                {f'<div style="background-color: #ecfdf5; border-left: 4px solid #10b981; padding: 12px 16px; border-radius: 4px; margin-bottom: 24px; color: #065f46; font-size: 14px; font-weight: 600;">💰 Total Saved Opportunity: ${total_saved:.2f}</div>' if total_saved > 0 else ''}
                <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse: collapse; margin-top: 12px;">
                  <thead>
                    <tr style="background-color: #f8fafc; border-bottom: 2px solid #e2e8f0;">
                      <th style="padding: 10px 12px; text-align: left; font-size: 12px; color: #64748b; text-transform: uppercase;">Component</th>
                      <th style="padding: 10px 12px; text-align: right; font-size: 12px; color: #64748b; text-transform: uppercase;">Price</th>
                      <th style="padding: 10px 12px; text-align: right; font-size: 12px; color: #64748b; text-transform: uppercase;">Action</th>
                    </tr>
                  </thead>
                  <tbody>{item_rows}</tbody>
                </table>
              </td>
            </tr>
            <tr>
              <td style="background-color: #f8fafc; padding: 16px 24px; text-align: center; border-top: 1px solid #e2e8f0; font-size: 12px; color: #94a3b8;">
                You received this email because you are subscribed to RigScouter AI hardware price alerts.<br>
                <a href="https://rigscouter.ishaankoradia.com/dashboard/settings" style="color: #0284c7; text-decoration: none;">Manage Notification Preferences</a>
              </td>
            </tr>
          </table>
        </body>
        </html>
        """

    async def dispatch_all_subscribers(self, force: bool = False) -> Dict[str, Any]:
        """
        Core idempotent dispatch job for all eligible users.
        Guarantees strict 20-hour throttle to prevent duplicate daily emails.
        """
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%b %d, %Y")

        # 1. Fetch subscribed users
        res = await asyncio.to_thread(
            supabase.table("user_preferences").select("*").neq("summary_frequency", "none").execute
        )
        preferences = res.data or []
        if not preferences:
            return {"status": "no_subscribers", "dispatched": 0}

        dispatched = 0
        skipped = []

        for pref in preferences:
            user_id = pref.get("user_id")
            freq = pref.get("summary_frequency") or "daily"

            # Parse delivery channels
            raw_channels = pref.get("delivery_channels") or {}
            channels = json.loads(raw_channels) if isinstance(raw_channels, str) else raw_channels
            target_email = channels.get("emailAddress")
            if not target_email or channels.get("email") is False:
                continue

            # 2. Strict Idempotency & Interval Check
            if not force:
                last_sent_iso = channels.get("last_sent_at")
                hours_since = 999.0
                if last_sent_iso:
                    try:
                        last_sent_dt = datetime.fromisoformat(last_sent_iso.replace("Z", "+00:00"))
                        hours_since = (now - last_sent_dt).total_seconds() / 3600.0
                    except Exception:
                        pass

                threshold = 20.0 if freq == "daily" else (68.0 if freq == "every_3_days" else 160.0)
                if hours_since < threshold:
                    logger.info(f"⏭️ [Throttle Skipped] User {user_id} ({target_email}) sent {hours_since:.1f}h ago (threshold: {threshold}h)")
                    skipped.append({"user_id": user_id, "reason": f"Throttled ({hours_since:.1f}h < {threshold}h)"})
                    continue

            # 3. Fetch user's watchlist
            wl_res = await asyncio.to_thread(
                supabase.table("watchlist_items").select("*").eq("user_id", user_id).execute
            )
            watchlist = wl_res.data or []
            if not watchlist:
                continue

            # 4. Generate AI briefing
            report = await self.generate_ai_summary(watchlist, frequency=freq)
            html = self.build_email_html(report, today_str)

            # 5. Send email
            email_id = await notification_service.send_resend_email(
                to=target_email,
                subject=report["headline"],
                html=html
            )

            # 6. Record audit & update last_sent_at
            if email_id:
                await notification_service.record_digest_audit(
                    user_id=user_id,
                    headline=report["headline"],
                    summary=report["executiveSummary"],
                    report_data=report,
                    saved_opportunity=report["totalSavedOpportunity"]
                )
                dispatched += 1

        return {
            "status": "completed",
            "dispatched": dispatched,
            "skipped_count": len(skipped),
            "skipped": skipped
        }

digest_service = DigestService()
