import os
import time
import uuid
import logging
from typing import Dict, List, Optional
from models import JoinData, PartyListResponseData

logger = logging.getLogger("PartyService")

# ---------------------------------------------------------------------------
# In-memory party registry.
# Parties are ephemeral (they live only while a game session is active),
# so keeping them in RAM is correct.  The SQLite DB is for durable data only.
# ---------------------------------------------------------------------------
_parties: Dict[str, dict] = {}

# How many seconds without a heartbeat before a party is declared stale.
HEARTBEAT_TIMEOUT = int(os.getenv("PARTY_HEARTBEAT_TIMEOUT", "90"))

# Public server host — overridden by env var for LAN / internet hosting
_SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")


def _make_join_data(game_id: str, party: dict) -> JoinData:
    return JoinData(
        ipAddress=party["ipAddress"],
        port=party["port"],
        sessionId=game_id,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_party(
    region: str,
    party_name: str,
    is_public: bool,
    host_steam_id: str,
    port: int = 0,
    ip_address: Optional[str] = None,
) -> tuple[str, dict]:
    """
    Register a new party.  Returns (game_id, party_dict).
    Port 0 means the dedicated server hasn't reported in yet.
    """
    game_id = str(uuid.uuid4())
    _parties[game_id] = {
        "region": region,
        "partyName": party_name,
        "maxPlayers": 6,
        "isPublic": is_public,
        "hostSteamId": host_steam_id,
        "players": {host_steam_id: False},
        "ipAddress": ip_address or _SERVER_HOST,
        "port": port,
        "status": "pending" if port == 0 else "ready",
        "lastHeartbeat": time.time(),
    }
    logger.info(f"[PARTY] Created {game_id} ({party_name}) host={host_steam_id}")
    return game_id, _parties[game_id]


def update_party_address(game_id: str, ip_address: str, port: int):
    """Called when the dedicated server reports ready."""
    party = _parties.get(game_id)
    if party:
        party["ipAddress"] = ip_address
        party["port"] = port
        party["status"] = "ready"
        party["lastHeartbeat"] = time.time()
        logger.info(f"[PARTY] {game_id} now ready at {ip_address}:{port}")


def get_party(game_id: str) -> Optional[dict]:
    return _parties.get(game_id)


def can_join(game_id: str, steam_id: str) -> bool:
    party = _parties.get(game_id)
    if not party:
        return False
    if steam_id in party["players"]:
        return True  # Already in — allow re-join
    return len(party["players"]) < party["maxPlayers"]


def join_party(game_id: str, steam_id: str) -> Optional[JoinData]:
    party = _parties.get(game_id)
    if not party:
        return None
    if len(party["players"]) >= party["maxPlayers"] and steam_id not in party["players"]:
        return None
    party["players"].setdefault(steam_id, False)
    return _make_join_data(game_id, party)


def leave_party(game_id: str, steam_id: str) -> bool:
    party = _parties.get(game_id)
    if not party:
        return False
    party["players"].pop(steam_id, None)

    if steam_id == party["hostSteamId"]:
        remaining = [s for s in party["players"] if s != steam_id]
        if remaining:
            party["hostSteamId"] = remaining[0]
            logger.info(f"[PARTY] {game_id}: host migrated to {party['hostSteamId']}")
        else:
            close_party(game_id)
            return True
    return True


def kick_player(game_id: str, host_steam_id: str, target_steam_id: str) -> bool:
    party = _parties.get(game_id)
    if not party:
        return False
    if party["hostSteamId"] != host_steam_id:
        return False
    if target_steam_id == host_steam_id:
        return False
    return bool(party["players"].pop(target_steam_id, None) is not None)


def update_player_status(game_id: str, steam_id: str, is_ready: bool) -> bool:
    party = _parties.get(game_id)
    if not party or steam_id not in party["players"]:
        return False
    party["players"][steam_id] = is_ready
    return True


def heartbeat(game_id: str) -> bool:
    party = _parties.get(game_id)
    if not party:
        return False
    party["lastHeartbeat"] = time.time()
    return True


def close_party(game_id: str):
    _parties.pop(game_id, None)
    logger.info(f"[PARTY] Closed {game_id}")


def list_parties(regions: Optional[List[str]] = None) -> List[PartyListResponseData]:
    _cleanup_stale()
    result = []
    for game_id, party in _parties.items():
        if not party["isPublic"]:
            continue
        if party["status"] != "ready":
            continue
        if regions and party["region"] not in regions:
            continue
        result.append(
            PartyListResponseData(
                gameId=game_id,
                region=party["region"],
                partyName=party["partyName"],
                currentPlayers=len(party["players"]),
                maxPlayers=party["maxPlayers"],
            )
        )
    return result


def _cleanup_stale():
    now = time.time()
    stale = [
        gid
        for gid, p in list(_parties.items())
        if now - p.get("lastHeartbeat", 0) > HEARTBEAT_TIMEOUT
    ]
    for gid in stale:
        logger.info(f"[PARTY] Removing stale party {gid}")
        _parties.pop(gid, None)
