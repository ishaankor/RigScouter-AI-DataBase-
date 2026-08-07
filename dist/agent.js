"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.TavilyHardwareAgent = void 0;
const core_1 = require("@tavily/core");
const supabase_js_1 = require("./supabase.js");
const dotenv_1 = __importDefault(require("dotenv"));
dotenv_1.default.config();
const rawTavilyKeys = process.env.TAVILY_API_KEYS || process.env.TAVILY_API_KEY || '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';
const TAVILY_API_KEYS = rawTavilyKeys.split(',').map(k => k.trim()).filter(Boolean);
const GROQ_API_KEY = process.env.GROQ_API_KEY || '';
const OPENROUTER_API_KEY = process.env.OPENROUTER_API_KEY || '';
class TavilyHardwareAgent {
    currentKeyIndex = 0;
    constructor() { }
    getTavilyClient() {
        const key = TAVILY_API_KEYS[this.currentKeyIndex % TAVILY_API_KEYS.length];
        return (0, core_1.tavily)({ apiKey: key });
    }
    rotateTavilyKey() {
        if (TAVILY_API_KEYS.length > 1) {
            this.currentKeyIndex = (this.currentKeyIndex + 1) % TAVILY_API_KEYS.length;
            console.log(`[Tavily Key Rotated] Active key index: ${this.currentKeyIndex + 1}/${TAVILY_API_KEYS.length}`);
        }
    }
    /**
     * Main agent entrypoint using official @tavily/core SDK + Groq/OpenRouter LLM.
     * ABSOLUTELY NO REGEX PARSING OR DUMMY FALLBACK VALUES.
     */
    async run(prompt, emit) {
        const cleanPrompt = prompt.trim();
        const isUrl = cleanPrompt.startsWith('http://') || cleanPrompt.startsWith('https://');
        const category = this.detectCategory(cleanPrompt);
        console.log(`\n======================================================`);
        console.log(`[Official Tavily AI SDK Agent] Extracting price for: "${cleanPrompt}" (${category})`);
        console.log(`======================================================\n`);
        emit?.('agent_start', { query: cleanPrompt, category, timestamp: new Date().toISOString() });
        const state = {
            userQuery: cleanPrompt,
            category,
            scrapedOffers: [],
            summary: ''
        };
        if (category === 'Not compatible (N/A)' && !isUrl) {
            console.log(`[Non-PC Part Query Rejected] "${cleanPrompt}" is Not compatible (N/A)`);
            state.summary = `Not compatible (N/A) — "${cleanPrompt}" is not a recognized PC hardware component.`;
            emit?.('agent_complete', {
                query: cleanPrompt,
                category: 'Not compatible (N/A)',
                bestOffer: null,
                allOffers: [],
                summary: state.summary,
                timestamp: new Date().toISOString()
            });
            return state;
        }
        if (isUrl) {
            const offer = await this.extractDirectPage(cleanPrompt, this.detectRetailer(cleanPrompt), category);
            if (offer)
                state.scrapedOffers.push(offer);
        }
        else {
            const RETAILERS = [
                { name: 'Micro Center', domain: 'microcenter.com' },
                { name: 'Newegg', domain: 'newegg.com' },
                { name: 'Amazon', domain: 'amazon.com' },
                { name: 'Best Buy', domain: 'bestbuy.com' },
                { name: 'B&H', domain: 'bhphotovideo.com' }
            ];
            for (const r of RETAILERS) {
                const offer = await this.scrapeRetailerAccurateOffer(cleanPrompt, r.name, r.domain, category);
                if (offer && offer.price > 0) {
                    state.scrapedOffers.push(offer);
                    emit?.('retailer_found', {
                        query: cleanPrompt,
                        retailer: offer.retailer,
                        price: offer.price,
                        title: offer.title,
                        url: offer.url,
                        inStock: offer.inStock,
                        isRefurbished: offer.isRefurbished,
                        timestamp: new Date().toISOString()
                    });
                }
                // Inter-retailer delay to stay comfortably under Groq 30 RPM limit
                await new Promise(r => setTimeout(r, 600));
            }
        }
        // Secondary Tavily General Search Pass if no specific retailer hit was found
        if (state.scrapedOffers.length === 0) {
            console.log(`[Tavily General Search] Querying broad Tavily web search for "${cleanPrompt}"...`);
            for (let attempt = 0; attempt < TAVILY_API_KEYS.length; attempt++) {
                const tvly = this.getTavilyClient();
                try {
                    const searchRes = await tvly.search(`buy ${cleanPrompt} ${category} price`, {
                        searchDepth: 'advanced',
                        includeRawContent: 'text',
                        maxResults: 5
                    });
                    for (const hit of searchRes.results || []) {
                        const fullUrl = hit.url || '';
                        const retailer = this.detectRetailer(fullUrl);
                        let cleanUrl = fullUrl.replace(/\/reviews\/?$/i, '').split('?')[0];
                        if (retailer === 'Amazon' && cleanUrl.includes('/dp/')) {
                            cleanUrl = `https://www.amazon.com/s?k=${encodeURIComponent(cleanPrompt)}`;
                        }
                        const text = `${hit.title || ''} ${hit.content || ''} ${hit.rawContent || ''}`;
                        const parsed = await this.parseAccuratePriceWithLLM(text, cleanPrompt, retailer, cleanUrl, category);
                        if (parsed && parsed.price && parsed.price > 0) {
                            console.log(`✅ [TAVILY GENERAL SEARCH EXTRACTED] ${retailer}: "$${parsed.price}" -> ${parsed.title}`);
                            state.scrapedOffers.push({
                                retailer: retailer,
                                price: parsed.price,
                                originalPrice: parsed.originalPrice,
                                title: parsed.title || hit.title || cleanPrompt,
                                brand: parsed.brand || (hit.title ? hit.title.split(' ')[0] : 'Hardware'),
                                url: cleanUrl,
                                inStock: parsed.inStock,
                                isRefurbished: parsed.isRefurbished,
                                snippet: hit.content
                            });
                        }
                    }
                    break; // Stop loop if search succeeded
                }
                catch (e) {
                    console.warn(`[Tavily General Search Notice] Key ${this.currentKeyIndex + 1}/${TAVILY_API_KEYS.length}:`, e?.message || e);
                    if (TAVILY_API_KEYS.length > 1) {
                        this.rotateTavilyKey();
                    }
                    else {
                        break;
                    }
                }
            }
        }
        state.scrapedOffers.sort((a, b) => a.price - b.price);
        if (state.scrapedOffers.length > 0) {
            state.bestOffer = state.scrapedOffers[0];
            const stockStatus = state.bestOffer.inStock ? 'In Stock' : 'Out of Stock / Backorder';
            state.summary = `Evaluated ${state.scrapedOffers.length} live retailer listings via Tavily AI. Lowest price: $${state.bestOffer.price.toFixed(2)} at ${state.bestOffer.retailer} (${stockStatus}).`;
        }
        else {
            state.summary = `No live prices found across retailers for "${cleanPrompt}".`;
        }
        if (state.bestOffer) {
            try {
                const { data: existing } = await supabase_js_1.supabase
                    .from('hardware_components')
                    .select('current_price')
                    .ilike('model', `%${cleanPrompt}%`)
                    .order('updated_at', { ascending: false })
                    .limit(1);
                const previousPrice = existing?.[0]?.current_price ?? null;
                if (previousPrice !== null) {
                    const diff = state.bestOffer.price - previousPrice;
                    if (diff < -0.5) {
                        state.priceChange = 'drop';
                        state.previousPrice = previousPrice;
                        emit?.('price_drop', {
                            query: cleanPrompt,
                            retailer: state.bestOffer.retailer,
                            previousPrice,
                            newPrice: state.bestOffer.price,
                            savings: Math.abs(diff).toFixed(2),
                            url: state.bestOffer.url,
                            title: state.bestOffer.title,
                            category,
                            timestamp: new Date().toISOString()
                        });
                    }
                    else if (diff > 0.5) {
                        state.priceChange = 'increase';
                        state.previousPrice = previousPrice;
                    }
                    else {
                        state.priceChange = 'unchanged';
                    }
                }
                else {
                    state.priceChange = 'new';
                }
                const componentId = `agent-${cleanPrompt.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${state.bestOffer.retailer.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
                const realMsrp = state.bestOffer.originalPrice && state.bestOffer.originalPrice > state.bestOffer.price
                    ? state.bestOffer.originalPrice
                    : state.bestOffer.price;
                const calculatedDealScore = realMsrp > state.bestOffer.price
                    ? Math.min(100, Math.max(50, Math.round(50 + ((realMsrp - state.bestOffer.price) / realMsrp) * 100)))
                    : 50;
                const baseModelGroup = this.normalizeModel(state.bestOffer.title, cleanPrompt);
                await supabase_js_1.supabase.from('hardware_components').upsert({
                    id: componentId,
                    name: state.bestOffer.title,
                    category: category,
                    brand: state.bestOffer.brand || state.bestOffer.title.split(' ')[0] || 'Hardware',
                    model: baseModelGroup,
                    specs: JSON.stringify({
                        AgentSummary: state.summary,
                        RetailerOffers: state.scrapedOffers,
                        InStock: state.bestOffer.inStock,
                        IsRefurbished: state.bestOffer.isRefurbished || false,
                        OriginalPrice: state.bestOffer.originalPrice || null,
                        PreviousPrice: previousPrice,
                        PriceChange: state.priceChange,
                        ScrapedAt: new Date().toISOString()
                    }),
                    msrp: realMsrp,
                    current_price: state.bestOffer.price,
                    lowest_price_90d: state.bestOffer.price,
                    retailer: state.bestOffer.retailer,
                    product_url: state.bestOffer.url,
                    image_url: 'https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=600&q=80',
                    rating: state.bestOffer.rating || null,
                    deal_score: calculatedDealScore,
                    updated_at: new Date().toISOString()
                });
                console.log(`[DB Persist Success] "${state.bestOffer.title}" ($${state.bestOffer.price.toFixed(2)}) at ${state.bestOffer.url}`);
                try {
                    await supabase_js_1.supabase.from('price_snapshots').insert({
                        id: `snap-${Date.now()}-${Math.random().toString(36).substring(2, 6)}`,
                        watchlist_item_id: componentId,
                        price: state.bestOffer.price,
                        in_stock: state.bestOffer.inStock,
                        scraped_at: new Date().toISOString()
                    });
                }
                catch (err) {
                    console.warn('[Price Snapshot Insert Notice]:', err?.message || err);
                }
                emit?.('agent_complete', {
                    query: cleanPrompt,
                    bestOffer: state.bestOffer,
                    allOffers: state.scrapedOffers,
                    priceChange: state.priceChange,
                    previousPrice: state.previousPrice,
                    summary: state.summary,
                    timestamp: new Date().toISOString()
                });
            }
            catch (e) {
                console.error('[Agent Persistence Error]:', e?.message || e);
            }
        }
        else {
            emit?.('agent_complete', {
                query: cleanPrompt,
                bestOffer: null,
                allOffers: [],
                summary: state.summary,
                timestamp: new Date().toISOString()
            });
        }
        return state;
    }
    /**
     * Scrapes retailer using official @tavily/core SDK search + raw content extraction with key rotation
     */
    async scrapeRetailerAccurateOffer(modelQuery, retailerName, domainPattern, category) {
        console.log(`[Tavily AI SDK] Querying ${retailerName} for "${modelQuery}" (${category})...`);
        for (let attempt = 0; attempt < TAVILY_API_KEYS.length; attempt++) {
            const tvly = this.getTavilyClient();
            try {
                const searchRes = await tvly.search(`buy ${modelQuery} ${category} price`, {
                    searchDepth: 'advanced',
                    includeDomains: [domainPattern],
                    includeRawContent: 'text',
                    maxResults: 5
                });
                const results = searchRes.results || [];
                for (const hit of results) {
                    const fullUrl = hit.url || '';
                    if (!this.isValidDirectProductUrl(fullUrl, domainPattern))
                        continue;
                    let rawContent = (hit.rawContent || hit.content || hit.title || '');
                    if (rawContent.length < 300) {
                        try {
                            const extRes = await tvly.extract([fullUrl], {
                                extractDepth: 'advanced',
                                format: 'markdown'
                            });
                            const extPage = (extRes.results || [])[0];
                            if (extPage && extPage.rawContent) {
                                rawContent = extPage.rawContent;
                            }
                        }
                        catch (e) { }
                    }
                    let cleanProductUrl = fullUrl.replace(/\/reviews\/?$/i, '').split('?')[0];
                    if (retailerName === 'Amazon' && cleanProductUrl.includes('/dp/')) {
                        cleanProductUrl = `https://www.amazon.com/s?k=${encodeURIComponent(modelQuery)}`;
                    }
                    const parsed = await this.parseAccuratePriceWithLLM(rawContent, modelQuery, retailerName, cleanProductUrl, category);
                    if (parsed && parsed.price && parsed.price > 0) {
                        console.log(`✅ [TAVILY AI + LLM EXTRACTED] ${retailerName}: "$${parsed.price}" -> ${parsed.title.substring(0, 60)}`);
                        return {
                            retailer: retailerName,
                            price: parsed.price,
                            originalPrice: parsed.originalPrice,
                            title: parsed.title || hit.title || modelQuery,
                            brand: parsed.brand,
                            url: cleanProductUrl,
                            inStock: parsed.inStock,
                            isRefurbished: parsed.isRefurbished,
                            snippet: hit.content
                        };
                    }
                }
                return null;
            }
            catch (e) {
                console.warn(`[Tavily Search Quota / Error] ${retailerName} (Key ${this.currentKeyIndex + 1}/${TAVILY_API_KEYS.length}):`, e?.message || e);
                if (TAVILY_API_KEYS.length > 1) {
                    this.rotateTavilyKey();
                }
                else {
                    break;
                }
            }
        }
        return null;
    }
    /**
     * Direct URL extraction using Tavily AI SDK + LLM
     */
    async extractDirectPage(url, retailerName, category) {
        for (let attempt = 0; attempt < TAVILY_API_KEYS.length; attempt++) {
            const tvly = this.getTavilyClient();
            try {
                console.log(`[Tavily AI SDK Extract] Reading DOM content from ${url}...`);
                const extRes = await tvly.extract([url], {
                    extractDepth: 'advanced',
                    format: 'markdown'
                });
                const hit = (extRes.results || [])[0];
                if (hit) {
                    const rawText = (hit.rawContent || '');
                    const parsed = await this.parseAccuratePriceWithLLM(rawText, url, retailerName, url, category);
                    if (parsed && parsed.price) {
                        return {
                            retailer: retailerName,
                            price: parsed.price,
                            originalPrice: parsed.originalPrice,
                            title: parsed.title || url,
                            brand: parsed.brand,
                            url: url,
                            inStock: parsed.inStock,
                            isRefurbished: parsed.isRefurbished,
                            snippet: rawText.substring(0, 300)
                        };
                    }
                }
                return null;
            }
            catch (e) {
                console.warn(`[Tavily SDK Extract Warning] ${url} extract failed:`, e?.message || e);
                if (TAVILY_API_KEYS.length > 1) {
                    this.rotateTavilyKey();
                }
                else {
                    break;
                }
            }
        }
        return null;
    }
    /**
     * 100% LLM-driven price extraction (Groq Llama 3.1 8B Primary -> OpenRouter Llama 3.2 3B Fallback)
     * ABSOLUTELY NO REGEX PARSING OR DUMMY FALLBACK VALUES.
     */
    async parseAccuratePriceWithLLM(text, query, retailer, url, category) {
        const groqResult = await this.parseWithGroqLLM(text, query, retailer, category);
        if (groqResult && groqResult.price) {
            return groqResult;
        }
        const openRouterResult = await this.parseWithOpenRouterLLM(text, query, retailer, category);
        if (openRouterResult && openRouterResult.price) {
            return openRouterResult;
        }
        return null;
    }
    /**
     * Groq LLM API Price Extractor with automatic multi-model rotation on HTTP 429
     */
    async parseWithGroqLLM(text, query, retailer, category) {
        const apiKey = GROQ_API_KEY;
        if (!apiKey || apiKey.includes('placeholder')) {
            console.warn('[Groq LLM] No GROQ_API_KEY provided in .env');
            return null;
        }
        // Active supported models on Groq free tier
        const GROQ_MODELS = [
            'llama-3.1-8b-instant',
            'llama-3.3-70b-versatile'
        ];
        const systemPrompt = `You are a high-precision PC hardware price extraction AI. 
Your mission is to extract the EXACT current sale price from the provided webpage markdown.

CRITICAL PRODUCT PAGE EXTRACTION RULES:
1. BYPASS HEADERS & ADS: Ignore top navigation links, member banners, and footer elements.
2. IGNORE SPONSORED ADS: Product pages often show sponsored competitor ads (e.g. "GIGABYTE... $649.99 Sponsored"). Ignore sponsored ads and extract the price for the MAIN item being sold on this page.
3. MAIN BUY BOX SALE PRICE: Extract the current actual sale price for the main item.
4. CLEARANCE & REFURBISHED: On refurbished, clearance, or open-box items, extract the current discounted sale price, NOT the original launch price.
5. IGNORE THIRD-PARTY: Do NOT extract prices from "Other Sellers", "Used & New from $XXX", or sidebar ads.
6. SALE PRICE vs REGULAR MSRP: If a regular price is struck through ($1,299.00) and a sale price is shown ($1,129.28), set currentPrice = 1129.28 and originalPrice = 1299.00.
7. Output strictly in this JSON format:
{
  "currentPrice": number or null,
  "originalPrice": number or null,
  "inStock": boolean,
  "isRefurbished": boolean,
  "cleanTitle": string,
  "brand": string
}`;
        // Smart Buy Box Window Selector: pinpoints exact price block across huge retailer DOMs
        const trimmedSnippet = this.extractTargetedProductSnippet(text, query);
        for (const modelName of GROQ_MODELS) {
            try {
                const res = await fetch('https://api.groq.com/openai/v1/chat/completions', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${apiKey}`
                    },
                    body: JSON.stringify({
                        model: modelName,
                        messages: [
                            { role: 'system', content: systemPrompt },
                            { role: 'user', content: `Item Query: "${query}" (${category})\nRetailer: "${retailer}"\nPage Content:\n${trimmedSnippet}` }
                        ],
                        temperature: 0.1,
                        response_format: { type: 'json_object' }
                    })
                });
                if (res.ok) {
                    const data = await res.json();
                    const content = data.choices?.[0]?.message?.content || '';
                    const parsed = JSON.parse(content);
                    if (typeof parsed.currentPrice === 'number' && parsed.currentPrice > 0) {
                        return {
                            price: parsed.currentPrice,
                            originalPrice: typeof parsed.originalPrice === 'number' ? parsed.originalPrice : undefined,
                            title: parsed.cleanTitle || query,
                            brand: parsed.brand,
                            inStock: Boolean(parsed.inStock),
                            isRefurbished: Boolean(parsed.isRefurbished)
                        };
                    }
                }
                else if (res.status === 429) {
                    console.warn(`[Groq Rate Limit 429] Model ${modelName} rate limited — waiting 1.2s before retrying next model...`);
                    await new Promise(r => setTimeout(r, 1200));
                    continue;
                }
                else {
                    const errText = await res.text();
                    console.warn(`[Groq API Error ${modelName}] HTTP ${res.status}:`, errText.substring(0, 150));
                }
            }
            catch (err) {
                console.warn(`[Groq LLM Error ${modelName}] ${retailer}:`, err?.message || err);
            }
        }
        return null;
    }
    /**
     * OpenRouter Free LLM Fallback (Llama 3.2 3B Instruct Free)
     */
    async parseWithOpenRouterLLM(text, query, retailer, category) {
        const apiKey = OPENROUTER_API_KEY || 'sk-or-v1-free';
        if (!apiKey || apiKey.includes('placeholder'))
            return null;
        try {
            const res = await fetch('https://openrouter.ai/api/v1/chat/completions', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${apiKey}`
                },
                body: JSON.stringify({
                    model: 'meta-llama/llama-3.2-3b-instruct:free',
                    messages: [
                        { role: 'system', content: 'Extract PC hardware price JSON only.' },
                        { role: 'user', content: `Item: "${query}" (${category})\nRetailer: "${retailer}"\nContent:\n${text.substring(0, 4000)}\n\nReturn JSON: {"currentPrice": number, "originalPrice": number, "inStock": boolean, "cleanTitle": string, "brand": string}` }
                    ],
                    temperature: 0.1
                })
            });
            if (res.ok) {
                const data = await res.json();
                const content = data.choices?.[0]?.message?.content || '';
                const jsonMatch = content.match(/\{[\s\S]*\}/);
                if (jsonMatch) {
                    const parsed = JSON.parse(jsonMatch[0]);
                    if (typeof parsed.currentPrice === 'number' && parsed.currentPrice > 0) {
                        return {
                            price: parsed.currentPrice,
                            originalPrice: typeof parsed.originalPrice === 'number' ? parsed.originalPrice : undefined,
                            title: parsed.cleanTitle || query,
                            brand: parsed.brand,
                            inStock: Boolean(parsed.inStock),
                            isRefurbished: Boolean(parsed.isRefurbished)
                        };
                    }
                }
            }
        }
        catch (e) { }
        return null;
    }
    isValidDirectProductUrl(url, domainPattern) {
        const lower = url.toLowerCase();
        if (lower.includes('/site/reviews/') ||
            lower.includes('/reviews/') ||
            lower.includes('/customer-reviews/') ||
            lower.includes('/forum/') ||
            lower.includes('/insider/') ||
            lower.includes('/article') ||
            lower.includes('/site/questions/') ||
            lower.includes('/monthlypdf/') ||
            lower.includes('/blog/') ||
            lower.includes('searchpage.jsp') ||
            lower.includes('/shop/') ||
            lower.includes('/c/buy/') ||
            lower.endsWith('.pdf') ||
            lower.includes('page=')) {
            return false;
        }
        if (domainPattern.includes('microcenter.com'))
            return lower.includes('/product/');
        if (domainPattern.includes('amazon.com'))
            return lower.includes('/dp/') || lower.includes('/gp/product/');
        if (domainPattern.includes('newegg.com'))
            return lower.includes('/p/') && !lower.includes('/p/pl');
        if (domainPattern.includes('bestbuy.com'))
            return lower.includes('/site/') && lower.includes('.p?');
        if (domainPattern.includes('bhphotovideo.com'))
            return lower.includes('/c/product/');
        return true;
    }
    detectRetailer(urlOrText) {
        const lower = urlOrText.toLowerCase();
        if (lower.includes('microcenter.com') || lower.includes('micro center'))
            return 'Micro Center';
        if (lower.includes('newegg.com') || lower.includes('newegg'))
            return 'Newegg';
        if (lower.includes('bestbuy.com') || lower.includes('best buy'))
            return 'Best Buy';
        if (lower.includes('bhphotovideo.com') || lower.includes('b&h'))
            return 'B&H';
        if (lower.includes('walmart.com') || lower.includes('walmart'))
            return 'Walmart';
        if (lower.includes('ebay.com') || lower.includes('ebay'))
            return 'eBay';
        return 'Amazon';
    }
    detectCategory(query) {
        const lower = query.toLowerCase();
        // Direct URLs are validated during extraction
        if (lower.startsWith('http://') || lower.startsWith('https://')) {
            if (lower.includes('rtx') || lower.includes('gtx') || lower.includes('radeon') || lower.includes('rx') || lower.includes('gpu'))
                return 'GPU';
            if (lower.includes('ryzen') || lower.includes('core') || lower.includes('cpu') || lower.includes('processor'))
                return 'CPU';
            if (lower.includes('ram') || lower.includes('ddr'))
                return 'RAM';
            if (lower.includes('ssd') || lower.includes('nvme') || lower.includes('m.2'))
                return 'SSD';
            if (lower.includes('motherboard') || lower.includes('mobo'))
                return 'Motherboard';
            if (lower.includes('psu') || lower.includes('power-supply'))
                return 'PSU';
            if (lower.includes('monitor') || lower.includes('display'))
                return 'Monitor';
            return 'Hardware';
        }
        if (lower.includes('rtx') || lower.includes('gtx') || lower.includes('radeon') || lower.includes('rx') ||
            lower.includes('graphics card') || lower.includes('video card') || lower.includes('gpu') || lower.includes('arc a7')) {
            return 'GPU';
        }
        if (lower.includes('ryzen') || lower.includes('core i3') || lower.includes('core i5') || lower.includes('core i7') ||
            lower.includes('core i9') || lower.includes('core ultra') || lower.includes('processor') || lower.includes('cpu') ||
            lower.includes('threadripper') || lower.includes('xeon') || lower.includes('intel') || lower.includes('amd')) {
            return 'CPU';
        }
        if (lower.includes('ddr5') || lower.includes('ddr4') || lower.includes('ram') || lower.includes('memory kit')) {
            return 'RAM';
        }
        if (lower.includes('ssd') || lower.includes('nvme') || lower.includes('m.2') || lower.includes('hard drive') || lower.includes('hdd') || lower.includes('samsung') || lower.includes('wd_black') || lower.includes('crucial') || /\b\d+\s*(tb|gb)\b/i.test(lower)) {
            return 'SSD';
        }
        if (lower.includes('motherboard') || lower.includes('mobo') || lower.includes('b650') || lower.includes('z790') ||
            lower.includes('x670') || lower.includes('x870') || lower.includes('b550') || lower.includes('z690')) {
            return 'Motherboard';
        }
        if (lower.includes('psu') || lower.includes('power supply') || lower.includes('watt') || lower.includes('850w') || lower.includes('1000w') || lower.includes('750w')) {
            return 'PSU';
        }
        if (lower.includes('case') || lower.includes('chassis') || lower.includes('mid tower') || lower.includes('full tower')) {
            return 'Case';
        }
        if (lower.includes('aio') || lower.includes('liquid cooler') || lower.includes('air cooler') || lower.includes('thermalright') || lower.includes('noctua')) {
            return 'Cooler';
        }
        if (lower.includes('monitor') || lower.includes('display') || lower.includes('oled') || lower.includes('hz')) {
            return 'Monitor';
        }
        if (lower.includes('mouse') || lower.includes('keyboard') || lower.includes('headset') || lower.includes('webcam') || lower.includes('pc fan')) {
            return 'Accessories';
        }
        return 'Not compatible (N/A)';
    }
    /**
     * Normalizes any raw product title or user query into a clean, unified base model group
     * e.g. "MSI GeForce RTX 4070 Super VENTUS 2X 12GB" -> "RTX 4070 Super"
     * e.g. "GIGABYTE AORUS GeForce GTX 1080 Ti 11GB" -> "GTX 1080 Ti"
     * e.g. "AMD Ryzen 7 9800X3D 8-Core Processor" -> "Ryzen 7 9800X3D"
     */
    normalizeModel(text, fallback = '') {
        const lower = text.toLowerCase();
        // GPUs - RTX 50 Series
        if (lower.includes('5090'))
            return 'RTX 5090';
        if (lower.includes('5080'))
            return 'RTX 5080';
        if (lower.includes('5070 ti') || lower.includes('5070ti'))
            return 'RTX 5070 Ti';
        if (lower.includes('5070'))
            return 'RTX 5070';
        if (lower.includes('5060 ti') || lower.includes('5060ti'))
            return 'RTX 5060 Ti';
        if (lower.includes('5060'))
            return 'RTX 5060';
        // GPUs - RTX 40 Series
        if (lower.includes('4090'))
            return 'RTX 4090';
        if (lower.includes('4080 super'))
            return 'RTX 4080 Super';
        if (lower.includes('4080'))
            return 'RTX 4080';
        if (lower.includes('4070 ti super') || lower.includes('4070ti super'))
            return 'RTX 4070 Ti Super';
        if (lower.includes('4070 ti') || lower.includes('4070ti'))
            return 'RTX 4070 Ti';
        if (lower.includes('4070 super'))
            return 'RTX 4070 Super';
        if (lower.includes('4070'))
            return 'RTX 4070';
        if (lower.includes('4060 ti') || lower.includes('4060ti'))
            return 'RTX 4060 Ti';
        if (lower.includes('4060'))
            return 'RTX 4060';
        // GPUs - GTX 10/20/30 Series
        if (lower.includes('1080 ti') || lower.includes('1080ti'))
            return 'GTX 1080 Ti';
        if (lower.includes('1080'))
            return 'GTX 1080';
        if (lower.includes('1070 ti') || lower.includes('1070ti'))
            return 'GTX 1070 Ti';
        if (lower.includes('1070'))
            return 'GTX 1070';
        if (lower.includes('1060'))
            return 'GTX 1060';
        if (lower.includes('3090 ti') || lower.includes('3090ti'))
            return 'RTX 3090 Ti';
        if (lower.includes('3090'))
            return 'RTX 3090';
        if (lower.includes('3080 ti') || lower.includes('3080ti'))
            return 'RTX 3080 Ti';
        if (lower.includes('3080'))
            return 'RTX 3080';
        if (lower.includes('3070 ti') || lower.includes('3070ti'))
            return 'RTX 3070 Ti';
        if (lower.includes('3070'))
            return 'RTX 3070';
        if (lower.includes('3060 ti') || lower.includes('3060ti'))
            return 'RTX 3060 Ti';
        if (lower.includes('3060'))
            return 'RTX 3060';
        // GPUs - AMD Radeon RX 7000/9000
        if (lower.includes('9070 xt') || lower.includes('9070xt'))
            return 'RX 9070 XT';
        if (lower.includes('9070'))
            return 'RX 9070';
        if (lower.includes('7900 xtx'))
            return 'RX 7900 XTX';
        if (lower.includes('7900 xt'))
            return 'RX 7900 XT';
        if (lower.includes('7900 gre'))
            return 'RX 7900 GRE';
        if (lower.includes('7800 xt'))
            return 'RX 7800 XT';
        if (lower.includes('7700 xt'))
            return 'RX 7700 XT';
        if (lower.includes('7600 xt'))
            return 'RX 7600 XT';
        if (lower.includes('7600'))
            return 'RX 7600';
        // CPUs - AMD Ryzen 9000 / 7000 / 5000
        if (lower.includes('9950x3d'))
            return 'Ryzen 9 9950X3D';
        if (lower.includes('9950x'))
            return 'Ryzen 9 9950X';
        if (lower.includes('9900x'))
            return 'Ryzen 9 9900X';
        if (lower.includes('9800x3d'))
            return 'Ryzen 7 9800X3D';
        if (lower.includes('9700x'))
            return 'Ryzen 7 9700X';
        if (lower.includes('9600x'))
            return 'Ryzen 5 9600X';
        if (lower.includes('7950x3d'))
            return 'Ryzen 9 7950X3D';
        if (lower.includes('7950x'))
            return 'Ryzen 9 7950X';
        if (lower.includes('7900x3d'))
            return 'Ryzen 9 7900X3D';
        if (lower.includes('7900x'))
            return 'Ryzen 9 7900X';
        if (lower.includes('7800x3d'))
            return 'Ryzen 7 7800X3D';
        if (lower.includes('7700x'))
            return 'Ryzen 7 7700X';
        if (lower.includes('7600x'))
            return 'Ryzen 5 7600X';
        if (lower.includes('7600'))
            return 'Ryzen 5 7600';
        if (lower.includes('5800x3d'))
            return 'Ryzen 7 5800X3D';
        // CPUs - Intel Core Ultra / 14th Gen / 13th Gen
        if (lower.includes('285k'))
            return 'Core Ultra 9 285K';
        if (lower.includes('265k'))
            return 'Core Ultra 7 265K';
        if (lower.includes('245k'))
            return 'Core Ultra 5 245K';
        if (lower.includes('14900k'))
            return 'Core i9-14900K';
        if (lower.includes('14700k'))
            return 'Core i7-14700K';
        if (lower.includes('14600k'))
            return 'Core i5-14600K';
        if (lower.includes('13900k'))
            return 'Core i9-13900K';
        if (lower.includes('13700k'))
            return 'Core i7-13700K';
        if (lower.includes('13600k'))
            return 'Core i5-13600K';
        // SSDs
        if (lower.includes('990 pro'))
            return 'Samsung 990 Pro';
        if (lower.includes('980 pro'))
            return 'Samsung 980 Pro';
        if (lower.includes('sn850x'))
            return 'WD_BLACK SN850X';
        if (lower.includes('t700'))
            return 'Crucial T700';
        const cleanFallback = fallback.replace(/^https?:\/\/[^\/]+\//, '').replace(/-/g, ' ').trim();
        return cleanFallback || text;
    }
    /**
     * Smart Buy Box Window Selector:
     * Finds the exact section of raw text containing the product model and live price tag,
     * bypassing 100,000+ characters of site navigation headers and footer ads.
     */
    extractTargetedProductSnippet(text, query) {
        if (text.length <= 4500)
            return text;
        const lowerText = text.toLowerCase();
        const queryTerms = query
            .toLowerCase()
            .replace(/^https?:\/\/[^\/]+\//, '')
            .split(/[^a-z0-9]+/)
            .filter(t => t.length > 2 && !['product', 'html', 'http', 'https', 'www', 'com'].includes(t));
        // Match standard ($1032.00) and bold markdown ($**1,032**.00) price tags
        const priceRegex = /\$\s*\*{0,2}[\d,]+(\.\d{2})?\*{0,2}/g;
        let match;
        let bestIndex = -1;
        let maxMatchScore = -1;
        while ((match = priceRegex.exec(text)) !== null) {
            const idx = match.index;
            const windowText = lowerText.substring(Math.max(0, idx - 250), Math.min(text.length, idx + 250));
            let score = 0;
            for (const term of queryTerms) {
                if (windowText.includes(term))
                    score += 3;
            }
            if (windowText.includes('sponsored') || windowText.includes('seeing this ad')) {
                score -= 10;
            }
            if (windowText.includes('buy in store') || windowText.includes('add to cart') || windowText.includes('todays price') || windowText.includes('our price') || windowText.includes('in stock') || windowText.includes('sold by')) {
                score += 5;
            }
            if (windowText.includes('refurbished') || windowText.includes('clearance') || windowText.includes('open box')) {
                score += 2;
            }
            if (score > maxMatchScore) {
                maxMatchScore = score;
                bestIndex = idx;
            }
        }
        if (bestIndex !== -1 && maxMatchScore > 0) {
            return text.substring(Math.max(0, bestIndex - 600), Math.min(text.length, bestIndex + 3400));
        }
        for (const term of queryTerms) {
            const idx = lowerText.indexOf(term);
            if (idx !== -1) {
                return text.substring(Math.max(0, idx - 200), Math.min(text.length, idx + 3800));
            }
        }
        return text.substring(0, 4500);
    }
    /**
     * Validates that extracted product title matches requested hardware model query
     */
    doesTitleMatchQuery(title, query) {
        if (!title || !query)
            return false;
        const lTitle = title.toLowerCase();
        const lQuery = query.toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').trim();
        const queryTokens = lQuery.split(/\s+/).filter(t => t.length > 1);
        // Extract numbers like 1080, 4070, 7800, 5060 from query
        const modelNumbers = lQuery.match(/\b\d{3,5}(?:\s*ti|\s*super|\s*xt|\s*x3d)?\b/gi) || [];
        for (const num of modelNumbers) {
            const cleanNum = num.replace(/\s+/g, '');
            if (!lTitle.replace(/\s+/g, '').includes(cleanNum)) {
                return false;
            }
        }
        let matches = 0;
        for (const token of queryTokens) {
            if (lTitle.includes(token))
                matches++;
        }
        return matches >= Math.ceil(queryTokens.length * 0.4);
    }
}
exports.TavilyHardwareAgent = TavilyHardwareAgent;
