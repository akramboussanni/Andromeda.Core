import logging
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter
from models import (
    StatsNewRequest, StatsNewResponse, StatsNewResponsePlayer,
    ServerReadyRequest, ServerHeartbeatRequest, ServerShutdownRequest,
    Response,
)
import services.party_service as ps
import database as db

logger = logging.getLogger("ServerRoutes")
router = APIRouter()


@router.post("/server/ready", response_model=Response)
async def server_ready(req: ServerReadyRequest):
    """
    Called by a dedicated game server when it has finished starting up
    and is ready to accept TCP connections from clients.
    Updates the party's IP:port from pending → ready.
    """
    logger.info(
        f"[SERVER-READY] session={req.sessionId} port={req.port} region={req.region}"
    )
    import os
    host = os.getenv("SERVER_HOST", "127.0.0.1")
    ps.update_party_address(req.sessionId, host, req.port)
    ps.heartbeat(req.sessionId)
    return Response()


@router.post("/server/heartbeat", response_model=Response)
async def server_heartbeat(req: ServerHeartbeatRequest):
    if ps.heartbeat(req.sessionId):
        return Response()
    return Response(status=404, message="Session not found")


@router.post("/server/shutdown", response_model=Response)
async def server_shutdown(req: ServerShutdownRequest):
    logger.info(f"[SERVER-SHUTDOWN] session={req.sessionId}")
    ps.close_party(req.sessionId)
    return Response()


@router.post("/stats/new", response_model=StatsNewResponse)
async def stats_new(req: StatsNewRequest):
    """
    End-of-game stats from the dedicated server.
    Persists match history for each player and awards credits.
    """
    now = datetime.utcnow().isoformat()
    game_id = str(time.time_ns())

    # Build batch match-history insert rows
    history_rows = [
        (
            p.steamId,
            now,
            game_id,
            req.gameLength,
            int(req.aliensWon),
            int(req.crewWon),
            int(p.wasAlien),
            p.characterGuid,
            p.abilityGuid,
            p.itemGuid,
            p.alienGuid,
            p.perkA,
            p.perkB,
            p.perkC,
        )
        for p in req.players
        if p.steamId and p.steamId != "DEDICATED_SERVER_TOKEN"
    ]
    if history_rows:
        await db.add_match_history_entries(history_rows)

    # Award credits — load only the players we need out of the DB
    from services.user_service import UserService
    steam_ids = [p.steamId for p in req.players if p.steamId]
    players_map = {p.steamId: p for p in (await UserService.get_many(steam_ids)) if p}

    response_players = []
    for stat in req.players:
        if not stat.steamId:
            continue

        player_data = players_map.get(stat.steamId)
        if player_data:
            # Only award credits to registered players (not guests)
            existing = await db.player_exists(stat.steamId)
            if existing:
                player_data.credits += stat.creditsEarned
                player_data.totalGames += 1
                await UserService.save(player_data)

        response_players.append(
            StatsNewResponsePlayer(
                steamId=stat.steamId,
                rank=player_data.rank if player_data else 1,
                creditsEarned=stat.creditsEarned,
            )
        )

    return StatsNewResponse(data=response_players)
