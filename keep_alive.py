#!/usr/bin/env python3
"""
Supabase Keep-Alive CLI & Background Service
============================================
Prevents Supabase projects on the free tier from pausing due to inactivity
by issuing authenticated queries to the database and/or invoking the Edge Function.

Usage:
  python keep_alive.py --once             # Execute an immediate single ping
  python keep_alive.py --test             # Run a diagnostic test on DB & Edge Function
  python keep_alive.py --daemon           # Run continuously in background (default every 24h)
  python keep_alive.py --interval 43200   # Run loop every 12 hours
"""

import os
import sys
import time
import argparse
import logging
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv

load_dotenv()

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("SupabaseKeepAlive")

# Supabase Credentials
SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or "https://mfzokxffhmedvtuhykdw.supabase.co"
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY") or os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY") or ""

def get_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }

def ping_database_rest() -> dict:
    """Executes a direct PostgREST select query to generate DB activity."""
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/hardware_components?select=id,name,updated_at&limit=3"
    start = time.perf_counter()
    resp = requests.get(url, headers=get_headers(), timeout=15)
    latency_ms = round((time.perf_counter() - start) * 1000, 2)

    resp.raise_for_status()
    data = resp.json()
    return {
        "status": "success",
        "type": "database_rest_query",
        "records_retrieved": len(data),
        "latency_ms": latency_ms,
        "sample": data[0].get("name") if data else None
    }

def ping_edge_function() -> dict:
    """Invokes the Supabase Edge Function keep-alive endpoint."""
    url = f"{SUPABASE_URL.rstrip('/')}/functions/v1/keep-alive"
    start = time.perf_counter()
    try:
        resp = requests.get(url, headers=get_headers(), timeout=15)
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        if resp.status_code == 200:
            return {
                "status": "success",
                "type": "edge_function",
                "latency_ms": latency_ms,
                "data": resp.json()
            }
        else:
            return {
                "status": "warning",
                "type": "edge_function",
                "code": resp.status_code,
                "latency_ms": latency_ms,
                "error": resp.text[:200]
            }
    except Exception as e:
        return {
            "status": "error",
            "type": "edge_function",
            "error": str(e)
        }

def run_keep_alive_cycle():
    """Runs a complete keep-alive cycle hitting both DB and Edge Function."""
    logger.info("=" * 60)
    logger.info("🚀 Initiating Supabase Keep-Alive Heartbeat")
    logger.info(f"Target Project: {SUPABASE_URL}")
    
    # 1. Database REST Query
    try:
        db_res = ping_database_rest()
        logger.info(f"✅ DB PostgREST Query: OK ({db_res['records_retrieved']} items found in {db_res['latency_ms']}ms)")
        if db_res.get("sample"):
            logger.info(f"   Sample Item: \"{db_res['sample']}\"")
    except Exception as e:
        logger.error(f"❌ DB Query Failed: {e}")

    # 2. Edge Function Invocation
    ef_res = ping_edge_function()
    if ef_res["status"] == "success":
        logger.info(f"✅ Edge Function: OK (Executed in {ef_res['latency_ms']}ms)")
    elif ef_res["status"] == "warning" and ef_res.get("code") == 404:
        logger.warning("⚠️  Edge Function: 404 Not Found (Edge Function not deployed yet; direct DB query kept project active)")
    else:
        logger.info(f"ℹ️  Edge Function response: {ef_res.get('error') or ef_res.get('code')}")

    logger.info(f"Heartbeat timestamp: {datetime.now(timezone.utc).isoformat()}")
    logger.info("=" * 60)

def main():
    parser = argparse.ArgumentParser(description="Supabase Keep-Alive Utility")
    parser.add_argument("--once", action="store_true", help="Execute single keep-alive ping and exit")
    parser.add_argument("--test", action="store_true", help="Run diagnostic health check")
    parser.add_argument("--daemon", action="store_true", help="Run continuously in background")
    parser.add_argument("--interval", type=int, default=86400, help="Interval in seconds for daemon mode (default: 86400 = 24h)")

    args = parser.parse_args()

    if not SUPABASE_KEY:
        logger.error("Error: SUPABASE_ANON_KEY or NEXT_PUBLIC_SUPABASE_ANON_KEY not configured in .env")
        sys.exit(1)

    if args.test or args.once:
        run_keep_alive_cycle()
        sys.exit(0)

    # Daemon mode (default if no flag passed)
    interval_hours = round(args.interval / 3600, 1)
    logger.info(f"Starting Supabase Keep-Alive Daemon (Pinging every {args.interval}s / {interval_hours}h)...")
    
    # Run once immediately on start
    run_keep_alive_cycle()

    while True:
        try:
            time.sleep(args.interval)
            run_keep_alive_cycle()
        except KeyboardInterrupt:
            logger.info("Keep-Alive Daemon stopped by user.")
            break
        except Exception as e:
            logger.error(f"Unexpected error in daemon loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
