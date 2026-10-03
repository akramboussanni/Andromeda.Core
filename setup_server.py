#!/usr/bin/env python3
"""Andromeda server setup: one machine, one API (Andromeda.Core).

Windows: double-click Setup-Andromeda-Server.bat from the server bundle, which
finds or installs Python and runs this file.
Anywhere: python setup_server.py [--help | --check | --non-interactive ...]

Core is configured with GAME_SESSION_PROVIDER=local and starts the game server
processes itself. Andromeda.Orchestrator is only for advanced multi-host setups.
The implementation lives in server_setup/ (standard library only).
"""

import sys

# Kept free of newer syntax so old interpreters reach this message instead of a SyntaxError.
if sys.version_info < (3, 10):
    sys.stderr.write(
        "Andromeda setup needs Python 3.10 or newer (this is Python %d.%d).\n"
        "Install it from https://www.python.org/downloads/ and run the setup again.\n" % sys.version_info[:2]
    )
    sys.exit(1)

if __name__ == "__main__":
    from server_setup.cli import main

    sys.exit(main())
