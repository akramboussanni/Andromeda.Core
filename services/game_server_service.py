import base64
import json
import logging
import os
import socket
import subprocess
import threading
import time
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

logger = logging.getLogger("GameServerService")

GAME_EXE = os.getenv(
    "GAME_EXECUTABLE_PATH",
    r"C:\Program Files (x86)\Steam\steamapps\common\EnemyOnBoard\enemy-on-board.exe",
)
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
GAME_SESSION_PROVIDER = os.getenv("GAME_SESSION_PROVIDER", "local").strip().lower()
HOST_API_BASE_URL = os.getenv("GAME_HOST_API_BASE_URL", "").strip().rstrip("/")
HOST_API_TOKEN = os.getenv("GAME_HOST_API_TOKEN", "").strip()
HOST_API_TIMEOUT_SECONDS = int(os.getenv("GAME_HOST_API_TIMEOUT_SECONDS", "8"))
BOOT_TRIGGER_URL = os.getenv("GAME_HOST_BOOT_TRIGGER_URL", "").strip()
BOOT_TRIGGER_TOKEN = os.getenv("GAME_HOST_BOOT_TRIGGER_TOKEN", "").strip()
HETZNER_API_TOKEN = os.getenv("HETZNER_API_TOKEN", "").strip()
HETZNER_SERVER_ID = os.getenv("HETZNER_SERVER_ID", "").strip()
BOOT_PROVIDER = os.getenv("GAME_HOST_BOOT_PROVIDER", "none").strip().lower()
GAME_RUNTIME_MODE = os.getenv("GAME_RUNTIME_MODE", "process").strip().lower()
PORT_RANGE_START = int(os.getenv("GAME_PORT_RANGE_START", "7777"))
PORT_RANGE_END = int(os.getenv("GAME_PORT_RANGE_END", "7977"))
MAX_EOB_INSTANCES = int(os.getenv("MAX_EOB_INSTANCES", "6"))
DOCKER_IMAGE = os.getenv("GAME_SERVER_DOCKER_IMAGE", "").strip()
DOCKER_CONTAINER_PREFIX = os.getenv("GAME_SERVER_DOCKER_CONTAINER_PREFIX", "andromeda-eob")
DOCKER_ENTRYPOINT = os.getenv("GAME_SERVER_DOCKER_ENTRYPOINT", "wine ./enemy-on-board.exe")
BOOT_RETRY_SECONDS = int(os.getenv("GAME_HOST_BOOT_RETRY_SECONDS", "30"))
BOOT_WARMUP_SECONDS = int(os.getenv("GAME_HOST_BOOT_WARMUP_SECONDS", "180"))
BOOT_WAIT_MESSAGE = os.getenv("GAME_HOST_BOOT_WAIT_MESSAGE", "Please wait a little, servers are booting.")

_boot_lock = threading.Lock()
_last_boot_request_at = 0.0
_warm_until = 0.0
_port_lock = threading.Lock()
_used_ports = set()
_session_runtime = {}


def get_boot_wait_message() -> str:
    return BOOT_WAIT_MESSAGE


def get_host_boot_state() -> dict:
    with _boot_lock:
        now = time.time()
        exe_present = os.path.exists(GAME_EXE)
        warming = (not exe_present) and (now < _warm_until)
        return {
            "ready": exe_present,
            "warming": warming,
            "sessionProvider": GAME_SESSION_PROVIDER,
            "provider": BOOT_PROVIDER,
            "lastBootRequestAt": _last_boot_request_at,
            "warmUntil": _warm_until,
            "message": BOOT_WAIT_MESSAGE,
        }


def _host_api_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if HOST_API_TOKEN:
        headers["Authorization"] = f"Bearer {HOST_API_TOKEN}"
    return headers


def _try_parse_json(text: str) -> dict | None:
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _host_api_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict | None]:
    if not HOST_API_BASE_URL:
        raise RuntimeError("GAME_HOST_API_BASE_URL is not configured")

    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(f"{HOST_API_BASE_URL}{path}", data=body, method=method)
    for k, v in _host_api_headers().items():
        req.add_header(k, v)

    try:
        with urlrequest.urlopen(req, timeout=HOST_API_TIMEOUT_SECONDS) as resp:
            text = resp.read().decode("utf-8") if resp.length != 0 else ""
            data = _try_parse_json(text)
            return resp.status, data
    except HTTPError as exc:
        text = exc.read().decode("utf-8") if exc.fp else ""
        data = _try_parse_json(text)
        return exc.code, data


def _trigger_boot_request_if_needed() -> bool:
    global _last_boot_request_at, _warm_until

    provider = BOOT_PROVIDER
    if provider == "none":
        return False

    if provider == "webhook" and not BOOT_TRIGGER_URL:
        logger.warning("[GAMESERVER] GAME_HOST_BOOT_PROVIDER=webhook but GAME_HOST_BOOT_TRIGGER_URL is empty")
        return False

    if provider == "hetzner" and (not HETZNER_API_TOKEN or not HETZNER_SERVER_ID):
        logger.warning("[GAMESERVER] GAME_HOST_BOOT_PROVIDER=hetzner but HETZNER_API_TOKEN/HETZNER_SERVER_ID are missing")
        return False

    if provider not in ("webhook", "hetzner"):
        logger.warning(f"[GAMESERVER] Unknown GAME_HOST_BOOT_PROVIDER={provider}")
        return False

    with _boot_lock:
        now = time.time()
        if now - _last_boot_request_at < BOOT_RETRY_SECONDS:
            return True

        try:
            if provider == "webhook":
                req = urlrequest.Request(BOOT_TRIGGER_URL, method="POST")
                req.add_header("Content-Type", "application/json")
                if BOOT_TRIGGER_TOKEN:
                    req.add_header("Authorization", f"Bearer {BOOT_TRIGGER_TOKEN}")
                payload = json.dumps({"reason": "player-demand"}).encode("utf-8")
                with urlrequest.urlopen(req, data=payload, timeout=8) as resp:
                    if resp.status < 200 or resp.status >= 300:
                        logger.warning(f"[GAMESERVER] Boot trigger returned HTTP {resp.status}")
                        return False
            elif provider == "hetzner":
                hetzner_url = f"https://api.hetzner.cloud/v1/servers/{HETZNER_SERVER_ID}/actions/poweron"
                req = urlrequest.Request(hetzner_url, method="POST")
                req.add_header("Authorization", f"Bearer {HETZNER_API_TOKEN}")
                req.add_header("Content-Type", "application/json")
                with urlrequest.urlopen(req, data=b"{}", timeout=10) as resp:
                    if resp.status < 200 or resp.status >= 300:
                        logger.warning(f"[GAMESERVER] Hetzner poweron returned HTTP {resp.status}")
                        return False

            _last_boot_request_at = now
            _warm_until = now + BOOT_WARMUP_SECONDS
            logger.info("[GAMESERVER] Boot trigger sent successfully; host marked warming")
            return True
        except HTTPError as exc:
            # 409/412 are common transient responses when a server is already running
            # or currently transitioning state; treat as "boot requested".
            if exc.code in (409, 412):
                _last_boot_request_at = now
                _warm_until = now + BOOT_WARMUP_SECONDS
                logger.info(f"[GAMESERVER] Boot trigger returned {exc.code}; treating host as warming")
                return True
            logger.error(f"[GAMESERVER] Boot trigger failed HTTP {exc.code}: {exc}")
            return False
        except URLError as exc:
            logger.error(f"[GAMESERVER] Boot trigger failed: {exc}")
            return False
        except Exception as exc:
            logger.error(f"[GAMESERVER] Unexpected boot trigger error: {exc}")
            return False


def _is_port_available(port: int) -> bool:
    # Validate both TCP and UDP to reduce collisions with voice/game sockets.
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        tcp.bind(("0.0.0.0", port))
        udp.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        tcp.close()
        udp.close()


def _allocate_port_pair(session_id: str) -> tuple[int, int]:
    with _port_lock:
        if session_id in _session_runtime:
            state = _session_runtime[session_id]
            return state["gamePort"], state["voicePort"]

        active_instances = len(_session_runtime)
        if active_instances >= MAX_EOB_INSTANCES:
            raise RuntimeError(f"Max instance limit reached ({MAX_EOB_INSTANCES})")

        if PORT_RANGE_START % 2 == 0:
            start = PORT_RANGE_START + 1
        else:
            start = PORT_RANGE_START

        for game_port in range(start, PORT_RANGE_END + 1, 2):
            voice_port = game_port + 1
            if voice_port > PORT_RANGE_END:
                break

            if game_port in _used_ports or voice_port in _used_ports:
                continue

            if not _is_port_available(game_port) or not _is_port_available(voice_port):
                continue

            _used_ports.add(game_port)
            _used_ports.add(voice_port)
            return game_port, voice_port

    raise RuntimeError("No free game/voice port pair available")


def _release_port_pair(session_id: str):
    with _port_lock:
        state = _session_runtime.get(session_id)
        if not state:
            return
        _used_ports.discard(state["gamePort"])
        _used_ports.discard(state["voicePort"])


def spawn_server(
    region: str,
    name: str,
    session_id: str,
    is_public: bool,
    gamemode: str = "CustomParty",
    gamemode_data=None,
) -> int:
    """
    Spawns a new game server process on an available port.

    Return values:
    - positive port: process started
    - -1: hard failure (host unavailable / executable missing / spawn failed)
    - -2: host warmup in progress (boot requested, caller should ask user to wait)
    """
    if GAME_SESSION_PROVIDER not in ("local", "host-api"):
        logger.error(f"[GAMESERVER] Unsupported GAME_SESSION_PROVIDER={GAME_SESSION_PROVIDER}")
        return -1

    if GAME_SESSION_PROVIDER == "host-api":
        try:
            status, data = _host_api_request(
                "POST",
                "/sessions/create",
                payload={
                    "region": region,
                    "name": name,
                    "sessionId": session_id,
                    "isPublic": is_public,
                    "gamemode": gamemode,
                    "gamemodeData": gamemode_data,
                },
            )

            if status == 200 and data:
                game_port = int(data.get("gamePort", 0))
                voice_port = int(data.get("voicePort", game_port + 1 if game_port > 0 else 0))
                _session_runtime[session_id] = {
                    "runtime": "host-api",
                    "gamePort": game_port,
                    "voicePort": voice_port,
                    "createdAt": time.time(),
                }
                return game_port

            if status in (425, 429, 503, 504):
                _trigger_boot_request_if_needed()
                return -2

            logger.error(f"[GAMESERVER] Host API create failed status={status} data={data}")
            return -1
        except Exception as exc:
            logger.warning(f"[GAMESERVER] Host API unavailable: {exc}")
            if _trigger_boot_request_if_needed():
                return -2
            return -1

    if GAME_RUNTIME_MODE not in ("process", "docker"):
        logger.error(f"[GAMESERVER] Unsupported GAME_RUNTIME_MODE={GAME_RUNTIME_MODE}")
        return -1

    if GAME_RUNTIME_MODE == "process" and not os.path.exists(GAME_EXE):
        logger.warning(f"[GAMESERVER] Executable not found: {GAME_EXE}")
        if _trigger_boot_request_if_needed():
            logger.info("[GAMESERVER] Host warmup in progress; returning wait signal")
            return -2
        logger.error("[GAMESERVER] Host unavailable and no boot trigger configured")
        return -1

    try:
        game_port, voice_port = _allocate_port_pair(session_id)
    except RuntimeError as e:
        logger.error(f"[GAMESERVER] {e}")
        return -1
    
    # Map friendly names to GamemodeList.Key if needed
    # (The DedicatedServerStartup patch expects a valid enum name)
    
    args = [
        "-batchmode",
        "-nographics",
        "--server",
        "--port", str(game_port),
        "--region", region,
        "--session-id", session_id,
        "--name", name,
        "--mode", gamemode,
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
            args.extend(["--mode-data-b64", mode_data_b64])
        except Exception as exc:
            logger.warning(f"[GAMESERVER] Failed to serialize gamemode_data for {session_id}: {exc}")

    if is_public:
        args.append("--public")

    try:
        if GAME_RUNTIME_MODE == "docker":
            if not DOCKER_IMAGE:
                raise RuntimeError("GAME_SERVER_DOCKER_IMAGE is required for docker runtime")

            container_name = f"{DOCKER_CONTAINER_PREFIX}-{session_id[:12]}"
            full_cmd = [
                "docker", "run", "-d", "--rm",
                "--name", container_name,
                "-p", f"{game_port}:{game_port}/udp",
                "-p", f"{game_port}:{game_port}/tcp",
                "-p", f"{voice_port}:{voice_port}/udp",
                "-e", f"SERVER_HOST={SERVER_HOST}",
                DOCKER_IMAGE,
            ]

            if DOCKER_ENTRYPOINT:
                full_cmd.extend(DOCKER_ENTRYPOINT.split())
            full_cmd.extend(args)

            logger.info(f"[GAMESERVER] Starting docker session {session_id} on {game_port}/{voice_port}")
            result = subprocess.check_output(full_cmd, stderr=subprocess.STDOUT, text=True).strip()
            _session_runtime[session_id] = {
                "runtime": "docker",
                "container": container_name,
                "containerId": result,
                "gamePort": game_port,
                "voicePort": voice_port,
                "createdAt": time.time(),
            }
            return game_port

        cmd = [GAME_EXE] + args
        logger.info(f"[GAMESERVER] Executing: {' '.join(cmd)}")
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        proc = subprocess.Popen(cmd, creationflags=flags)
        _session_runtime[session_id] = {
            "runtime": "process",
            "pid": proc.pid,
            "proc": proc,
            "gamePort": game_port,
            "voicePort": voice_port,
            "createdAt": time.time(),
        }
        logger.info(f"[GAMESERVER] Process started on gamePort={game_port} voicePort={voice_port} session={session_id}")
        return game_port
    except Exception as exc:
        logger.error(f"[GAMESERVER] Spawn failed: {exc}")
        with _port_lock:
            _session_runtime.pop(session_id, None)
            _used_ports.discard(game_port)
            _used_ports.discard(voice_port)
        return -1


def get_session_ports(session_id: str) -> dict | None:
    if GAME_SESSION_PROVIDER == "host-api":
        try:
            status, data = _host_api_request("GET", f"/sessions/{session_id}/ports")
            if status == 200 and data:
                return {
                    "gamePort": data.get("gamePort"),
                    "voicePort": data.get("voicePort"),
                }
        except Exception:
            pass

    with _port_lock:
        state = _session_runtime.get(session_id)
        if not state:
            return None
        return {
            "gamePort": state["gamePort"],
            "voicePort": state["voicePort"],
        }


def stop_session(session_id: str, reason: str = "manual") -> bool:
    if GAME_SESSION_PROVIDER == "host-api":
        try:
            status, _ = _host_api_request("POST", f"/sessions/{session_id}/stop", payload={"reason": reason})
            with _port_lock:
                _session_runtime.pop(session_id, None)
            return status in (200, 404)
        except Exception as exc:
            logger.warning(f"[GAMESERVER] Failed to stop host-api session {session_id}: {exc}")
            return False

    with _port_lock:
        state = _session_runtime.get(session_id)

    if not state:
        return False

    runtime = state.get("runtime")
    try:
        if runtime == "docker":
            container = state.get("container")
            if container:
                subprocess.call(["docker", "stop", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc = state.get("proc")
            if proc is not None and proc.poll() is None:
                proc.terminate()
    except Exception as exc:
        logger.warning(f"[GAMESERVER] Failed to stop session {session_id}: {exc}")
    finally:
        with _port_lock:
            _session_runtime.pop(session_id, None)
            _used_ports.discard(state["gamePort"])
            _used_ports.discard(state["voicePort"])

    logger.info(f"[GAMESERVER] Session stopped session={session_id} reason={reason}")
    return True
