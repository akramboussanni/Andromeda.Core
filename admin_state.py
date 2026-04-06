"""
Shared in-memory state for admin commands.
The mod polls /client/commands and acts on version increments.
"""

PENDING_COMMANDS = {
    "broadcast": {"version": 0, "message": ""},
    "force_exit": {"version": 0},
}
