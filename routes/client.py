import uuid
import logging
from fastapi import APIRouter, Header
from typing import Optional

import services.party_service as ps
import services.auth_service as auth
from services.user_service import UserService
from services.progression_service import ProgressionService
from services.game_server_service import spawn_server
from models import (
    Response,
    PlayersGetRequest, PlayersGetResponse,
    PlayersAuthGetRequest, PlayersAuthGetResponse,
    FundsOffersGetResponse, FundOfferData,
    GamesNewRequest, GamesNewResponse, JoinData,
    GamesJoinRequest, GamesJoinResponse,
    MatchStartRequest, MatchStartResponseData, MatchStartResponse,
    CharactersLevelsNewResponse, CharactersLevelsNewResponseData,
    PartyCreateRequest, PartyCreateResponse,
    PartyJoinRequest, PartyJoinResponse,
    PartyListRequest, PartyListResponse,
    CharactersLevelsGetRequest, CharactersLevelsGetResponse, PlayerCharacterLevelData,
    CharactersLevelsUnlockRequest, CharactersLevelsNewRequest,
    PartyPlayerStatus, PartyDetailsResponseData, PartyDetailsResponse,
    PartyLeaveRequest, PartyKickRequest, PartyStatusUpdateRequest,
    GamesCustomNewRequest, GamesCustomNewResponse,
)

logger = logging.getLogger("ClientRoutes")
router = APIRouter()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _auth_hdr(authorization: Optional[str]) -> Optional[str]:
    """Strip 'Bearer ' prefix if present."""
    if not authorization:
        return None
    return authorization.removeprefix("Bearer ").strip() or None


# ===========================================================================
# PARTY ROUTES
# ===========================================================================

@router.post("/party/create", response_model=PartyCreateResponse)
async def party_create(
    request: PartyCreateRequest, authorization: Optional[str] = Header(None)
):
    steam_id = await auth.get_steam_id(_auth_hdr(authorization))

    # Spawn a dedicated server (async — it will call /server/ready when ready)
    port = spawn_server(
        region=request.region,
        name=request.partyName,
        is_public=request.isPublic,
    )

    game_id, party = ps.create_party(
        region=request.region,
        party_name=request.partyName,
        is_public=request.isPublic,
        host_steam_id=steam_id,
        port=port if port > 0 else 0,
    )

    # If the exe wasn't found (port=-1), still create the party in pending state
    # so the game client can poll for it.
    return PartyCreateResponse(
        data=JoinData(
            ipAddress=party["ipAddress"],
            port=party["port"],
            sessionId=game_id,
        )
    )


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
async def players_get(req: PlayersGetRequest):
    """
    Bulk-fetch player profiles.
    Unknown players get a guest profile — no DB records are created.
    This endpoint is called with ALL friend/lobby steam IDs, so we must
    never auto-create accounts here.
    """
    data = await UserService.get_many(req.steamIds or [])
    return PlayersGetResponse(data=data)


# ===========================================================================
# GAME SESSION ROUTES
# ===========================================================================

@router.post("/games/new", response_model=GamesNewResponse)
async def games_new(req: GamesNewRequest):
    """Start a new public game session (not a party — used for quick-join matchmaking)."""
    session_id = str(uuid.uuid4())
    port = spawn_server(
        region=req.region,
        name=req.gameName,
        session_id=session_id,
        is_public=req.isPublic,
    )
    if port == -1:
        return GamesNewResponse(status=500, message="Failed to spawn server", data=None)

    # Register a party entry so clients can see it
    import services.party_service as ps2
    game_id, party = ps2.create_party(
        region=req.region,
        party_name=req.gameName,
        is_public=req.isPublic,
        host_steam_id="server",
        port=port,
    )
    return GamesNewResponse(
        data=JoinData(ipAddress=party["ipAddress"], port=port, sessionId=game_id)
    )


@router.post("/games/join", response_model=GamesJoinResponse)
async def games_join(request: GamesJoinRequest, authorization: Optional[str] = Header(None)):
    steam_id = await auth.get_steam_id(_auth_hdr(authorization))
    join_data = ps.join_party(request.gameId, steam_id)
    if not join_data:
        return GamesJoinResponse(status=404, message="Game not found or full", data=None)
    return GamesJoinResponse(data=join_data)


@router.post("/games/custom/new", response_model=GamesCustomNewResponse)
async def games_custom_new(req: GamesCustomNewRequest):
    """Create a custom game mode server."""
    session_id = str(uuid.uuid4())
    port = spawn_server(
        region=req.region,
        name=f"Custom-{req.gamemodeName}",
        session_id=session_id,
        is_public=False,
    )
    if port == -1:
        return GamesCustomNewResponse(status=500, message="Failed to spawn server", data=None)

    _, party = ps.create_party(
        region=req.region,
        party_name=f"Custom-{req.gamemodeName}",
        is_public=False,
        host_steam_id="server",
        port=port,
    )
    return GamesCustomNewResponse(data=session_id)


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
