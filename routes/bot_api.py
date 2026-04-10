import os
import time
from typing import Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import urlopen

import database as db
import services.party_service as ps
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

BOT_API_ENABLED = os.getenv("BOT_API_ENABLED", "false").strip().lower() == "true"
BOT_API_TOKEN = os.getenv("BOT_API_TOKEN", "").strip()
STEAM_WEB_API_KEY = os.getenv("STEAM_WEB_API_KEY", "").strip()
_STEAM_NAME_CACHE: Dict[str, dict] = {}
_STEAM_NAME_CACHE_TTL_SECONDS = 600


class BotLinkRequest(BaseModel):
    discordUserId: str
    steamId: str
    discordUsername: str = ""


def _is_api_enabled() -> bool:
    return BOT_API_ENABLED and bool(BOT_API_TOKEN)


def _is_valid_steam_id(steam_id: str) -> bool:
    return steam_id.isdigit() and len(steam_id) == 17 and steam_id.startswith("765")


def _authorized(x_bot_token: Optional[str], authorization: Optional[str]) -> bool:
    token = (x_bot_token or "").strip()
    if not token and authorization:
        token = authorization.removeprefix("Bearer ").strip()
    return bool(token) and token == BOT_API_TOKEN


def _sanitize_players(players: dict) -> list:
    result = []
    for steam_id in players.keys():
        if steam_id in ("server", "", None, "DEDICATED_SERVER_TOKEN"):
            continue
        if isinstance(steam_id, str) and steam_id.isdigit() and len(steam_id) >= 10:
            result.append(steam_id)
    return result


def _resolve_steam_names(steam_ids: List[str]) -> Dict[str, str]:
    if not STEAM_WEB_API_KEY:
        return {}

    now = time.time()
    normalized = []
    for sid in steam_ids:
        sid_text = str(sid or "").strip()
        if sid_text and sid_text.isdigit():
            normalized.append(sid_text)

    if not normalized:
        return {}

    names: Dict[str, str] = {}
    to_fetch: List[str] = []
    for sid in normalized:
        cached = _STEAM_NAME_CACHE.get(sid)
        if cached and (now - float(cached.get("ts", 0))) <= _STEAM_NAME_CACHE_TTL_SECONDS:
            name = str(cached.get("name") or "").strip()
            if name:
                names[sid] = name
            continue
        to_fetch.append(sid)

    if not to_fetch:
        return names

    try:
        query = urlencode({
            "key": STEAM_WEB_API_KEY,
            "steamids": ",".join(to_fetch),
        })
        url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?" + query
        with urlopen(url, timeout=6) as response:
            raw = response.read().decode("utf-8", errors="ignore")

        import json

        payload = json.loads(raw)
        players = payload.get("response", {}).get("players", [])
        for player in players:
            sid = str(player.get("steamid") or "").strip()
            persona = str(player.get("personaname") or "").strip()
            if sid and persona:
                _STEAM_NAME_CACHE[sid] = {"name": persona, "ts": now}
                names[sid] = persona
    except Exception:
        pass

    return names


@router.get("/bot/v1/health")
async def bot_health():
    if not _is_api_enabled():
        return JSONResponse({"detail": "Bot API disabled"}, status_code=404)
    return {"status": "ok"}


@router.get("/bot/v1/state")
async def bot_state(
    x_bot_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    if not _is_api_enabled():
        return JSONResponse({"detail": "Bot API disabled"}, status_code=404)
    if not _authorized(x_bot_token, authorization):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    lobbies = []
    ingame_players = set()
    steam_name_by_id = {}

    for game_id, party in ps._parties.items():
        players = _sanitize_players(party.get("players", {}))
        for steam_id in players:
            ingame_players.add(steam_id)

        telemetry_players = party.get("players_telemetry", []) or []
        for player in telemetry_players:
            if not isinstance(player, dict):
                continue

            steam_id = str(player.get("steamId") or player.get("steam_id") or "").strip()
            username = str(player.get("username") or player.get("name") or "").strip()
            if steam_id and username:
                steam_name_by_id[steam_id] = username

        is_public_origin = bool(party.get("wasPublicAtCreation", party.get("isPublic", False)))
        if not is_public_origin:
            continue

        lobbies.append(
            {
                "gameId": game_id,
                "partyName": party.get("partyName", ""),
                "region": party.get("region", ""),
                "status": party.get("status", "unknown"),
                "currentPlayers": len(players),
                "maxPlayers": int(party.get("maxPlayers", 0) or 0),
                "players": players,
                "isPublic": bool(party.get("wasPublicAtCreation", party.get("isPublic", False))),
                "ipAddress": party.get("ipAddress", ""),
                "port": int(party.get("port", 0) or 0),
            }
        )

    lobbies.sort(key=lambda x: (x.get("status") != "ready", x.get("partyName", "")))

    # Get pending links for all in-game players
    pending_links_by_steam = {}
    for steam_id in ingame_players:
        pending = await db.get_pending_links_for_steam(steam_id)
        if pending:
            pending_links_by_steam[steam_id] = pending

    all_pending_links = await db.get_all_pending_links()
    all_pending_by_steam = {}
    for pending in all_pending_links:
        steam_id = pending.get("steam_id")
        if steam_id:
            all_pending_by_steam.setdefault(steam_id, []).append(pending)

    try:
        import log_server

        id_to_name = log_server.get_id_to_name_map() or {}
        for steam_id in ingame_players:
            if steam_id in steam_name_by_id:
                continue
            name = id_to_name.get(steam_id)
            if name:
                steam_name_by_id[steam_id] = str(name)
    except Exception:
        pass

    # Fallback: resolve remaining names from Steam Web API (if key configured).
    unresolved = [sid for sid in ingame_players if sid not in steam_name_by_id]
    if unresolved:
        steam_api_names = _resolve_steam_names(unresolved)
        for sid, name in steam_api_names.items():
            if sid and name and sid not in steam_name_by_id:
                steam_name_by_id[sid] = name

    return {
        "timestamp": int(time.time()),
        "lobbies": lobbies,
        "ingameSteamIds": sorted(ingame_players),
        "steamNameById": steam_name_by_id,
        "pendingLinksBySteam": pending_links_by_steam,
        "pendingLinks": all_pending_links,
        "pendingLinksBySteamAll": all_pending_by_steam,
    }


@router.get("/bot/v1/steam-names")
async def bot_steam_names(
    steam_ids: str,
    x_bot_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    if not _is_api_enabled():
        return JSONResponse({"detail": "Bot API disabled"}, status_code=404)
    if not _authorized(x_bot_token, authorization):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    ids = [s.strip() for s in str(steam_ids or "").split(",") if s.strip()]
    names = _resolve_steam_names(ids)
    return {"count": len(names), "names": names}


@router.post("/bot/v1/links")
async def bot_link_create(
    body: BotLinkRequest,
    x_bot_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    if not _is_api_enabled():
        return JSONResponse({"detail": "Bot API disabled"}, status_code=404)
    if not _authorized(x_bot_token, authorization):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    if not body.discordUserId.isdigit():
        return JSONResponse({"detail": "Invalid discord user id"}, status_code=400)
    if not _is_valid_steam_id(body.steamId):
        return JSONResponse({"detail": "Invalid steam id"}, status_code=400)

    existing_discord = await db.get_discord_link_record_by_discord(body.discordUserId)
    if existing_discord:
        return JSONResponse(
            {
                "detail": "Discord account already has a link or pending request",
                "existing": existing_discord,
            },
            status_code=409,
        )

    existing_steam = await db.get_discord_link_record_by_steam(body.steamId)
    if existing_steam:
        return JSONResponse(
            {
                "detail": "Steam account already has a link or pending request",
                "existing": existing_steam,
            },
            status_code=409,
        )

    await db.upsert_discord_link(body.discordUserId, body.steamId, discord_username=body.discordUsername)
    return {"status": "ok", "discordUserId": body.discordUserId, "steamId": body.steamId}


@router.get("/bot/v1/links/{discord_user_id}/record")
async def bot_link_record_by_discord(
    discord_user_id: str,
    x_bot_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    if not _is_api_enabled():
        return JSONResponse({"detail": "Bot API disabled"}, status_code=404)
    if not _authorized(x_bot_token, authorization):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    record = await db.get_discord_link_record_by_discord(discord_user_id)
    if not record:
        return JSONResponse({"detail": "Link not found"}, status_code=404)
    return record


@router.get("/bot/v1/links/by-steam/{steam_id}")
async def bot_link_record_by_steam(
    steam_id: str,
    x_bot_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    if not _is_api_enabled():
        return JSONResponse({"detail": "Bot API disabled"}, status_code=404)
    if not _authorized(x_bot_token, authorization):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    record = await db.get_discord_link_record_by_steam(steam_id)
    if not record:
        return JSONResponse({"detail": "Link not found"}, status_code=404)
    return record


@router.get("/bot/v1/links/{discord_user_id}")
async def bot_link_get(
    discord_user_id: str,
    x_bot_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    if not _is_api_enabled():
        return JSONResponse({"detail": "Bot API disabled"}, status_code=404)
    if not _authorized(x_bot_token, authorization):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    steam_id = await db.get_steam_by_discord(discord_user_id)
    if not steam_id:
        return JSONResponse({"detail": "Link not found"}, status_code=404)
    return {"discordUserId": discord_user_id, "steamId": steam_id}


@router.delete("/bot/v1/links/{discord_user_id}")
async def bot_link_delete(
    discord_user_id: str,
    x_bot_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    if not _is_api_enabled():
        return JSONResponse({"detail": "Bot API disabled"}, status_code=404)
    if not _authorized(x_bot_token, authorization):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    deleted = await db.delete_discord_link(discord_user_id)
    return {"status": "ok", "deleted": int(deleted)}


@router.post("/bot/v1/links/{discord_user_id}/confirm")
async def bot_link_confirm(
    discord_user_id: str,
    body: BotLinkRequest,
    x_bot_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    if not _is_api_enabled():
        return JSONResponse({"detail": "Bot API disabled"}, status_code=404)
    if not _authorized(x_bot_token, authorization):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    confirmed = await db.confirm_discord_link(discord_user_id, body.steamId)
    return {"status": "ok", "confirmed": confirmed}


@router.post("/bot/v1/links/{discord_user_id}/reject")
async def bot_link_reject(
    discord_user_id: str,
    body: BotLinkRequest,
    block_duration: str = "24h",
    x_bot_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    if not _is_api_enabled():
        return JSONResponse({"detail": "Bot API disabled"}, status_code=404)
    if not _authorized(x_bot_token, authorization):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    if block_duration == "forever":
        await db.block_link_forever(discord_user_id, body.steamId)
        return {"status": "ok", "blocked": "forever"}
    else:
        await db.block_link_for_24h(discord_user_id, body.steamId)
        return {"status": "ok", "blocked": "24h"}


@router.get("/bot/v1/links")
async def bot_link_list(
    x_bot_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    if not _is_api_enabled():
        return JSONResponse({"detail": "Bot API disabled"}, status_code=404)
    if not _authorized(x_bot_token, authorization):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    links = await db.get_all_discord_links()
    return {"count": len(links), "links": links}


@router.get("/bot/v1/leaderboard")
async def bot_leaderboard(
    limit: int = 50,
    x_bot_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    if not _is_api_enabled():
        return JSONResponse({"detail": "Bot API disabled"}, status_code=404)
    if not _authorized(x_bot_token, authorization):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    limit = min(max(1, int(limit or 50)), 1000)
    leaders = await db.get_leaderboard(limit=limit)
    return {"count": len(leaders), "leaderboard": leaders}


@router.get("/bot/v1/player-stats/{steam_id}")
async def bot_player_stats(
    steam_id: str,
    x_bot_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    if not _is_api_enabled():
        return JSONResponse({"detail": "Bot API disabled"}, status_code=404)
    if not _authorized(x_bot_token, authorization):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    async with db.get_db() as conn:
        async with conn.execute(
            """
            SELECT
                steam_id,
                rank,
                credits,
                total_games,
                COALESCE(wins, 0) AS wins,
                COALESCE(losses, 0) AS losses,
                CASE
                    WHEN (COALESCE(wins, 0) + COALESCE(losses, 0)) > 0
                    THEN ROUND(100.0 * COALESCE(wins, 0) / (COALESCE(wins, 0) + COALESCE(losses, 0)), 2)
                    ELSE 0
                END AS win_rate
            FROM players
            WHERE steam_id = ?
            LIMIT 1
            """,
            (steam_id,),
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return JSONResponse({"detail": "Player not found"}, status_code=404)

    return dict(row)
