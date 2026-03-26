# Andromeda.Core

Andromeda.Core is the control-plane API for matchmaking, party lifecycle, player auth, progression, and game session orchestration.

## What it does

- Exposes client routes for party creation, join/leave, and match start flows.
- Exposes server routes for ready/heartbeat/shutdown and post-match stats.
- Keeps party/session state in memory for active runtime coordination.
- Persists player and progression data in SQLite.
- Can run game sessions locally or delegate to Andromeda.Orchestrator.
- Serves a secured managed-zip endpoint for CI dependency retrieval.

## Production architecture

- Run this service as your central API.
- Set GAME_SESSION_PROVIDER=host-api.
- Point GAME_HOST_API_BASE_URL to the host runtime service.
- Keep this API stateless except DB and short-lived in-memory party state.

## Key environment variables

- SERVER_HOST
- GAME_SESSION_PROVIDER=local|host-api
- GAME_HOST_API_BASE_URL
- GAME_HOST_API_TOKEN
- GAME_HOST_API_TIMEOUT_SECONDS
- GAME_HOST_BOOT_PROVIDER=none|webhook|hetzner
- GAME_HOST_BOOT_TRIGGER_URL
- GAME_HOST_BOOT_TRIGGER_TOKEN
- GAME_HOST_BOOT_RETRY_SECONDS
- GAME_HOST_BOOT_WARMUP_SECONDS
- MANAGED_ZIP_PATH
- MANAGED_ZIP_TOKEN

## Local development

1. Install dependencies from requirements.txt.
2. Keep GAME_SESSION_PROVIDER=local and GAME_HOST_BOOT_PROVIDER=none.
3. Start the API:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Security notes

- Set MANAGED_ZIP_TOKEN in production.
- Set GAME_HOST_API_TOKEN when using host-api mode.
- Restrict host runtime API access at network level.
