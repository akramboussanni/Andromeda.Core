"""Reading, merging and safely writing ``.env`` files."""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Iterable, Mapping

# Values copied from .env.example files that must never be treated as real secrets (lowercase).
PLACEHOLDERS = {"", "change-me", "changeme", "your_api_key_here"}

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env(text: str) -> dict[str, str]:
    """Parse the subset of dotenv syntax Andromeda uses."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def read_env(path: Path) -> dict[str, str]:
    try:
        return parse_env(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def real_secret(value: str | None) -> str | None:
    """Return ``value`` unless it is empty or an example placeholder."""
    if value is None or value.strip().lower() in PLACEHOLDERS:
        return None
    return value.strip()


def format_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = "" if value is None else str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("env values cannot contain newlines")
    # Single quotes keep Windows backslashes literal for python-dotenv.
    if text and (any(ch.isspace() for ch in text) or any(ch in text for ch in "#\"'$`")):
        if "'" in text:
            raise ValueError(f"cannot safely quote value containing a single quote: {text!r}")
        return f"'{text}'"
    return text


def merge(
    existing: Mapping[str, str],
    managed: Mapping[str, object],
    defaults: Mapping[str, object] | None = None,
    remove: Iterable[str] = (),
) -> tuple[dict[str, object], dict[str, object], dict[str, str]]:
    """Split the final file into managed, default and preserved sections.

    * managed keys always take the new value;
    * default keys keep any existing value (advanced settings the operator tuned);
    * keys in ``remove`` are dropped (settings this mode must not carry);
    * every other existing key is preserved untouched.
    """
    defaults = defaults or {}
    dropped = set(remove)
    resolved_defaults = {k: existing.get(k, v) for k, v in defaults.items() if k not in managed}
    preserved = {
        k: v for k, v in existing.items()
        if k not in managed and k not in resolved_defaults and k not in dropped
    }
    return dict(managed), resolved_defaults, preserved


def render_env(
    header: str,
    managed: Mapping[str, object],
    defaults: Mapping[str, object] | None = None,
    preserved: Mapping[str, str] | None = None,
) -> str:
    lines = [f"# {line}" if line else "#" for line in header.splitlines()]
    for title, section in (
        ("Managed by setup (rerun setup to change)", managed),
        ("Defaults (safe to edit; setup keeps your values)", defaults or {}),
        ("Preserved from the previous file", preserved or {}),
    ):
        if section:
            lines.extend(["", f"# --- {title} ---"])
            lines.extend(f"{key}={format_value(value)}" for key, value in section.items())
    return "\n".join(lines) + "\n"


def backup_path(path: Path, now: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    candidate = path.with_name(f"{path.name}.backup-{stamp}")
    counter = 1
    while candidate.exists():  # two reruns within one second must not overwrite a backup
        candidate = path.with_name(f"{path.name}.backup-{stamp}-{counter}")
        counter += 1
    return candidate


def write_private(path: Path, text: str) -> None:
    """Atomically write ``text`` readable only by the current user (where the OS supports it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_env(path: Path, text: str, overwrite: bool, backup: bool = True) -> Path | None:
    """Write an env file; return the backup path if an existing file was replaced.

    Raises ``FileExistsError`` when the file exists and ``overwrite`` is false.
    """
    backup_file = None
    if path.exists():
        if not overwrite:
            raise FileExistsError(f"{path} already exists")
        if backup:
            backup_file = backup_path(path)
            shutil.copy2(path, backup_file)
            try:
                backup_file.chmod(0o600)
            except OSError:
                pass
    write_private(path, text)
    return backup_file
