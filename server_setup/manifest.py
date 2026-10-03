"""Release pins: which Andromeda.Mod and MelonLoader builds this server setup installs.

The downloadable server bundle carries ``andromeda-server-manifest.json`` next to
its ``core`` folder (written by ``server/build_server_bundle.py`` in the
Andromeda superproject). A plain Core checkout has no manifest and uses the
defaults below, which match this Core version.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import ANDROMEDA_VERSION, DEFAULT_MOD_VERSION

MANIFEST_NAME = "andromeda-server-manifest.json"
SCHEMA = 1
MOD_ASSET_PATTERN = r"^Andromeda\.Mod.*\.(zip|dll)$"  # same rule as Andromeda.Installer
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(RuntimeError):
    pass


@dataclass
class Pins:
    version: str = ANDROMEDA_VERSION
    mod_repo: str = "akramboussanni/Andromeda.Mod"
    mod_tag: str = DEFAULT_MOD_VERSION
    mod_asset_pattern: str = MOD_ASSET_PATTERN
    mod_url: str = ""
    mod_sha256: str = ""
    melonloader_repo: str = "LavaGang/MelonLoader"
    melonloader_version: str = "latest"
    melonloader_asset: str = "MelonLoader.x64.zip"
    melonloader_url: str = ""
    melonloader_sha256: str = ""
    steam_app_id: str = "999860"
    source: Path | None = None

    @property
    def description(self) -> str:
        where = f"pinned by {self.source.name}" if self.source else "built-in defaults"
        return f"Andromeda {self.version}: Andromeda.Mod {self.mod_tag}, MelonLoader {self.melonloader_version} ({where})"


def _sha(value: object, field: str) -> str:
    text = str(value or "").strip().lower()
    if text and not _SHA256_RE.match(text):
        raise ManifestError(f"{field} must be a 64-character SHA-256 hex digest")
    return text


def from_dict(data: dict, source: Path | None = None) -> Pins:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ManifestError(f"unsupported manifest schema (expected {SCHEMA})")
    mod = data.get("mod") or {}
    melon = data.get("melonloader") or {}
    steam = data.get("steam") or {}
    pins = Pins(source=source)
    pins.version = str(data.get("version") or pins.version)
    pins.mod_repo = str(mod.get("repo") or pins.mod_repo)
    pins.mod_tag = str(mod.get("tag") or pins.version)
    pins.mod_asset_pattern = str(mod.get("assetPattern") or pins.mod_asset_pattern)
    pins.mod_url = str(mod.get("url") or "")
    pins.mod_sha256 = _sha(mod.get("sha256"), "mod.sha256")
    pins.melonloader_repo = str(melon.get("repo") or pins.melonloader_repo)
    pins.melonloader_version = str(melon.get("version") or pins.melonloader_version)
    pins.melonloader_asset = str(melon.get("asset") or pins.melonloader_asset)
    pins.melonloader_url = str(melon.get("url") or "")
    pins.melonloader_sha256 = _sha(melon.get("sha256"), "melonloader.sha256")
    pins.steam_app_id = str(steam.get("appId") or pins.steam_app_id)
    try:
        re.compile(pins.mod_asset_pattern)
    except re.error as exc:
        raise ManifestError(f"mod.assetPattern is not a valid regular expression: {exc}") from None
    return pins


def load(path: Path | None) -> Pins:
    """Pins from ``path`` when it exists, otherwise the built-in defaults."""
    if path is None or not path.is_file():
        return Pins()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"{path} is not a readable manifest: {exc}") from None
    return from_dict(data, source=path)
