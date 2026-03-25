import asyncio
import base64
import json
import logging
import os
import socket
import subprocess

logger = logging.getLogger("GameServerService")

GAME_EXE = os.getenv(
    "GAME_EXECUTABLE_PATH",
    r"C:\Program Files (x86)\Steam\steamapps\common\EnemyOnBoard\enemy-on-board.exe",
)
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")


def find_free_port(start: int = 7777, max_tries: int = 100) -> int:
    for port in range(start, start + (max_tries * 2), 2):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port found in range.")


def spawn_server(
    region: str,
    name: str,
    session_id: str,
    is_public: bool,
    gamemode: str = "CustomParty",
    gamemode_data=None,
) -> int:
    """Spawns a new game server process on an available port."""
    if not os.path.exists(GAME_EXE):
        logger.error(f"[GAMESERVER] Executable not found: {GAME_EXE}")
        return -1

    try:
        port = find_free_port()
    except RuntimeError as e:
        logger.error(f"[GAMESERVER] {e}")
        return -1
    
    # Map friendly names to GamemodeList.Key if needed
    # (The DedicatedServerStartup patch expects a valid enum name)
    
    cmd = [
        GAME_EXE,
        "-batchmode",
        "-nographics",
        "--server",
        "--port", str(port),
        "--region", region,
        "--session-id", session_id,
        "--name", name,
        "--mode", gamemode
    ]

    if gamemode_data is not None:
        try:
            if hasattr(gamemode_data, "model_dump"):
                serializable = gamemode_data.model_dump()
            elif hasattr(gamemode_data, "dict"):
                serializable = gamemode_data.dict()
            else:
                serializable = gamemode_data

            mode_data_json = json.dumps(serializable, separators=(",", ":"))
            mode_data_b64 = base64.b64encode(mode_data_json.encode("utf-8")).decode("ascii")
            cmd.extend(["--mode-data-b64", mode_data_b64])
        except Exception as exc:
            logger.warning(f"[GAMESERVER] Failed to serialize gamemode_data for {session_id}: {exc}")

    if is_public:
        cmd.append("--public")

    logger.info(f"[GAMESERVER] Executing: {' '.join(cmd)}")
    flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    try:
        subprocess.Popen(cmd, creationflags=flags)
        logger.info(f"[GAMESERVER] Process started on port {port} for session {session_id}")
        return port
    except Exception as exc:
        logger.error(f"[GAMESERVER] Spawn failed: {exc}")
        return -1
