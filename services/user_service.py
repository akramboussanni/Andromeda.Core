import logging
import os
from typing import Dict, List, Optional

import database as db
from catalog import Catalog
from models import PlayerCharacterData, PlayerCharacterLevelData, PlayerData
from services.progression_service import ProgressionService

logger = logging.getLogger("UserService")

ENABLE_MAXED_PROGRESSION = os.getenv("ENABLE_MAXED_PROGRESSION", "true").lower() == "true"
MAXED_ASCENSION_VALUE = 6

def _unique_in_order(values: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _catalog_skins_by_character() -> Dict[str, List[str]]:
    return {c.guid: list(c.skins or []) for c in Catalog.get_characters()}


def _max_ability_for_character(char_guid: str) -> Optional[str]:
    progression = Catalog.get_progression()
    char_prog = progression.get("characters", {}).get(char_guid, {})
    tiers = char_prog.get("ability", {}).get("tiers", [])
    if not tiers:
        return None
    return tiers[-1]


def _max_perks_for_character(char_guid: str) -> List[str]:
    progression = Catalog.get_progression()
    char_prog = progression.get("characters", {}).get(char_guid, {})
    char_perks = [p.get("tiers", [])[-1] for p in char_prog.get("perks", []) if p.get("tiers")]
    general_perks = [p.get("tiers", [])[-1] for p in progression.get("general_perks", []) if p.get("tiers")]
    return _unique_in_order(char_perks + general_perks)


def _maxed_level_history(char_guid: str, max_ability: Optional[str]) -> List[PlayerCharacterLevelData]:
    progression = Catalog.get_progression()
    char_prog = progression.get("characters", {}).get(char_guid, {})
    char_perks = [p.get("tiers", [])[-1] for p in char_prog.get("perks", []) if p.get("tiers")]
    general_perks = [p.get("tiers", [])[-1] for p in progression.get("general_perks", []) if p.get("tiers")]

    # Keep character perk nodes aligned with normal progression layout.
    character_perk_slots = {6, 11}

    char_index = 0
    general_index = 0

    def pick_char_perk() -> Optional[str]:
        nonlocal char_index
        if char_perks:
            perk = char_perks[char_index % len(char_perks)]
            char_index += 1
            return perk
        if general_perks:
            perk = general_perks[general_index % len(general_perks)]
            return perk
        return None

    def pick_general_perk() -> Optional[str]:
        nonlocal general_index
        if general_perks:
            perk = general_perks[general_index % len(general_perks)]
            general_index += 1
            return perk
        if char_perks:
            perk = char_perks[char_index % len(char_perks)]
            return perk
        return None

    history: List[PlayerCharacterLevelData] = []

    for idx in range(ProgressionService.MAX_LEVEL_CAP):
        if idx == 0:
            history.append(
                PlayerCharacterLevelData(
                    offeredAbilities=[max_ability] if max_ability else [],
                    offeredPerks=[],
                    chosenAbility=max_ability,
                    chosenPerk=None,
                )
            )
            continue

        chosen_perk = pick_char_perk() if idx in character_perk_slots else pick_general_perk()
        history.append(
            PlayerCharacterLevelData(
                offeredAbilities=[],
                offeredPerks=[chosen_perk] if chosen_perk else [],
                chosenAbility=None,
                chosenPerk=chosen_perk,
            )
        )

    return history


def _build_character_progress(char_guid: str, base_skins: List[str]) -> PlayerCharacterData:
    if ENABLE_MAXED_PROGRESSION:
        ability = _max_ability_for_character(char_guid)
        perks = _max_perks_for_character(char_guid)
        return PlayerCharacterData(
            guid=char_guid,
            ascension=MAXED_ASCENSION_VALUE,
            level=ProgressionService.MAX_LEVEL_CAP - 1,
            abilities=[ability] if ability else [],
            perks=perks,
            skins=list(base_skins or []),
            levelHistory=_maxed_level_history(char_guid, ability),
            pendingLevel=None,
        )

    grant_ability, level_history = ProgressionService.generate_level_1_logic(char_guid, 0)
    return PlayerCharacterData(
        guid=char_guid,
        ascension=0,
        level=1,
        abilities=[grant_ability] if grant_ability else [],
        perks=[],
        skins=list(base_skins or []),
        levelHistory=level_history,
        pendingLevel=None,
    )


def _normalize_player_default_skins(player: PlayerData) -> bool:
    catalog_skins = _catalog_skins_by_character()
    changed = False
    for ch in player.characters:
        existing = list(ch.skins or [])
        if ch.guid == "cryonaut":
            existing = ["wraith_spy" if s in ("cryonaut", "Finn", "cryonoaut") else s for s in existing]

        merged = _unique_in_order(list(catalog_skins.get(ch.guid, [])) + existing)
        if merged != ch.skins:
            ch.skins = merged
            changed = True
    return changed


def _normalize_player_progression(player: PlayerData) -> bool:
    if not ENABLE_MAXED_PROGRESSION:
        return False

    changed = False
    for ch in player.characters:
        target_ability = _max_ability_for_character(ch.guid)
        target_abilities = [target_ability] if target_ability else []
        target_perks = _max_perks_for_character(ch.guid)
        target_history = _maxed_level_history(ch.guid, target_ability)

        if ch.ascension != MAXED_ASCENSION_VALUE:
            ch.ascension = MAXED_ASCENSION_VALUE
            changed = True
        if ch.level != ProgressionService.MAX_LEVEL_CAP - 1:
            ch.level = ProgressionService.MAX_LEVEL_CAP - 1
            changed = True
        if ch.abilities != target_abilities:
            ch.abilities = target_abilities
            changed = True
        if ch.perks != target_perks:
            ch.perks = target_perks
            changed = True
        if ch.pendingLevel is not None:
            ch.pendingLevel = None
            changed = True
        if ch.levelHistory != target_history:
            ch.levelHistory = target_history
            changed = True

    return changed


def _normalize_player(player: PlayerData) -> bool:
    skin_changed = _normalize_player_default_skins(player)
    progression_changed = _normalize_player_progression(player)
    return skin_changed or progression_changed


def _build_default_player(steam_id: str) -> PlayerData:
    """
    Build new player data in memory — does NOT touch the DB.
    Grants all characters at level 1 (ability unlocked), all items and skins.
    """
    characters: List[PlayerCharacterData] = []

    for char_def in Catalog.get_characters():
        characters.append(_build_character_progress(char_def.guid, list(char_def.skins)))

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
    def is_maxed_progression_enabled(cls) -> bool:
        return ENABLE_MAXED_PROGRESSION

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
        elif _normalize_player(player):
            await db.save_player(player)
        return player

    @classmethod
    async def get(cls, steam_id: str) -> Optional[PlayerData]:
        """
        Fetch an existing player. Returns None if not found.
        Does NOT create a new record.
        """
        player = await db.get_player(steam_id)
        if player is not None and _normalize_player(player):
            await db.save_player(player)
        return player

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
        changed = False
        for player in result:
            if _normalize_player(player):
                await db.save_player(player)
                changed = True

        if changed:
            logger.info("[USER] Normalized player defaults for one or more players.")

        for sid in steam_ids:
            if sid not in existing_ids:
                result.append(_build_guest_player(sid))

        return result

    @classmethod
    async def save(cls, player: PlayerData):
        await db.save_player(player)
