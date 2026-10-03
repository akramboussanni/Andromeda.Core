"""Guided setup for the standard single-server Andromeda deployment.

One Windows machine runs exactly one API, Andromeda.Core, with
GAME_SESSION_PROVIDER=local: Core itself starts and stops the native game
server processes. Andromeda.Orchestrator is not used (it is only for advanced
multi-host deployments).

Everything in this package uses only the Python standard library so it can run
before any virtual environment exists.
"""

MIN_PYTHON = (3, 10)
# Release this setup belongs to; also the default Andromeda.Mod release tag.
ANDROMEDA_VERSION = "0.11.7"
DEFAULT_MOD_VERSION = "0.11.7"
