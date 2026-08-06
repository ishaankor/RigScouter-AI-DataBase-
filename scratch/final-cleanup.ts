import { supabase } from '../src/supabase.js';
import dotenv from 'dotenv';
dotenv.config();

async function finalClean() {
  console.log('🧹 Final targeted cleanup...\n');

  // 1. RTX 4070 Super @ Best Buy reviews page
  const { count: c1 } = await supabase
    .from('hardware_components')
    .delete({ count: 'exact' })
    .like('product_url', '%bestbuy.com/site/reviews/%');
  console.log('Deleted Best Buy reviews entries:', c1);

  // 2. RTX 4080 Super @ Amazon $120 — this is an Amazon /dp/ page but the price is $120
  //    which is impossible for an RTX 4080 Super (MSRP $999+). Remove it.
  const { count: c2 } = await supabase
    .from('hardware_components')
    .delete({ count: 'exact' })
    .ilike('model', '%4080%')
    .lt('current_price', 200);
  console.log('Deleted impossible 4080 Super price entries (<$200):', c2);

  // 3. Ryzen entry where model field = a full URL (old bug)
  const { count: c3 } = await supabase
    .from('hardware_components')
    .delete({ count: 'exact' })
    .like('model', 'https://%');
  console.log('Deleted URL-as-model entries:', c3);

  // Final count
  const { data } = await supabase
    .from('hardware_components')
    .select('model, current_price, retailer, product_url, updated_at')
    .order('updated_at', { ascending: false });

  console.log(`\n✅ Final clean DB (${data?.length || 0} entries):`);
  for (const r of data || []) {
    const url = r.product_url || '';
    const ok = (url.includes('/dp/') || url.includes('/p/N82E') || url.includes('/product/') ||
      (url.includes('/c/product/') && !url.includes('/c/buy/'))) &&
      !url.includes('/reviews/') && !url.includes('searchpage');
    console.log(`  ${ok ? '✅' : '❌'} $${r.current_price.toFixed ? r.current_price.toFixed(2) : r.current_price} @ ${r.retailer} — ${r.model}`);
  }
}

finalClean().catch(console.error);
