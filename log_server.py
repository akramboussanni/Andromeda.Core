import asyncio
import re
import socketserver
import threading
import time

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TAG_MAP = {
    "[error]":   "error",
    "[warning]": "warning",
    "[action]":  "info",
    "[success]": "info",
    "[tcp]":     "info",
    "[api]":     "info",
    "[lobby]":   "info",
    "[server]":  "info",
    "[info]":    "info",
}

_STEAM_ID_RE = re.compile(r'(?:SteamID[=:\[]|steamId[=:])(\d{15,20})', re.IGNORECASE)
_CONTINUATION_RE = re.compile(r'^\s+at\s')


def _detect_level(line: str) -> str:
    lower = line.lower()
    for key, level in _TAG_MAP.items():
        if key in lower:
            return level
    if "exception" in lower or "error" in lower:
        return "error"
    if "warning" in lower:
        return "warning"
    return "info"


def _extract_steam_id(line: str):
    m = _STEAM_ID_RE.search(line)
    return m.group(1) if m else None


def _is_continuation(line: str) -> bool:
    return bool(_CONTINUATION_RE.match(line)) or line.startswith("\tat ")


# ---------------------------------------------------------------------------
# Async queue — TCP thread drops entries here, drain task writes to SQLite
# ---------------------------------------------------------------------------

_queue: asyncio.Queue | None = None
_loop:  asyncio.AbstractEventLoop | None = None


def _enqueue(entry: dict):
    if _queue is None or _loop is None:
        return
    try:
        _loop.call_soon_threadsafe(_queue.put_nowait, entry)
    except Exception:
        pass


async def _drain():
    import logs_db
    while True:
        try:
            entry = await asyncio.wait_for(_queue.get(), timeout=5.0)
            await logs_db.ingest(entry)
            _queue.task_done()
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass


def init_queue(loop: asyncio.AbstractEventLoop):
    global _queue, _loop
    _queue = asyncio.Queue()
    _loop  = loop
    loop.create_task(_drain())


# ---------------------------------------------------------------------------
# TCP handler
# ---------------------------------------------------------------------------

class LogHandler(socketserver.BaseRequestHandler):
    def handle(self):
        buf         = b""
        pending_ts  = ""
        pending_msg = ""

        def flush():
            if not pending_msg:
                return
            _enqueue({
                "level":    _detect_level(pending_msg),
                "message":  pending_msg,
                "service":  "tcp",
                "steam_id": _extract_steam_id(pending_msg),
            })

        try:
            while True:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    if _is_continuation(line) and pending_msg:
                        pending_msg += "\n  " + line.strip()
                    else:
                        flush()
                        pending_ts  = time.strftime("%H:%M:%S")
                        pending_msg = line
            flush()
        except Exception as e:
            print(f"[LogServer] connection error: {e}")
            flush()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class _ServerThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        # Using ThreadingTCPServer to allow multiple game instances to log concurrently
        self.server = socketserver.ThreadingTCPServer(("0.0.0.0", 9090), LogHandler)

    def run(self):
        print("[LogServer] Listening on :9090")
        self.server.serve_forever()


def start_log_server():
    _ServerThread().start()
