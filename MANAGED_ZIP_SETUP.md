# Managed Zip Setup for CI

This server now exposes a secured endpoint that GitHub Actions can use to download the game managed dependency zip.

## Endpoint

- Method: GET
- Path: /build/managed-zip
- Auth: optional token, enabled when MANAGED_ZIP_TOKEN is set

Auth options:
- `Authorization: Bearer <token>`
- `X-Managed-Token: <token>`

## Required file

Place your private managed zip at:
- `data/managed/EnemyOnBoard_Managed.zip`

Or set a custom path with:
- `MANAGED_ZIP_PATH=/absolute/path/to/EnemyOnBoard_Managed.zip`

## Environment variables

- `MANAGED_ZIP_PATH` (optional)
- `MANAGED_ZIP_TOKEN` (recommended)

Host boot/warmup support:
- `GAME_HOST_BOOT_PROVIDER=none|webhook|hetzner` (default `none` for local testing)
- `GAME_HOST_BOOT_WAIT_MESSAGE` (optional text shown to clients)
- `GAME_HOST_BOOT_TRIGGER_URL` (optional generic boot webhook)
- `GAME_HOST_BOOT_TRIGGER_TOKEN` (optional token for webhook)
- `GAME_HOST_BOOT_RETRY_SECONDS` (default 30)
- `GAME_HOST_BOOT_WARMUP_SECONDS` (default 180)

Session provider mode (central API -> game runtime):
- `GAME_SESSION_PROVIDER=local|host-api` (default `local`)
- `GAME_HOST_API_BASE_URL` (required when `GAME_SESSION_PROVIDER=host-api`)
- `GAME_HOST_API_TOKEN` (optional auth token for host API)
- `GAME_HOST_API_TIMEOUT_SECONDS` (default `8`)

Game runtime + port management:
- `GAME_RUNTIME_MODE=process|docker` (default `process`)
- `GAME_PORT_RANGE_START` (default `7777`)
- `GAME_PORT_RANGE_END` (default `7977`)
- `MAX_EOB_INSTANCES` (default `6`)

Docker runtime settings:
- `GAME_SERVER_DOCKER_IMAGE` (required for docker mode)
- `GAME_SERVER_DOCKER_CONTAINER_PREFIX` (default `andromeda-eob`)
- `GAME_SERVER_DOCKER_ENTRYPOINT` (default `wine ./enemy-on-board.exe`)

Direct Hetzner boot support (optional fallback if no webhook URL):
- `HETZNER_API_TOKEN`
- `HETZNER_SERVER_ID`

## GitHub Actions example

Set repo secrets:
- `ANDROMEDA_GAME_MANAGED_ZIP_URL` = `https://<your-api-domain>/build/managed-zip`
- `ANDROMEDA_GAME_MANAGED_ZIP_TOKEN` = same value as server `MANAGED_ZIP_TOKEN`

This keeps game dependency files private and outside git.

## Runtime behavior

- Each spawned session gets a tracked game/voice port pair.
- Voice port is always `gamePort + 1`.
- Sessions can be queried via `GET /server/session/{session_id}/ports`.

## Recommended architecture

- Central API (`Andromeda.Core`) runs with `GAME_SESSION_PROVIDER=host-api`.
- Host runtime API (`Andromeda.Orchestrator`) runs on the VM and manages Docker/process sessions.
- Central API can still use local mode for dev/testing.
