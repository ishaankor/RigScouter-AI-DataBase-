import { TavilyHardwareAgent } from '../src/agent.js';
import { supabase } from '../src/supabase.js';
import dotenv from 'dotenv';

dotenv.config();

async function verifyLiveUrls() {
  console.log('🔍 Running live URL price verification against Supabase DB items...\n');

  const { data: items } = await supabase
    .from('hardware_components')
    .select('id, name, model, current_price, retailer, product_url')
    .order('updated_at', { ascending: false })
    .limit(5);

  if (!items || items.length === 0) {
    console.log('No components found in DB.');
    return;
  }

  const agent = new TavilyHardwareAgent();

  for (const item of items) {
    console.log(`--------------------------------------------------`);
    console.log(`DB Item: "${item.name}"`);
    console.log(`DB Price: $${item.current_price} @ ${item.retailer}`);
    console.log(`URL: ${item.product_url}`);

    try {
      const state = await agent.run(item.product_url);
      const offer = state.bestOffer;

      if (offer) {
        console.log(`\n✅ RE-EXTRACTED PRICE: $${offer.price.toFixed(2)}`);
        console.log(`   Title: "${offer.title}"`);
        console.log(`   In Stock: ${offer.inStock}`);
        if (Math.abs(offer.price - item.current_price) < 1.0) {
          console.log(`   🎯 VERIFIED MATCH! Extracted price matches DB price.`);
        } else {
          console.log(`   ℹ️  Price difference detected: DB was $${item.current_price}, live re-extraction is $${offer.price.toFixed(2)}`);
        }
      } else {
        console.log(`   ❌ Could not re-extract offer from URL.`);
      }
    } catch (e: any) {
      console.error(`   Error verifying URL:`, e?.message || e);
    }
    console.log(`--------------------------------------------------\n`);
  }
}

verifyLiveUrls().catch(console.error);
