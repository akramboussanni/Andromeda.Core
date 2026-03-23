import logging
from fastapi import APIRouter
from models import (
    AbilitiesGetResponse,
    CharactersGetResponse,
    ItemsGetResponse,
    PerksGetResponse,
    SkinsGetResponse,
    LevelsGetResponse, LevelData,
    VersionCheckRequest, VersionCheckResponse,
    MatchInfoRequest, MatchInfoResponse, RegionData,
    PlayersGamesGetRequest, PlayersGamesGetResponse, PlayersGamesGetData,
    GamesStatsGetRequest, GamesStatsGetResponse, GameStatsGetData,
)
from catalog import Catalog
import database as db

logger = logging.getLogger("SharedRoutes")
router = APIRouter()

# Levels structure: matches the game's expected 20-level layout.
# Must be consistent with ProgressionService level logic.
_CHAR_PERK_LEVELS = {6, 11}
_LEVELS = []
for _i in range(20):
    if _i == 0:
        _unlock = "ability"
    elif _i in _CHAR_PERK_LEVELS:
        _unlock = "character_perk"
    else:
        _unlock = "general_perk"
    _LEVELS.append(LevelData(ascension=0, order=_i + 1, unlockType=_unlock, cost=(_i + 1) * 100))


@router.post("/abilities/get", response_model=AbilitiesGetResponse)
async def abilities_get():
    return AbilitiesGetResponse(data=Catalog.get_abilities())


@router.post("/characters/get", response_model=CharactersGetResponse)
async def characters_get():
    return CharactersGetResponse(data=Catalog.get_characters())


@router.post("/items/get", response_model=ItemsGetResponse)
async def items_get():
    return ItemsGetResponse(data=Catalog.get_items())


@router.post("/perks/get", response_model=PerksGetResponse)
async def perks_get():
    return PerksGetResponse(data=Catalog.get_perks())


@router.post("/skins/get", response_model=SkinsGetResponse)
async def skins_get():
    return SkinsGetResponse(data=Catalog.get_skins())


@router.post("/levels/get", response_model=LevelsGetResponse)
async def levels_get():
    return LevelsGetResponse(data=_LEVELS)


@router.post("/version/check", response_model=VersionCheckResponse)
async def version_check(req: VersionCheckRequest = None):
    return VersionCheckResponse(data=True)


@router.post("/match/info", response_model=MatchInfoResponse)
async def match_info(req: MatchInfoRequest):
    regions = req.regions or ["us"]
    data = [RegionData(region=r, averageWaitTime=0) for r in regions]
    return MatchInfoResponse(data=data)


@router.post("/players/games/get", response_model=PlayersGamesGetResponse)
async def players_games_get(req: PlayersGamesGetRequest):
    """Return match history for a player from the SQLite database."""
    rows = await db.get_match_history(req.steamId, limit=50)
    data = [
        PlayersGamesGetData(
            timestamp=r["timestamp"],
            gameId=0,
            gameLength=r["game_length"],
            aliensWon=bool(r["aliens_won"]),
            crewWon=bool(r["crew_won"]),
            wasAlien=bool(r["was_alien"]),
            character=r["character_guid"],
            ability=r["ability_guid"],
            item=r["item_guid"],
            alien=r["alien_guid"],
            perkA=r["perk_a"],
            perkB=r["perk_b"],
            perkC=r["perk_c"],
        )
        for r in rows
    ]
    return PlayersGamesGetResponse(data=data)


@router.post("/games/stats/get", response_model=GamesStatsGetResponse)
async def games_stats_get(req: GamesStatsGetRequest):
    """End-of-game stats displayed in the results screen."""
    # For now return empty — could be enhanced to store per-game stats
    return GamesStatsGetResponse(data=[])
