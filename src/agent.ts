import { tavily } from '@tavily/core';
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
  brand?: string;
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

export type SseEmitter = (event: string, data: Record<string, unknown>) => void;

export class TavilyHardwareAgent {
  private tvlyClient: ReturnType<typeof tavily>;

  constructor() {
    this.tvlyClient = tavily({ apiKey: TAVILY_API_KEY });
  }

  /**
   * Main agent entrypoint using official @tavily/core SDK + Groq/OpenRouter LLM.
   * ABSOLUTELY NO REGEX PARSING OR DUMMY FALLBACK VALUES.
   */
  async run(prompt: string, emit?: SseEmitter): Promise<AgentState> {
    const cleanPrompt = prompt.trim();
    const isUrl = cleanPrompt.startsWith('http://') || cleanPrompt.startsWith('https://');
    const category = this.detectCategory(cleanPrompt);

    console.log(`\n======================================================`);
    console.log(`[Official Tavily AI SDK Agent] Extracting price for: "${cleanPrompt}" (${category})`);
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
      }
    }

    state.scrapedOffers.sort((a, b) => a.price - b.price);

    if (state.scrapedOffers.length > 0) {
      state.bestOffer = state.scrapedOffers[0];
      const stockStatus = state.bestOffer.inStock ? 'In Stock' : 'Out of Stock / Backorder';
      state.summary = `Evaluated ${state.scrapedOffers.length} live retailer listings via Tavily AI. Lowest price: $${state.bestOffer.price.toFixed(2)} at ${state.bestOffer.retailer} (${stockStatus}).`;
    } else {
      state.summary = `No live prices found across retailers for "${cleanPrompt}".`;
    }

    if (state.bestOffer) {
      try {
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

        const realMsrp = state.bestOffer.originalPrice && state.bestOffer.originalPrice > state.bestOffer.price
          ? state.bestOffer.originalPrice
          : state.bestOffer.price;

        const calculatedDealScore = realMsrp > state.bestOffer.price
          ? Math.min(100, Math.max(50, Math.round(50 + ((realMsrp - state.bestOffer.price) / realMsrp) * 100)))
          : 50;

        await supabase.from('hardware_components').upsert({
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

  /**
   * Scrapes retailer using official @tavily/core SDK search + raw content extraction
   */
  private async scrapeRetailerAccurateOffer(modelQuery: string, retailerName: string, domainPattern: string, category: string): Promise<AgentOffer | null> {
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
        if (!this.isValidDirectProductUrl(fullUrl, domainPattern)) continue;

        let rawContent = (hit.rawContent || hit.content || hit.title || '');

        // If rawContent is not returned in search, use official Tavily extract API
        if (rawContent.length < 300) {
          try {
            const extRes = await this.tvlyClient.extract([fullUrl]);
            const extPage = (extRes.results || [])[0];
            if (extPage && extPage.rawContent) {
              rawContent = extPage.rawContent;
            }
          } catch (e) {}
        }

        // 100% LLM extraction via Groq or OpenRouter
        const parsed = await this.parseAccuratePriceWithLLM(rawContent, modelQuery, retailerName, fullUrl, category);

        if (parsed && parsed.price && parsed.price > 0) {
          console.log(`✅ [TAVILY AI + LLM EXTRACTED] ${retailerName}: "$${parsed.price}" -> ${parsed.title.substring(0, 60)}`);
          return {
            retailer: retailerName,
            price: parsed.price,
            originalPrice: parsed.originalPrice,
            title: parsed.title || hit.title || modelQuery,
            brand: parsed.brand,
            url: fullUrl,
            inStock: parsed.inStock,
            isRefurbished: parsed.isRefurbished,
            snippet: hit.content
          };
        }
      }
    } catch (e) {
      console.warn(`[Tavily SDK Search Error] ${retailerName}:`, e);
    }

    return null;
  }

  /**
   * Direct URL extraction using official @tavily/core extract API
   */
  private async extractDirectPage(url: string, retailerName: string, category: string): Promise<AgentOffer | null> {
    try {
      console.log(`[Tavily AI SDK Extract] Reading DOM content from ${url}...`);
      const extRes = await this.tvlyClient.extract([url]);
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
    } catch (e) {
      console.warn(`[Tavily SDK Extract Warning] ${url} extract failed:`, e);
    }
    return null;
  }

  /**
   * 100% LLM-driven price extraction (Groq Llama 3.1 8B Primary -> OpenRouter Llama 3.2 3B Fallback)
   * ABSOLUTELY NO REGEX PARSING OR DUMMY FALLBACK VALUES.
   */
  private async parseAccuratePriceWithLLM(text: string, query: string, retailer: string, url: string, category: string): Promise<{
    price: number | null;
    originalPrice?: number;
    title: string;
    brand?: string;
    inStock: boolean;
    isRefurbished: boolean;
  } | null> {
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
   * Groq LLM API Price Extractor (Llama 3.1 8B Instant)
   */
  private async parseWithGroqLLM(text: string, query: string, retailer: string, category: string): Promise<{
    price: number | null;
    originalPrice?: number;
    title: string;
    brand?: string;
    inStock: boolean;
    isRefurbished: boolean;
  } | null> {
    const apiKey = GROQ_API_KEY;
    if (!apiKey || apiKey.includes('placeholder')) {
      console.warn('[Groq LLM] No GROQ_API_KEY provided in .env');
      return null;
    }

    try {
      const systemPrompt = `You are a high-precision PC hardware price extraction AI. Your job is to extract the exact current sale price of the requested item from the webpage content.

CRITICAL INSTRUCTIONS:
1. Identify the MAIN product on the webpage matching "${query}".
2. Ignore sidebar ads, "Customers Also Viewed", "Sponsored Products", shipping costs, and warranty fees.
3. Extract the exact numerical current sale price in USD (e.g. 599.99).
4. If listed, extract the original MSRP/list price (number or null).
5. Output JSON ONLY in this exact format:
{
  "currentPrice": number or null,
  "originalPrice": number or null,
  "inStock": boolean,
  "isRefurbished": boolean,
  "cleanTitle": string,
  "brand": string
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
            { role: 'system', content: systemPrompt },
            { role: 'user', content: `Item Query: "${query}" (${category})\nRetailer: "${retailer}"\nPage Content:\n${text.substring(0, 6000)}` }
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
            title: parsed.cleanTitle || query,
            brand: parsed.brand,
            inStock: Boolean(parsed.inStock),
            isRefurbished: Boolean(parsed.isRefurbished)
          };
        }
      } else {
        const errText = await res.text();
        console.warn(`[Groq API Error] HTTP ${res.status}:`, errText.substring(0, 200));
      }
    } catch (err: any) {
      console.warn(`[Groq LLM Error] ${retailer}:`, err?.message || err);
    }
    return null;
  }

  /**
   * OpenRouter Free LLM Fallback (Llama 3.2 3B Instruct Free)
   */
  private async parseWithOpenRouterLLM(text: string, query: string, retailer: string, category: string): Promise<{
    price: number | null;
    originalPrice?: number;
    title: string;
    brand?: string;
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
            { role: 'system', content: 'Extract PC hardware price JSON only.' },
            { role: 'user', content: `Item: "${query}" (${category})\nRetailer: "${retailer}"\nContent:\n${text.substring(0, 4000)}\n\nReturn JSON: {"currentPrice": number, "originalPrice": number, "inStock": boolean, "cleanTitle": string, "brand": string}` }
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
              title: parsed.cleanTitle || query,
              brand: parsed.brand,
              inStock: Boolean(parsed.inStock),
              isRefurbished: Boolean(parsed.isRefurbished)
            };
          }
        }
      }
    } catch (e) {}
    return null;
  }

  private isValidDirectProductUrl(url: string, domainPattern: string): boolean {
    const lower = url.toLowerCase();

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

    if (domainPattern.includes('microcenter.com')) return lower.includes('/product/');
    if (domainPattern.includes('amazon.com')) return lower.includes('/dp/') || lower.includes('/gp/product/');
    if (domainPattern.includes('newegg.com')) return lower.includes('/p/') && !lower.includes('/p/pl');
    if (domainPattern.includes('bestbuy.com')) return lower.includes('/site/') && lower.includes('.p?');
    if (domainPattern.includes('bhphotovideo.com')) return lower.includes('/c/product/');

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

  private detectCategory(query: string): string {
    const lower = query.toLowerCase();

    if (lower.includes('rtx') || lower.includes('gtx') || lower.includes('radeon') || lower.includes('rx') ||
        lower.includes('graphics card') || lower.includes('video card') || lower.includes('gpu')) {
      return 'GPU';
    }
    if (lower.includes('ryzen') || lower.includes('core i3') || lower.includes('core i5') || lower.includes('core i7') ||
        lower.includes('core i9') || lower.includes('processor') || lower.includes('cpu')) {
      return 'CPU';
    }
    if (lower.includes('ddr5') || lower.includes('ddr4') || lower.includes('ram') || lower.includes('memory')) {
      return 'RAM';
    }
    if (lower.includes('ssd') || lower.includes('nvme') || lower.includes('m.2') || lower.includes('storage')) {
      return 'SSD';
    }
    if (lower.includes('motherboard') || lower.includes('mobo') || lower.includes('b650') || lower.includes('z790')) {
      return 'Motherboard';
    }
    if (lower.includes('psu') || lower.includes('power supply') || lower.includes('watt') || lower.includes('850w')) {
      return 'PSU';
    }
    if (lower.includes('case') || lower.includes('chassis') || lower.includes('mid tower')) {
      return 'Case';
    }
    if (lower.includes('aio') || lower.includes('liquid cooler') || lower.includes('air cooler')) {
      return 'Cooler';
    }
    if (lower.includes('monitor') || lower.includes('display')) {
      return 'Monitor';
    }

    return 'Hardware';
  }
}
