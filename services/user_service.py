import logging
from typing import List, Optional

import database as db
from catalog import Catalog
from models import PlayerCharacterData, PlayerData
from services.progression_service import ProgressionService

logger = logging.getLogger("UserService")


def _build_default_player(steam_id: str) -> PlayerData:
    """
    Build new player data in memory — does NOT touch the DB.
    Grants all characters at level 1 (ability unlocked), all items and skins.
    """
    characters: List[PlayerCharacterData] = []

    for char_def in Catalog.get_characters():
        grant_ability, level_history = ProgressionService.generate_level_1_logic(
            char_def.guid, 0
        )
        characters.append(
            PlayerCharacterData(
                guid=char_def.guid,
                ascension=0,
                level=1,
                abilities=[grant_ability] if grant_ability else [],
                perks=[],
                skins=list(char_def.skins),
                levelHistory=level_history,
                pendingLevel=None,
            )
        )

    items = [i.guid for i in Catalog.get_items()] + [s.guid for s in Catalog.get_skins()]

    return PlayerData(
        steamId=steam_id,
        rank=1,
        credits=999999.0,
        funds=999999.0,
        items=items,
        characters=characters,
        totalGames=0,
        kickstarterBacker=True,
    )


def _build_guest_player(steam_id: str) -> PlayerData:
    """
    Return a lightweight in-memory profile for unknown players
    (e.g. friends in lobby) WITHOUT writing anything to the database.
    Contains enough data for the game to display rank/credits in the HUD.
    """
    return PlayerData(
        steamId=steam_id,
        rank=1,
        credits=0.0,
        funds=0.0,
        items=[],
        characters=[],
        totalGames=0,
        kickstarterBacker=False,
    )


class UserService:

    @classmethod
    async def get_or_create(cls, steam_id: str) -> PlayerData:
        """
        Used on auth — creates a full account if the player is new.
        This is the ONLY place that writes a new player to the DB.
        """
        player = await db.get_player(steam_id)
        if player is None:
            logger.info(f"[USER] New player: {steam_id} — creating account.")
            player = _build_default_player(steam_id)
            await db.create_player(player)
        return player

    @classmethod
    async def get(cls, steam_id: str) -> Optional[PlayerData]:
        """
        Fetch an existing player. Returns None if not found.
        Does NOT create a new record.
        """
        return await db.get_player(steam_id)

    @classmethod
    async def get_many(cls, steam_ids: List[str]) -> List[PlayerData]:
        """
        Bulk-fetch players. For IDs not in the DB, returns a lightweight
        guest profile so the game has something valid to display without
        polluting the database with blank accounts.
        """
        if not steam_ids:
            return []

        existing = await db.get_players_batch(steam_ids)
        existing_ids = {p.steamId for p in existing}

        result = list(existing)
        for sid in steam_ids:
            if sid not in existing_ids:
                result.append(_build_guest_player(sid))

        return result

    @classmethod
    async def save(cls, player: PlayerData):
        await db.save_player(player)
