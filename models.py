from typing import List, Optional

from pydantic import BaseModel

# --- Shared Models ---

class Request(BaseModel):
    pass

class Response(BaseModel):
    status: int = 200
    message: str = "OK"
    data: Optional[object] = None

class AbilityData(BaseModel):
    guid: str
    tier: int = 1
    previousGuid: Optional[str] = None

class CharacterData(BaseModel):
    guid: str
    abilities: List[str] = []
    perks: List[str] = []
    skins: List[str] = []
    fundCost: float = 0.0
    creditCost: float = 0.0
    purchasable: bool = True

class ItemData(BaseModel):
    guid: str
    cost: float = 0.0
    purchasable: bool = True

class PerkData(BaseModel):
    guid: str
    tier: int = 1
    previousGuid: Optional[str] = None

class SkinData(BaseModel):
    guid: str
    cost: float = 0.0
    purchasable: bool = True

class PlayerCharacterLevelData(BaseModel):
    offeredAbilities: List[str] = []
    offeredPerks: List[str] = []
    chosenAbility: Optional[str] = None
    chosenPerk: Optional[str] = None

class PlayerCharacterData(BaseModel):
    guid: str
    ascension: int
    level: int
    abilities: List[str]
    perks: List[str]
    skins: List[str]
    levelHistory: List[PlayerCharacterLevelData] = []
    pendingLevel: Optional[PlayerCharacterLevelData] = None

class PlayerData(BaseModel):
    steamId: str
    rank: int
    credits: float
    funds: float
    items: List[str]
    characters: List[PlayerCharacterData]
    totalGames: int
    kickstarterBacker: bool

class RegionData(BaseModel):
    region: str
    averageWaitTime: int = 0

class JoinData(BaseModel):
    ipAddress: str
    port: int
    voicePort: Optional[int] = None
    sessionId: str

class GamemodeData(BaseModel):
    pass 

class FundOfferData(BaseModel):
    guid: str
    cost: int
    amount: float
    description: str

class LevelData(BaseModel):
    ascension: float
    order: float
    unlockType: str
    cost: float

class PlayersGamesGetData(BaseModel):
    timestamp: str
    gameId: int
    gameLength: float
    aliensWon: bool
    crewWon: bool
    wasAlien: bool
    character: str
    ability: str
    item: str
    alien: str
    perkA: str
    perkB: str
    perkC: str

# --- Client Requests ---

class GamesNewRequest(Request):
    version: str
    region: str
    isPublic: bool
    maxPlayers: int
    gameName: str
    gamemodeName: str
    gamemodeData: Optional[GamemodeData] = None

class GamesJoinRequest(Request):
    version: str
    region: str
    gameId: str

class MatchStartRequest(Request):
    version: str
    region: str
    steamIds: List[str]

class PlayersAuthGetRequest(Request):
    authToken: str

class PlayersGetRequest(Request):
    steamIds: List[str]

class FundsOffersGetRequest(Request):
    pass

class VersionCheckRequest(Request):
    version: str

class MatchInfoRequest(Request):
    version: Optional[str] = None
    regions: Optional[List[str]] = None

class AbilitiesGetRequest(Request):
    pass

class CharactersGetRequest(Request):
    pass

class ItemsGetRequest(Request):
    pass

class PerksGetRequest(Request):
    pass

class SkinsGetRequest(Request):
    pass

class LevelsGetRequest(Request):
    pass

class GamesCustomNewRequest(Request):
    version: Optional[str] = None
    region: str
    maxPlayers: int = 12
    gamemodeName: str
    gamemodeData: Optional[object] = None

class GamesStatsGetRequest(Request):
    gameId: str

class GameStatsGetData(BaseModel):
    username: str = ""
    steamId: str = ""
    wasAlien: bool = False
    character: str = ""
    ability: str = ""
    item: str = ""
    alien: str = ""
    perkA: str = ""
    perkB: str = ""
    perkC: str = ""
    damageDone: float = 0
    healingDone: float = 0
    kills: int = 0
    generatorsRepaired: int = 0
    kickstarterBacker: bool = False

class PlayersGamesGetRequest(Request):
    steamId: str

class CharactersLevelsGetRequest(Request):
    steamId: str
    characterGuid: str

class CharactersLevelsUnlockRequest(Request):
    characterGuid: str
    abilityGuid: Optional[str] = None
    perkGuid: Optional[str] = None

class CharactersLevelsNewRequest(Request):
    characterGuid: str


# --- Server Requests ---

class StatsNewRequestPlayer(BaseModel):
    steamId: str
    name: str
    creditsEarned: float
    wasAlien: bool
    wasLeaver: bool
    damageDone: float
    healingDone: float
    kills: int
    generatorsRepaired: int
    characterGuid: str
    itemGuid: str
    alienGuid: str
    abilityGuid: str
    perkA: str
    perkB: str
    perkC: str

class StatsNewRequest(Request):
    aliensWon: bool
    crewWon: bool
    wasPublic: bool
    gameLength: float
    players: List[StatsNewRequestPlayer]

class ServerReadyRequest(Request):
    sessionId: str
    port: int
    region: str

class ServerReadyResponse(Response):
    pass

class ServerHeartbeatRequest(Request):
    sessionId: str

class ServerShutdownRequest(Request):
    sessionId: str
    reason: Optional[str] = None

# --- Response Wrappers ---

class PlayersAuthGetResponse(Response):
    data: Optional[PlayerData] = None

class PlayersGetResponse(Response):
    data: List[PlayerData]

class VersionCheckResponse(Response):
    data: bool

class GamesNewResponse(Response):
    data: Optional[JoinData] = None

class GamesJoinResponse(Response):
    data: Optional[JoinData] = None

# Added missing Response Wrappers
class AbilitiesGetResponse(Response):
    data: List[AbilityData]

class CharactersGetResponse(Response):
    data: List[CharacterData]

class ItemsGetResponse(Response):
    data: List[ItemData]

class PerksGetResponse(Response):
    data: List[PerkData]

class SkinsGetResponse(Response):
    data: List[SkinData]

class LevelsGetResponse(Response):
    data: List[LevelData]

class PlayersGamesGetResponse(Response):
    data: List[PlayersGamesGetData]



class CharactersLevelsGetResponse(Response):
    data: List[PlayerCharacterLevelData]

class FundsOffersGetResponse(Response):
    data: List[FundOfferData] = []

class GamesCustomNewResponse(Response):
    data: Optional[str] = None

class GamesStatsGetResponse(Response):
    data: List[GameStatsGetData] = []

class MatchInfoResponse(Response):
    data: List[RegionData] = []

class MatchStartResponseData(BaseModel):
    gameId: str
    matchId: str
    waitTime: int

class MatchStartResponse(Response):
    data: MatchStartResponseData

class CharactersLevelsNewResponseData(BaseModel):
    offeredPerks: List[str]
    offeredAbilities: List[str]

class CharactersLevelsNewResponse(Response):
    data: CharactersLevelsNewResponseData

class StatsNewResponsePlayer(BaseModel):
    steamId: str
    rank: int
    creditsEarned: float

class StatsNewResponse(Response):
    data: List[StatsNewResponsePlayer]

# --- Party Models ---

# --- Party Models ---

class PartyPlayerStatus(BaseModel):
    steamId: str
    username: str
    isReady: bool = False
    isHost: bool = False

class PartyDetailsResponseData(BaseModel):
    gameId: str
    region: str
    partyName: str
    maxPlayers: int
    isPublic: bool
    hostSteamId: str
    players: List[PartyPlayerStatus]

class PartyDetailsResponse(Response):
    data: Optional[PartyDetailsResponseData] = None

class PartyCreateRequest(Request):
    version: str
    region: str
    partyName: str
    partyToken: Optional[str] = None
    isPublic: bool

class PartyCreateResponse(Response):
    data: Optional[JoinData] = None

class PartyJoinRequest(Request):
    version: str
    region: str
    gameId: str

class PartyJoinResponse(Response):
    data: Optional[JoinData] = None

class PartyLeaveRequest(Request):
    gameId: str

class PartyKickRequest(Request):
    gameId: str
    targetSteamId: str

class PartyStatusUpdateRequest(Request):
    gameId: str
    isReady: bool

class PartyListRequest(Request):
    version: str
    regions: List[str]

class PartyListResponseData(BaseModel):
    gameId: str
    region: str
    partyName: str
    currentPlayers: int
    maxPlayers: int

class PartyListResponse(Response):
    data: List[PartyListResponseData]

class AnalyticsNewRequestData(BaseModel):
    os: str
    resolution: str
    timezone: int
    language: str

class AnalyticsNewRequest(Request):
    userId: str
    data: AnalyticsNewRequestData

class EmailExistsRequest(Request):
    pass

class EmailExistsResponse(Response):
    data: bool
