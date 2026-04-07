"""
Server-Sent Events command bus.

Admin routes call push() to instantly deliver commands to all connected clients.
Each connected client holds one asyncio.Queue. When the client disconnects the
queue is removed — no state lingers on the server.
"""
import asyncio
import json
from typing import Set

_clients: Set[asyncio.Queue] = set()


def push(event_type: str, data: dict) -> int:
    """Push an SSE event to every connected client. Returns number of clients notified."""
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    for q in list(_clients):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass  # slow client — skip rather than block
    return len(_clients)


async def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    _clients.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _clients.discard(q)


def connected_count() -> int:
    return len(_clients)
