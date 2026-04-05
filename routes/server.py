import logging
import os
import shutil
import tempfile
import time
from datetime import datetime
from typing import Optional

import database as db
import services.party_service as ps
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from models import (
    Response,
    ServerHeartbeatRequest,
    ServerReadyRequest,
    ServerShutdownRequest,
    StatsNewRequest,
    StatsNewResponse,
    StatsNewResponsePlayer,
)
from services.game_server_service import (
    get_host_boot_state,
    get_server_host,
    get_session_ports,
    stop_session,
)
from starlette.background import BackgroundTask

logger = logging.getLogger("ServerRoutes")
router = APIRouter()
MANAGED_ZIP_PATH = os.getenv("MANAGED_ZIP_PATH", os.path.join("data", "managed", "EnemyOnBoard_Managed.zip"))
MANAGED_ZIP_TOKEN = os.getenv("MANAGED_ZIP_TOKEN", "").strip()
EOB_MANAGED_DEP_DIR = os.getenv("EOB_MANAGED_DEP_DIR", "").strip() or os.getenv("EOB_MANAGED_DIR", "").strip()


def _cleanup_file(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as exc:
        logger.warning(f"[MANAGED-ZIP] Failed to delete temporary zip '{path}': {exc}")


def _resolve_managed_zip_for_ci() -> tuple[str, Optional[BackgroundTask]]:
    # Primary mode: explicit zip path, backwards compatible with current setup.
    resolved_zip = os.path.abspath(MANAGED_ZIP_PATH)
    if os.path.exists(resolved_zip):
        return resolved_zip, None

    # CI fallback: build a zip from the game Managed directory on demand.
    if EOB_MANAGED_DEP_DIR:
        managed_dir = os.path.abspath(EOB_MANAGED_DEP_DIR)
        if not os.path.isdir(managed_dir):
            raise HTTPException(
                status_code=500,
                detail=f"EOB_MANAGED_DEP_DIR does not exist or is not a directory: {managed_dir}",
            )

        expected = os.path.join(managed_dir, "Assembly-CSharp-Publicized.dll")
        if not os.path.exists(expected):
            raise HTTPException(
                status_code=500,
                detail="Managed dependency source is missing Assembly-CSharp-Publicized.dll",
            )

        temp_base = os.path.join(
            tempfile.gettempdir(),
            f"andromeda_eob_managed_{int(time.time())}",
        )
        temp_zip = shutil.make_archive(temp_base, "zip", managed_dir)
        return temp_zip, BackgroundTask(_cleanup_file, temp_zip)

    raise HTTPException(status_code=404, detail="Managed zip not found")


@router.post("/server/ready", response_model=Response)
async def server_ready(req: ServerReadyRequest):
    """
    Called by a dedicated game server when it has finished starting up
    and is ready to accept UDP connections from clients.
    """
    logger.info(
        f"[SERVER-READY] session={req.sessionId} port={req.port} region={req.region}"
    )
    host = get_server_host()
    ps.update_party_address(req.sessionId, host, req.port)
    ps.heartbeat(req.sessionId)
    return Response()


@router.get("/server/host/status", response_model=Response)
async def server_host_status():
    return Response(data=get_host_boot_state())


@router.post("/server/heartbeat", response_model=Response)
async def server_heartbeat(req: ServerHeartbeatRequest):
    if ps.heartbeat(req.sessionId):
        return Response()
    return Response(status=404, message="Session not found")


@router.post("/server/shutdown", response_model=Response)
async def server_shutdown(req: ServerShutdownRequest):
    reason_str = req.reason or "Unknown / Closed manually"
    logger.info(f"[SERVER-SHUTDOWN] session={req.sessionId} reason={reason_str}")
    stop_session(req.sessionId, reason=reason_str)
    ps.close_party(req.sessionId)
    return Response()


@router.get("/server/session/{session_id}/ports", response_model=Response)
async def server_session_ports(session_id: str):
    ports = get_session_ports(session_id)
    if not ports:
        return Response(status=404, message="Session not found", data=None)
    return Response(data=ports)


@router.get("/build/managed-zip")
async def build_managed_zip(
    authorization: Optional[str] = Header(None),
    x_managed_token: Optional[str] = Header(None, alias="X-Managed-Token"),
):
    if MANAGED_ZIP_TOKEN:
        bearer = (authorization or "").removeprefix("Bearer ").strip()
        provided = x_managed_token or bearer
        if provided != MANAGED_ZIP_TOKEN:
            raise HTTPException(status_code=401, detail="Unauthorized")

    resolved, cleanup_task = _resolve_managed_zip_for_ci()

    return FileResponse(
        path=resolved,
        filename=os.path.basename(resolved),
        media_type="application/zip",
        background=cleanup_task,
    )


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
