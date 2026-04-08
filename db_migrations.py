import time
from dataclasses import dataclass
from typing import Awaitable, Callable, List

import aiosqlite

MigrationRunner = Callable[[aiosqlite.Connection], Awaitable[None]]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    run: MigrationRunner


def _migration_by_version(version: int) -> Migration:
    for migration in MIGRATIONS:
        if migration.version == version:
            return migration
    raise ValueError(f"Unknown migration version: {version}")


async def _ensure_migrations_table(db: aiosqlite.Connection):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at REAL NOT NULL
        )
        """
    )
    await db.commit()


async def _table_exists(db: aiosqlite.Connection, table_name: str) -> bool:
    async with db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ) as cur:
        return await cur.fetchone() is not None


async def _is_applied(db: aiosqlite.Connection, version: int) -> bool:
    async with db.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?",
        (version,),
    ) as cur:
        return await cur.fetchone() is not None


async def _mark_applied(db: aiosqlite.Connection, migration: Migration):
    await db.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
        (migration.version, migration.name, time.time()),
    )


async def _bootstrap_legacy_migration_state(db: aiosqlite.Connection, logger=None):
    """
    Production safety bootstrap:
    if schema_migrations is missing but legacy tables already exist,
    backfill migration records so baseline migrations are not re-applied.
    """
    has_players = await _table_exists(db, "players")
    has_player_characters = await _table_exists(db, "player_characters")
    has_match_history = await _table_exists(db, "match_history")
    has_log_entries = await _table_exists(db, "log_entries")

    core_schema_exists = (
        has_players
        and has_player_characters
        and has_match_history
        and has_log_entries
    )

    if core_schema_exists:
        await _mark_applied(db, _migration_by_version(1))
        if logger:
            logger.info("[DB] Bootstrapped migration history: marked v001_core_schema as applied")

    await db.commit()


async def _migration_001_core_schema(db: aiosqlite.Connection):
    await db.executescript(
        """
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

        CREATE TABLE IF NOT EXISTS log_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at REAL    NOT NULL,
            ts          TEXT,
            level       TEXT    NOT NULL DEFAULT 'info',
            service     TEXT,
            steam_id    TEXT,
            session_id  TEXT,
            game_name   TEXT,
            game_mode   TEXT,
            game_region TEXT,
            version     TEXT,
            message     TEXT    NOT NULL DEFAULT '',
            unity_message TEXT,
            stack       TEXT,
            extra_json  TEXT    NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_log_received ON log_entries(received_at DESC);
        CREATE INDEX IF NOT EXISTS idx_log_steam    ON log_entries(steam_id);
        CREATE INDEX IF NOT EXISTS idx_log_level    ON log_entries(level);
        CREATE INDEX IF NOT EXISTS idx_log_session  ON log_entries(session_id);
        """
    )


async def _migration_002_discord_links(db: aiosqlite.Connection):
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS discord_links (
            discord_user_id TEXT PRIMARY KEY,
            steam_id        TEXT UNIQUE NOT NULL,
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_discord_links_steam ON discord_links(steam_id);
        """
    )


async def _migration_003_wins_losses(db: aiosqlite.Connection):
    await db.executescript(
        """
        ALTER TABLE players ADD COLUMN wins INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE players ADD COLUMN losses INTEGER NOT NULL DEFAULT 0;
        
        CREATE INDEX IF NOT EXISTS idx_player_wins ON players(wins DESC);
        """
    )


async def _migration_004_pending_links(db: aiosqlite.Connection):
    await db.executescript(
        """
        ALTER TABLE discord_links ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
        ALTER TABLE discord_links ADD COLUMN discord_username TEXT DEFAULT '';
        ALTER TABLE discord_links ADD COLUMN blocked_until REAL DEFAULT 0;
        ALTER TABLE discord_links ADD COLUMN blocked_forever INTEGER NOT NULL DEFAULT 0;
        
        CREATE INDEX IF NOT EXISTS idx_discord_links_status ON discord_links(status);
        """
    )



async def _migration_005_player_name_color(db: aiosqlite.Connection):
    await db.executescript(
        """
        ALTER TABLE players ADD COLUMN name_color TEXT;
        """
    )


MIGRATIONS: List[Migration] = [
    Migration(1, "core_schema", _migration_001_core_schema),
    Migration(2, "discord_links", _migration_002_discord_links),
    Migration(3, "wins_losses", _migration_003_wins_losses),
    Migration(4, "pending_links", _migration_004_pending_links),
    Migration(5, "player_name_color", _migration_005_player_name_color),
]


async def run_migrations(db: aiosqlite.Connection, logger=None):
    has_migrations_table = await _table_exists(db, "schema_migrations")
    await _ensure_migrations_table(db)

    if not has_migrations_table:
        await _bootstrap_legacy_migration_state(db, logger=logger)

    for migration in MIGRATIONS:
        if await _is_applied(db, migration.version):
            continue

        if logger:
            logger.info(f"[DB] Applying migration v{migration.version:03d}_{migration.name}")

        await migration.run(db)
        await _mark_applied(db, migration)
        await db.commit()

        if logger:
            logger.info(f"[DB] Applied migration v{migration.version:03d}_{migration.name}")
