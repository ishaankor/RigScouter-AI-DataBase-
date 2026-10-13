import logging
from supabase import create_client, Client
from app.core.config import settings

logger = logging.getLogger("rigscouter.database")

_client_instance: Client | None = None

def get_supabase_client() -> Client:
    """
    Returns an authoritative Supabase client.
    Prefers SUPABASE_SERVICE_ROLE_KEY to ensure backend tasks, crons,
    and history tracking never hit RLS blockages.
    """
    global _client_instance
    if _client_instance is not None:
        return _client_instance

    url = settings.SUPABASE_URL
    key = settings.SUPABASE_SERVICE_ROLE_KEY or settings.SUPABASE_ANON_KEY

    if not url or not key:
        logger.error("[Database] Critical: Supabase URL or Key missing in settings.")
        raise ValueError("Supabase URL and API Key must be configured in environment.")

    _client_instance = create_client(url, key)
    is_service = bool(settings.SUPABASE_SERVICE_ROLE_KEY)
    logger.info(f"[Database] Initialized Supabase client (Service Role: {is_service})")
    return _client_instance

# Lazy accessor proxy
class SupabaseProxy:
    def __getattr__(self, name):
        client = get_supabase_client()
        return getattr(client, name)

supabase = SupabaseProxy()
