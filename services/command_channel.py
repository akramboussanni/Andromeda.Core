import threading
import time
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_next_command_id = 1
_consumer_offsets: Dict[str, int] = {}
_commands: List[Dict[str, Any]] = []
_max_commands = 500

_legacy_broadcast = {"version": 0, "message": ""}
_legacy_force_exit = {"version": 0, "message": ""}


def _trim_locked(now: float) -> None:
    global _commands
    _commands = [c for c in _commands if c["expiresAt"] > now]
    if len(_commands) > _max_commands:
        _commands = _commands[-_max_commands:]


def enqueue_command(
    kind: str,
    payload: Optional[Dict[str, Any]] = None,
    target_process: Optional[str] = None,
    target_session_id: Optional[str] = None,
    source: str = "admin",
    ttl_seconds: int = 180,
) -> Dict[str, Any]:
    global _next_command_id

    payload = payload or {}
    kind = (kind or "").strip().lower()
    if not kind:
        raise ValueError("kind is required")

    now = time.time()
    ttl = max(5, min(int(ttl_seconds or 180), 3600))

    with _lock:
        cmd = {
            "id": _next_command_id,
            "kind": kind,
            "payload": payload,
            "targetProcess": (target_process or "").strip().lower() or None,
            "targetSessionId": (target_session_id or "").strip() or None,
            "source": source,
            "createdAt": now,
            "expiresAt": now + ttl,
        }
        _next_command_id += 1
        _commands.append(cmd)

        if kind == "broadcast":
            _legacy_broadcast["version"] += 1
            _legacy_broadcast["message"] = str(payload.get("message") or "")
        elif kind == "force_exit":
            _legacy_force_exit["version"] += 1
            _legacy_force_exit["message"] = str(payload.get("message") or "")

        _trim_locked(now)
        return dict(cmd)


def pull_commands(
    consumer_id: str,
    process: Optional[str] = None,
    session_id: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    if not consumer_id:
        consumer_id = "anon"

    process_norm = (process or "").strip().lower() or None
    session_norm = (session_id or "").strip() or None
    cap = max(1, min(int(limit or 50), 200))

    with _lock:
        now = time.time()
        _trim_locked(now)
        offset = _consumer_offsets.get(consumer_id, 0)

        out: List[Dict[str, Any]] = []
        max_seen = offset
        for cmd in _commands:
            cid = cmd["id"]
            if cid <= offset:
                continue

            target_process = cmd.get("targetProcess")
            if target_process and target_process not in {"any", "*", process_norm}:
                continue

            target_session = cmd.get("targetSessionId")
            if target_session and target_session != session_norm:
                continue

            out.append({
                "id": cid,
                "kind": cmd["kind"],
                "payload": cmd.get("payload") or {},
                "source": cmd.get("source") or "",
                "createdAt": cmd["createdAt"],
            })
            if cid > max_seen:
                max_seen = cid
            if len(out) >= cap:
                break

        if max_seen > offset:
            _consumer_offsets[consumer_id] = max_seen

        return out


def get_legacy_snapshot() -> Dict[str, Dict[str, Any]]:
    with _lock:
        return {
            "broadcast": dict(_legacy_broadcast),
            "force_exit": dict(_legacy_force_exit),
        }
