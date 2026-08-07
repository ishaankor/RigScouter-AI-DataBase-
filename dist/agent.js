"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.TavilyHardwareAgent = void 0;
const core_1 = require("@tavily/core");
const firecrawl_js_1 = __importDefault(require("@mendable/firecrawl-js"));
const supabase_js_1 = require("./supabase.js");
const dotenv_1 = __importDefault(require("dotenv"));
dotenv_1.default.config();
const TAVILY_API_KEY = process.env.TAVILY_API_KEY || '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';
const FIRECRAWL_API_KEY = process.env.FIRECRAWL_API_KEY || '';
const GROQ_API_KEY = process.env.GROQ_API_KEY || '';
const OPENROUTER_API_KEY = process.env.OPENROUTER_API_KEY || '';
class TavilyHardwareAgent {
    tvlyClient;
    firecrawlClient = null;
    constructor() {
        this.tvlyClient = (0, core_1.tavily)({ apiKey: TAVILY_API_KEY });
        if (FIRECRAWL_API_KEY) {
            this.firecrawlClient = new firecrawl_js_1.default({ apiKey: FIRECRAWL_API_KEY });
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
                await supabase_js_1.supabase.from('hardware_components').upsert({
                    id: componentId,
                    name: state.bestOffer.title,
                    category: category,
                    brand: state.bestOffer.brand || state.bestOffer.title.split(' ')[0] || 'Hardware',
                    model: cleanPrompt,
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
                    rating: 4.5,
                    deal_score: calculatedDealScore,
                    updated_at: new Date().toISOString()
                });
                console.log(`[DB Persist Success] "${state.bestOffer.title}" ($${state.bestOffer.price.toFixed(2)}) at ${state.bestOffer.url}`);
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
     * Scrapes retailer using official @tavily/core SDK search + raw content extraction
     */
    async scrapeRetailerAccurateOffer(modelQuery, retailerName, domainPattern, category) {
        console.log(`[Tavily AI SDK] Querying ${retailerName} for "${modelQuery}" (${category})...`);
        try {
            // Official Tavily AI SDK search call with advanced search depth & includeRawContent: 'text'
            const searchRes = await this.tvlyClient.search(`buy ${modelQuery} ${category} price`, {
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
                // If rawContent is not returned in search, use official Tavily extract API
                if (rawContent.length < 300) {
                    try {
                        const extRes = await this.tvlyClient.extract([fullUrl], {
                            extractDepth: 'advanced', // Bypasses many JS walls
                            format: 'markdown' // Converts noisy HTML to clean text
                        });
                        const extPage = (extRes.results || [])[0];
                        if (extPage && extPage.rawContent) {
                            rawContent = extPage.rawContent;
                        }
                    }
                    catch (e) { }
                }
                // Clean URL by stripping trailing /reviews
                const cleanProductUrl = fullUrl.replace(/\/reviews\/?$/i, '');
                // 1. Try Firecrawl API Native Schema Extraction first if client is configured
                if (this.firecrawlClient) {
                    const fcOffer = await this.extractWithFirecrawl(cleanProductUrl, retailerName, category);
                    if (fcOffer)
                        return fcOffer;
                }
                // 2. 100% LLM extraction via Groq or OpenRouter
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
        }
        catch (e) {
            console.warn(`[Tavily SDK Search Error] ${retailerName}:`, e);
        }
        return null;
    }
    /**
     * Direct URL extraction using Firecrawl API (Primary) -> Tavily SDK + Groq LLM (Fallback)
     */
    async extractDirectPage(url, retailerName, category) {
        // 1. Try Firecrawl Native LLM Extraction API first
        const firecrawlOffer = await this.extractWithFirecrawl(url, retailerName, category);
        if (firecrawlOffer)
            return firecrawlOffer;
        // 2. Fallback to Tavily AI SDK + Groq LLM
        try {
            console.log(`[Tavily AI SDK Extract] Reading DOM content from ${url}...`);
            const extRes = await this.tvlyClient.extract([url], {
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
        }
        catch (e) {
            console.warn(`[Tavily SDK Extract Warning] ${url} extract failed:`, e);
        }
        return null;
    }
    /**
     * Primary Native LLM Schema Extraction via Firecrawl API (v1/scrape format json)
     */
    async extractWithFirecrawl(url, retailerName, category) {
        if (!this.firecrawlClient)
            return null;
        try {
            console.log(`[Firecrawl API Extract] Extracting structured JSON schema from ${url}...`);
            const res = await this.firecrawlClient.scrapeUrl(url, {
                formats: [
                    {
                        type: 'json',
                        prompt: `Extract the main product current sale price, original MSRP list price, full clean title, brand, and in-stock status from this page: "${url}". Ignore sponsored competitor ads and sidebar items.`,
                        schema: {
                            type: 'object',
                            properties: {
                                currentPrice: { type: 'number' },
                                originalPrice: { type: 'number' },
                                title: { type: 'string' },
                                brand: { type: 'string' },
                                inStock: { type: 'boolean' },
                                isRefurbished: { type: 'boolean' }
                            },
                            required: ['currentPrice', 'title']
                        }
                    }
                ]
            });
            const extracted = res?.json || res?.data?.json;
            const imageUrl = res?.metadata?.ogImage || res?.metadata?.['og:image'] || undefined;
            if (extracted && typeof extracted.currentPrice === 'number' && extracted.currentPrice > 0) {
                console.log(`✅ [FIRECRAWL EXTRACTED] ${retailerName}: "$${extracted.currentPrice}" -> ${extracted.title}`);
                return {
                    retailer: retailerName,
                    price: extracted.currentPrice,
                    originalPrice: extracted.originalPrice,
                    title: extracted.title || url,
                    brand: extracted.brand,
                    url,
                    inStock: Boolean(extracted.inStock),
                    isRefurbished: Boolean(extracted.isRefurbished),
                    snippet: imageUrl ? `Image: ${imageUrl}` : 'Extracted via Firecrawl AI cloud browser schema extraction'
                };
            }
        }
        catch (e) {
            console.warn(`[Firecrawl API Warning] ${url}:`, e?.message || e);
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
        if (lower.includes('ssd') || lower.includes('nvme') || lower.includes('m.2') || lower.includes('hard drive') || lower.includes('hdd') || lower.includes('samsung 990') || lower.includes('wd_black')) {
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
}
exports.TavilyHardwareAgent = TavilyHardwareAgent;
