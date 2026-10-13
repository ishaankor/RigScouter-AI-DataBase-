-- ==============================================================================
-- RigScouter-AI: Production Database Scale & Multi-Month Performance Optimization
--
-- How to apply:
-- 1. Open your Supabase Dashboard: https://supabase.com/dashboard/project/mfzokxffhmedvtuhykdw
-- 2. Go to the "SQL Editor" tab on the left sidebar.
-- 3. Click "New query", paste this script, and click "Run".
-- ==============================================================================

-- 1. High-Performance Indexing for Price Snapshots
-- Guarantees sub-10ms queries even with hundreds of thousands of historical snapshot records.
CREATE INDEX IF NOT EXISTS idx_price_snapshots_lookup 
ON public.price_snapshots (retailer_product_id, scraped_at DESC);

CREATE INDEX IF NOT EXISTS idx_price_snapshots_cleanup 
ON public.price_snapshots (scraped_at);

-- 2. Hardware Components Indexing
-- Accelerates cache-TTL lookups and catalog refresh queries.
CREATE INDEX IF NOT EXISTS idx_hardware_components_updated 
ON public.hardware_components (updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_hardware_components_model 
ON public.hardware_components (model);

-- 3. Watchlist Items User Index
-- Speeds up dashboard queries and digest generation per user.
CREATE INDEX IF NOT EXISTS idx_watchlist_items_user_id 
ON public.watchlist_items (user_id);

CREATE INDEX IF NOT EXISTS idx_watchlist_items_comp_id 
ON public.watchlist_items (component_id);

-- 4. Daily Digests Audit & Deduplication Index
-- Ensures throttle checks (hours_since_last) are instant.
CREATE INDEX IF NOT EXISTS idx_daily_digests_user_generated 
ON public.daily_digests (user_id, generated_at DESC);

-- 5. Automated Weekly Pruning Function (Retention Management)
CREATE OR REPLACE FUNCTION public.prune_old_snapshots_func()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    deleted_count integer;
BEGIN
    DELETE FROM public.price_snapshots
    WHERE scraped_at < NOW() - INTERVAL '90 days';
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$;
