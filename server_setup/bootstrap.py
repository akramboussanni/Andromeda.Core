"""Create Core's virtual environment and install its requirements."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Callable

from . import MIN_PYTHON
from .plan import venv_python

MARKER = ".andromeda-requirements.sha256"

Runner = Callable[..., subprocess.CompletedProcess]


class BootstrapError(RuntimeError):
    pass


def requirements_digest(requirements: Path) -> str:
    return hashlib.sha256(requirements.read_bytes()).hexdigest()


def _python_version_ok(python: Path, run: Runner) -> bool:
    check = "import sys; sys.exit(0 if sys.version_info >= %r else 1)" % (MIN_PYTHON,)
    try:
        return run([str(python), "-c", check], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_venv(
    core_dir: Path,
    windows: bool,
    run: Runner = subprocess.run,
    log: Callable[[str], None] = print,
    base_python: str = sys.executable,
) -> Path:
    """Make ``<core_dir>/venv`` and install ``requirements.txt``. Safe to call repeatedly."""
    requirements = core_dir / "requirements.txt"
    if not requirements.is_file():
        raise BootstrapError(f"{requirements} is missing")
    python = venv_python(core_dir, windows)
    venv_dir = core_dir / "venv"

    if python.exists():
        if not _python_version_ok(python, run):
            raise BootstrapError(
                f"The existing virtual environment {venv_dir} uses Python older than "
                f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} or is broken. Delete that folder and run setup again."
            )
    else:
        log(f"  Creating virtual environment in {venv_dir}")
        result = run([base_python, "-m", "venv", str(venv_dir)])
        if result.returncode != 0 or not python.exists():
            raise BootstrapError(f"could not create a virtual environment in {venv_dir}")

    marker = venv_dir / MARKER
    digest = requirements_digest(requirements)
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == digest:
        log("  Core's Python packages are already installed")
        return python

    log("  Installing Core's Python packages (this can take a few minutes)")
    result = run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements)])
    if result.returncode != 0:
        raise BootstrapError(
            f"pip could not install {requirements}. Check the internet connection and run setup again."
        )
    marker.write_text(digest + "\n", encoding="utf-8")
    return python
