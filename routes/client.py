import logging
import time
import uuid
from threading import Lock
from typing import Optional

import services.auth_service as auth
import services.party_service as ps
from fastapi import APIRouter, Header
from models import (
    CharactersLevelsGetRequest,
    CharactersLevelsGetResponse,
    CharactersLevelsNewRequest,
    CharactersLevelsNewResponse,
    CharactersLevelsNewResponseData,
    CharactersLevelsUnlockRequest,
    FundsOffersGetResponse,
    GamesCustomNewRequest,
    GamesCustomNewResponse,
    GamesJoinRequest,
    GamesJoinResponse,
    GamesNewRequest,
    GamesNewResponse,
    JoinData,
    MatchStartRequest,
    MatchStartResponse,
    MatchStartResponseData,
    PartyCreateRequest,
    PartyCreateResponse,
    PartyDetailsResponse,
    PartyDetailsResponseData,
    PartyJoinRequest,
    PartyJoinResponse,
    PartyKickRequest,
    PartyLeaveRequest,
    PartyListRequest,
    PartyListResponse,
    PartyPlayerStatus,
    PartyStatusUpdateRequest,
    PlayerCharacterLevelData,
    PlayersAuthGetRequest,
    PlayersAuthGetResponse,
    PlayersGetRequest,
    PlayersGetResponse,
    Response,
)
from services.game_server_service import get_boot_wait_message, spawn_server
from services.progression_service import ProgressionService
from services.user_service import UserService

logger = logging.getLogger("ClientRoutes")
router = APIRouter()
BOOT_WAIT_MESSAGE = get_boot_wait_message()


# Prevent duplicate server spawns from repeated create requests
# (especially around end-of-game transition retries).
_SPAWN_DEDUPE_WINDOW_SECONDS = 180.0
# Extra short window to collapse multi-client end-of-match create storms
# into a single spawned server.
_GLOBAL_SPAWN_BURST_WINDOW_SECONDS = 20.0
_spawn_dedupe_lock = Lock()
_recent_spawns = {}
_inflight_spawns = set()
_recent_global_spawns = {}
_inflight_global_spawns = set()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _auth_hdr(authorization: Optional[str]) -> Optional[str]:
    """Strip 'Bearer ' prefix if present."""
    if not authorization:
        return None
    return authorization.removeprefix("Bearer ").strip() or None


def _cleanup_recent_spawns(now: float):
    stale = [
        source_session
        for source_session, item in _recent_spawns.items()
        if now - item["createdAt"] > _SPAWN_DEDUPE_WINDOW_SECONDS
    ]
    for source_session in stale:
        _recent_spawns.pop(source_session, None)

    stale_global = [
        key
        for key, item in _recent_global_spawns.items()
        if now - item["createdAt"] > _GLOBAL_SPAWN_BURST_WINDOW_SECONDS
    ]
    for key in stale_global:
        _recent_global_spawns.pop(key, None)


def _build_spawn_key(
    endpoint: str,
    region: str,
    source_session_id: Optional[str],
    authorization: Optional[str],
    game_mode: str,
    is_public: bool,
    max_players: int,
) -> str:
    caller = source_session_id or _auth_hdr(authorization) or "anon"
    return f"{endpoint}|{caller}|{region}|{game_mode}|{is_public}|{max_players}"


def _find_recent_spawn(spawn_key: str) -> Optional[str]:
    now = time.time()
    with _spawn_dedupe_lock:
        _cleanup_recent_spawns(now)
        item = _recent_spawns.get(spawn_key)
        if not item:
            return None

        game_id = item["gameId"]
        if ps.get_party(game_id) is None:
            _recent_spawns.pop(spawn_key, None)
            return None

        return game_id


def _build_global_spawn_key(
    endpoint: str,
    region: str,
    game_mode: str,
    is_public: bool,
    max_players: int,
) -> str:
    return f"{endpoint}|{region}|{game_mode}|{is_public}|{max_players}"


def _find_recent_global_spawn(global_spawn_key: str) -> Optional[str]:
    now = time.time()
    with _spawn_dedupe_lock:
        _cleanup_recent_spawns(now)
        item = _recent_global_spawns.get(global_spawn_key)
        if not item:
            return None

        game_id = item["gameId"]
        if ps.get_party(game_id) is None:
            _recent_global_spawns.pop(global_spawn_key, None)
            return None

        return game_id


def _remember_spawn(spawn_key: str, game_id: str):
    with _spawn_dedupe_lock:
        _cleanup_recent_spawns(time.time())
        _recent_spawns[spawn_key] = {
            "gameId": game_id,
            "createdAt": time.time(),
        }


def _remember_global_spawn(global_spawn_key: str, game_id: str):
    with _spawn_dedupe_lock:
        _cleanup_recent_spawns(time.time())
        _recent_global_spawns[global_spawn_key] = {
            "gameId": game_id,
            "createdAt": time.time(),
        }


def _begin_spawn(spawn_key: str) -> bool:
    with _spawn_dedupe_lock:
        if spawn_key in _inflight_spawns:
            return False
        _inflight_spawns.add(spawn_key)
        return True


def _end_spawn(spawn_key: str):
    with _spawn_dedupe_lock:
        _inflight_spawns.discard(spawn_key)


def _begin_global_spawn(global_spawn_key: str) -> bool:
    with _spawn_dedupe_lock:
        if global_spawn_key in _inflight_global_spawns:
            return False
        _inflight_global_spawns.add(global_spawn_key)
        return True


def _end_global_spawn(global_spawn_key: str):
    with _spawn_dedupe_lock:
        _inflight_global_spawns.discard(global_spawn_key)


# ===========================================================================
# PARTY ROUTES
# ===========================================================================

@router.post("/party/create", response_model=PartyCreateResponse)
async def party_create(
    request: PartyCreateRequest, authorization: Optional[str] = Header(None)
):
    spawn_key = _build_spawn_key(
        endpoint="party/create",
        region=request.region,
        source_session_id=None,
        authorization=authorization,
        game_mode="CustomParty",
        is_public=request.isPublic,
        max_players=8,
    )

    recent_game_id = _find_recent_spawn(spawn_key)
    if recent_game_id:
        party = ps.get_party(recent_game_id)
        if party:
            return PartyCreateResponse(
                data=JoinData(
                    ipAddress=party["ipAddress"],
                    port=party["port"],
                    voicePort=party.get("voicePort"),
                    sessionId=recent_game_id,
                )
            )

    if not _begin_spawn(spawn_key):
        in_progress_game_id = _find_recent_spawn(spawn_key)
        if in_progress_game_id:
            party = ps.get_party(in_progress_game_id)
            if party:
                return PartyCreateResponse(
                    data=JoinData(
                        ipAddress=party["ipAddress"],
                        port=party["port"],
                        voicePort=party.get("voicePort"),
                        sessionId=in_progress_game_id,
                    )
                )
        return PartyCreateResponse(status=503, message=BOOT_WAIT_MESSAGE, data=None)

    steam_id = await auth.get_steam_id(_auth_hdr(authorization))
    game_id = str(uuid.uuid4())

    try:
        # Spawn a dedicated server (async — it will call /server/ready when ready)
        port = spawn_server(
            region=request.region,
            name=request.partyName,
            session_id=game_id,
            is_public=request.isPublic,
        )

        if port == -2:
            _, pending_party = ps.create_party(
                region=request.region,
                party_name=request.partyName,
                is_public=request.isPublic,
                host_steam_id=steam_id,
                port=0,
                game_id=game_id,
            )
            _remember_spawn(spawn_key, game_id)
            return PartyCreateResponse(
                status=200,
                message=BOOT_WAIT_MESSAGE,
                data=JoinData(
                    ipAddress=pending_party["ipAddress"],
                    port=pending_party["port"],
                    voicePort=pending_party.get("voicePort"),
                    sessionId=game_id,
                ),
            )

        if port == -1:
            return PartyCreateResponse(status=500, message="Failed to spawn server", data=None)

        _, party = ps.create_party(
            region=request.region,
            party_name=request.partyName,
            is_public=request.isPublic,
            host_steam_id=steam_id,
            port=port if port > 0 else 0,
            voice_port=(port + 1) if port > 0 else 0,
            game_id=game_id,
        )
        _remember_spawn(spawn_key, game_id)

        # If the exe wasn't found (port=-1), still create the party in pending state
        # so the game client can poll for it.
        return PartyCreateResponse(
            data=JoinData(
                ipAddress=party["ipAddress"],
                port=party["port"],
                voicePort=party.get("voicePort"),
                sessionId=game_id,
            )
        )
    finally:
        _end_spawn(spawn_key)


@router.post("/party/join", response_model=PartyJoinResponse)
async def party_join(
    request: PartyJoinRequest, authorization: Optional[str] = Header(None)
):
    steam_id = await auth.get_steam_id(_auth_hdr(authorization))
    join_data = ps.join_party(request.gameId, steam_id)
    if not join_data:
        return PartyJoinResponse(status=404, message="Party not found or full", data=None)
    return PartyJoinResponse(data=join_data)


@router.post("/party/leave", response_model=Response)
async def party_leave(
    request: PartyLeaveRequest, authorization: Optional[str] = Header(None)
):
    steam_id = await auth.get_steam_id(_auth_hdr(authorization))
    ps.leave_party(request.gameId, steam_id)
    return Response()


@router.post("/party/kick", response_model=Response)
async def party_kick(
    request: PartyKickRequest, authorization: Optional[str] = Header(None)
):
    host_steam_id = await auth.get_steam_id(_auth_hdr(authorization))
    if not ps.kick_player(request.gameId, host_steam_id, request.targetSteamId):
        return Response(status=403, message="Not authorized or target not found")
    return Response()


@router.post("/party/status/update", response_model=Response)
async def party_status_update(
    request: PartyStatusUpdateRequest, authorization: Optional[str] = Header(None)
):
    steam_id = await auth.get_steam_id(_auth_hdr(authorization))
    if not ps.update_player_status(request.gameId, steam_id, request.isReady):
        return Response(status=404, message="Player or party not found")
    return Response()


@router.get("/party/details/{game_id}", response_model=PartyDetailsResponse)
async def party_details(game_id: str):
    party = ps.get_party(game_id)
    if not party:
        return PartyDetailsResponse(status=404, message="Party not found", data=None)

    # Fetch player names in a single batch query
    steam_ids = list(party["players"].keys())
    players_data = await UserService.get_many(steam_ids)
    id_to_name = {p.steamId: p.steamId for p in players_data}  # fallback = steam_id

    players_status = [
        PartyPlayerStatus(
            steamId=sid,
            username=id_to_name.get(sid, sid),
            isReady=party["players"][sid],
            isHost=(sid == party["hostSteamId"]),
        )
        for sid in steam_ids
    ]

    return PartyDetailsResponse(
        data=PartyDetailsResponseData(
            gameId=game_id,
            region=party["region"],
            partyName=party["partyName"],
            maxPlayers=party["maxPlayers"],
            isPublic=party["isPublic"],
            hostSteamId=party["hostSteamId"],
            players=players_status,
        )
    )


@router.post("/party/list", response_model=PartyListResponse)
async def party_list(request: PartyListRequest):
    parties = ps.list_parties(request.regions)
    return PartyListResponse(data=parties)


# ===========================================================================
# PLAYER ROUTES
# ===========================================================================

@router.post("/players/auth/get", response_model=PlayersAuthGetResponse)
async def players_auth_get(req: PlayersAuthGetRequest):
    """
    Authenticate a player and return (or create) their profile.
    This is the ONLY endpoint that creates new player accounts.
    """
    steam_id = await auth.register_token(req.authToken)
    if not steam_id:
        return PlayersAuthGetResponse(status=401, message="Authentication failed", data=None)

    player = await UserService.get_or_create(steam_id)
    return PlayersAuthGetResponse(data=player)


@router.post("/players/get", response_model=PlayersGetResponse)
async def players_get(req: PlayersGetRequest, authorization: Optional[str] = Header(None)):
    """
    Bulk-fetch player profiles.
    Unknown players get a guest profile — no DB records are created.
    However, the authenticated caller themselves must have their account guaranteed.
    """
    my_steam_id = await auth.get_steam_id(_auth_hdr(authorization))
    if my_steam_id:
        # Guarantee the calling user actually exists in the DB (creates one if missing)
        await UserService.get_or_create(my_steam_id)

    data = await UserService.get_many(req.steamIds or [])
    return PlayersGetResponse(data=data)


# ===========================================================================
# GAME SESSION ROUTES
# ===========================================================================

@router.post("/games/new", response_model=GamesNewResponse)
async def games_new(
    req: GamesNewRequest,
    source_session_id: Optional[str] = Header(None, alias="Session-Id"),
    authorization: Optional[str] = Header(None),
):
    """Start a new public game session (not a party — used for quick-join matchmaking)."""
    spawn_key = _build_spawn_key(
        endpoint="games/new",
        region=req.region,
        source_session_id=source_session_id,
        authorization=authorization,
        game_mode=req.gamemodeName,
        is_public=req.isPublic,
        max_players=req.maxPlayers,
    )
    global_spawn_key = _build_global_spawn_key(
        endpoint="games/new",
        region=req.region,
        game_mode=req.gamemodeName,
        is_public=req.isPublic,
        max_players=req.maxPlayers,
    )

    recent_global_game_id = _find_recent_global_spawn(global_spawn_key)
    if recent_global_game_id:
        party = ps.get_party(recent_global_game_id)
        if party:
            return GamesNewResponse(
                data=JoinData(
                    ipAddress=party["ipAddress"],
                    port=party["port"],
                    voicePort=party.get("voicePort"),
                    sessionId=recent_global_game_id,
                )
            )

    recent_game_id = _find_recent_spawn(spawn_key)
    if recent_game_id:
        party = ps.get_party(recent_game_id)
        if party:
            return GamesNewResponse(
                data=JoinData(
                    ipAddress=party["ipAddress"],
                    port=party["port"],
                    voicePort=party.get("voicePort"),
                    sessionId=recent_game_id,
                )
            )

    if not _begin_global_spawn(global_spawn_key):
        in_progress_global_game_id = _find_recent_global_spawn(global_spawn_key)
        if in_progress_global_game_id:
            party = ps.get_party(in_progress_global_game_id)
            if party:
                return GamesNewResponse(
                    data=JoinData(
                        ipAddress=party["ipAddress"],
                        port=party["port"],
                        voicePort=party.get("voicePort"),
                        sessionId=in_progress_global_game_id,
                    )
                )
        return GamesNewResponse(status=503, message=BOOT_WAIT_MESSAGE, data=None)

    if not _begin_spawn(spawn_key):
        in_progress_game_id = _find_recent_spawn(spawn_key)
        if in_progress_game_id:
            party = ps.get_party(in_progress_game_id)
            if party:
                return GamesNewResponse(
                    data=JoinData(
                        ipAddress=party["ipAddress"],
                        port=party["port"],
                        voicePort=party.get("voicePort"),
                        sessionId=in_progress_game_id,
                    )
                )
        _end_global_spawn(global_spawn_key)
        return GamesNewResponse(status=503, message=BOOT_WAIT_MESSAGE, data=None)

    try:
        session_id = str(uuid.uuid4())
        port = spawn_server(
            region=req.region,
            name=req.gameName,
            session_id=session_id,
            is_public=req.isPublic,
            gamemode=req.gamemodeName,
            gamemode_data=req.gamemodeData,
        )
        if port == -2:
            return GamesNewResponse(status=503, message=BOOT_WAIT_MESSAGE, data=None)
        if port == -1:
            return GamesNewResponse(status=500, message="Failed to spawn server", data=None)

        # Register a party entry so clients can see it
        import services.party_service as ps2
        game_id, party = ps2.create_party(
            region=req.region,
            party_name=req.gameName,
            is_public=req.isPublic,
            host_steam_id="server",
            max_players=req.maxPlayers,
            port=port,
            voice_port=(port + 1),
            game_id=session_id,
        )
        _remember_spawn(spawn_key, game_id)
        _remember_global_spawn(global_spawn_key, game_id)
        return GamesNewResponse(
            data=JoinData(
                ipAddress=party["ipAddress"],
                port=port,
                voicePort=party.get("voicePort") or ((port + 1) if port > 0 else None),
                sessionId=game_id,
            )
        )
    finally:
        _end_spawn(spawn_key)
        _end_global_spawn(global_spawn_key)


@router.post("/games/join", response_model=GamesJoinResponse)
async def games_join(request: GamesJoinRequest, authorization: Optional[str] = Header(None)):
    steam_id = await auth.get_steam_id(_auth_hdr(authorization))
    join_data = ps.join_party(request.gameId, steam_id)
    if not join_data:
        return GamesJoinResponse(status=404, message="Game not found or full", data=None)
    # If the game server is still starting up (port=0), return 404 so the
    # client's built-in retry loop (ProgramClient.Join: 10 attempts, exponential
    # backoff) keeps polling until /server/ready has been called.
    if join_data.port == 0:
        logger.info(f"[GAMES-JOIN] Game {request.gameId} server not ready yet (port=0), client will retry")
        return GamesJoinResponse(status=503, message=BOOT_WAIT_MESSAGE, data=None)
    logger.info(f"[GAMES-JOIN] Directing {steam_id} to {join_data.ipAddress}:{join_data.port} for game {request.gameId}")
    return GamesJoinResponse(data=join_data)


@router.post("/games/custom/new", response_model=GamesCustomNewResponse)
async def games_custom_new(
    req: GamesCustomNewRequest,
    source_session_id: Optional[str] = Header(None, alias="Session-Id"),
    authorization: Optional[str] = Header(None),
):
    """Create a custom game mode server."""
    spawn_key = _build_spawn_key(
        endpoint="games/custom/new",
        region=req.region,
        source_session_id=source_session_id,
        authorization=authorization,
        game_mode=req.gamemodeName,
        is_public=False,
        max_players=req.maxPlayers,
    )

    recent_game_id = _find_recent_spawn(spawn_key)
    if recent_game_id:
        return GamesCustomNewResponse(data=recent_game_id)

    if not _begin_spawn(spawn_key):
        in_progress_game_id = _find_recent_spawn(spawn_key)
        if in_progress_game_id:
            return GamesCustomNewResponse(data=in_progress_game_id)
        return GamesCustomNewResponse(status=503, message=BOOT_WAIT_MESSAGE, data=None)

    try:
        session_id = str(uuid.uuid4())
        port = spawn_server(
            region=req.region,
            name=f"Custom-{req.gamemodeName}",
            session_id=session_id,
            is_public=False,
            gamemode=req.gamemodeName,
            gamemode_data=req.gamemodeData,
        )
        if port == -2:
            return GamesCustomNewResponse(status=503, message=BOOT_WAIT_MESSAGE, data=None)
        if port == -1:
            return GamesCustomNewResponse(status=500, message="Executable not found", data=None)

        ps.create_party(
            region=req.region,
            party_name=f"Custom-{req.gamemodeName}",
            is_public=False,
            host_steam_id="server",
            max_players=req.maxPlayers,
            port=port,
            voice_port=(port + 1),
            game_id=session_id,
        )
        _remember_spawn(spawn_key, session_id)
        return GamesCustomNewResponse(data=session_id)
    finally:
        _end_spawn(spawn_key)


@router.post("/match/start", response_model=MatchStartResponse)
async def match_start(req: MatchStartRequest):
    return MatchStartResponse(
        data=MatchStartResponseData(
            gameId=str(uuid.uuid4()),
            matchId=str(uuid.uuid4()),
            waitTime=1,
        )
    )


@router.post("/match/get")
async def match_get(body: dict = None):
    """Polling endpoint for matchmaking status."""
    return {"status": 200, "message": "OK", "data": {"gameId": "", "waitTime": 0}}


@router.post("/match/stop", response_model=Response)
async def match_stop():
    return Response()


# ===========================================================================
# PROGRESSION ROUTES
# ===========================================================================

@router.post("/characters/levels/new", response_model=CharactersLevelsNewResponse)
async def characters_levels_new(
    body: CharactersLevelsNewRequest, authorization: Optional[str] = Header(None)
):
    if UserService.is_maxed_progression_enabled():
        return CharactersLevelsNewResponse(
            data=CharactersLevelsNewResponseData(offeredPerks=[], offeredAbilities=[])
        )

    steam_id = await auth.get_steam_id(_auth_hdr(authorization))
    char_guid = body.characterGuid

    user = await UserService.get(steam_id)
    if not user:
        return CharactersLevelsNewResponse(
            status=404, message="Player not found",
            data=CharactersLevelsNewResponseData(offeredPerks=[], offeredAbilities=[]),
        )

    user_char = next((c for c in user.characters if c.guid == char_guid), None)
    if not user_char:
        return CharactersLevelsNewResponse(
            status=404, message="Character not found",
            data=CharactersLevelsNewResponseData(offeredPerks=[], offeredAbilities=[]),
        )

    current_level = user_char.level

    # ---------- ASCENSION ----------
    if current_level >= ProgressionService.MAX_LEVEL_CAP - 1:
        user_char.ascension += 1
        user_char.perks = []
        user_char.levelHistory = []
        user_char.pendingLevel = None

        grant_ability, history_entries = ProgressionService.generate_level_1_logic(
            char_guid, user_char.ascension
        )
        if grant_ability:
            user_char.abilities = [grant_ability]
            user_char.levelHistory = history_entries
            user_char.level = 1
        else:
            user_char.abilities = []
            user_char.level = 0

        await UserService.save(user)
        return CharactersLevelsNewResponse(
            data=CharactersLevelsNewResponseData(offeredPerks=[], offeredAbilities=[])
        )

    # ---------- STANDARD LEVEL UP ----------
    next_level = current_level + 1
    level_cost = 0 if current_level <= 1 else next_level * 100

    if user.credits < level_cost:
        return CharactersLevelsNewResponse(
            status=403,
            message=f"Insufficient credits. Need {level_cost}, have {user.credits}",
            data=CharactersLevelsNewResponseData(offeredPerks=[], offeredAbilities=[]),
        )

    user.credits -= level_cost
    offered_perks, offered_abilities = ProgressionService.get_level_offers(
        char_guid,
        next_level,
        user_char.ascension,
        set(user_char.abilities),
        set(user_char.perks),
    )
    user_char.pendingLevel = PlayerCharacterLevelData(
        offeredPerks=offered_perks,
        offeredAbilities=offered_abilities,
        chosenAbility=None,
        chosenPerk=None,
    )
    await UserService.save(user)

    return CharactersLevelsNewResponse(
        data=CharactersLevelsNewResponseData(
            offeredPerks=offered_perks, offeredAbilities=offered_abilities
        )
    )


@router.post("/characters/levels/unlock", response_model=Response)
async def characters_levels_unlock(
    body: CharactersLevelsUnlockRequest, authorization: Optional[str] = Header(None)
):
    if UserService.is_maxed_progression_enabled():
        return Response()

    steam_id = await auth.get_steam_id(_auth_hdr(authorization))
    user = await UserService.get(steam_id)
    if not user:
        return Response(status=404, message="Player not found")

    user_char = next((c for c in user.characters if c.guid == body.characterGuid), None)
    if not user_char:
        return Response(status=404, message="Character not found")
    if not user_char.pendingLevel:
        return Response(status=400, message="No pending level up found")

    pending = user_char.pendingLevel
    if body.abilityGuid:
        if body.abilityGuid not in pending.offeredAbilities:
            return Response(status=400, message="Ability not in offered list")
        pending.chosenAbility = body.abilityGuid
        user_char.abilities.append(body.abilityGuid)
    elif body.perkGuid:
        if body.perkGuid not in pending.offeredPerks:
            return Response(status=400, message="Perk not in offered list")
        pending.chosenPerk = body.perkGuid
        user_char.perks.append(body.perkGuid)

    user_char.levelHistory.append(pending)
    user_char.pendingLevel = None
    if user_char.level < ProgressionService.MAX_LEVEL_CAP - 1:
        user_char.level += 1

    await UserService.save(user)
    return Response()


@router.post("/characters/levels/get", response_model=CharactersLevelsGetResponse)
async def characters_levels_get(request: CharactersLevelsGetRequest):
    user = await UserService.get(request.steamId)
    if not user:
        return CharactersLevelsGetResponse(
            data=[PlayerCharacterLevelData() for _ in range(ProgressionService.MAX_LEVEL_CAP)]
        )

    user_char = next((c for c in user.characters if c.guid == request.characterGuid), None)
    if not user_char:
        return CharactersLevelsGetResponse(
            data=[PlayerCharacterLevelData() for _ in range(ProgressionService.MAX_LEVEL_CAP)]
        )

    levels_data = []
    for i in range(ProgressionService.MAX_LEVEL_CAP):
        if i < len(user_char.levelHistory):
            levels_data.append(user_char.levelHistory[i])
        elif i == len(user_char.levelHistory):
            if user_char.pendingLevel:
                levels_data.append(user_char.pendingLevel)
            else:
                sim_perks, sim_abilities = ProgressionService.get_level_offers(
                    request.characterGuid,
                    i + 1,
                    user_char.ascension,
                    set(user_char.abilities),
                    set(user_char.perks),
                )
                pending = PlayerCharacterLevelData(
                    offeredAbilities=sim_abilities,
                    offeredPerks=sim_perks,
                    chosenAbility=None,
                    chosenPerk=None,
                )
                user_char.pendingLevel = pending
                await UserService.save(user)
                levels_data.append(pending)
        else:
            levels_data.append(PlayerCharacterLevelData())

    return CharactersLevelsGetResponse(data=levels_data)


# ===========================================================================
# SHOP / STUBS
# ===========================================================================

@router.post("/analytics/new", response_model=Response)
async def analytics_new():
    return Response()

@router.post("/version/check", response_model=Response)
async def version_check_client():
    return Response(data=True)

@router.post("/items/purchase", response_model=Response)
async def items_purchase():
    return Response()

@router.post("/characters/purchase/funds", response_model=Response)
async def char_purchase_funds():
    return Response()

@router.post("/characters/purchase/credits", response_model=Response)
async def char_purchase_credits():
    return Response()

@router.post("/skins/purchase", response_model=Response)
async def skins_purchase():
    return Response()

@router.post("/funds/offers/get", response_model=FundsOffersGetResponse)
async def funds_offers_get():
    # Return empty — no microtransactions on a private server
    return FundsOffersGetResponse(data=[])

@router.post("/funds/purchase/new")
async def funds_purchase_new():
    return {"status": 200, "message": "OK", "data": {"orderId": str(uuid.uuid4())}}

@router.post("/funds/purchase/complete", response_model=Response)
async def funds_purchase_complete():
    return Response()

@router.post("/email/exists")
async def email_exists():
    return {"status": 200, "message": "OK", "data": False}

@router.post("/email/new", response_model=Response)
async def email_new():
    return Response()

@router.post("/onboarding/complete", response_model=Response)
async def onboarding_complete():
    return Response()

@router.post("/bundles/promos/unlock", response_model=Response)
async def bundles_promos_unlock():
    return Response()
