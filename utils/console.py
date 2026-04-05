"""
utils/console.py
Enables ANSI/VT100 colour output on Windows CMD & PowerShell.

Controlled by the ENABLE_ANSI_COLORS env var (default: true).
Set ENABLE_ANSI_COLORS=false to suppress all colour codes (e.g. for log files).
"""

import os
import sys
import logging

logger = logging.getLogger("Andromeda.Core")


def enable_ansi_colors() -> bool:
    """
    Enable VT100 virtual-terminal processing on Windows so that ANSI colour
    escape codes render correctly in CMD and PowerShell instead of printing
    as raw garbage like [92m.

    Returns True if colours are active, False if disabled or unsupported.
    Controlled by env var ENABLE_ANSI_COLORS (default: true).
    """
    if os.getenv("ENABLE_ANSI_COLORS", "true").lower() == "false":
        logger.info("[Console] ANSI colours disabled via ENABLE_ANSI_COLORS=false")
        return False

    if os.name != "nt":
        # Unix/macOS — ANSI works out of the box
        return True

    if not sys.stdout.isatty():
        # Running piped / redirected to a file — skip (avoids junk in logs)
        logger.debug("[Console] stdout is not a TTY, skipping ANSI enable")
        return False

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32

        # STD_OUTPUT_HANDLE = -11
        handle = kernel32.GetStdHandle(-11)
        mode   = ctypes.c_ulong()

        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            new_mode = mode.value | 0x0004
            if kernel32.SetConsoleMode(handle, new_mode):
                logger.info("[Console] ANSI/VT100 colours enabled (Windows VT processing)")
                return True
            else:
                logger.warning("[Console] SetConsoleMode failed — colours may not render")
        else:
            logger.warning("[Console] GetConsoleMode failed — not a real console?")

    except Exception as e:
        logger.warning(f"[Console] Could not enable ANSI colours: {e}")

    return False
