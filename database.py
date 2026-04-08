import json
import logging
import os
import time
from typing import List, Optional

import aiosqlite
from db_migrations import run_migrations
from models import PlayerCharacterData, PlayerCharacterLevelData, PlayerData

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
    """Apply pending schema migrations."""
    async with get_db() as db:
        await run_migrations(db, logger=logger)
    logger.info("[DB] Migrations applied.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_player(row, char_rows) -> PlayerData:
    """Convert raw DB rows into a PlayerData model."""
    wins = row["wins"] if row["wins"] is not None else 0
    losses = row["losses"] if row["losses"] is not None else 0

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
        wins=wins,
        losses=losses,
        kickstarterBacker=bool(row["kickstarter_backer"]),
        nameColor=row["name_color"],
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
                items_json, name_color, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                player.steamId,
                player.rank,
                player.credits,
                player.funds,
                player.totalGames,
                int(player.kickstarterBacker),
                json.dumps(player.items),
                player.nameColor,
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
                items_json, name_color, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(steam_id) DO UPDATE SET
                   rank=excluded.rank,
                   credits=excluded.credits,
                   funds=excluded.funds,
                   total_games=excluded.total_games,
                   kickstarter_backer=excluded.kickstarter_backer,
                   items_json=excluded.items_json,
                   name_color=excluded.name_color,
                   updated_at=excluded.updated_at""",
            (
                player.steamId,
                player.rank,
                player.credits,
                player.funds,
                player.totalGames,
                int(player.kickstarterBacker),
                json.dumps(player.items),
                player.nameColor,
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


async def upsert_discord_link(discord_user_id: str, steam_id: str, discord_username: str = "", status: str = "pending"):
    """Create a pending Discord -> Steam link. Must be confirmed in-game."""
    now = time.time()
    async with get_db() as db:
        # If this exact pair is currently blocked, preserve the block instead of recreating the row.
        async with db.execute(
            """
            SELECT blocked_until, blocked_forever FROM discord_links
            WHERE discord_user_id = ? AND steam_id = ?
            LIMIT 1
            """,
            (discord_user_id, steam_id),
        ) as cur:
            existing = await cur.fetchone()

        if existing:
            blocked_until = float(existing["blocked_until"] or 0)
            blocked_forever = int(existing["blocked_forever"] or 0) == 1
            if blocked_forever or blocked_until > now:
                await db.execute(
                    """
                    UPDATE discord_links
                    SET discord_username = ?, updated_at = ?
                    WHERE discord_user_id = ? AND steam_id = ?
                    """,
                    (discord_username, now, discord_user_id, steam_id),
                )
                await db.commit()
                return

        # Delete conflicting links
        await db.execute("DELETE FROM discord_links WHERE discord_user_id = ? OR steam_id = ?", (discord_user_id, steam_id))
        await db.execute(
            """
            INSERT INTO discord_links 
            (discord_user_id, steam_id, status, discord_username, blocked_until, blocked_forever, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (discord_user_id, steam_id, status, discord_username, 0, 0, now, now),
        )
        await db.commit()


async def get_steam_by_discord(discord_user_id: str) -> Optional[str]:
    """Get confirmed Steam ID for a Discord user."""
    async with get_db() as db:
        async with db.execute(
            "SELECT steam_id FROM discord_links WHERE discord_user_id = ? AND status = 'confirmed'",
            (discord_user_id,),
        ) as cur:
            row = await cur.fetchone()
            return row["steam_id"] if row else None


async def get_discord_by_steam_ids(steam_ids: List[str]) -> dict:
    """Get confirmed Discord IDs for Steam IDs."""
    if not steam_ids:
        return {}
    placeholders = ",".join("?" * len(steam_ids))
    async with get_db() as db:
        async with db.execute(
            f"SELECT discord_user_id, steam_id FROM discord_links WHERE steam_id IN ({placeholders}) AND status = 'confirmed'",
            steam_ids,
        ) as cur:
            rows = await cur.fetchall()
    return {row["steam_id"]: row["discord_user_id"] for row in rows}


async def get_all_discord_links() -> list:
    async with get_db() as db:
        async with db.execute(
            "SELECT discord_user_id, steam_id, created_at, updated_at FROM discord_links WHERE status = 'confirmed' ORDER BY updated_at DESC"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def get_discord_link_record_by_discord(discord_user_id: str) -> Optional[dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT discord_user_id, steam_id, status, discord_username, blocked_until, blocked_forever, created_at, updated_at FROM discord_links WHERE discord_user_id = ? LIMIT 1",
            (discord_user_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_discord_link_record_by_steam(steam_id: str) -> Optional[dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT discord_user_id, steam_id, status, discord_username, blocked_until, blocked_forever, created_at, updated_at FROM discord_links WHERE steam_id = ? LIMIT 1",
            (steam_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def delete_discord_link(discord_user_id: str) -> int:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM discord_links WHERE discord_user_id = ?", (discord_user_id,))
        await db.commit()
        return int(cur.rowcount or 0)


async def delete_discord_link_by_steam(steam_id: str) -> int:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM discord_links WHERE steam_id = ? AND status = 'confirmed'", (steam_id,))
        await db.commit()
        return int(cur.rowcount or 0)


async def get_pending_links_for_steam(steam_id: str) -> list:
    """Get pending link requests for a Steam ID (for in-game popup)."""
    now = time.time()
    async with get_db() as db:
        async with db.execute(
            """
            SELECT discord_user_id, discord_username FROM discord_links 
            WHERE steam_id = ? 
            AND status = 'pending'
            AND blocked_forever = 0
            AND (blocked_until = 0 OR blocked_until < ?)
            """,
            (steam_id, now),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def get_all_pending_links() -> list:
    now = time.time()
    async with get_db() as db:
        async with db.execute(
            """
            SELECT discord_user_id, steam_id, discord_username
            FROM discord_links
            WHERE status = 'pending'
            AND blocked_forever = 0
            AND (blocked_until = 0 OR blocked_until < ?)
            ORDER BY updated_at DESC
            """,
            (now,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def confirm_discord_link(discord_user_id: str, steam_id: str) -> bool:
    """Confirm a pending link (called from in-game)."""
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE discord_links SET status = 'confirmed' WHERE discord_user_id = ? AND steam_id = ?",
            (discord_user_id, steam_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def block_link_for_24h(discord_user_id: str, steam_id: str) -> bool:
    """Block a link request for 24 hours."""
    now = time.time()
    blocked_until = now + 86400  # 24 hours
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE discord_links SET blocked_until = ? WHERE discord_user_id = ? AND steam_id = ?",
            (blocked_until, discord_user_id, steam_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def block_link_forever(discord_user_id: str, steam_id: str) -> bool:
    """Block a link request permanently."""
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE discord_links SET blocked_forever = 1 WHERE discord_user_id = ? AND steam_id = ?",
            (discord_user_id, steam_id),
        )
        await db.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Wins/Losses
# ---------------------------------------------------------------------------

async def increment_win(steam_id: str) -> bool:
    """Increment wins for a player. Returns True if successful."""
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE players SET wins = wins + 1 WHERE steam_id = ?",
            (steam_id,),
        )
        await db.commit()
        return cur.rowcount > 0


async def increment_loss(steam_id: str) -> bool:
    """Increment losses for a player. Returns True if successful."""
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE players SET losses = losses + 1 WHERE steam_id = ?",
            (steam_id,),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_leaderboard(limit: int = 50) -> List[dict]:
    """Get top players sorted by wins (descending), then by win rate."""
    async with get_db() as db:
        async with db.execute(
            """
            SELECT 
                steam_id, 
                wins, 
                losses,
                (wins + losses) as total_games,
                CASE 
                    WHEN (wins + losses) > 0 THEN ROUND(100.0 * wins / (wins + losses), 2)
                    ELSE 0
                END as win_rate
            FROM players 
            WHERE (wins + losses) > 0
            ORDER BY wins DESC, win_rate DESC 
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(row) for row in rows]
