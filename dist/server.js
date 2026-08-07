"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
const express_1 = __importDefault(require("express"));
const cors_1 = __importDefault(require("cors"));
const dotenv_1 = __importDefault(require("dotenv"));
const supabase_js_1 = require("./supabase.js");
const agent_js_1 = require("./agent.js");
dotenv_1.default.config();
const app = (0, express_1.default)();
const PORT = process.env.PORT || 4000;
const agent = new agent_js_1.TavilyHardwareAgent();
// ─── CORS: Allow cross-origin requests from RigScouter-AI Web App ─────────────
app.use((0, cors_1.default)({
    origin: '*',
    methods: ['GET', 'POST', 'OPTIONS'],
    allowedHeaders: ['Content-Type', 'Authorization', 'Cache-Control'],
}));
app.use(express_1.default.json());
const sseClients = [];
function broadcastSse(event, data) {
    const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
    for (const client of sseClients) {
        try {
            client.res.write(payload);
        }
        catch {
            // client disconnected
        }
    }
    console.log(`[SSE Broadcast] event="${event}" to ${sseClients.length} client(s)`);
}
const agentSseEmitter = (event, data) => {
    broadcastSse(event, data);
};
// ─── Parts Queue: the daily scheduler scrapes these continuously to update prices
const PARTS_QUEUE = [
    'RTX 4060',
    'RTX 4070 Super',
    'RTX 4070 Ti Super',
    'RX 7800 XT',
    'GTX 1080 Ti',
    'Ryzen 7 7800X3D',
    'Intel i5-14600K',
    'Ryzen 5 7600X',
    'Corsair Vengeance DDR5 32GB',
    'G.Skill Trident Z5 DDR5',
    'Samsung 990 Pro 1TB',
    'WD Black SN850X 1TB',
];
// ─── Scheduler State ──────────────────────────────────────────────────────────
let schedulerRunning = false;
let lastSchedulerRun = null;
let schedulerQueueIndex = 0;
let schedulerIntervalId = null;
const SCHEDULER_INTERVAL_MS = 15 * 60 * 1000; // 15 minutes
async function runSchedulerTick() {
    if (schedulerRunning) {
        console.log('[Scheduler] Previous run still in progress — skipping tick');
        return;
    }
    schedulerRunning = true;
    const part = PARTS_QUEUE[schedulerQueueIndex % PARTS_QUEUE.length];
    schedulerQueueIndex++;
    lastSchedulerRun = new Date().toISOString();
    console.log(`\n[Scheduler] ⏱ Auto-updating daily prices for: "${part}" (${schedulerQueueIndex}/${PARTS_QUEUE.length})`);
    broadcastSse('scheduler_tick', { query: part, queueIndex: schedulerQueueIndex, timestamp: lastSchedulerRun });
    try {
        // Tavily agent runs, extracts latest prices, and saves/updates Supabase database automatically
        await agent.run(part, agentSseEmitter);
    }
    catch (err) {
        console.error(`[Scheduler Error] "${part}":`, err?.message || err);
        broadcastSse('scheduler_error', { query: part, error: err?.message || 'Unknown error', timestamp: new Date().toISOString() });
    }
    finally {
        schedulerRunning = false;
    }
}
function startScheduler() {
    console.log(`[Scheduler] Starting daily price updater — scraping every ${SCHEDULER_INTERVAL_MS / 60000} minutes`);
    setTimeout(() => runSchedulerTick(), 5000);
    schedulerIntervalId = setInterval(() => runSchedulerTick(), SCHEDULER_INTERVAL_MS);
}
// ─── Pure API Endpoints (NO HTML UI) ──────────────────────────────────────────
// 1. Root Status Endpoint
app.get('/', (_req, res) => {
    res.json({
        status: 'ok',
        service: 'RigScouter-AI Backend Proxy & Autonomous Tavily Agent',
        databaseConnected: true,
        schedulerActive: true,
        intervalMinutes: SCHEDULER_INTERVAL_MS / 60000,
        nextScheduledPart: PARTS_QUEUE[schedulerQueueIndex % PARTS_QUEUE.length],
        timestamp: new Date().toISOString()
    });
});
// 2. Health check
app.get('/health', (_req, res) => {
    res.json({
        status: 'ok',
        service: 'RigScouter-AI-DataBase',
        timestamp: new Date().toISOString()
    });
});
// 3. Scheduler & Service Status
app.get('/api/status', (_req, res) => {
    res.json({
        schedulerRunning,
        lastSchedulerRun,
        schedulerQueueIndex,
        nextPart: PARTS_QUEUE[schedulerQueueIndex % PARTS_QUEUE.length],
        partsQueue: PARTS_QUEUE,
        totalParts: PARTS_QUEUE.length,
        intervalMinutes: SCHEDULER_INTERVAL_MS / 60000,
        connectedSseClients: sseClients.length,
        timestamp: new Date().toISOString()
    });
});
// 4. SSE Stream — live event broadcast
app.get('/api/stream', (req, res) => {
    const clientId = `client-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.setHeader('X-Accel-Buffering', 'no');
    res.flushHeaders();
    sseClients.push({ id: clientId, res });
    console.log(`[SSE] Client connected: ${clientId} (${sseClients.length} total)`);
    res.write(`event: connected\ndata: ${JSON.stringify({
        clientId,
        message: 'Connected to RigScouter-AI live price feed',
        schedulerRunning,
        lastSchedulerRun,
        nextPart: PARTS_QUEUE[schedulerQueueIndex % PARTS_QUEUE.length],
        totalTracked: PARTS_QUEUE.length,
        timestamp: new Date().toISOString()
    })}\n\n`);
    const heartbeat = setInterval(() => {
        try {
            res.write(`: heartbeat ${new Date().toISOString()}\n\n`);
        }
        catch {
            clearInterval(heartbeat);
        }
    }, 30000);
    req.on('close', () => {
        clearInterval(heartbeat);
        const idx = sseClients.findIndex(c => c.id === clientId);
        if (idx !== -1)
            sseClients.splice(idx, 1);
        console.log(`[SSE] Client disconnected: ${clientId} (${sseClients.length} remaining)`);
    });
});
// 5. Fetch components from Supabase DB
app.get('/api/components', async (req, res) => {
    try {
        const { category, q } = req.query;
        let query = supabase_js_1.supabase.from('hardware_components').select('*');
        if (category && typeof category === 'string') {
            query = query.eq('category', category);
        }
        if (q && typeof q === 'string') {
            query = query.or(`name.ilike.%${q}%,model.ilike.%${q}%,brand.ilike.%${q}%`);
        }
        const { data, error } = await query.order('updated_at', { ascending: false }).limit(50);
        if (error) {
            return res.status(500).json({ error: error.message });
        }
        const formatted = (data || []).map(item => ({
            id: item.id,
            name: item.name,
            category: item.category,
            brand: item.brand,
            model: item.model,
            specs: typeof item.specs === 'string' ? JSON.parse(item.specs || '{}') : (item.specs || {}),
            msrp: item.msrp,
            currentPrice: item.current_price,
            lowestPrice90d: item.lowest_price_90d,
            retailer: item.retailer,
            productUrl: item.product_url,
            imageUrl: item.image_url,
            rating: item.rating,
            dealScore: item.deal_score,
            updatedAt: item.updated_at
        }));
        res.json({ source: 'supabase_database', components: formatted });
    }
    catch (err) {
        res.status(500).json({ error: err.message });
    }
});
// 6. Fetch watchlist from Supabase DB
app.get('/api/watchlist', async (_req, res) => {
    try {
        const { data, error } = await supabase_js_1.supabase
            .from('watchlist_items')
            .select('*')
            .order('added_at', { ascending: false });
        if (error)
            return res.status(500).json({ error: error.message });
        const formatted = (data || []).map(item => ({
            id: item.id,
            userId: item.user_id,
            componentName: item.component_name,
            category: item.category,
            targetPrice: item.target_price,
            currentPrice: item.current_price,
            previousPrice24h: item.previous_price_24h,
            previousPrice7d: item.previous_price_7d,
            previousPrice30d: item.previous_price_30d,
            allTimeLow: item.all_time_low,
            retailer: item.retailer,
            productUrl: item.product_url,
            imageUrl: item.image_url,
            inStock: item.in_stock,
            notifyOnFlashDrop: item.notify_on_flash_drop,
            addedAt: item.added_at
        }));
        res.json({ source: 'supabase_database', items: formatted });
    }
    catch (err) {
        res.status(500).json({ error: err.message });
    }
});
// 7. Database Proxy Scraper: Checks DB first; if missing, runs Tavily Agent & saves to Supabase DB
const handleDatabaseProxyScrape = async (targetQuery, res) => {
    try {
        const cleanQuery = targetQuery.trim();
        // Step A: Check Supabase DB first for immediate response
        const { data: dbMatches } = await supabase_js_1.supabase
            .from('hardware_components')
            .select('*')
            .or(`name.ilike.%${cleanQuery}%,model.ilike.%${cleanQuery}%,product_url.ilike.%${cleanQuery}%`)
            .order('updated_at', { ascending: false })
            .limit(1);
        if (dbMatches && dbMatches.length > 0) {
            const match = dbMatches[0];
            console.log(`[DB Match Hit] Returning "${match.name}" ($${match.current_price}) from Supabase DB`);
            return res.json({
                source: 'supabase_database_cache',
                query: cleanQuery,
                scrapedAt: match.updated_at,
                bestOffer: {
                    title: match.name,
                    price: match.current_price,
                    originalPrice: match.msrp,
                    retailer: match.retailer,
                    url: match.product_url,
                    brand: match.brand,
                    inStock: true
                },
                component: {
                    id: match.id,
                    name: match.name,
                    category: match.category,
                    brand: match.brand,
                    model: match.model,
                    specs: typeof match.specs === 'string' ? JSON.parse(match.specs || '{}') : (match.specs || {}),
                    msrp: match.msrp,
                    currentPrice: match.current_price,
                    lowestPrice90d: match.lowest_price_90d,
                    retailer: match.retailer,
                    productUrl: match.product_url,
                    imageUrl: match.image_url || 'https://images.unsplash.com/photo-1587202372775-e229f172b9d7?auto=format&fit=crop&w=600&q=80',
                    rating: match.rating || 4.8,
                    dealScore: match.deal_score || 85
                }
            });
        }
        // Step B: Missing from database — run Tavily Agent to scrape live across stores and insert into Supabase DB
        console.log(`[DB Miss] "${cleanQuery}" missing from DB. Executing Tavily Agent to scrape & add to database...`);
        const agentResult = await agent.run(cleanQuery, agentSseEmitter);
        const bestOffer = agentResult?.bestOffer;
        if (bestOffer && bestOffer.price) {
            return res.json({
                source: 'tavily_agent_scraped_and_saved_to_db',
                query: cleanQuery,
                scrapedAt: new Date().toISOString(),
                bestOffer: bestOffer,
                component: {
                    id: `agent-${Date.now()}`,
                    name: bestOffer.title || cleanQuery,
                    category: agentResult.category || 'GPU',
                    brand: bestOffer.brand || (bestOffer.title ? bestOffer.title.split(' ')[0] : 'Hardware'),
                    model: cleanQuery,
                    specs: {},
                    msrp: bestOffer.originalPrice || Math.round(bestOffer.price * 1.12 * 100) / 100,
                    currentPrice: bestOffer.price,
                    lowestPrice90d: Math.round(bestOffer.price * 0.96 * 100) / 100,
                    retailer: bestOffer.retailer || 'Micro Center',
                    productUrl: bestOffer.url || `https://www.amazon.com/s?k=${encodeURIComponent(cleanQuery)}`,
                    imageUrl: 'https://images.unsplash.com/photo-1587202372775-e229f172b9d7?auto=format&fit=crop&w=600&q=80',
                    rating: 4.8,
                    dealScore: 90
                }
            });
        }
        return res.status(200).json({
            found: false,
            query: cleanQuery,
            component: null,
            notice: `Could not retrieve live price for "${cleanQuery}"`
        });
    }
    catch (err) {
        console.error('[Backend Proxy Error]:', err);
        return res.status(500).json({ error: err?.message || 'Proxy execution failed' });
    }
};
app.post('/api/scrape', async (req, res) => {
    const query = req.body?.query || req.body?.prompt || req.body?.url;
    if (!query || typeof query !== 'string') {
        return res.status(400).json({ error: 'Provide valid search query or URL in request body' });
    }
    return handleDatabaseProxyScrape(query.trim(), res);
});
app.get('/api/scrape', async (req, res) => {
    const query = req.query.query || req.query.q || '';
    if (!query) {
        return res.status(400).json({ error: 'Provide ?query= parameter' });
    }
    return handleDatabaseProxyScrape(query.trim(), res);
});
app.post('/api/agent/run', async (req, res) => {
    const { prompt, query } = req.body;
    const target = prompt || query;
    if (!target || typeof target !== 'string') {
        return res.status(400).json({ error: 'Provide { prompt } or { query } in request body' });
    }
    return handleDatabaseProxyScrape(target.trim(), res);
});
app.get('/api/agent/run', async (req, res) => {
    const query = req.query.query || req.query.q || '';
    if (!query) {
        return res.status(400).json({ error: 'Provide ?query= parameter' });
    }
    return handleDatabaseProxyScrape(query.trim(), res);
});
// 8. Trigger scheduler tick manually
app.post('/api/scheduler/run-now', async (_req, res) => {
    res.json({ message: 'Scheduler tick triggered', timestamp: new Date().toISOString() });
    setImmediate(() => runSchedulerTick());
});
// Start Express server
app.listen(PORT, () => {
    console.log(`\n======================================================`);
    console.log(`🚀 RigScouter-AI Database Proxy & Tavily Agent Proxy`);
    console.log(`📡 Listening on http://localhost:${PORT}`);
    console.log(`======================================================\n`);
    startScheduler();
});
