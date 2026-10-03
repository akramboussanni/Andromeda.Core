# Andromeda.Core

Andromeda.Core is the control-plane API for matchmaking, party lifecycle, player auth, progression, and game session orchestration.

## What it does

- Exposes client routes for party creation, join/leave, and match start flows.
- Exposes server routes for ready/heartbeat/shutdown and post-match stats.
- Keeps party/session state in memory for active runtime coordination.
- Persists player and progression data in SQLite.
- Can run game sessions locally or delegate to Andromeda.Orchestrator.
- Serves a secured managed-zip endpoint for CI dependency retrieval.

## Standard deployment: one Windows server, one Core API

Download `Andromeda.Server.Windows.zip` from the Andromeda release, extract it, and double-click `core\Setup-Andromeda-Server.bat`. Git and repository clones are not required. The setup:

- creates Core's private Python environment and installs its requirements;
- installs SteamCMD, the game server, MelonLoader, and the compatible Andromeda.Mod;
- writes one Core `.env` with `GAME_SESSION_PROVIDER=local`;
- configures `SERVER_IP=auto`, which refreshes the router-facing public IPv4 when session data is requested;
- optionally downloads `cloudflared`, accepts the dashboard tunnel token through a hidden prompt, and installs Cloudflare's Windows service (no browser login and no Andromeda token file);
- creates the Core launcher and Windows Firewall rules.

Andromeda.Orchestrator is not installed or started in this mode. It is only needed for advanced multi-host deployments.

## Advanced multi-host architecture

- Run this service as your central API.
- Set GAME_SESSION_PROVIDER=host-api.
- Point GAME_HOST_API_BASE_URL to the host runtime service.
- Keep this API stateless except DB and short-lived in-memory party state.

## Key environment variables

- SERVER_IP: `auto` resolves the current router-facing public IPv4 on demand with a short cache. An explicit public IPv4 disables automatic refresh.
- SERVER_IP_CACHE_SECONDS: refresh interval for `SERVER_IP=auto` (default 30 seconds).
- SERVER_HOST: compatibility snapshot for older 0.11.1 tooling. New session responses use `SERVER_IP`.
- CORE_HOST / CORE_PORT: listen address used by `python main.py` (defaults `0.0.0.0` / `8000`; `127.0.0.1` when a Cloudflare Tunnel or local reverse proxy fronts Core). Command-line `--host`/`--port` still override them.
- ADMIN_PASSWORD (or ADMIN_TOKEN): operator console password for `/admin`.
- STEAM_API_KEY / STEAM_APP_ID: verify Steam logins.
- GAME_SESSION_PROVIDER=local|host-api
- GAME_HOST_API_BASE_URL
- GAME_HOST_API_TOKEN
- GAME_HOST_API_TIMEOUT_SECONDS
- GAME_SESSION_API_URL (explicit API URL injected into each launched game session)
- GAME_HOST_BOOT_PROVIDER=none|webhook|gcloud
- GAME_HOST_BOOT_TRIGGER_URL
- GAME_HOST_BOOT_TRIGGER_TOKEN
- GCP_PROJECT_ID
- GCP_ZONE
- GCP_INSTANCE_NAME
- GCP_ACCESS_TOKEN (optional; if omitted, metadata service token is used)
- GCP_API_TIMEOUT_SECONDS
- GCP_RESOLVE_IP_ON_BOOT
- GAME_HOST_BOOT_RETRY_SECONDS
- GAME_HOST_BOOT_WARMUP_SECONDS
- MANAGED_ZIP_PATH
- MANAGED_ZIP_TOKEN
- EOB_MANAGED_DEP_DIR (fallback source dir for building managed zip)
- ENABLE_LOG_SERVER=true|false (optional legacy TCP collector; defaults to false)
- LOG_SERVER_PORT (defaults to 9090 when the collector is enabled)
- LOG_SERVER_MODE=collector|stub (default collector; stub accepts and discards TCP logs without storing them)

## Local development

1. Copy `.env.example` to `.env` or run `python setup_server.py`.
2. Install dependencies from requirements.txt.
3. Keep GAME_SESSION_PROVIDER=local and GAME_HOST_BOOT_PROVIDER=none for a single-machine development setup.
4. Start the API:

```bash
python main.py                      # uses CORE_HOST/CORE_PORT from .env
uvicorn main:app --host 0.0.0.0 --port 8000
```

`GET /health` returns `{"status": "ok", ...}` once Core is up.

## Security notes

- Set MANAGED_ZIP_TOKEN in production.
- Set GAME_HOST_API_TOKEN when using host-api mode.
- Restrict host runtime API access at network level.
