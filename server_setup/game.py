"""Install the native Windows game server: SteamCMD, Enemy On Board, MelonLoader, Andromeda.Mod."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping
from urllib import request

from . import ANDROMEDA_VERSION
from .manifest import Pins

STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
USER_AGENT = f"Andromeda-Setup/{ANDROMEDA_VERSION}"
# Non-secret: lets reruns reuse SteamCMD's cached login for the same account.
STEAM_USER_FILE = "andromeda_steam_user.txt"
# Records which Andromeda.Mod build is installed so reruns do not download it again.
MOD_STAMP_FILE = ".andromeda-mod.json"


class GameInstallError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str, what: str) -> str:
    actual = sha256_file(path)
    if expected and actual != expected.lower():
        raise GameInstallError(
            f"{what} failed its integrity check (SHA-256 {actual}, expected {expected}). "
            "The download was corrupted or changed; nothing was installed."
        )
    return actual


def safe_extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract a zip, refusing entries that would escape ``dest`` (zip-slip)."""
    dest = Path(dest).resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (dest / member.filename).resolve()
            if target != dest and dest not in target.parents:
                raise GameInstallError(f"refusing to extract unsafe path from archive: {member.filename}")
        archive.extractall(dest)


def download(url: str, dest: Path, opener: Callable[..., object] = request.urlopen, timeout: float = 120.0) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    req = request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(req, timeout=timeout) as resp, open(partial, "wb") as out:
            shutil.copyfileobj(resp, out)
        os.replace(partial, dest)
    except Exception as exc:
        raise GameInstallError(f"could not download {url}: {exc}") from exc
    finally:
        if partial.exists():
            partial.unlink()
    return dest


def fetch_json(url: str, opener: Callable[..., object] = request.urlopen, timeout: float = 30.0) -> object:
    req = request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with opener(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def steamcmd_command(
    steamcmd: Path,
    game_dir: Path,
    app_id: str,
    user: str,
    password: str = "",
    guard_code: str = "",
    branch: str = "",
    branch_password: str = "",
    validate: bool = True,
) -> list[str]:
    """Without a password SteamCMD uses its cached login or prompts (hidden) for one itself."""
    cmd = [str(steamcmd), "+force_install_dir", str(game_dir), "+login", user or "anonymous"]
    if user and user != "anonymous" and password:
        cmd.append(password)
        if guard_code:
            cmd.append(guard_code)
    cmd.extend(["+app_update", str(app_id)])
    if branch:
        cmd.extend(["-beta", branch])
        if branch_password:
            cmd.extend(["-betapassword", branch_password])
    if validate:
        cmd.append("validate")
    cmd.append("+quit")
    return cmd


def release_api_url(repo: str, tag: str) -> str:
    tag = (tag or "").strip()
    if tag in ("", "latest"):
        return f"https://api.github.com/repos/{repo}/releases/latest"
    return f"https://api.github.com/repos/{repo}/releases/tags/{tag}"


def pick_release_asset(release: object, pattern: str) -> str | None:
    """browser_download_url of the first asset whose name matches ``pattern`` (DLLs before zips)."""
    if not isinstance(release, dict):
        return None
    regex = re.compile(pattern, re.IGNORECASE)
    matches = []
    for asset in release.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name, url = str(asset.get("name") or ""), str(asset.get("browser_download_url") or "")
        if name and url and regex.match(name):
            matches.append((not name.lower().endswith(".dll"), name, url))
    return sorted(matches)[0][2] if matches else None


def mod_fallback_urls(repo: str, tag: str) -> list[str]:
    """Direct download URLs used when the GitHub API is unavailable (e.g. rate limited)."""
    base = f"https://github.com/{repo}/releases"
    tag = (tag or "").strip()
    if tag in ("", "latest"):
        return [f"{base}/latest/download/Andromeda.Mod.dll"]
    bare = tag.lstrip("v")
    return [f"{base}/download/{bare}/Andromeda.Mod.dll", f"{base}/download/v{bare}/Andromeda.Mod.dll"]


def melonloader_url(pins: Pins, env: Mapping[str, str]) -> str:
    override = env.get("MELONLOADER_URL", "").strip()
    # A MELONLOADER_URL meant for the Linux Docker image must not leak into a Windows install.
    if override and "linux" not in override.lower():
        return override
    if pins.melonloader_url:
        return pins.melonloader_url
    base = f"https://github.com/{pins.melonloader_repo}/releases"
    version = (pins.melonloader_version or "").strip()
    if version in ("", "latest"):
        return f"{base}/latest/download/{pins.melonloader_asset}"
    return f"{base}/download/v{version.lstrip('v')}/{pins.melonloader_asset}"


def melonloader_installed(game_dir: Path) -> bool:
    return (game_dir / "MelonLoader").is_dir() and (game_dir / "version.dll").is_file()


def mod_path(game_dir: Path) -> Path:
    return game_dir / "Mods" / "Andromeda.Mod.dll"


def read_mod_stamp(game_dir: Path) -> dict:
    try:
        data = json.loads((game_dir / "Mods" / MOD_STAMP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class GameInstaller:
    data_dir: Path
    exe: Path
    pins: Pins = field(default_factory=Pins)
    log: Callable[[str], None] = print
    # Prompt function (prompt, default) -> answer, or None when running non-interactively.
    ask: Callable[[str, str], str] | None = None
    fetch: Callable[[str, Path], Path] = download
    fetch_json: Callable[[str], object] = fetch_json
    run: Callable[[list[str]], int] = subprocess.call
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    @property
    def game_dir(self) -> Path:
        return self.exe.parent

    def install_all(self, update: bool) -> None:
        if update or not self.exe.is_file():
            self.install_game(self.install_steamcmd(), update)
        else:
            self.log(f"  Game already installed: {self.exe}")
        self.install_melonloader()
        self.install_mod()

    def install_steamcmd(self) -> Path:
        steamcmd_dir = self.data_dir / "steamcmd"
        steamcmd = steamcmd_dir / "steamcmd.exe"
        if steamcmd.is_file():
            self.log("  SteamCMD is already installed.")
            return steamcmd
        self.log("  Downloading SteamCMD (Valve)...")
        archive = self.fetch(STEAMCMD_URL, self.data_dir / "steamcmd.zip")
        try:
            safe_extract_zip(archive, steamcmd_dir)
        finally:
            archive.unlink(missing_ok=True)
        if not steamcmd.is_file():
            raise GameInstallError(f"SteamCMD was extracted but {steamcmd} is missing")
        return steamcmd

    def install_game(self, steamcmd: Path, update: bool) -> None:
        if self.exe.is_file() and not update:
            self.log(f"  Game already installed: {self.exe}")
            return
        self.game_dir.mkdir(parents=True, exist_ok=True)
        remembered = steamcmd.parent / STEAM_USER_FILE
        previous = remembered.read_text(encoding="utf-8").strip() if remembered.is_file() else ""
        user = self.env.get("STEAM_USER", "").strip()
        if not user and self.ask is not None:
            self.log("  SteamCMD needs a Steam account that has Enemy On Board in its library.")
            self.log("  SteamCMD asks for the password and Steam Guard code itself; the password is not shown.")
            user = self.ask("Steam username (or 'anonymous')", previous or "anonymous")
        user = user or previous or "anonymous"
        for attempt in (1, 2):
            cmd = steamcmd_command(
                steamcmd, self.game_dir, self.env.get("EOB_APP_ID", self.pins.steam_app_id), user,
                # Only for unattended installs; interactive runs let SteamCMD prompt instead.
                password=self.env.get("STEAM_PASS", ""),
                guard_code=self.env.get("STEAM_AUTH_CODE", "") or self.env.get("STEAM_GUARD_CODE", ""),
                branch=self.env.get("STEAM_BRANCH", ""),
                branch_password=self.env.get("STEAM_BRANCH_PASSWORD", ""),
                validate=self.env.get("EOB_VALIDATE", "1") != "0",
            )
            # The command is never printed: it can contain the Steam password.
            self.log(f"  Running SteamCMD as '{user}' (this can take a while the first time)...")
            self.run(cmd)  # SteamCMD's exit codes are unreliable; check the result instead.
            if self.exe.is_file():
                self.log(f"  Game installed: {self.exe}")
                if user != "anonymous":
                    remembered.write_text(user, encoding="utf-8")
                return
            if attempt == 1 and user == "anonymous" and self.ask is not None:
                self.log("  The anonymous download did not work; Steam needs an account that owns the game.")
                user = self.ask("Steam username (press Enter to give up)", "")
                if not user:
                    break
            else:
                break
        raise GameInstallError(
            f"the game was not downloaded ({self.exe} is missing). Run setup again and sign in with a Steam "
            f"account that owns Enemy On Board, or copy an existing game folder to {self.game_dir}"
        )

    def install_melonloader(self) -> None:
        if melonloader_installed(self.game_dir):
            self.log("  MelonLoader is already installed.")
            return
        url = melonloader_url(self.pins, self.env)
        self.log(f"  Downloading MelonLoader ({self.pins.melonloader_version})...")
        archive = self.fetch(url, self.data_dir / "melonloader.zip")
        try:
            # A pinned hash only applies to the pinned URL, not to a MELONLOADER_URL override.
            expected = self.pins.melonloader_sha256 if url == self.pins.melonloader_url else ""
            verify_sha256(archive, expected, "The MelonLoader download")
            safe_extract_zip(archive, self.game_dir)
        finally:
            archive.unlink(missing_ok=True)
        if not melonloader_installed(self.game_dir):
            raise GameInstallError("MelonLoader was extracted but version.dll / the MelonLoader folder are missing")

    def _mod_urls(self) -> tuple[list[str], str]:
        """Candidate URLs for the pinned mod and the SHA-256 they must match (if known)."""
        override = self.env.get("ANDROMEDA_MOD_URL", "").strip()
        if override:
            return [override], ""
        if self.pins.mod_url:
            return [self.pins.mod_url], self.pins.mod_sha256
        try:
            url = pick_release_asset(
                self.fetch_json(release_api_url(self.pins.mod_repo, self.pins.mod_tag)), self.pins.mod_asset_pattern
            )
        except Exception as exc:  # rate limits, outages: the direct URLs below still work
            self.log(f"  [WARN] GitHub release lookup failed ({exc.__class__.__name__}); trying direct download links.")
            url = None
        urls = [url] if url else []
        return urls + [u for u in mod_fallback_urls(self.pins.mod_repo, self.pins.mod_tag) if u not in urls], ""

    def install_mod(self) -> None:
        mods_dir = self.game_dir / "Mods"
        tag = self.pins.mod_tag or "latest"
        stamp = read_mod_stamp(self.game_dir)
        if (
            tag != "latest" and not self.env.get("ANDROMEDA_MOD_URL")
            and stamp.get("tag") == tag and mod_path(self.game_dir).is_file()
            and stamp.get("sha256") == sha256_file(mod_path(self.game_dir))
        ):
            self.log(f"  Andromeda.Mod {tag} is already installed.")
            return
        mods_dir.mkdir(parents=True, exist_ok=True)
        payload = self.data_dir / "Andromeda.Mod.download"
        urls, expected = self._mod_urls()
        last_error: Exception | None = None
        self.log(f"  Downloading Andromeda.Mod {tag}...")
        for url in urls:
            try:
                self.fetch(url, payload)
                break
            except GameInstallError as exc:
                last_error = exc
        else:
            raise GameInstallError(
                f"Andromeda.Mod {tag} could not be downloaded from {self.pins.mod_repo} ({last_error}). "
                "Check the internet connection, or pass --mod-version with an existing release tag."
            )
        try:
            verify_sha256(payload, expected, f"The Andromeda.Mod {tag} download")
            if zipfile.is_zipfile(payload):
                safe_extract_zip(payload, mods_dir)
            elif payload.read_bytes()[:2] == b"MZ":
                shutil.copyfile(payload, mod_path(self.game_dir))
            else:
                raise GameInstallError("the Andromeda.Mod download is neither a DLL nor a zip (check ANDROMEDA_MOD_URL)")
        finally:
            payload.unlink(missing_ok=True)
        if not mod_path(self.game_dir).is_file():
            raise GameInstallError(f"Andromeda.Mod was downloaded but {mod_path(self.game_dir)} is missing")
        (mods_dir / MOD_STAMP_FILE).write_text(
            json.dumps({"tag": tag, "sha256": sha256_file(mod_path(self.game_dir))}, indent=2) + "\n", encoding="utf-8"
        )
        self.log(f"  Andromeda.Mod {tag} installed in {mods_dir}")
