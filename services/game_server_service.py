import asyncio
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
    for port in range(start, start + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port found in range.")


def spawn_server(
    region: str = "us-east",
    name: str = "Dedicated Server",
    session_id: str = "",
    is_public: bool = True,
) -> int:
    """
    Launch a dedicated game server instance in a new console window.
    Returns the port it was assigned, or -1 on failure.
    The server process will call /server/ready when it's listening,
    at which point PartyService.update_party_address() is triggered.
    """
    if not os.path.exists(GAME_EXE):
        logger.error(f"[GAMESERVER] Executable not found: {GAME_EXE}")
        return -1

    try:
        port = find_free_port()
    except RuntimeError as e:
        logger.error(f"[GAMESERVER] {e}")
        return -1

    cmd = [
        GAME_EXE,
        "-batchmode",
        "-nographics",
        "--server",
        "--port", str(port),
        "--region", region,
        "--name", name,
        "--session-id", session_id,
    ]
    if is_public:
        cmd.append("--public")

    flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    try:
        subprocess.Popen(cmd, creationflags=flags)
        logger.info(f"[GAMESERVER] Spawned on port {port} (session={session_id})")
        return port
    except Exception as exc:
        logger.error(f"[GAMESERVER] Spawn failed: {exc}")
        return -1
