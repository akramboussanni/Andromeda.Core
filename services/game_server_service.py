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
from urllib.parse import quote

try:
    import google.auth
    from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:
    google = None
    GoogleAuthRequest = None

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
HOST_API_TRACE = os.getenv("GAME_HOST_API_TRACE", "false").strip().lower() in ("1", "true", "yes", "on")
SESSION_API_URL = os.getenv("GAME_SESSION_API_URL", "").strip().rstrip("/")
BOOT_TRIGGER_URL = os.getenv("GAME_HOST_BOOT_TRIGGER_URL", "").strip()
BOOT_TRIGGER_TOKEN = os.getenv("GAME_HOST_BOOT_TRIGGER_TOKEN", "").strip()
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "").strip()
GCP_ZONE = os.getenv("GCP_ZONE", "").strip()
GCP_INSTANCE_NAME = os.getenv("GCP_INSTANCE_NAME", "").strip()
GCP_ACCESS_TOKEN = os.getenv("GCP_ACCESS_TOKEN", "").strip()
GCP_SERVICE_ACCOUNT_KEY_FILE = os.getenv("GCP_SERVICE_ACCOUNT_KEY_FILE", "").strip()
GCP_OAUTH_SCOPES = os.getenv(
    "GCP_OAUTH_SCOPES",
    "https://www.googleapis.com/auth/compute,https://www.googleapis.com/auth/cloud-platform",
).strip()
GCP_API_TIMEOUT_SECONDS = int(os.getenv("GCP_API_TIMEOUT_SECONDS", "10"))
GCP_RESOLVE_IP_ON_BOOT = os.getenv("GCP_RESOLVE_IP_ON_BOOT", "true").strip().lower() in ("1", "true", "yes", "on")
GCP_AUTO_SUSPEND_IDLE_SECONDS = int(os.getenv("GCP_AUTO_SUSPEND_IDLE_SECONDS", "0"))
GCP_AUTO_SUSPEND_CHECK_SECONDS = int(os.getenv("GCP_AUTO_SUSPEND_CHECK_SECONDS", "30"))
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
_resolved_server_host = SERVER_HOST
_resolved_server_host_at = 0.0
_port_lock = threading.Lock()
_used_ports = set()
_session_runtime = {}
_gcp_credentials_lock = threading.Lock()
_gcp_credentials = None
_last_session_activity_at = time.time()


def _mark_session_activity() -> None:
    global _last_session_activity_at
    _last_session_activity_at = time.time()


def _is_boot_warm_active() -> bool:
    with _boot_lock:
        return time.time() < _warm_until


def get_boot_wait_message() -> str:
    return BOOT_WAIT_MESSAGE


def get_host_boot_state() -> dict:
    if BOOT_PROVIDER == "gcloud":
        now = time.time()
        # Keep the cached host reasonably fresh for reporting endpoints.
        if now - _resolved_server_host_at > 30:
            _refresh_server_host_from_gcloud()

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
            "serverHost": _resolved_server_host,
            "serverHostResolvedAt": _resolved_server_host_at,
            "message": BOOT_WAIT_MESSAGE,
        }


def get_server_host(force_refresh: bool = False) -> str:
    """Return the best-known reachable host/IP used by clients to connect to sessions."""
    if force_refresh and BOOT_PROVIDER == "gcloud":
        _refresh_server_host_from_gcloud()
    return _resolved_server_host


def _set_resolved_server_host(host: str):
    global _resolved_server_host, _resolved_server_host_at
    if host:
        _resolved_server_host = host
        _resolved_server_host_at = time.time()


def _host_api_headers() -> dict:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Andromeda-Core/1.0"
    }
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

    url = f"{HOST_API_BASE_URL}{path}"
    
    if HOST_API_TRACE:
        logger.info(
            "[HOST-API] -> %s %s payloadKeys=%s",
            method,
            url,
            sorted(list(payload.keys())) if isinstance(payload, dict) else [],
        )

    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url, data=body, method=method)
    for k, v in _host_api_headers().items():
        req.add_header(k, v)

    try:
        with urlrequest.urlopen(req, timeout=HOST_API_TIMEOUT_SECONDS) as resp:
            text = resp.read().decode("utf-8") if resp.length != 0 else ""
            data = _try_parse_json(text)
            if HOST_API_TRACE:
                logger.info("[HOST-API] <- %s %s status=%s", method, url, resp.status)
            return resp.status, data
    except HTTPError as exc:
        text = exc.read().decode("utf-8") if exc.fp else ""
        data = _try_parse_json(text)
        if HOST_API_TRACE:
            logger.warning("[HOST-API] <- %s %s status=%s", method, url, exc.code)
        return exc.code, data


def _trigger_boot_request_if_needed() -> bool:
    global _last_boot_request_at, _warm_until

    provider = BOOT_PROVIDER
    if provider == "none":
        return False

    if provider == "webhook" and not BOOT_TRIGGER_URL:
        logger.warning("[GAMESERVER] GAME_HOST_BOOT_PROVIDER=webhook but GAME_HOST_BOOT_TRIGGER_URL is empty")
        return False

    if provider == "gcloud" and (not GCP_PROJECT_ID or not GCP_ZONE or not GCP_INSTANCE_NAME):
        logger.warning("[GAMESERVER] GAME_HOST_BOOT_PROVIDER=gcloud but GCP_PROJECT_ID/GCP_ZONE/GCP_INSTANCE_NAME are missing")
        return False

    if provider not in ("webhook", "gcloud"):
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
            elif provider == "gcloud":
                _gcloud_start_instance()
                if GCP_RESOLVE_IP_ON_BOOT:
                    _refresh_server_host_from_gcloud()

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


def _gcp_instance_base_url() -> str:
    project = quote(GCP_PROJECT_ID, safe="")
    zone = quote(GCP_ZONE, safe="")
    instance = quote(GCP_INSTANCE_NAME, safe="")
    return f"https://compute.googleapis.com/compute/v1/projects/{project}/zones/{zone}/instances/{instance}"


def _gcp_metadata_access_token() -> str:
    req = urlrequest.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        method="GET",
    )
    req.add_header("Metadata-Flavor", "Google")
    with urlrequest.urlopen(req, timeout=GCP_API_TIMEOUT_SECONDS) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        return str(payload.get("access_token", "")).strip()


def _gcp_scopes() -> list[str]:
    scopes = [scope.strip() for scope in GCP_OAUTH_SCOPES.split(",") if scope.strip()]
    if scopes:
        return scopes
    return [
        "https://www.googleapis.com/auth/compute",
        "https://www.googleapis.com/auth/cloud-platform",
    ]


def _gcp_google_auth_access_token() -> str:
    if google is None or GoogleAuthRequest is None:
        raise RuntimeError("google-auth is not installed")

    global _gcp_credentials
    with _gcp_credentials_lock:
        if _gcp_credentials is None:
            scopes = _gcp_scopes()
            if GCP_SERVICE_ACCOUNT_KEY_FILE:
                creds, _ = google.auth.load_credentials_from_file(GCP_SERVICE_ACCOUNT_KEY_FILE, scopes=scopes)
            else:
                creds, _ = google.auth.default(scopes=scopes)
            _gcp_credentials = creds

        if not _gcp_credentials.valid or _gcp_credentials.expired or not _gcp_credentials.token:
            _gcp_credentials.refresh(GoogleAuthRequest())

        return str(_gcp_credentials.token or "").strip()


def _gcp_access_token() -> str:
    if GCP_ACCESS_TOKEN:
        return GCP_ACCESS_TOKEN
    try:
        token = _gcp_google_auth_access_token()
        if token:
            return token
    except Exception as exc:
        logger.warning(f"[GAMESERVER] Failed to obtain GCP token via google-auth: {exc}")
    return _gcp_metadata_access_token()


def _gcp_request(method: str, path_suffix: str = "", payload: dict | None = None) -> tuple[int, dict | None]:
    token = _gcp_access_token()
    if not token:
        raise RuntimeError("No GCP access token available")

    url = f"{_gcp_instance_base_url()}{path_suffix}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urlrequest.urlopen(req, timeout=GCP_API_TIMEOUT_SECONDS) as resp:
            text = resp.read().decode("utf-8") if resp.length != 0 else ""
            data = _try_parse_json(text)
            return resp.status, data
    except HTTPError as exc:
        text = exc.read().decode("utf-8") if exc.fp else ""
        data = _try_parse_json(text)
        return exc.code, data


def _extract_gcp_external_ip(instance_data: dict | None) -> str | None:
    if not isinstance(instance_data, dict):
        return None
    nics = instance_data.get("networkInterfaces")
    if not isinstance(nics, list):
        return None
    for nic in nics:
        if not isinstance(nic, dict):
            continue
        access_configs = nic.get("accessConfigs")
        if not isinstance(access_configs, list):
            continue
        for cfg in access_configs:
            if not isinstance(cfg, dict):
                continue
            ip = cfg.get("natIP")
            if isinstance(ip, str) and ip.strip():
                return ip.strip()
    return None


def _refresh_server_host_from_gcloud() -> str | None:
    try:
        status, data = _gcp_request("GET")
        if status != 200:
            logger.warning(f"[GAMESERVER] GCP instance GET failed status={status} data={data}")
            return None
        ip = _extract_gcp_external_ip(data)
        if not ip:
            logger.warning("[GAMESERVER] GCP instance has no external natIP")
            return None
        _set_resolved_server_host(ip)
        return ip
    except Exception as exc:
        logger.warning(f"[GAMESERVER] Failed to refresh server host from GCP: {exc}")
        return None


def _gcloud_start_instance() -> bool:
    instance_status = _gcp_instance_status()
    if instance_status == "RUNNING":
        return True

    if instance_status == "SUSPENDED":
        resume_status, resume_data = _gcp_request("POST", "/resume", payload={})
        if resume_status in (200, 202):
            return True
        if resume_status == 400 and isinstance(resume_data, dict):
            msg = json.dumps(resume_data).lower()
            if "already" in msg and "running" in msg:
                return True
        raise RuntimeError(f"GCP resume instance failed status={resume_status} data={resume_data}")

    status, data = _gcp_request("POST", "/start", payload={})
    if status in (200, 202):
        return True
    if status == 400 and isinstance(data, dict):
        # API often reports "already running" as a 400 with explanatory message.
        msg = json.dumps(data).lower()
        if "already" in msg and "running" in msg:
            return True
        # Fallback: if state changed between status check and start, resume may still be required.
        if "suspended" in msg and "resume" in msg:
            resume_status, resume_data = _gcp_request("POST", "/resume", payload={})
            if resume_status in (200, 202):
                return True
            raise RuntimeError(f"GCP resume instance failed status={resume_status} data={resume_data}")
    raise RuntimeError(f"GCP start instance failed status={status} data={data}")


def _gcp_instance_status() -> str | None:
    status, data = _gcp_request("GET")
    if status != 200 or not isinstance(data, dict):
        return None
    value = data.get("status")
    if not isinstance(value, str):
        return None
    return value.strip().upper() or None


def _gcloud_suspend_instance() -> bool:
    status, data = _gcp_request("POST", "/suspend", payload={})
    if status in (200, 202):
        return True
    if status == 400 and isinstance(data, dict):
        msg = json.dumps(data).lower()
        if "already" in msg and "suspended" in msg:
            return True
    raise RuntimeError(f"GCP suspend instance failed status={status} data={data}")


def _auto_suspend_loop() -> None:
    while True:
        try:
            time.sleep(max(5, GCP_AUTO_SUSPEND_CHECK_SECONDS))

            if GAME_SESSION_PROVIDER != "host-api" or BOOT_PROVIDER != "gcloud":
                continue

            with _port_lock:
                active_sessions = len(_session_runtime)

            if active_sessions > 0:
                _mark_session_activity()
                continue

            idle_for = time.time() - _last_session_activity_at
            if idle_for < GCP_AUTO_SUSPEND_IDLE_SECONDS:
                continue

            status = _gcp_instance_status()
            if status != "RUNNING":
                continue

            if _gcloud_suspend_instance():
                logger.info(
                    f"[GAMESERVER] Auto-suspended GCP host after {int(idle_for)}s idle"
                )
                _mark_session_activity()
        except Exception as exc:
            logger.warning(f"[GAMESERVER] Auto-suspend loop error: {exc}")


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
        if BOOT_PROVIDER == "gcloud":
            try:
                instance_status = _gcp_instance_status()
                if instance_status and instance_status != "RUNNING":
                    logger.info(f"[GAMESERVER] GCP instance status={instance_status}; triggering boot before host-api create")
                    if _trigger_boot_request_if_needed():
                        return -2
            except Exception as exc:
                logger.warning(f"[GAMESERVER] Failed to query GCP instance status before host-api create: {exc}")

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
                    "apiUrl": SESSION_API_URL or None,
                },
            )

            if status == 200 and data:
                game_port = int(data.get("gamePort", 0))
                voice_port = int(data.get("voicePort", game_port + 1 if game_port > 0 else 0))
                if BOOT_PROVIDER == "gcloud":
                    _refresh_server_host_from_gcloud()
                _session_runtime[session_id] = {
                    "runtime": "host-api",
                    "gamePort": game_port,
                    "voicePort": voice_port,
                    "createdAt": time.time(),
                }
                _mark_session_activity()
                return game_port

            if status in (425, 429, 503, 504):
                _trigger_boot_request_if_needed()
                return -2

            if status == 403 and BOOT_PROVIDER == "gcloud":
                # A 403 can happen while infra/proxy catches up right after boot,
                # but persistent 403 usually means auth/policy misconfiguration.
                boot_requested = _trigger_boot_request_if_needed()
                if boot_requested and _is_boot_warm_active():
                    return -2
                logger.error("[GAMESERVER] Host API returned 403 while not warming; check GAME_HOST_API_TOKEN and edge access policy")
                return -1

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

    if SESSION_API_URL:
        args.extend(["--api-url", SESSION_API_URL])

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
                "-e", f"ANDROMEDA_API_URL={SESSION_API_URL}",
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
            _mark_session_activity()
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
        _mark_session_activity()
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
            _mark_session_activity()
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
    _mark_session_activity()
    return True


if GCP_AUTO_SUSPEND_IDLE_SECONDS > 0:
    threading.Thread(target=_auto_suspend_loop, name="gcp-auto-suspend", daemon=True).start()
    logger.info(
        "[GAMESERVER] GCP auto-suspend enabled idleSeconds=%s checkSeconds=%s",
        GCP_AUTO_SUSPEND_IDLE_SECONDS,
        max(5, GCP_AUTO_SUSPEND_CHECK_SECONDS),
    )
