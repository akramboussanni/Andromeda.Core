import uvicorn
import logging
import argparse
import os
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from routes import shared, client, server, logs
from utils.logging_middleware import log_requests_middleware
from utils.upnp import open_port
from catalog import Catalog
import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("Server")

app = FastAPI(title="Parasite Private Server", version="2.0.0")

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
app.include_router(logs.router)


@app.get("/")
async def root():
    return {"message": "Parasite Private Server", "version": "2.0.0"}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    # 1. Initialise SQLite tables
    await db.init_db()
    logger.info("[INIT] Database ready.")

    # 2. Ensure catalog is loaded (already happens at import, but be explicit)
    Catalog.load()
    char_count = len(Catalog.get_characters())
    perk_count = len(Catalog.get_perks())
    logger.info(f"[INIT] Catalog loaded — {char_count} characters, {perk_count} perks.")

    # 3. Start log server (unchanged)
    try:
        from log_server import start_log_server
        start_log_server()
    except Exception as e:
        logger.warning(f"[INIT] Log server not started: {e}")

    # 4. Optional UPnP
    if os.getenv("ENABLE_UPNP", "false").lower() == "true":
        logger.info("[INIT] Opening UPnP ports…")
        open_port(8000, "TCP", "Parasite API")
    else:
        logger.info("[INIT] UPnP disabled.")


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
