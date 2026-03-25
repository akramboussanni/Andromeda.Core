import argparse
import asyncio
import logging
from typing import List

import database as db
from services.user_service import _normalize_player_default_skins

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GrantDefaultSkins")


async def _fetch_all_steam_ids() -> List[str]:
    async with db.get_db() as conn:
        async with conn.execute("SELECT steam_id FROM players ORDER BY steam_id") as cur:
            rows = await cur.fetchall()
    return [row["steam_id"] for row in rows]


async def grant_default_skins(steam_id: str = "", dry_run: bool = False) -> int:
    await db.init_db()

    if steam_id:
        steam_ids = [steam_id]
    else:
        steam_ids = await _fetch_all_steam_ids()

    if not steam_ids:
        logger.info("No players found.")
        return 0

    changed_count = 0

    for sid in steam_ids:
        player = await db.get_player(sid)
        if player is None:
            logger.warning(f"Skipping missing player: {sid}")
            continue

        if _normalize_player_default_skins(player):
            changed_count += 1
            if not dry_run:
                await db.save_player(player)
                logger.info(f"Updated default skins for {sid}")
            else:
                logger.info(f"Would update default skins for {sid}")

    if dry_run:
        logger.info(f"Dry run complete. Players needing updates: {changed_count}")
    else:
        logger.info(f"Completed. Players updated: {changed_count}")

    return changed_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill default skins for existing player profiles.")
    parser.add_argument(
        "--steam-id",
        default="",
        help="Optional single Steam ID to update. If omitted, updates all players.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview how many players would be updated without writing changes.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(grant_default_skins(steam_id=args.steam_id, dry_run=args.dry_run))
