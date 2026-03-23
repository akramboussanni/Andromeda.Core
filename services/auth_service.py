import asyncio
import logging
import os
from typing import Dict, Optional

import httpx

logger = logging.getLogger("AuthService")

# ---------------------------------------------------------------------------
# In-memory token cache:  auth_token -> steam_id
# In production this could go into Redis with a TTL.
# ---------------------------------------------------------------------------
_cache: Dict[str, str] = {}

# A dedicated event loop for the rare sync callers (TCP handler).
# Using asyncio.run() from a thread that already HAS a loop crashes;
# we keep our own separate loop for that purpose.
_sync_loop = asyncio.new_event_loop()


async def _validate_steam_ticket(ticket_hex: str) -> Optional[str]:
    """Call the Steam Web API to verify a hex-encoded auth ticket."""
    api_key = os.getenv("STEAM_API_KEY", "")
    app_id = os.getenv("STEAM_APP_ID", "999860")

    if not api_key or api_key == "YOUR_API_KEY_HERE":
        logger.warning("[AUTH] No valid Steam API key — skipping ticket validation.")
        return None

    url = "https://api.steampowered.com/ISteamUserAuth/AuthenticateUserTicket/v1/"
    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            resp = await client.get(
                url, params={"key": api_key, "appid": app_id, "ticket": ticket_hex}
            )
            resp.raise_for_status()
            content = resp.json().get("response", {})
            if "error" in content:
                logger.warning(f"[AUTH] Steam error: {content['error']}")
                return None
            params = content.get("params", {})
            if params.get("result") == "OK":
                return params.get("steamid")
        except Exception as exc:
            logger.error(f"[AUTH] Steam validation exception: {exc}")
    return None


async def register_token(auth_token: str) -> str:
    """
    Validate an auth token and return the resolved Steam64 ID.
    Results are cached for the lifetime of the process.
    """
    if not auth_token:
        logger.warning("[AUTH] Empty token.")
        return ""

    if auth_token in _cache:
        return _cache[auth_token]

    # Raw Steam64 ID (dev / batchmode servers)
    if auth_token.isdigit() and len(auth_token) == 17 and auth_token.startswith("765"):
        logger.info(f"[AUTH] Raw SteamID accepted (dev mode): {auth_token}")
        _cache[auth_token] = auth_token
        return auth_token

    # Dedicated server internal token
    if auth_token == "DEDICATED_SERVER_TOKEN":
        _cache[auth_token] = auth_token
        return auth_token

    # Full Steam ticket validation
    logger.info(f"[AUTH] Validating ticket: {auth_token[:16]}…")
    steam_id = await _validate_steam_ticket(auth_token)
    if steam_id:
        logger.info(f"[AUTH] Ticket validated → {steam_id}")
        _cache[auth_token] = steam_id
        return steam_id

    logger.warning("[AUTH] Ticket validation failed — returning empty string.")
    return ""


async def get_steam_id(auth_header: Optional[str]) -> str:
    """Resolve an Authorization header value to a Steam64 ID."""
    if not auth_header:
        return ""
    return await register_token(auth_header)


def get_steam_id_sync(auth_header: Optional[str]) -> str:
    """
    Thread-safe synchronous wrapper — uses a dedicated event loop so it
    never conflicts with the FastAPI event loop running in another thread.
    """
    if not auth_header:
        return ""
    if auth_header in _cache:
        return _cache[auth_header]
    return _sync_loop.run_until_complete(register_token(auth_header))
