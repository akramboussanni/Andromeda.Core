import argparse
import asyncio
import logging
import os

import uvicorn
from dotenv import load_dotenv
from utils.console import enable_ansi_colors

load_dotenv()
enable_ansi_colors()  # enable VT100 on Windows CMD — controlled by ENABLE_ANSI_COLORS in .env

import database as db
from catalog import Catalog
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from routes import admin, bot_api, client, server, shared
from landing_page import LANDING_PAGE
from utils.logging_middleware import log_requests_middleware
from utils.upnp import open_port

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

    # 4. The legacy TCP log collector is optional. Most deployments only need
    # application stdout and the admin audit data stored by Core itself.
    if os.getenv("ENABLE_LOG_SERVER", "false").lower() in ("1", "true", "yes", "on"):
        try:
            from log_server import init_queue, start_log_server
            log_port = int(os.getenv("LOG_SERVER_PORT", "9090"))
            start_log_server(port=log_port)
            init_queue(asyncio.get_event_loop())
            logger.info("[INIT] Log server started on :%s, draining to SQLite.", log_port)
        except Exception as e:
            logger.warning(f"[INIT] Log server not started: {e}")
    else:
        logger.info("[INIT] Optional TCP log server disabled (ENABLE_LOG_SERVER=false).")

    # 4. Optional UPnP
    if os.getenv("ENABLE_UPNP", "false").lower() == "true":
        logger.info("[INIT] Opening UPnP ports…")
        open_port(int(os.getenv("CORE_PORT", "8000")), "TCP", "Andromeda API")
    else:
        logger.info("[INIT] UPnP disabled.")
        
    yield

app = FastAPI(title="Andromeda Private Server", version="2.0.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
if os.getenv("ENABLE_API_REQUEST_LOGGING", "false").lower() == "true":
    app.middleware("http")(log_requests_middleware)
    logger.info("[INIT] API request logging middleware enabled.")
else:
    logger.info("[INIT] API request logging middleware disabled.")

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(shared.router)
app.include_router(client.router)
app.include_router(server.router)
app.include_router(admin.router)
app.include_router(bot_api.router)


if os.getenv("ENABLE_LANDING_PAGE", "false").lower() in ("1", "true", "yes", "on"):
    @app.get("/", response_class=HTMLResponse)
    async def root():
        return LANDING_PAGE

    logger.info("[INIT] Public landing page enabled at /.")
else:
    logger.info("[INIT] Public landing page disabled (ENABLE_LANDING_PAGE=false).")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Andromeda.Core", "version": "0.11.4"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Andromeda Private Server")
    # CORE_HOST/CORE_PORT come from .env (written by setup_server.py).
    parser.add_argument("--host", default=os.getenv("CORE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CORE_PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="Hot-reload (dev only)")
    args = parser.parse_args()

    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
