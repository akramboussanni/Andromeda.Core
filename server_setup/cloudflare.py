"""Cloudflare Tunnel helpers: find cloudflared, find tunnel credentials, write a config.

A Cloudflare Tunnel only carries Core's HTTP API and website. Game and voice
traffic is UDP and an ordinary tunnel does not carry it: players always connect
to the server's public IPv4 address directly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib import request

from . import ANDROMEDA_VERSION

WINDOWS_DOWNLOAD_URL = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
)
INSTALL_DOCS_URL = (
    "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
)
DEFAULT_TUNNEL_NAME = "andromeda"

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_TUNNEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


@dataclass(frozen=True)
class TunnelCredentials:
    tunnel_id: str
    path: Path


def is_windows() -> bool:
    return os.name == "nt"


def normalize_hostname(value: str) -> str:
    """Accept ``play.example.com`` or a pasted URL; return the bare lowercase hostname."""
    host = value.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].rstrip(".")
    if not _HOSTNAME_RE.match(host):
        raise ValueError(f"{value!r} is not a valid hostname (example: play.example.com)")
    return host


def validate_tunnel_name(value: str) -> str:
    value = value.strip()
    if not _TUNNEL_NAME_RE.match(value):
        raise ValueError("tunnel names may only use letters, digits, '.', '-' and '_'")
    return value


def cloudflared_candidates(root: Path, env: dict[str, str] | None = None, windows: bool | None = None) -> list[Path]:
    """Well-known install locations, checked after PATH."""
    env = dict(os.environ) if env is None else env
    windows = is_windows() if windows is None else windows
    if windows:
        candidates = [root / "tools" / "cloudflared.exe"]
        for var in ("ProgramFiles(x86)", "ProgramFiles"):
            base = env.get(var)
            if base:
                candidates.append(Path(base) / "cloudflared" / "cloudflared.exe")
        local = env.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Microsoft" / "WinGet" / "Links" / "cloudflared.exe")
        return candidates
    return [root / "tools" / "cloudflared", Path("/usr/local/bin/cloudflared"), Path("/opt/homebrew/bin/cloudflared")]


def find_cloudflared(
    root: Path,
    which: Callable[[str], str | None] = shutil.which,
    candidates: Iterable[Path] | None = None,
) -> Path | None:
    found = which("cloudflared")
    if found:
        return Path(found)
    for candidate in cloudflared_candidates(root) if candidates is None else candidates:
        if candidate.is_file():
            return candidate
    return None


def cloudflared_version(path: Path, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str | None:
    try:
        result = run([str(path), "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or result.stderr or "").strip()
    return output.splitlines()[0] if output else "unknown"


def credential_dirs(home: Path | None = None, env: dict[str, str] | None = None) -> list[Path]:
    """Directories cloudflared itself searches for cert.pem and <tunnel-id>.json."""
    env = dict(os.environ) if env is None else env
    home = home or Path.home()
    dirs = [home / ".cloudflared", home / ".cloudflare-warp", home / "cloudflare-warp"]
    if env.get("USERPROFILE"):
        dirs.append(Path(env["USERPROFILE"]) / ".cloudflared")
    dirs.extend([Path("/etc/cloudflared"), Path("/usr/local/etc/cloudflared")])
    unique: list[Path] = []
    for directory in dirs:
        if directory not in unique:
            unique.append(directory)
    return unique


def read_tunnel_credentials(path: Path) -> TunnelCredentials:
    """Load only the TunnelID from a credentials file; the secret is never returned."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is not a readable cloudflared credentials file: {exc}") from None
    tunnel_id = data.get("TunnelID") if isinstance(data, dict) else None
    if not isinstance(tunnel_id, str) or not tunnel_id.strip():
        raise ValueError(f"{path} does not contain a TunnelID")
    return TunnelCredentials(tunnel_id.strip(), path)


def find_tunnel_credentials(dirs: Sequence[Path]) -> list[TunnelCredentials]:
    """All named-tunnel credential files, newest first."""
    found: list[TunnelCredentials] = []
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            try:
                found.append(read_tunnel_credentials(path))
            except ValueError:
                continue
    found.sort(key=lambda creds: creds.path.stat().st_mtime, reverse=True)
    return found


def has_origin_cert(dirs: Sequence[Path]) -> bool:
    return any((directory / "cert.pem").is_file() for directory in dirs)


def _yaml_scalar(value: str) -> str:
    return json.dumps(value)  # JSON strings are valid double-quoted YAML scalars


def render_config(
    tunnel: str,
    routes: Sequence[tuple[str, int]],
    credentials_file: Path | None = None,
) -> str:
    """Render a cloudflared config mapping each hostname to a localhost port."""
    if not routes:
        raise ValueError("at least one hostname is required")
    lines = [
        "# Generated by Andromeda server setup (Andromeda.Core/setup_server.py)",
        "# Only Core's HTTP API/website is tunnelled. Game/voice UDP ports must reach the public IP directly.",
        f"tunnel: {_yaml_scalar(tunnel)}",
    ]
    if credentials_file is not None:
        lines.append(f"credentials-file: {_yaml_scalar(str(credentials_file))}")
    lines.append("ingress:")
    for hostname, port in routes:
        lines.append(f"  - hostname: {_yaml_scalar(hostname)}")
        lines.append(f"    service: http://localhost:{int(port)}")
    lines.append("  - service: http_status:404")
    return "\n".join(lines) + "\n"


def config_hostnames(text: str) -> list[str]:
    """Hostnames listed in an existing cloudflared config (simple line-based scan)."""
    hosts = []
    for line in text.splitlines():
        match = re.match(r"^\s*-?\s*hostname:\s*['\"]?([^'\"\s#]+)", line)
        if match:
            hosts.append(match.group(1).lower())
    return hosts


def quote_arg(value: str) -> str:
    return f'"{value}"' if any(ch.isspace() for ch in value) else value


def setup_steps(cloudflared: str, tunnel_name: str, hostnames: Sequence[str]) -> list[str]:
    """The exact one-time commands to create and route a named tunnel."""
    exe = quote_arg(cloudflared)
    steps = [
        f"{exe} tunnel login",
        f"{exe} tunnel create {tunnel_name}",
    ]
    steps.extend(f"{exe} tunnel route dns {tunnel_name} {host}" for host in hostnames)
    return steps


def download_cloudflared(
    dest: Path,
    url: str = WINDOWS_DOWNLOAD_URL,
    opener: Callable[..., object] = request.urlopen,
    timeout: float = 120.0,
) -> Path:
    """Download the official Windows cloudflared binary to ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    req = request.Request(url, headers={"User-Agent": f"Andromeda-Setup/{ANDROMEDA_VERSION}"})
    try:
        with opener(req, timeout=timeout) as resp, open(partial, "wb") as out:
            shutil.copyfileobj(resp, out)
        with open(partial, "rb") as check:
            if check.read(2) != b"MZ":
                raise RuntimeError("downloaded file is not a Windows executable")
        if partial.stat().st_size < 1_000_000:
            raise RuntimeError("downloaded file is unexpectedly small")
        os.replace(partial, dest)
    finally:
        if partial.exists():
            partial.unlink()
    return dest


def non_windows_install_hint() -> str:
    if sys.platform == "darwin":
        return f"brew install cloudflared   (or see {INSTALL_DOCS_URL})"
    return f"Install the cloudflared package for your distribution: {INSTALL_DOCS_URL}"
