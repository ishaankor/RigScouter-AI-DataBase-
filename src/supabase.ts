import { createClient } from '@supabase/supabase-js';
import dotenv from 'dotenv';

dotenv.config();

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL || 'https://mfzokxffhmedvtuhykdw.supabase.co';
const SUPABASE_ANON_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1mem9reGZmaG1lZHZ0dWh5a2R3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3Mzg4MDE1NDMsImV4cCI6MjA1NDM3NzU0M30.rQ6H7qjF21n6S85i01gWd6yE4d2C7e8F9g0h1i2j3k4';

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
