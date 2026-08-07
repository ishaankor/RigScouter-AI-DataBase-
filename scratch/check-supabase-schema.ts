import { supabase } from '../src/supabase';
import dotenv from 'dotenv';

dotenv.config();

async function checkSupabaseSchema() {
  console.log('🔍 Checking Supabase table schemas...');

  // Test watchlist_items insert
  const testWatchItem = {
    id: `test-watch-${Date.now()}`,
    user_id: 'test-user-123',
    component_name: 'Test RTX 4070',
    category: 'GPU',
    target_price: 500,
    current_price: 549.99,
    retailer: 'Amazon',
    product_url: 'https://amazon.com',
    image_url: 'https://images.unsplash.com/photo-1587202372775-e229f172b9d7'
  };

  const { data: insertData, error: insertErr } = await supabase
    .from('watchlist_items')
    .insert([testWatchItem])
    .select();

  console.log('\n--- Watchlist Insert Result ---');
  if (insertErr) {
    console.error('Watchlist insert error:', insertErr);

    // Try camelCase fallback
    const camelTest = {
      id: `test-watch-${Date.now()}`,
      userId: 'test-user-123',
      componentName: 'Test RTX 4070',
      category: 'GPU',
      targetPrice: 500,
      currentPrice: 549.99,
      retailer: 'Amazon',
      productUrl: 'https://amazon.com',
      imageUrl: 'https://images.unsplash.com/photo-1587202372775-e229f172b9d7'
    };

    const { data: camelData, error: camelErr } = await supabase
      .from('watchlist_items')
      .insert([camelTest])
      .select();

    console.log('CamelCase Watchlist insert error:', camelErr);
    console.log('CamelCase Watchlist insert success data:', camelData);
  } else {
    console.log('snake_case Watchlist insert success:', insertData);
  }
}

checkSupabaseSchema().catch(console.error);
