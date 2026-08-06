import { supabase } from '../src/supabase.js';
import dotenv from 'dotenv';
dotenv.config();

const BAD_IDS = [
  'tavily-llm-agent-1786039693652',  // $159 Newegg insider blog
  'tavily-agent-1785998766154',       // $300 Amazon search page
  'tavily-agent-1785998538111',       // $194.97 Amazon search page
  'tavily-agent-1785998476325',       // $119.71 Best Buy searchpage
  'tavily-agent-1785997031399',       // $509.99 Best Buy searchpage
  'tavily-agent-1785996864427',       // $776 B&H category browse
  'tavily-agent-1785996591724',       // $561 eBay shop browse
];

// Also purge entries with the wrong-product: RTX 5080 for 1080Ti query
const WRONG_PRODUCT_PATTERNS = [
  '%5080%',   // RTX 5080 title
];

async function cleanup() {
  console.log('🧹 Purging bad price entries from Supabase DB...\n');

  // 1. Delete by known bad IDs
  const { error: e1, count: c1 } = await supabase
    .from('hardware_components')
    .delete({ count: 'exact' })
    .in('id', BAD_IDS);

  if (e1) {
    console.error('Error deleting by IDs:', e1.message);
  } else {
    console.log(`✅ Deleted ${c1} bad entries by ID`);
  }

  // 2. Delete entries whose product_url is a search/browse page
  const BAD_URL_PATTERNS = [
    '%/s?k=%',
    '%searchpage.jsp%',
    '%/shop/%',
    '%/c/buy/%',
    '%/insider/%',
    '%ebay.com/shop%',
    '%amazon.com%/s?%',
  ];

  for (const pattern of BAD_URL_PATTERNS) {
    const { error, count } = await supabase
      .from('hardware_components')
      .delete({ count: 'exact' })
      .like('product_url', pattern);

    if (error) {
      console.error(`Error deleting pattern ${pattern}:`, error.message);
    } else if (count && count > 0) {
      console.log(`✅ Deleted ${count} entries matching URL pattern: ${pattern}`);
    }
  }

  // 3. Delete entries where name contains "RTX 5080" but model contains "1080"
  const { data: wrongProducts } = await supabase
    .from('hardware_components')
    .select('id, name, model, current_price')
    .like('name', '%5080%');

  if (wrongProducts && wrongProducts.length > 0) {
    const wrongIds = wrongProducts
      .filter(r => r.model && r.model.toLowerCase().includes('1080'))
      .map(r => r.id);

    if (wrongIds.length > 0) {
      const { count } = await supabase
        .from('hardware_components')
        .delete({ count: 'exact' })
        .in('id', wrongIds);
      console.log(`✅ Deleted ${count} wrong-product entries (5080 for 1080Ti query)`);
    }
  }

  // 4. Delete prices that are impossibly low for GPUs or CPUs
  const { data: lowPriced } = await supabase
    .from('hardware_components')
    .select('id, category, current_price, name')
    .or('and(category.eq.GPU,current_price.lt.80),and(category.eq.CPU,current_price.lt.100)');

  if (lowPriced && lowPriced.length > 0) {
    console.log('\n⚠️  Suspiciously low prices found:');
    for (const r of lowPriced) {
      console.log(`  [${r.category}] $${r.current_price} — ${r.name?.substring(0, 60)}`);
    }
    const lowIds = lowPriced.map(r => r.id);
    const { count } = await supabase
      .from('hardware_components')
      .delete({ count: 'exact' })
      .in('id', lowIds);
    console.log(`✅ Deleted ${count} impossibly-low-price entries`);
  }

  // 5. Show what remains
  console.log('\n📊 Remaining DB entries after cleanup:');
  const { data: remaining } = await supabase
    .from('hardware_components')
    .select('id, name, category, model, current_price, retailer, product_url, updated_at')
    .order('updated_at', { ascending: false });

  for (const r of (remaining || [])) {
    const url = r.product_url || '';
    const isProductPage = url.includes('/dp/') || url.includes('/p/N82E') || url.includes('/product/') || url.includes('/c/product/');
    console.log(`  [${r.category}] ${r.model} @ ${r.retailer}: $${r.current_price} ${isProductPage ? '✅' : '⚠️ '}`);
    console.log(`         ${url.substring(0, 80)}`);
  }

  console.log(`\nTotal remaining: ${remaining?.length || 0}`);
}

cleanup().catch(console.error);
