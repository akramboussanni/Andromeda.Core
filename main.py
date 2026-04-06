import asyncio
import uvicorn
import logging
import argparse
import os
from dotenv import load_dotenv
from utils.console import enable_ansi_colors

load_dotenv()
enable_ansi_colors()  # enable VT100 on Windows CMD — controlled by ENABLE_ANSI_COLORS in .env

from fastapi import FastAPI
from routes import shared, client, server, admin
from utils.logging_middleware import log_requests_middleware
from utils.upnp import open_port
from catalog import Catalog
import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("Server")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Initialise SQLite tables
    await db.init_db()
    logger.info("[INIT] Database ready.")

    # 2. Ensure catalog is loaded (already happens at import, but be explicit)
    Catalog.load()
    char_count = len(Catalog.get_characters())
    perk_count = len(Catalog.get_perks())
    logger.info(f"[INIT] Catalog loaded — {char_count} characters, {perk_count} perks.")

    # 3. Warn if admin panel has no password set
    if not os.getenv("ADMIN_PASSWORD") and not os.getenv("ADMIN_TOKEN"):
        logger.warning("[INIT] ADMIN_PASSWORD is not set — admin panel is open to anyone!")
    else:
        logger.info("[INIT] Admin panel auth enabled.")

    # 4. Start TCP log server and wire it into the async event loop
    try:
        from log_server import start_log_server, init_queue
        start_log_server()
        init_queue(asyncio.get_event_loop())
        logger.info("[INIT] Log server started on :9090, draining to SQLite.")
    except Exception as e:
        logger.warning(f"[INIT] Log server not started: {e}")

    # 4. Optional UPnP
    if os.getenv("ENABLE_UPNP", "false").lower() == "true":
        logger.info("[INIT] Opening UPnP ports…")
        open_port(8000, "TCP", "Parasite API")
    else:
        logger.info("[INIT] UPnP disabled.")
        
    yield

app = FastAPI(title="Parasite Private Server", version="2.0.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
app.middleware("http")(log_requests_middleware)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(shared.router)
app.include_router(client.router)
app.include_router(server.router)
app.include_router(admin.router)


@app.get("/")
async def root():
    return {"message": "Parasite Private Server", "version": "2.0.0"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parasite Private Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="Hot-reload (dev only)")
    args = parser.parse_args()

    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
