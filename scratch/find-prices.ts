import { tavily } from '@tavily/core';
import dotenv from 'dotenv';

dotenv.config();

async function findBoldPricesInNewegg() {
  const tvly = tavily({ apiKey: process.env.TAVILY_API_KEY });
  const url = 'https://www.newegg.com/nvidia-founders-edition-900-1g141-2544-000-geforce-rtx-4070-12gb-graphics-card/p/1FT-0004-00858';
  const res = await tvly.extract([url], { extractDepth: 'advanced', format: 'markdown' });
  const text = (res.results[0]?.rawContent || '');

  console.log('Total raw text length:', text.length);

  const regex = /\$\s*\*{0,2}[\d,]+(\.\d{2})?\*{0,2}/g;
  let match;
  while ((match = regex.exec(text)) !== null) {
    const context = text.substring(Math.max(0, match.index - 50), Math.min(text.length, match.index + 50)).replace(/\n/g, ' ');
    console.log(`Pos ${match.index}: ${match[0]} | "${context}"`);
  }
}

findBoldPricesInNewegg().catch(console.error);
