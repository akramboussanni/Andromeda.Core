import aiosqlite
import json
import time
import os
import logging
from typing import Optional, List
from models import PlayerData, PlayerCharacterData, PlayerCharacterLevelData

logger = logging.getLogger("Database")

DB_PATH = os.getenv("DB_PATH", "./parasite.db")


from contextlib import asynccontextmanager

@asynccontextmanager
async def get_db():
    """Yields a configured aiosqlite connection."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")   # concurrent reads
        await db.execute("PRAGMA synchronous=NORMAL") # faster writes, still safe
        await db.execute("PRAGMA foreign_keys=ON")
        yield db


async def init_db():
    """Create tables if they don't exist."""
    async with get_db() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS players (
                steam_id          TEXT PRIMARY KEY,
                rank              INTEGER NOT NULL DEFAULT 1,
                credits           REAL    NOT NULL DEFAULT 999999,
                funds             REAL    NOT NULL DEFAULT 999999,
                total_games       INTEGER NOT NULL DEFAULT 0,
                kickstarter_backer INTEGER NOT NULL DEFAULT 1,
                items_json        TEXT    NOT NULL DEFAULT '[]',
                created_at        REAL    NOT NULL,
                updated_at        REAL    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS player_characters (
                steam_id          TEXT    NOT NULL,
                char_guid         TEXT    NOT NULL,
                ascension         INTEGER NOT NULL DEFAULT 0,
                level             INTEGER NOT NULL DEFAULT 0,
                abilities_json    TEXT    NOT NULL DEFAULT '[]',
                perks_json        TEXT    NOT NULL DEFAULT '[]',
                skins_json        TEXT    NOT NULL DEFAULT '[]',
                level_history_json TEXT   NOT NULL DEFAULT '[]',
                pending_level_json TEXT,
                PRIMARY KEY (steam_id, char_guid)
            );

            CREATE TABLE IF NOT EXISTS match_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                steam_id        TEXT    NOT NULL,
                timestamp       TEXT    NOT NULL DEFAULT '',
                game_id         TEXT    NOT NULL DEFAULT '',
                game_length     REAL    NOT NULL DEFAULT 0,
                aliens_won      INTEGER NOT NULL DEFAULT 0,
                crew_won        INTEGER NOT NULL DEFAULT 0,
                was_alien       INTEGER NOT NULL DEFAULT 0,
                character_guid  TEXT    NOT NULL DEFAULT '',
                ability_guid    TEXT    NOT NULL DEFAULT '',
                item_guid       TEXT    NOT NULL DEFAULT '',
                alien_guid      TEXT    NOT NULL DEFAULT '',
                perk_a          TEXT    NOT NULL DEFAULT '',
                perk_b          TEXT    NOT NULL DEFAULT '',
                perk_c          TEXT    NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_match_steam ON match_history(steam_id);
            CREATE INDEX IF NOT EXISTS idx_char_steam  ON player_characters(steam_id);
        """)
        await db.commit()
    logger.info("[DB] Tables initialised.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_player(row, char_rows) -> PlayerData:
    """Convert raw DB rows into a PlayerData model."""
    characters: List[PlayerCharacterData] = []
    for cr in char_rows:
        history_raw = json.loads(cr["level_history_json"] or "[]")
        pending_raw = json.loads(cr["pending_level_json"]) if cr["pending_level_json"] else None

        history = [PlayerCharacterLevelData(**h) for h in history_raw]
        pending = PlayerCharacterLevelData(**pending_raw) if pending_raw else None

        characters.append(PlayerCharacterData(
            guid=cr["char_guid"],
            ascension=cr["ascension"],
            level=cr["level"],
            abilities=json.loads(cr["abilities_json"]),
            perks=json.loads(cr["perks_json"]),
            skins=json.loads(cr["skins_json"]),
            levelHistory=history,
            pendingLevel=pending,
        ))

    return PlayerData(
        steamId=row["steam_id"],
        rank=row["rank"],
        credits=row["credits"],
        funds=row["funds"],
        items=json.loads(row["items_json"]),
        characters=characters,
        totalGames=row["total_games"],
        kickstarterBacker=bool(row["kickstarter_backer"]),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def player_exists(steam_id: str) -> bool:
    async with get_db() as db:
        async with db.execute(
            "SELECT 1 FROM players WHERE steam_id = ?", (steam_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def get_player(steam_id: str) -> Optional[PlayerData]:
    """
    Return PlayerData for an existing player, or None if not found.
    DOES NOT create new records.
    """
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM players WHERE steam_id = ?", (steam_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None

        async with db.execute(
            "SELECT * FROM player_characters WHERE steam_id = ? ORDER BY char_guid",
            (steam_id,),
        ) as cur:
            char_rows = await cur.fetchall()

    return _row_to_player(row, char_rows)


async def get_players_batch(steam_ids: List[str]) -> List[PlayerData]:
    """
    Fetch multiple players in a single query.
    Returns only rows that actually exist — callers decide what to do with missing ones.
    """
    if not steam_ids:
        return []
    placeholders = ",".join("?" * len(steam_ids))
    async with get_db() as db:
        async with db.execute(
            f"SELECT * FROM players WHERE steam_id IN ({placeholders})", steam_ids
        ) as cur:
            player_rows = {r["steam_id"]: r for r in await cur.fetchall()}

        if not player_rows:
            return []

        found_ids = list(player_rows.keys())
        placeholders2 = ",".join("?" * len(found_ids))
        async with db.execute(
            f"SELECT * FROM player_characters WHERE steam_id IN ({placeholders2})"
            " ORDER BY steam_id, char_guid",
            found_ids,
        ) as cur:
            char_rows_all = await cur.fetchall()

    # Group chars by steam_id
    chars_by_id: dict = {}
    for cr in char_rows_all:
        chars_by_id.setdefault(cr["steam_id"], []).append(cr)

    return [
        _row_to_player(row, chars_by_id.get(sid, []))
        for sid, row in player_rows.items()
    ]


async def create_player(player: PlayerData):
    """Insert a brand-new player + their characters. Idempotent if already exists."""
    now = time.time()
    async with get_db() as db:
        await db.execute(
            """INSERT OR IGNORE INTO players
               (steam_id, rank, credits, funds, total_games, kickstarter_backer,
                items_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                player.steamId,
                player.rank,
                player.credits,
                player.funds,
                player.totalGames,
                int(player.kickstarterBacker),
                json.dumps(player.items),
                now,
                now,
            ),
        )
        for ch in player.characters:
            history_raw = [h.dict() for h in ch.levelHistory]
            pending_raw = ch.pendingLevel.dict() if ch.pendingLevel else None
            await db.execute(
                """INSERT OR IGNORE INTO player_characters
                   (steam_id, char_guid, ascension, level,
                    abilities_json, perks_json, skins_json,
                    level_history_json, pending_level_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    player.steamId,
                    ch.guid,
                    ch.ascension,
                    ch.level,
                    json.dumps(ch.abilities),
                    json.dumps(ch.perks),
                    json.dumps(ch.skins),
                    json.dumps(history_raw),
                    json.dumps(pending_raw) if pending_raw else None,
                ),
            )
        await db.commit()


async def save_player(player: PlayerData):
    """
    Upsert a player record.  Uses a single transaction so partial writes can't happen.
    """
    now = time.time()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO players
               (steam_id, rank, credits, funds, total_games, kickstarter_backer,
                items_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(steam_id) DO UPDATE SET
                   rank=excluded.rank,
                   credits=excluded.credits,
                   funds=excluded.funds,
                   total_games=excluded.total_games,
                   kickstarter_backer=excluded.kickstarter_backer,
                   items_json=excluded.items_json,
                   updated_at=excluded.updated_at""",
            (
                player.steamId,
                player.rank,
                player.credits,
                player.funds,
                player.totalGames,
                int(player.kickstarterBacker),
                json.dumps(player.items),
                now,
                now,
            ),
        )
        for ch in player.characters:
            history_raw = [h.dict() for h in ch.levelHistory]
            pending_raw = ch.pendingLevel.dict() if ch.pendingLevel else None
            await db.execute(
                """INSERT INTO player_characters
                   (steam_id, char_guid, ascension, level,
                    abilities_json, perks_json, skins_json,
                    level_history_json, pending_level_json)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(steam_id, char_guid) DO UPDATE SET
                       ascension=excluded.ascension,
                       level=excluded.level,
                       abilities_json=excluded.abilities_json,
                       perks_json=excluded.perks_json,
                       skins_json=excluded.skins_json,
                       level_history_json=excluded.level_history_json,
                       pending_level_json=excluded.pending_level_json""",
                (
                    player.steamId,
                    ch.guid,
                    ch.ascension,
                    ch.level,
                    json.dumps(ch.abilities),
                    json.dumps(ch.perks),
                    json.dumps(ch.skins),
                    json.dumps(history_raw),
                    json.dumps(pending_raw) if pending_raw else None,
                ),
            )
        await db.commit()


async def add_match_history_entries(entries: list):
    """Bulk-insert end-of-game stats rows."""
    if not entries:
        return
    async with get_db() as db:
        await db.executemany(
            """INSERT INTO match_history
               (steam_id, timestamp, game_id, game_length, aliens_won, crew_won,
                was_alien, character_guid, ability_guid, item_guid, alien_guid,
                perk_a, perk_b, perk_c)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            entries,
        )
        await db.commit()


async def get_match_history(steam_id: str, limit: int = 50) -> list:
    """Return the most recent matches for a player."""
    async with get_db() as db:
        async with db.execute(
            """SELECT * FROM match_history WHERE steam_id = ?
               ORDER BY id DESC LIMIT ?""",
            (steam_id, limit),
        ) as cur:
            return await cur.fetchall()
