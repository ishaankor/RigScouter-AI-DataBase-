import express, { Request, Response } from 'express';
import cors from 'cors';
import dotenv from 'dotenv';
import { supabase } from './supabase.js';
import { TavilyHardwareAgent, SseEmitter } from './agent.js';

dotenv.config();

const app = express();
const PORT = process.env.PORT || 4000;
const agent = new TavilyHardwareAgent();

// ─── CORS: allow all origins, including SSE ───────────────────────────────────
app.use(cors({
  origin: '*',
  methods: ['GET', 'POST', 'OPTIONS'],
  allowedHeaders: ['Content-Type', 'Authorization', 'Cache-Control'],
}));
app.use(express.json());

// ─── SSE Client Registry ──────────────────────────────────────────────────────
interface SseClient {
  id: string;
  res: Response;
}

const sseClients: SseClient[] = [];

function broadcastSse(event: string, data: Record<string, unknown>) {
  const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const client of sseClients) {
    try {
      client.res.write(payload);
    } catch {
      // client disconnected
    }
  }
  console.log(`[SSE Broadcast] event="${event}" to ${sseClients.length} client(s)`);
}

const agentSseEmitter: SseEmitter = (event, data) => {
  broadcastSse(event, data);
};

// ─── Parts Queue: the scheduler scrapes these in rotation ─────────────────────
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
let lastSchedulerRun: string | null = null;
let schedulerQueueIndex = 0;
let schedulerIntervalId: NodeJS.Timeout | null = null;
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

  console.log(`\n[Scheduler] ⏱ Auto-scraping: "${part}" (${schedulerQueueIndex}/${PARTS_QUEUE.length})`);
  broadcastSse('scheduler_tick', { query: part, queueIndex: schedulerQueueIndex, timestamp: lastSchedulerRun });

  try {
    await agent.run(part, agentSseEmitter);
  } catch (err: any) {
    console.error(`[Scheduler Error] "${part}":`, err?.message || err);
    broadcastSse('scheduler_error', { query: part, error: err?.message || 'Unknown error', timestamp: new Date().toISOString() });
  } finally {
    schedulerRunning = false;
  }
}

function startScheduler() {
  console.log(`[Scheduler] Starting — will scrape every ${SCHEDULER_INTERVAL_MS / 60000} minutes`);
  setTimeout(() => runSchedulerTick(), 5000);
  schedulerIntervalId = setInterval(() => runSchedulerTick(), SCHEDULER_INTERVAL_MS);
}

// ─── Routes ───────────────────────────────────────────────────────────────────

// 1. Health check
app.get('/health', (_req: Request, res: Response) => {
  res.json({
    status: 'ok',
    service: 'RigScouter-AI-DataBase',
    timestamp: new Date().toISOString()
  });
});

// 2. Scheduler & Service Status
app.get('/api/status', (_req: Request, res: Response) => {
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

// 3. SSE Stream — live event broadcast
app.get('/api/stream', (req: Request, res: Response) => {
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
    } catch {
      clearInterval(heartbeat);
    }
  }, 30000);

  req.on('close', () => {
    clearInterval(heartbeat);
    const idx = sseClients.findIndex(c => c.id === clientId);
    if (idx !== -1) sseClients.splice(idx, 1);
    console.log(`[SSE] Client disconnected: ${clientId} (${sseClients.length} remaining)`);
  });
});

// 4. Fetch components from Supabase DB
app.get('/api/components', async (req: Request, res: Response) => {
  try {
    const { category, q } = req.query;
    let query = supabase.from('hardware_components').select('*');

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
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 5. Fetch watchlist from Supabase DB
app.get('/api/watchlist', async (_req: Request, res: Response) => {
  try {
    const { data, error } = await supabase
      .from('watchlist_items')
      .select('*')
      .order('added_at', { ascending: false });

    if (error) return res.status(500).json({ error: error.message });

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
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 6. On-demand agent run
app.get('/api/agent/run', async (req: Request, res: Response) => {
  const query = (req.query.query as string) || (req.query.q as string) || '';
  if (!query) {
    return res.status(400).json({ error: 'Provide ?query= parameter' });
  }
  try {
    const result = await agent.run(query, agentSseEmitter);
    res.json({ source: 'tavily_autonomous_agent', result });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/agent/run', async (req: Request, res: Response) => {
  const { prompt, query } = req.body;
  const target = prompt || query;
  if (!target || typeof target !== 'string') {
    return res.status(400).json({ error: 'Provide { prompt } or { query } in request body' });
  }
  try {
    const result = await agent.run(target, agentSseEmitter);
    res.json({ source: 'tavily_autonomous_agent', result });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// 7. Trigger scheduler tick manually
app.post('/api/scheduler/run-now', async (_req: Request, res: Response) => {
  res.json({ message: 'Scheduler tick triggered', timestamp: new Date().toISOString() });
  setImmediate(() => runSchedulerTick());
});

// 8. Render Web Inspector UI (Live Dashboard for visual extraction monitoring)
app.get('/', async (_req: Request, res: Response) => {
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>RigScouter-AI — Tavily Live Extraction Inspector</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #090d16;
      --card: #111827;
      --border: #1f2937;
      --cyan: #06b6d4;
      --green: #10b981;
      --rose: #f43f5e;
      --yellow: #f59e0b;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: #f3f4f6;
      font-family: 'Inter', sans-serif;
      padding: 24px;
      line-height: 1.5;
    }
    .container { max-width: 1280px; margin: 0 auto; }
    header {
      display: flex; justify-content: space-between; align-items: center;
      padding-bottom: 20px; margin-bottom: 24px; border-bottom: 1px solid var(--border);
    }
    h1 { font-size: 24px; font-weight: 900; background: linear-gradient(to right, #38bdf8, #818cf8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
    .badge {
      display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px;
      border-radius: 9999px; font-size: 12px; font-weight: 700; border: 1px solid;
    }
    .badge-live { background: rgba(16, 185, 129, 0.15); color: var(--green); border-color: rgba(16, 185, 129, 0.3); }
    .card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 16px; padding: 24px; margin-bottom: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.5);
    }
    .form-group { display: flex; gap: 12px; margin-bottom: 16px; }
    input[type="text"] {
      flex: 1; background: #030712; border: 1px solid #374151; color: white;
      padding: 12px 16px; border-radius: 10px; font-size: 14px; font-family: inherit;
    }
    input[type="text"]:focus { outline: none; border-color: var(--cyan); }
    button {
      background: linear-gradient(135deg, var(--cyan), #3b82f6); color: #030712;
      font-weight: 800; padding: 12px 24px; border-radius: 10px; border: none; cursor: pointer;
      font-size: 14px; transition: all 0.2s;
    }
    button:hover { opacity: 0.9; transform: translateY(-1px); }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
    pre {
      background: #030712; border: 1px solid #1f2937; border-radius: 10px;
      padding: 16px; font-family: 'JetBrains Mono', monospace; font-size: 12px;
      color: #38bdf8; overflow-x: auto; max-height: 400px;
    }
    table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 13px; }
    th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--border); }
    th { color: #9ca3af; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
    tr:hover { background: rgba(255,255,255,0.02); }
    a { color: var(--cyan); text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>RigScouter-AI — Tavily Live Extraction Inspector</h1>
        <p style="font-size: 12px; color: #9ca3af; margin-top: 4px;">Universal Multi-Retailer Hardware Price Scraper & Persistence Service</p>
      </div>
      <div style="display: flex; gap: 10px;">
        <span class="badge badge-live">● RENDER DEPLOYED</span>
        <span id="sse-badge" class="badge" style="background: rgba(6,182,212,0.15); color: var(--cyan); border-color: rgba(6,182,212,0.3);">SSE STREAM READY</span>
      </div>
    </header>

    <!-- Interactive Extraction Console -->
    <div class="card">
      <h2 style="font-size: 16px; font-weight: 700; margin-bottom: 12px; color: white;">🔍 Live Tavily Extraction Sandbox</h2>
      <p style="font-size: 13px; color: #9ca3af; margin-bottom: 16px;">Type any hardware query (e.g., <code>RTX 4070 Super</code>, <code>Corsair RM850x</code>, <code>Samsung 990 Pro 2TB</code>) or direct product URL to see live Tavily extraction output.</p>

      <form id="extract-form" class="form-group">
        <input type="text" id="query-input" placeholder="e.g. RTX 4070 Super OR https://www.microcenter.com/product/..." value="RTX 4070 Super" required>
        <button type="submit" id="btn-submit">Extract Price Now</button>
      </form>

      <div class="grid">
        <div>
          <h3 style="font-size: 13px; color: #9ca3af; margin-bottom: 8px;">Extracted Offer Result (JSON)</h3>
          <pre id="json-output">// Click "Extract Price Now" to run live Tavily extraction...</pre>
        </div>
        <div>
          <h3 style="font-size: 13px; color: #9ca3af; margin-bottom: 8px;">Real-Time SSE Event Stream Log</h3>
          <pre id="sse-log">// Listening for live backend SSE events (/api/stream)...</pre>
        </div>
      </div>
    </div>

    <!-- Live Supabase Catalog -->
    <div class="card">
      <div style="display: flex; justify-content: space-between; align-items: center;">
        <h2 style="font-size: 16px; font-weight: 700; color: white;">📊 Supabase Database Catalog</h2>
        <button onclick="loadComponents()" style="padding: 6px 14px; font-size: 12px;">Refresh Catalog</button>
      </div>
      <div style="overflow-x: auto;">
        <table>
          <thead>
            <tr>
              <th>Category</th>
              <th>Hardware Model</th>
              <th>Price</th>
              <th>MSRP</th>
              <th>Retailer</th>
              <th>Product Link</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody id="catalog-body">
            <tr><td colspan="7" style="text-align: center; color: #6b7280;">Loading hardware components...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    const form = document.getElementById('extract-form');
    const input = document.getElementById('query-input');
    const btn = document.getElementById('btn-submit');
    const jsonOutput = document.getElementById('json-output');
    const sseLog = document.getElementById('sse-log');
    const catalogBody = document.getElementById('catalog-body');

    // ── Live SSE Subscription ─────────────────────────────────────────
    function initSSE() {
      const es = new EventSource('/api/stream');
      es.addEventListener('connected', (e) => appendLog('CONNECTED', JSON.parse(e.data)));
      es.addEventListener('agent_start', (e) => appendLog('START', JSON.parse(e.data)));
      es.addEventListener('retailer_found', (e) => appendLog('OFFER FOUND', JSON.parse(e.data)));
      es.addEventListener('price_drop', (e) => appendLog('PRICE DROP ⚡', JSON.parse(e.data)));
      es.addEventListener('agent_complete', (e) => {
        appendLog('COMPLETE', JSON.parse(e.data));
        loadComponents();
      });
      es.addEventListener('scheduler_tick', (e) => appendLog('SCHEDULER TICK ⏱', JSON.parse(e.data)));
    }

    function appendLog(tag, data) {
      const time = new Date().toLocaleTimeString();
      const line = \`[\${time}] [\${tag}] \${JSON.stringify(data)}\n\`;
      sseLog.textContent = line + sseLog.textContent.substring(0, 3000);
    }

    // ── Live Extraction Trigger ───────────────────────────────────────
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (!q) return;

      btn.disabled = true;
      btn.textContent = 'Extracting Tavily DOM...';
      jsonOutput.textContent = '// Running multi-retailer Tavily search & structured parse...';

      try {
        const res = await fetch('/api/agent/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt: q })
        });
        const data = await res.json();
        jsonOutput.textContent = JSON.stringify(data, null, 2);
      } catch (err) {
        jsonOutput.textContent = '// Error: ' + err.message;
      } finally {
        btn.disabled = false;
        btn.textContent = 'Extract Price Now';
        loadComponents();
      }
    });

    // ── Load DB Components ────────────────────────────────────────────
    async function loadComponents() {
      try {
        const res = await fetch('/api/components');
        const data = await res.json();
        const comps = data.components || [];

        if (comps.length === 0) {
          catalogBody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: #6b7280;">No components found.</td></tr>';
          return;
        }

        catalogBody.innerHTML = comps.map(c => \`
          <tr>
            <td><span style="background: rgba(6,182,212,0.15); color: #38bdf8; font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 4px;">\${c.category}</span></td>
            <td style="font-weight: 600; color: white;">\${c.name.substring(0, 50)}</td>
            <td style="font-weight: 800; color: #10b981;">$\${c.currentPrice.toFixed(2)}</td>
            <td style="color: #6b7280; text-decoration: line-through;">$\${c.msrp.toFixed(2)}</td>
            <td style="color: #9ca3af;">\${c.retailer}</td>
            <td><a href="\${c.productUrl}" target="_blank">View Direct Deal ↗</a></td>
            <td style="font-size: 11px; color: #6b7280;">\${new Date(c.updatedAt || Date.now()).toLocaleTimeString()}</td>
          </tr>
        \`).join('');
      } catch (e) {
        catalogBody.innerHTML = '<tr><td colspan="7" style="color: #f43f5e;">Failed to load catalog.</td></tr>';
      }
    }

    initSSE();
    loadComponents();
  </script>
</body>
</html>`;

  res.setHeader('Content-Type', 'text/html');
  res.send(html);
});

// ─── Start ────────────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`🚀 RigScouter-AI-DataBase Backend running on port ${PORT}`);
  console.log(`📡 SSE live feed: http://localhost:${PORT}/api/stream`);
  console.log(`📊 Status:        http://localhost:${PORT}/api/status`);
  console.log(`🔧 Components:    http://localhost:${PORT}/api/components`);
  console.log(`🖥 Inspector Web:  http://localhost:${PORT}/`);
  startScheduler();
});
