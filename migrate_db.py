import asyncio
import json
import logging
import os
import time
from models import PlayerData

# Import database module to get setup and functions
import database as db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Migrate")

OLD_JSON_PATH = os.path.join(os.path.dirname(__file__), "data", "users.json")

async def migrate():
    if not os.path.exists(OLD_JSON_PATH):
        logger.error(f"Could not find old DB at {OLD_JSON_PATH}")
        return

    logger.info("Initializing new SQLite database...")
    await db.init_db()

    logger.info(f"Loading legacy JSON: {OLD_JSON_PATH}")
    with open(OLD_JSON_PATH, "r") as f:
        try:
            users_dict = json.load(f)
        except json.JSONDecodeError:
            logger.error("Failed to parse JSON!")
            return

    count = len(users_dict)
    logger.info(f"Found {count} user records. Beginning migration...")

    start_time = time.time()
    migrated = 0

    # Group into batches to improve performance
    players_to_insert = []
    
    for steam_id, raw_data in users_dict.items():
        try:
            player = PlayerData(**raw_data)
            players_to_insert.append(player)
        except Exception as e:
            logger.warning(f"Failed to parse player {steam_id}: {e}")
            continue

        migrated += 1
        if len(players_to_insert) >= 100:
            # We don't have a bulk 'save_players', so we'll just save individually
            # but inside a single gather to parallelize somewhat
            await asyncio.gather(*(db.save_player(p) for p in players_to_insert))
            players_to_insert.clear()
            logger.info(f"Migrated {migrated}/{count}...")
            
    # Save the remainder
    if players_to_insert:
        await asyncio.gather(*(db.save_player(p) for p in players_to_insert))
        logger.info(f"Migrated {migrated}/{count}...")

    elapsed = time.time() - start_time
    logger.info(f"Migration complete! Successfully migrated {migrated} users in {elapsed:.2f} seconds.")

if __name__ == "__main__":
    asyncio.run(migrate())
