# Andromeda.Core

![Andromeda banner](Assets/banner.png)

Andromeda.Core is the control-plane API for matchmaking, party lifecycle, player auth, progression, and session orchestration.

[![Discord](https://img.shields.io/badge/Discord-Community-5865F2?logo=discord&logoColor=white)](https://discord.gg/fMbrCUKHP8)

## Project Status

This service is under active refactor.
Some modules still contain legacy patterns, temporary glue code, or rapidly iterated implementations while architecture cleanup is ongoing.
Some sections were AI-assisted during fast delivery phases and are being progressively hardened and standardized.

## What It Does

- Exposes client routes for party creation, join/leave, and match start flows.
- Exposes server routes for ready/heartbeat/shutdown and post-match stats.
- Keeps party/session state in memory for active runtime coordination.
- Persists player and progression data in SQLite.
- Can run game sessions locally or delegate to Andromeda.Orchestrator.
- Serves a secured managed-zip endpoint for CI dependency retrieval.

## Feature Breakdown

- Client API layer:
  - party lifecycle (`/party/create`, `/party/join`, `/party/leave`, `/party/kick`)
  - game/session endpoints (`/games/new`, `/games/join`, `/match/start`, `/match/stop`)
  - account and progression flows (`/players/*`, `/characters/levels/*`)
- Server API layer:
  - dedicated host readiness, heartbeat, and shutdown endpoints
  - session port discovery and spawn configuration delivery
  - post-match stats ingestion
- Community integration layer:
  - bot API for lobby/state visibility, account linking, and player stats surfaces
- Operator tooling:
  - admin web panel and management endpoints for sessions, players, logs, and commands
  - optional host boot providers and orchestrator delegation support

## Production Architecture

- Run this service as your central API.
- Set `GAME_SESSION_PROVIDER=host-api`.
- Point `GAME_HOST_API_BASE_URL` to the host runtime service.
- Keep this API stateless except DB and short-lived in-memory party state.

## Key Environment Variables

- `SERVER_HOST`
- `GAME_SESSION_PROVIDER=local|host-api`
- `GAME_HOST_API_BASE_URL`
- `GAME_HOST_API_TOKEN`
- `GAME_HOST_API_TIMEOUT_SECONDS`
- `GAME_SESSION_API_URL` (explicit API URL injected into each launched game session)
- `GAME_SERVER_SPAWN_DEBUG` (when true, Core appends `--debug` to spawned game servers)
- `GAME_HOST_BOOT_PROVIDER=none|webhook|gcloud`
- `GAME_HOST_BOOT_TRIGGER_URL`
- `GAME_HOST_BOOT_TRIGGER_TOKEN`
- `GCP_PROJECT_ID`
- `GCP_ZONE`
- `GCP_INSTANCE_NAME`
- `GCP_ACCESS_TOKEN` (optional; if omitted, metadata service token is used)
- `GCP_API_TIMEOUT_SECONDS`
- `GCP_RESOLVE_IP_ON_BOOT`
- `GAME_HOST_BOOT_RETRY_SECONDS`
- `GAME_HOST_BOOT_WARMUP_SECONDS`
- `MANAGED_ZIP_PATH`
- `MANAGED_ZIP_TOKEN`
- `EOB_MANAGED_DEP_DIR` (fallback source dir for building managed zip)

## Local Development

1. Install dependencies from requirements.txt.
2. Keep `GAME_SESSION_PROVIDER=local` and `GAME_HOST_BOOT_PROVIDER=none`.
3. Start the API:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Security Notes

- Set `MANAGED_ZIP_TOKEN` in production.
- Set `GAME_HOST_API_TOKEN` when using host-api mode.
- Restrict host runtime API access at network level.

## Community

[Join Discord](https://discord.gg/fMbrCUKHP8) for deployment support, live player counts, and an active community.
