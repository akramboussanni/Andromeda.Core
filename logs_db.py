"""
Persistent log storage backed by SQLite.
Replaces the in-memory deque for anything that needs to survive restarts
or handle high volume with efficient filtering.
"""
import json
import time
from typing import Optional

from database import get_db

# Fields we pull out of the JSON into their own columns for indexing/filtering.
_INDEXED_FIELDS = {"level", "service", "steam_id", "session_id",
                   "game_name", "game_mode", "game_region", "version", "timestamp"}


async def ingest(entry: dict) -> int:
    """
    Persist a single structured log entry.
    Returns the new row id.
    """
    level   = str(entry.get("level") or "info").lower()
    ts      = entry.get("timestamp")
    service = entry.get("service")
    steam_id   = entry.get("steam_id")
    session_id = entry.get("session_id")
    game_name  = entry.get("game_name")
    game_mode  = entry.get("game_mode")
    game_region = entry.get("game_region")
    version     = entry.get("version")
    message     = str(entry.get("message") or "")

    # Stash everything else in extra_json so nothing is lost
    extra = {k: v for k, v in entry.items() if k not in _INDEXED_FIELDS and k != "message"}
    extra_json = json.dumps(extra, ensure_ascii=False)

    async with get_db() as db:
        cur = await db.execute(
            """INSERT INTO log_entries
               (received_at, ts, level, service, steam_id, session_id,
                game_name, game_mode, game_region, version, message, extra_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), ts, level, service, steam_id, session_id,
             game_name, game_mode, game_region, version, message, extra_json),
        )
        await db.commit()
        return cur.lastrowid


async def query(
    *,
    level:      Optional[str] = None,
    steam_id:   Optional[str] = None,
    session_id: Optional[str] = None,
    search:     Optional[str] = None,
    after_id:   Optional[int] = None,   # for "load older" pagination
    before_id:  Optional[int] = None,   # for "load newer" pagination
    limit: int = 100,
) -> tuple[list[dict], int]:
    """
    Return (rows, total_matching_count).
    Rows are ordered newest-first.
    """
    clauses = []
    params  = []

    if level:
        clauses.append("level = ?")
        params.append(level.lower())
    if steam_id:
        clauses.append("steam_id = ?")
        params.append(steam_id)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if search:
        clauses.append("(message LIKE ? OR extra_json LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if after_id:
        clauses.append("id < ?")
        params.append(after_id)
    if before_id:
        clauses.append("id > ?")
        params.append(before_id)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    async with get_db() as db:
        async with db.execute(
            f"SELECT COUNT(*) as c FROM log_entries {where}", params
        ) as cur:
            total = (await cur.fetchone())["c"]

        async with db.execute(
            f"""SELECT id, received_at, ts, level, service, steam_id, session_id,
                       game_name, game_mode, game_region, version, message, extra_json
                FROM log_entries {where}
                ORDER BY id DESC LIMIT ?""",
            params + [limit],
        ) as cur:
            rows = await cur.fetchall()

    result = []
    for r in rows:
        row = dict(r)
        try:
            row["extra"] = json.loads(row.pop("extra_json") or "{}")
        except Exception:
            row["extra"] = {}
        result.append(row)

    return result, total


async def get_known_steam_ids() -> list[str]:
    """Return all distinct steam_ids seen in logs."""
    async with get_db() as db:
        async with db.execute(
            "SELECT DISTINCT steam_id FROM log_entries WHERE steam_id IS NOT NULL ORDER BY steam_id"
        ) as cur:
            rows = await cur.fetchall()
    return [r["steam_id"] for r in rows]


async def delete_before(received_at: float) -> int:
    """Prune log entries older than the given Unix timestamp. Returns count deleted."""
    async with get_db() as db:
        cur = await db.execute(
            "DELETE FROM log_entries WHERE received_at < ?", (received_at,)
        )
        await db.commit()
        return cur.rowcount
