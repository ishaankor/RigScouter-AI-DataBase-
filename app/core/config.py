import os
from typing import List
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # Service Information
    SERVICE_NAME: str = "RigScouter-AI Multi-Retailer Pricing Engine"
    VERSION: str = "2.0.0"
    PORT: int = int(os.environ.get("PORT", "10000"))
    
    # Supabase Configuration
    SUPABASE_URL: str = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "https://mfzokxffhmedvtuhykdw.supabase.co")
    SUPABASE_SERVICE_ROLE_KEY: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    SUPABASE_ANON_KEY: str = os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_ANON_KEY", "")
    
    # AI & LLM Providers
    GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")

    # Retailer & Proxy APIs
    ZENROWS_API_KEY: str = os.environ.get("ZENROWS_API_KEY", "91418034a0339b39d3cfab8f77d001a8006896a7")
    ZENROWS_QUOTA_EXHAUSTED: bool = True  # Managed flag for ZenRows availability
    
    EBAY_CLIENT_ID: str = os.environ.get("EBAY_CLIENT_ID", "")
    EBAY_CLIENT_SECRET: str = os.environ.get("EBAY_CLIENT_SECRET", "")
    EBAY_DEV_ID: str = os.environ.get("EBAY_DEV_ID", "")

    TAVILY_API_KEYS: List[str] = [
        k for k in [
            os.environ.get("TAVILY_API_KEY", ""),
            "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",
            "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",
            "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",
            "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"
        ] if k
    ]

    # Email & Notifications
    RESEND_API_KEY: str = os.environ.get("RESEND_API_KEY", "")
    RESEND_DOMAIN: str = os.environ.get("RESEND_DOMAIN", "rigscouter@ishaankoradia.com")
    ADMIN_ALERT_EMAIL: str = os.environ.get("ADMIN_ALERT_EMAIL", "ishaankor@gmail.com")
    CRON_SECRET: str = os.environ.get("CRON_SECRET", "rigscouter-cron-secret")

settings = Settings()
