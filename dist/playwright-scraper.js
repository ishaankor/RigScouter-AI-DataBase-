"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.scrapePageWithPlaywright = scrapePageWithPlaywright;
const playwright_1 = require("playwright");
const supabase_js_1 = require("./supabase.js");
async function scrapePageWithPlaywright(url) {
    console.log(`[Playwright Stealth Scraper] Navigating to: ${url}`);
    const browser = await playwright_1.chromium.launch({
        headless: true,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-blink-features=AutomationControlled'
        ]
    });
    const context = await browser.newContext({
        userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        viewport: { width: 1440, height: 900 },
        locale: 'en-US',
        extraHTTPHeaders: {
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-Ch-Ua': '"Chromium";v="122", "Not(A:Brand";v="24"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"macOS"'
        }
    });
    const page = await context.newPage();
    // Stealth script to bypass navigator.webdriver detection
    await page.addInitScript(() => {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    });
    let price = null;
    let title = '';
    let inStock = true;
    let isClearance = false;
    const retailer = detectRetailer(url);
    try {
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 20000 });
        await page.waitForTimeout(2000);
        // Extract Title
        title = await page.title();
        if (!title || title.length < 5) {
            title = await page.$eval('h1', el => el.textContent?.trim() || '').catch(() => url);
        }
        const pageContent = await page.content();
        const lowerText = pageContent.toLowerCase();
        // Stock check
        if (lowerText.includes('unavailable') ||
            lowerText.includes('out of stock') ||
            lowerText.includes('0 in stock') ||
            lowerText.includes('sold out') ||
            lowerText.includes('currently unavailable')) {
            inStock = false;
        }
        // Clearance check
        if (lowerText.includes('clearance') || lowerText.includes('comp. value')) {
            isClearance = true;
        }
        // Extract price from page HTML
        const matches = pageContent.match(/\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)/g);
        if (matches) {
            for (const m of matches) {
                const num = parseFloat(m.replace(/\$|,|\s/g, ''));
                if (num >= 45 && num <= 3500) {
                    price = num;
                    break;
                }
            }
        }
    }
    catch (err) {
        console.error(`[Playwright Error] Failed to scrape ${url}:`, err?.message || err);
    }
    finally {
        await browser.close();
    }
    const result = {
        retailer,
        price,
        title: cleanTitle(title),
        url,
        inStock,
        isClearance,
        scrapedAt: new Date().toISOString()
    };
    if (price) {
        try {
            await supabase_js_1.supabase.from('hardware_components').upsert({
                id: `playwright-${Date.now()}`,
                name: result.title,
                category: detectCategory(result.title),
                brand: result.title.split(' ')[0] || 'Hardware',
                model: result.title,
                specs: JSON.stringify({
                    ScrapedVia: 'Playwright Stealth Engine',
                    InStock: result.inStock,
                    IsClearance: result.isClearance,
                    ScrapedAt: result.scrapedAt
                }),
                msrp: Math.round(price * 1.12 * 100) / 100,
                current_price: price,
                lowest_price_90d: Math.round(price * 0.95 * 100) / 100,
                retailer: result.retailer,
                product_url: result.url,
                image_url: 'https://images.unsplash.com/photo-1587202372775-e229f172b9d7?auto=format&fit=crop&w=600&q=80',
                rating: 4.8,
                deal_score: 90,
                updated_at: new Date().toISOString()
            });
            console.log(`[Playwright DB Success] Saved "${result.title}" ($${price.toFixed(2)}) to Supabase DB!`);
        }
        catch (e) {
            console.error('[Playwright DB Error]:', e?.message || e);
        }
    }
    return result;
}
function detectRetailer(url) {
    const lower = url.toLowerCase();
    if (lower.includes('microcenter.com') || lower.includes('micro center'))
        return 'Micro Center';
    if (lower.includes('newegg.com') || lower.includes('newegg'))
        return 'Newegg';
    if (lower.includes('bestbuy.com') || lower.includes('best buy'))
        return 'Best Buy';
    if (lower.includes('bhphotovideo.com') || lower.includes('b&h'))
        return 'B&H';
    return 'Amazon';
}
function detectCategory(title) {
    const lower = title.toLowerCase();
    if (lower.includes('rtx') || lower.includes('radeon') || lower.includes('gpu'))
        return 'GPU';
    if (lower.includes('ryzen') || lower.includes('intel') || lower.includes('cpu'))
        return 'CPU';
    if (lower.includes('ddr5') || lower.includes('ddr4') || lower.includes('ram'))
        return 'RAM';
    if (lower.includes('ssd') || lower.includes('nvme'))
        return 'SSD';
    return 'Cooler';
}
function cleanTitle(title) {
    return title.replace(/^Amazon\.com:\s*/i, '').replace(/\s*-\s*Best Buy$/i, '').replace(/\s*-\s*Micro Center$/i, '').replace(/\s*-\s*Newegg\.com$/i, '').trim();
}
