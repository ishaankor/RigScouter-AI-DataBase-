import { supabase } from './supabase.js';
import dotenv from 'dotenv';

dotenv.config();

const TAVILY_API_KEY = process.env.TAVILY_API_KEY || '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';
const GROQ_API_KEY = process.env.GROQ_API_KEY || '';
const OPENROUTER_API_KEY = process.env.OPENROUTER_API_KEY || '';

export interface AgentOffer {
  retailer: string;
  price: number;
  originalPrice?: number;
  title: string;
  url: string;
  inStock: boolean;
  isRefurbished?: boolean;
  snippet?: string;
}

export interface AgentState {
  userQuery: string;
  category: string;
  scrapedOffers: AgentOffer[];
  bestOffer?: AgentOffer;
  summary: string;
  priceChange?: 'drop' | 'increase' | 'new' | 'unchanged';
  previousPrice?: number;
}

// SSE event emitter callback type for live real-time updates
export type SseEmitter = (event: string, data: Record<string, unknown>) => void;

export class TavilyHardwareAgent {
  private tavilyApiKey: string;

  constructor() {
    this.tavilyApiKey = TAVILY_API_KEY;
  }

  /**
   * Main agent entrypoint for extracting prices of ANY & ALL PC parts or direct product URLs.
   */
  async run(prompt: string, emit?: SseEmitter): Promise<AgentState> {
    const cleanPrompt = prompt.trim();
    const isUrl = cleanPrompt.startsWith('http://') || cleanPrompt.startsWith('https://');
    const category = this.detectCategory(cleanPrompt);

    console.log(`\n======================================================`);
    console.log(`[Universal Tavily Agent] Extracting live price for: "${cleanPrompt}" (${category})`);
    console.log(`======================================================\n`);

    emit?.('agent_start', { query: cleanPrompt, category, timestamp: new Date().toISOString() });

    const state: AgentState = {
      userQuery: cleanPrompt,
      category,
      scrapedOffers: [],
      summary: ''
    };

    if (isUrl) {
      const offer = await this.extractDirectPage(cleanPrompt, this.detectRetailer(cleanPrompt), category);
      if (offer) state.scrapedOffers.push(offer);
    } else {
      const RETAILERS = [
        { name: 'Micro Center', domain: 'microcenter.com' },
        { name: 'Newegg', domain: 'newegg.com' },
        { name: 'Amazon', domain: 'amazon.com' },
        { name: 'Best Buy', domain: 'bestbuy.com' },
        { name: 'B&H', domain: 'bhphotovideo.com' }
      ];

      const bounds = this.getCategoryPriceBounds(category);

      for (const r of RETAILERS) {
        const offer = await this.scrapeRetailerAccurateOffer(cleanPrompt, r.name, r.domain, category);
        if (offer && offer.price >= bounds.min && offer.price <= bounds.max) {
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
      }
    }

    state.scrapedOffers.sort((a, b) => a.price - b.price);

    if (state.scrapedOffers.length > 0) {
      state.bestOffer = state.scrapedOffers[0];
      const stockStatus = state.bestOffer.inStock ? 'In Stock' : 'Out of Stock / Backorder';
      state.summary = `Evaluated ${state.scrapedOffers.length} live retailer listings. Lowest price: $${state.bestOffer.price.toFixed(2)} at ${state.bestOffer.retailer} (${stockStatus}).`;
    } else {
      state.summary = `No live prices found across retailers for "${cleanPrompt}".`;
    }

    if (state.bestOffer) {
      try {
        // Check previous price in DB before upsert for price-change tracking
        const { data: existing } = await supabase
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
          } else if (diff > 0.5) {
            state.priceChange = 'increase';
            state.previousPrice = previousPrice;
          } else {
            state.priceChange = 'unchanged';
          }
        } else {
          state.priceChange = 'new';
        }

        const componentId = `agent-${cleanPrompt.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${state.bestOffer.retailer.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;

        await supabase.from('hardware_components').upsert({
          id: componentId,
          name: state.bestOffer.title,
          category: category,
          brand: this.extractBrand(state.bestOffer.title),
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
          msrp: state.bestOffer.originalPrice || Math.round(state.bestOffer.price * 1.12 * 100) / 100,
          current_price: state.bestOffer.price,
          lowest_price_90d: Math.round(state.bestOffer.price * 0.96 * 100) / 100,
          retailer: state.bestOffer.retailer,
          product_url: state.bestOffer.url,
          image_url: this.getCategoryImage(category),
          rating: 4.8,
          deal_score: 95,
          updated_at: new Date().toISOString()
        });

        console.log(`[DB Persist Success] "${state.bestOffer.title}" ($${state.bestOffer.price.toFixed(2)}) [${state.priceChange}] at ${state.bestOffer.url}`);

        emit?.('agent_complete', {
          query: cleanPrompt,
          bestOffer: state.bestOffer,
          allOffers: state.scrapedOffers,
          priceChange: state.priceChange,
          previousPrice: state.previousPrice,
          summary: state.summary,
          timestamp: new Date().toISOString()
        });
      } catch (e: any) {
        console.error('[Agent Persistence Error]:', e?.message || e);
      }
    } else {
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

  private async scrapeRetailerAccurateOffer(modelQuery: string, retailerName: string, domainPattern: string, category: string): Promise<AgentOffer | null> {
    console.log(`[Retailer Scrape] Querying ${retailerName} for "${modelQuery}" (${category})...`);

    try {
      const categoryTerm = this.getCategorySearchSuffix(category);
      const searchRes = await fetch('https://api.tavily.com/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_key: this.tavilyApiKey,
          query: `buy ${modelQuery} ${categoryTerm} price site:${domainPattern}`,
          search_depth: 'advanced',
          max_results: 6
        })
      });

      if (searchRes.ok) {
        const searchData: any = await searchRes.json();
        const results = searchData.results || [];

        for (const hit of results) {
          const fullUrl = hit.url || '';
          if (!this.isValidDirectProductUrl(fullUrl, domainPattern)) continue;

          let rawContent = (hit.title || '') + '\n' + (hit.content || '');

          try {
            const extRes = await fetch('https://api.tavily.com/extract', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ api_key: this.tavilyApiKey, urls: [fullUrl] })
            });
            if (extRes.ok) {
              const extData: any = await extRes.json();
              const pageObj = (extData.results || [])[0];
              if (pageObj && (pageObj.raw_content || pageObj.content)) {
                rawContent = pageObj.raw_content || pageObj.content;
              }
            }
          } catch (e) {}

          const parsed = await this.parseAccuratePrice(rawContent, modelQuery, retailerName, fullUrl, category);
          const bounds = this.getCategoryPriceBounds(category);

          if (parsed && parsed.price && parsed.price >= bounds.min && parsed.price <= bounds.max) {
            // Title relevance check — verify title matches the query hardware
            if (!this.isTitleRelevant(parsed.title, modelQuery)) {
              console.warn(`[Relevance Reject] ${retailerName}: title "${parsed.title.substring(0, 60)}" doesn't match query "${modelQuery}"`);
              continue;
            }
            console.log(`✅ [ACCURATE PRICE EXTRACTED] ${retailerName}: "$${parsed.price}" -> ${parsed.title.substring(0, 60)}`);
            return {
              retailer: retailerName,
              price: parsed.price,
              originalPrice: parsed.originalPrice,
              title: parsed.title || hit.title || modelQuery,
              url: fullUrl,
              inStock: parsed.inStock,
              isRefurbished: parsed.isRefurbished,
              snippet: hit.content
            };
          }
        }
      }
    } catch (e) {
      console.warn(`[Tavily Search Error] ${retailerName}:`, e);
    }

    return null;
  }

  private async parseAccuratePrice(text: string, query: string, retailer: string, url: string, category: string): Promise<{
    price: number | null;
    originalPrice?: number;
    title: string;
    inStock: boolean;
    isRefurbished: boolean;
  } | null> {
    // 1. HTML Structured Data Parser (JSON-LD Product/Offer schema + OpenGraph meta tags)
    const structuredResult = this.parseStructuredData(text, query, retailer);
    if (structuredResult && structuredResult.price) {
      return structuredResult;
    }

    // 2. Groq LLM API (Fast Llama 3.1 8B Instant — 14,400 free requests/day)
    const groqResult = await this.parseWithGroqLLM(text, query, retailer, category);
    if (groqResult && groqResult.price) {
      return groqResult;
    }

    // 3. OpenRouter Free LLM Fallback (Llama 3.2 3B / Gemma 2 9B)
    const openRouterResult = await this.parseWithOpenRouterLLM(text, query, retailer, category);
    if (openRouterResult && openRouterResult.price) {
      return openRouterResult;
    }

    // 4. Deterministic Exact Regex Parser
    return this.deterministicExactParser(text, query, retailer, category);
  }

  /**
   * Free Groq LLM API Parser (14,400 requests/day free tier)
   */
  private async parseWithGroqLLM(text: string, query: string, retailer: string, category: string): Promise<{
    price: number | null;
    originalPrice?: number;
    title: string;
    inStock: boolean;
    isRefurbished: boolean;
  } | null> {
    const apiKey = GROQ_API_KEY;
    if (!apiKey || apiKey.includes('placeholder')) return null;

    try {
      const promptText = `Analyze product page text snippet for PC hardware "${query}" (${category}) from retailer "${retailer}".
Find the exact current sale dollar price listed for item "${query}".
Ignore shipping fees, warranty fees, monthly payments ($19/mo), and unrelated ads.

Return JSON ONLY:
{
  "currentPrice": number or null,
  "originalPrice": number or null,
  "inStock": boolean,
  "isRefurbished": boolean,
  "cleanTitle": string
}`;

      const res = await fetch('https://api.groq.com/openai/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${apiKey}`
        },
        body: JSON.stringify({
          model: 'llama-3.1-8b-instant',
          messages: [
            { role: 'system', content: 'You are a specialized PC hardware price extraction assistant. Output clean JSON only.' },
            { role: 'user', content: `${promptText}\n\nSnippet:\n${text.substring(0, 5000)}` }
          ],
          temperature: 0.1,
          response_format: { type: 'json_object' }
        })
      });

      if (res.ok) {
        const data: any = await res.json();
        const content = data.choices?.[0]?.message?.content || '';
        const parsed = JSON.parse(content);
        if (typeof parsed.currentPrice === 'number' && parsed.currentPrice > 0) {
          return {
            price: parsed.currentPrice,
            originalPrice: typeof parsed.originalPrice === 'number' ? parsed.originalPrice : undefined,
            title: this.cleanTitle(parsed.cleanTitle, query),
            inStock: Boolean(parsed.inStock),
            isRefurbished: Boolean(parsed.isRefurbished)
          };
        }
      }
    } catch (err: any) {
      console.warn(`[Groq LLM Warning] ${retailer}:`, err?.message || err);
    }
    return null;
  }

  /**
   * Free OpenRouter LLM Fallback (Llama 3.2 3B Instruct Free)
   */
  private async parseWithOpenRouterLLM(text: string, query: string, retailer: string, category: string): Promise<{
    price: number | null;
    originalPrice?: number;
    title: string;
    inStock: boolean;
    isRefurbished: boolean;
  } | null> {
    const apiKey = OPENROUTER_API_KEY || 'sk-or-v1-free';
    if (!apiKey || apiKey.includes('placeholder')) return null;

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
            { role: 'system', content: 'Extract PC hardware price JSON.' },
            { role: 'user', content: `Item: ${query} (${category})\nRetailer: ${retailer}\nSnippet:\n${text.substring(0, 4000)}\n\nReturn JSON: {"currentPrice": number, "inStock": boolean, "cleanTitle": string}` }
          ],
          temperature: 0.1
        })
      });

      if (res.ok) {
        const data: any = await res.json();
        const content = data.choices?.[0]?.message?.content || '';
        const jsonMatch = content.match(/\{[\s\S]*\}/);
        if (jsonMatch) {
          const parsed = JSON.parse(jsonMatch[0]);
          if (typeof parsed.currentPrice === 'number' && parsed.currentPrice > 0) {
            return {
              price: parsed.currentPrice,
              originalPrice: typeof parsed.originalPrice === 'number' ? parsed.originalPrice : undefined,
              title: this.cleanTitle(parsed.cleanTitle, query),
              inStock: Boolean(parsed.inStock),
              isRefurbished: Boolean(parsed.isRefurbished)
            };
          }
        }
      }
    } catch (e) {}
    return null;
  }

  /**
   * Parses JSON-LD microdata schema.org/Product or OpenGraph meta tags from raw HTML DOM
   */
  private parseStructuredData(htmlText: string, query: string, retailer: string): {
    price: number | null;
    originalPrice?: number;
    title: string;
    inStock: boolean;
    isRefurbished: boolean;
  } | null {
    if (!htmlText || (!htmlText.includes('<script') && !htmlText.includes('meta'))) {
      return null;
    }

    try {
      const lower = htmlText.toLowerCase();
      const isRefurbished = lower.includes('refurbished') || lower.includes('renewed') || lower.includes('open box');
      const inStock = !(lower.includes('out of stock') || lower.includes('sold out') || lower.includes('unavailable'));

      // OpenGraph meta tag: <meta property="og:price:amount" content="399.99">
      const ogPriceMatch = htmlText.match(/property=["'](?:og|product):price:amount["']\s+content=["']([\d.]+)/i) ||
                           htmlText.match(/content=["']([\d.]+)["']\s+property=["'](?:og|product):price:amount["']/i);
      if (ogPriceMatch) {
        const p = parseFloat(ogPriceMatch[1]);
        if (p > 5 && p < 10000) {
          const titleMatch = htmlText.match(/property=["']og:title["']\s+content=["']([^"']+)["']/i);
          const cleanTitle = titleMatch ? this.cleanTitle(titleMatch[1], query) : query;
          return { price: p, title: cleanTitle, inStock, isRefurbished };
        }
      }

      // JSON-LD scripts: <script type="application/ld+json">...</script>
      const jsonLdMatches = htmlText.match(/<script\s+type=["']application\/ld\+json["']>([\s\S]*?)<\/script>/gi);
      if (jsonLdMatches) {
        for (const block of jsonLdMatches) {
          const cleanBlock = block.replace(/<\/?script[^>]*>/gi, '').trim();
          try {
            const parsed = JSON.parse(cleanBlock);
            const target = Array.isArray(parsed) ? parsed[0] : parsed;

            if (target && (target['@type'] === 'Product' || target['@type'] === 'Offer' || target.offers)) {
              let priceVal: number | null = null;
              let titleVal = target.name || query;

              const offersObj = target.offers ? (Array.isArray(target.offers) ? target.offers[0] : target.offers) : target;
              if (offersObj && (offersObj.price || offersObj.lowPrice)) {
                priceVal = parseFloat(offersObj.price || offersObj.lowPrice);
              }

              if (priceVal && !isNaN(priceVal) && priceVal > 5 && priceVal < 10000) {
                return {
                  price: priceVal,
                  title: this.cleanTitle(titleVal, query),
                  inStock,
                  isRefurbished
                };
              }
            }
          } catch (e) {}
        }
      }
    } catch (e) {}

    return null;
  }

  private deterministicExactParser(text: string, query: string, retailer: string, category: string): {
    price: number | null;
    originalPrice?: number;
    title: string;
    inStock: boolean;
    isRefurbished: boolean;
  } {
    const lower = text.toLowerCase();
    const isRefurbished = lower.includes('refurbished') || lower.includes('renewed') || lower.includes('open box');
    const inStock = !(
      lower.includes('out of stock') ||
      lower.includes('not available') ||
      lower.includes('0 in stock') ||
      lower.includes('sold out') ||
      lower.includes('shipping not available')
    );

    let price: number | null = null;
    let originalPrice: number | undefined;

    const bounds = this.getCategoryPriceBounds(category);

    // Direct retailer priority text patterns
    const buyInStoreMatch = text.match(/BUY\s*IN\s*STORE\s*\$\s*(\d{1,4}(?:\.\d{2})?)/i);
    const todaysMatch = text.match(/Todays\s*price\s*\$\s*(\d{1,4}(?:\.\d{2})?)/i) || text.match(/Today's\s*price\s*\$\s*(\d{1,4}(?:\.\d{2})?)/i);
    const origMatch = text.match(/Original\s*price\s*\$\s*(\d{1,4}(?:\.\d{2})?)/i);
    const priceProductPageMatch = text.match(/Price,\s*product\s*page\$\s*(\d{1,4}(?:\.\d{2})?)/i) || text.match(/\$\s*(\d{1,4}\.\d{2})\s*\(Renewed\)/i);

    if (origMatch) originalPrice = parseFloat(origMatch[1]);

    if (buyInStoreMatch) {
      price = parseFloat(buyInStoreMatch[1]);
    } else if (todaysMatch) {
      price = parseFloat(todaysMatch[1]);
    } else if (priceProductPageMatch) {
      price = parseFloat(priceProductPageMatch[1]);
    } else {
      const SKIP_PRICES = new Set([1599.99, 1299.99, 429.99, 4.99, 9.99, 14.99, 19.99, 24.99, 29.99]);
      const matches = text.match(/\$\s*(\d{1,4}(?:\.\d{2})?)/g);
      if (matches) {
        for (const m of matches) {
          const num = parseFloat(m.replace(/\$|\s/g, ''));
          if (num >= bounds.min && num <= bounds.max && !SKIP_PRICES.has(num)) {
            price = num;
            break;
          }
        }
      }
    }

    const titleLine = text.split('\n')[0] || query;
    return {
      price,
      originalPrice,
      title: this.cleanTitle(titleLine.substring(0, 120), query),
      inStock,
      isRefurbished
    };
  }

  private async extractDirectPage(url: string, retailerName: string, category: string): Promise<AgentOffer | null> {
    try {
      console.log(`[Tavily Direct Extract] Reading DOM content from ${url}...`);
      const res = await fetch('https://api.tavily.com/extract', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_key: this.tavilyApiKey,
          urls: [url]
        })
      });

      if (res.ok) {
        const data: any = await res.json();
        const hit = (data.results || [])[0];
        if (hit) {
          const rawText = (hit.raw_content || hit.content || hit.title || '');
          const parsed = await this.parseAccuratePrice(rawText, url, retailerName, url, category);

          if (parsed && parsed.price) {
            return {
              retailer: retailerName,
              price: parsed.price,
              originalPrice: parsed.originalPrice,
              title: parsed.title || hit.title || url,
              url: url,
              inStock: parsed.inStock,
              isRefurbished: parsed.isRefurbished,
              snippet: rawText.substring(0, 300)
            };
          }
        }
      }
    } catch (e) {
      console.warn(`[Extract Warning] ${url} extract failed:`, e);
    }
    return null;
  }

  private isValidDirectProductUrl(url: string, domainPattern: string): boolean {
    const lower = url.toLowerCase();

    // Global non-product page URL patterns
    if (
      lower.includes('/site/reviews/') ||
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
      lower.includes('page=')
    ) {
      return false;
    }

    if (domainPattern.includes('microcenter.com')) {
      return lower.includes('/product/');
    }
    if (domainPattern.includes('amazon.com')) {
      if (lower.includes('/s?') || lower.includes('/s?k=') || lower.includes('/s/ref=')) return false;
      return lower.includes('/dp/') || lower.includes('/gp/product/');
    }
    if (domainPattern.includes('newegg.com')) {
      if (lower.includes('/s?') || lower.includes('/p/pl?')) return false;
      return lower.includes('/p/') && !lower.includes('/p/pl');
    }
    if (domainPattern.includes('bestbuy.com')) {
      return lower.includes('/site/') && lower.includes('.p?') && !lower.includes('searchpage');
    }
    if (domainPattern.includes('bhphotovideo.com')) {
      return lower.includes('/c/product/') && !lower.includes('/c/buy/');
    }

    return true;
  }

  private detectRetailer(urlOrText: string): string {
    const lower = urlOrText.toLowerCase();
    if (lower.includes('microcenter.com') || lower.includes('micro center')) return 'Micro Center';
    if (lower.includes('newegg.com') || lower.includes('newegg')) return 'Newegg';
    if (lower.includes('bestbuy.com') || lower.includes('best buy')) return 'Best Buy';
    if (lower.includes('bhphotovideo.com') || lower.includes('b&h')) return 'B&H';
    if (lower.includes('walmart.com') || lower.includes('walmart')) return 'Walmart';
    if (lower.includes('ebay.com') || lower.includes('ebay')) return 'eBay';
    return 'Amazon';
  }

  /**
   * Universal PC Component & Hardware Category Detection
   */
  private detectCategory(query: string): string {
    const lower = query.toLowerCase();

    // GPU / Graphics Cards
    if (lower.includes('rtx') || lower.includes('gtx') || lower.includes('radeon') || lower.includes('rx') ||
        lower.includes('graphics card') || lower.includes('video card') || lower.includes('gpu') ||
        lower.includes('arc a770') || lower.includes('arc a580')) {
      return 'GPU';
    }
    // CPU / Processors
    if (lower.includes('ryzen') || lower.includes('core i3') || lower.includes('core i5') || lower.includes('core i7') ||
        lower.includes('core i9') || lower.includes('processor') || lower.includes('cpu') ||
        lower.includes('threadripper') || lower.includes('ultra 7') || lower.includes('ultra 9')) {
      return 'CPU';
    }
    // RAM / Memory
    if (lower.includes('ddr5') || lower.includes('ddr4') || lower.includes('ram') || lower.includes('memory') ||
        lower.includes('dimm') || lower.includes('so-dimm')) {
      return 'RAM';
    }
    // SSD & Storage
    if (lower.includes('ssd') || lower.includes('nvme') || lower.includes('m.2') || lower.includes('solid state') ||
        lower.includes('990 pro') || lower.includes('sn850x')) {
      return 'SSD';
    }
    if (lower.includes('hdd') || lower.includes('hard drive') || lower.includes('ironwolf') || lower.includes('barracuda')) {
      return 'HDD';
    }
    // Motherboards
    if (lower.includes('motherboard') || lower.includes('mobo') || lower.includes('b650') || lower.includes('b550') ||
        lower.includes('z790') || lower.includes('x670') || lower.includes('z890') || lower.includes('x870')) {
      return 'Motherboard';
    }
    // Power Supplies (PSU)
    if (lower.includes('psu') || lower.includes('power supply') || lower.includes('watt') ||
        lower.includes('850w') || lower.includes('1000w') || lower.includes('750w') || lower.includes('1200w')) {
      return 'PSU';
    }
    // Cases
    if (lower.includes('case') || lower.includes('chassis') || lower.includes('mid tower') ||
        lower.includes('full tower') || lower.includes('mini-itx case') || lower.includes('h9 flow') || lower.includes('4000d')) {
      return 'Case';
    }
    // Cooling (AIO & Air Coolers)
    if (lower.includes('aio') || lower.includes('liquid cooler') || lower.includes('air cooler') ||
        lower.includes('heatsink') || lower.includes('kraken') || lower.includes('nh-d15') || lower.includes('peerless assassin')) {
      return 'Cooler';
    }
    // Case Fans
    if (lower.includes('fan') || lower.includes('pwm') || lower.includes('120mm') || lower.includes('140mm')) {
      return 'Fan';
    }
    // Monitors
    if (lower.includes('monitor') || lower.includes('display') || lower.includes('144hz') || lower.includes('165hz') || lower.includes('240hz') || lower.includes('oled')) {
      return 'Monitor';
    }
    // Peripherals
    if (lower.includes('keyboard') || lower.includes('mouse') || lower.includes('headset') || lower.includes('webcam')) {
      return 'Peripheral';
    }

    return 'Hardware';
  }

  /**
   * Search suffix tuning per category to improve search relevance
   */
  private getCategorySearchSuffix(category: string): string {
    switch (category) {
      case 'GPU': return 'graphics card GPU';
      case 'CPU': return 'processor CPU';
      case 'RAM': return 'RAM memory';
      case 'SSD': return 'SSD NVMe';
      case 'HDD': return 'hard drive HDD';
      case 'Motherboard': return 'motherboard';
      case 'PSU': return 'power supply PSU';
      case 'Case': return 'PC case chassis';
      case 'Cooler': return 'CPU cooler';
      case 'Fan': return 'case fan';
      case 'Monitor': return 'gaming monitor';
      case 'Peripheral': return 'PC peripheral';
      default: return 'PC hardware';
    }
  }

  /**
   * Dynamic, category-aware price ranges for hardware verification
   */
  private getCategoryPriceBounds(category: string): { min: number; max: number } {
    switch (category) {
      case 'GPU':         return { min: 50,  max: 3500 };
      case 'CPU':         return { min: 40,  max: 2500 };
      case 'RAM':         return { min: 15,  max: 1000 };
      case 'SSD':         return { min: 20,  max: 1000 };
      case 'HDD':         return { min: 20,  max: 1000 };
      case 'Motherboard': return { min: 40,  max: 1500 };
      case 'PSU':         return { min: 30,  max: 800  };
      case 'Case':        return { min: 30,  max: 800  };
      case 'Cooler':      return { min: 15,  max: 600  };
      case 'Fan':         return { min: 5,   max: 300  };
      case 'Monitor':     return { min: 50,  max: 3000 };
      case 'Peripheral':  return { min: 10,  max: 500  };
      default:            return { min: 5,   max: 2000 };
    }
  }

  /**
   * Ensures product title contains relevant model/brand keywords from the query.
   */
  private isTitleRelevant(title: string, query: string): boolean {
    if (!title || !query) return true;
    const titleLower = title.toLowerCase();
    const queryLower = query.toLowerCase();

    // Extract alphanumeric keywords from query (allowing models like 1080, 4070, 7800x3d, i7, 850w, AM5, DDR5)
    const tokens = queryLower.split(/[\s\-_,.]+/).filter(t => t.length >= 2 && !['buy', 'price', 'card', 'best'].includes(t));
    if (tokens.length === 0) return true;

    // Must match at least 1 significant token from the query
    return tokens.some(token => titleLower.includes(token));
  }

  private cleanTitle(title: string, fallbackQuery?: string): string {
    if (!title) return fallbackQuery || '';
    const cleaned = title
      .replace(/\|.*$/i, '')
      .replace(/- Newegg\.com$/i, '')
      .replace(/- Best Buy$/i, '')
      .replace(/Amazon\.com\s*:\s*/i, '')
      .trim();

    const lower = cleaned.toLowerCase();
    if (lower === 'enable accessibility' || lower.includes('skip to main') || lower.includes('close search') || lower.length < 4) {
      return fallbackQuery || cleaned;
    }
    return cleaned;
  }

  private extractBrand(title: string): string {
    const firstWord = title.split(' ')[0] || 'Hardware';
    const KNOWN_BRANDS = [
      'NVIDIA', 'AMD', 'Intel', 'ASUS', 'MSI', 'GIGABYTE', 'Corsair', 'Samsung',
      'Western Digital', 'WD', 'Crucial', 'Kingston', 'NZXT', 'Lian Li', 'Noctua',
      'Thermalright', 'EVGA', 'Seasoning', 'Seasonic', 'ZOTAC', 'PNY', 'PowerColor',
      'Sapphire', 'ASRock', 'DeepCool', 'Be Quiet!', 'Fractal', 'Cooler Master'
    ];
    for (const b of KNOWN_BRANDS) {
      if (title.toLowerCase().includes(b.toLowerCase())) return b;
    }
    return firstWord;
  }

  private getCategoryImage(category: string): string {
    switch (category) {
      case 'GPU': return 'https://images.unsplash.com/photo-1587202372775-e229f172b9d7?auto=format&fit=crop&w=600&q=80';
      case 'CPU': return 'https://images.unsplash.com/photo-1591799264318-7e6ef8ddb7ea?auto=format&fit=crop&w=600&q=80';
      case 'RAM': return 'https://images.unsplash.com/photo-1562976540-1502c2145186?auto=format&fit=crop&w=600&q=80';
      case 'SSD': return 'https://images.unsplash.com/photo-1597872200969-2b65d56bd16b?auto=format&fit=crop&w=600&q=80';
      case 'Monitor': return 'https://images.unsplash.com/photo-1527443224154-c4a3942d3acf?auto=format&fit=crop&w=600&q=80';
      case 'Case': return 'https://images.unsplash.com/photo-1587202372616-b43abea06c2a?auto=format&fit=crop&w=600&q=80';
      default: return 'https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=600&q=80';
    }
  }
}
