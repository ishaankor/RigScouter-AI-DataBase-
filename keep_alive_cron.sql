-- ==============================================================================
-- Supabase Inactivity Keep-Alive Setup: pg_cron + pg_net In-Database Scheduler
--
-- How to apply:
-- 1. Open your Supabase Dashboard: https://supabase.com/dashboard/project/mfzokxffhmedvtuhykdw
-- 2. Go to the "SQL Editor" tab on the left sidebar.
-- 3. Click "New query", paste this entire script, and click "Run".
-- ==============================================================================

-- 1. Enable pg_cron (Database Cron Scheduler) and pg_net (Async Network Requests)
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net;

-- Grant usage on cron schema to postgres
GRANT USAGE ON SCHEMA cron TO postgres;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA cron TO postgres;

-- 2. Create a lightweight heartbeat tracking table
CREATE TABLE IF NOT EXISTS public._supabase_heartbeats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL DEFAULT 'pg_cron',
    recorded_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- Enable RLS and public policies for heartbeat table
ALTER TABLE public._supabase_heartbeats ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE tablename = '_supabase_heartbeats' AND policyname = 'Allow public read of heartbeats'
    ) THEN
        CREATE POLICY "Allow public read of heartbeats" ON public._supabase_heartbeats FOR SELECT USING (true);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE tablename = '_supabase_heartbeats' AND policyname = 'Allow service insert of heartbeats'
    ) THEN
        CREATE POLICY "Allow service insert of heartbeats" ON public._supabase_heartbeats FOR INSERT WITH CHECK (true);
    END IF;
END $$;

-- 3. Stored Procedure to perform database heartbeat
CREATE OR REPLACE FUNCTION public.execute_supabase_heartbeat(caller_source text DEFAULT 'pg_cron')
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    component_cnt integer;
    result jsonb;
BEGIN
    -- A. Insert heartbeat entry (guarantees DB write activity)
    INSERT INTO public._supabase_heartbeats (source, recorded_at)
    VALUES (caller_source, timezone('utc'::text, now()));

    -- B. Retain only last 50 heartbeats to keep table lightweight
    DELETE FROM public._supabase_heartbeats
    WHERE id NOT IN (
        SELECT id FROM public._supabase_heartbeats
        ORDER BY recorded_at DESC
        LIMIT 50
    );

    -- C. Perform a read query across components (guarantees DB read activity)
    SELECT count(*) INTO component_cnt FROM public.hardware_components;

    result := jsonb_build_object(
        'status', 'alive',
        'timestamp', timezone('utc'::text, now()),
        'source', caller_source,
        'component_count', component_cnt
    );

    RETURN result;
END;
$$;

-- 4. Unschedule any previous keep-alive cron jobs if they exist
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN (SELECT jobid FROM cron.job WHERE jobname IN ('keep_alive_direct_sql', 'keep_alive_edge_function')) LOOP
        PERFORM cron.unschedule(r.jobid);
    END LOOP;
END $$;

-- 5. SCHEDULE METHOD A: Direct PostgreSQL SQL Cron (Runs every 2 days at 04:00 UTC)
-- This runs directly inside the PostgreSQL engine without any external network dependency.
SELECT cron.schedule(
    'keep_alive_direct_sql',
    '0 4 */2 * *',
    $$ SELECT public.execute_supabase_heartbeat('pg_cron_direct_sql'); $$
);

-- 6. SCHEDULE METHOD B (Optional): Trigger the Edge Function via pg_net HTTP Request
-- Runs every 2 days at 04:05 UTC to trigger the Edge Function endpoint
SELECT cron.schedule(
    'keep_alive_edge_function',
    '5 4 */2 * *',
    $$
    SELECT net.http_get(
        url := 'https://mfzokxffhmedvtuhykdw.supabase.co/functions/v1/keep-alive',
        headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'apikey', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1mem9reGZmaG1lZHZ0dWh5a2R3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU5NjY1NjIsImV4cCI6MjEwMTU0MjU2Mn0.pkKw3G6EbD0A3cUydWPA79WE07RJElQdWkRcQXjkUoQ'
        )
    );
    $$
);

-- 7. Perform an immediate test run right now to verify
SELECT public.execute_supabase_heartbeat('manual_initial_run');

-- 8. View active cron jobs status
SELECT jobid, jobname, schedule, active, command FROM cron.job;
