"""Guided, rerunnable setup for the standard single-server deployment: one machine, one API (Core)."""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import ANDROMEDA_VERSION, MIN_PYTHON, bootstrap, cloudflare, envfile, game, manifest, publicip
from .plan import (
    ENV_HEADER,
    HTTP_MODES,
    ORCHESTRATOR_KEYS,
    Plan,
    core_env,
    firewall_rules,
    format_command,
    netsh_commands,
    netsh_delete,
    normalize_url,
    render_posix_launcher,
    render_windows_launcher,
    services,
    stale_rules,
    validate,
    venv_python,
)

# Andromeda.Core folder (this package lives in it).
CORE_DIR = Path(__file__).resolve().parent.parent
TOTAL_STEPS = 8

# Setup's own answers, remembered in Core's .env so reruns can reuse them.
META_KEYS = (
    "ANDROMEDA_SETUP_HTTP_MODE",
    "ANDROMEDA_SETUP_CORE_HOSTNAME",
    "ANDROMEDA_SETUP_CORE_URL",
    "ANDROMEDA_SETUP_PUBLIC_IP_OVERRIDE",
    "ANDROMEDA_SETUP_TUNNEL_CONFIG",
)
# Written by the earlier Core + Orchestrator setup; meaningless in single-server mode.
LEGACY_META_KEYS = (
    "ANDROMEDA_SETUP_ORCH_HOSTNAME",
    "ANDROMEDA_SETUP_ORCHESTRATOR_URL",
    "ANDROMEDA_SETUP_PUBLIC_HOST_OVERRIDE",
)


class SetupError(Exception):
    """A problem the operator must fix; printed without a traceback."""


class SetupCancelled(Exception):
    """The operator chose to stop; nothing more was changed."""


def default_install_root(core_dir: Path | None = None) -> Path:
    """The extracted server bundle (``<root>/core`` + manifest), else the Core folder itself."""
    core_dir = core_dir or CORE_DIR
    if (core_dir.parent / manifest.MANIFEST_NAME).is_file():
        return core_dir.parent
    return core_dir


def game_data_dir(install_root: Path) -> Path:
    return install_root / "gameserver"


def default_game_exe(install_root: Path) -> Path:
    return game_data_dir(install_root) / "game" / "enemy-on-board.exe"


def resolve_path(value: str | Path, base: Path) -> Path:
    """Absolute path; relative values are taken from ``base``, never from the current folder."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

class UI:
    def __init__(
        self,
        interactive: bool,
        input_fn: Callable[[str], str] = input,
        secret_fn: Callable[[str], str] = getpass.getpass,
        out: Callable[[str], None] = print,
    ):
        self.interactive = interactive
        self._input = input_fn
        self._secret = secret_fn
        self.out = out

    def say(self, text: str = "") -> None:
        self.out(text)

    def step(self, number: int, title: str) -> None:
        self.out("")
        self.out(f"Step {number}/{TOTAL_STEPS}: {title}")
        self.out("-" * (len(title) + 12))

    def ok(self, text: str) -> None:
        self.out(f"  [OK]   {text}")

    def warn(self, text: str) -> None:
        self.out(f"  [WARN] {text}")

    def fail(self, text: str) -> None:
        self.out(f"  [FAIL] {text}")

    def _read(self, prompt: str, reader: Callable[[str], str]) -> str | None:
        if not self.interactive:
            return None
        try:
            return reader(prompt).strip()
        except EOFError:
            return None

    def ask(self, prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        answer = self._read(f"  {prompt}{suffix}: ", self._input)
        return answer or default

    def ask_valid(self, prompt: str, default: str, convert: Callable[[str], object]) -> object:
        while True:
            answer = self.ask(prompt, default)
            try:
                return convert(answer)
            except ValueError as exc:
                if not self.interactive:
                    raise SetupError(f"{prompt}: {exc}") from None
                self.warn(str(exc))

    def yes_no(self, prompt: str, default: bool) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        answer = self._read(f"  {prompt} {suffix} ", self._input)
        if not answer:
            return default
        return answer.lower() in ("y", "yes")

    def choose(self, prompt: str, options: list[tuple[str, str]], default: str) -> str:
        if not self.interactive:
            return default
        self.out(f"  {prompt}")
        for index, (_, label) in enumerate(options, 1):
            self.out(f"    {index}) {label}")
        keys = [key for key, _ in options]
        default_index = str(keys.index(default) + 1)
        while True:
            answer = self.ask("Choose a number", default_index)
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return keys[int(answer) - 1]
            self.warn(f"Please type a number from 1 to {len(options)}.")

    def secret(self, prompt: str) -> str:
        return self._read(f"  {prompt}: ", self._secret) or ""


# ---------------------------------------------------------------------------
# Injectable system access (replaced in tests)
# ---------------------------------------------------------------------------

def _is_admin() -> bool:
    if os.name != "nt":
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


@dataclass
class Deps:
    detect_ip: Callable[[], publicip.Detection] = publicip.detect_public_ipv4
    local_ip: Callable[[], str | None] = publicip.primary_local_ipv4
    find_cloudflared: Callable[[Path], Path | None] = cloudflare.find_cloudflared
    cloudflared_version: Callable[[Path], str | None] = cloudflare.cloudflared_version
    credential_dirs: Callable[[], list[Path]] = cloudflare.credential_dirs
    download_cloudflared: Callable[[Path], Path] = cloudflare.download_cloudflared
    ensure_venv: Callable[..., Path] = bootstrap.ensure_venv
    game_installer: Callable[..., game.GameInstaller] = game.GameInstaller
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    is_admin: Callable[[], bool] = _is_admin
    port_free: Callable[[int], bool] = _port_free
    windows: bool = field(default_factory=lambda: os.name == "nt")


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup_server.py",
        description=(
            "Guided setup for the standard single-server Andromeda deployment: Andromeda.Core is the only API "
            "and starts the game servers itself (GAME_SESSION_PROVIDER=local). Safe to run again: secrets "
            "are kept and nothing is overwritten without confirmation or --force."
        ),
    )
    general = parser.add_argument_group("general")
    general.add_argument("--non-interactive", action="store_true", help="never prompt; use flags, previous answers and defaults")
    general.add_argument("--advanced", action="store_true", help="also ask about ports, max matches, landing page and log collector")
    general.add_argument("--force", action="store_true", help="replace existing generated files without asking (a backup is kept)")
    general.add_argument("--rotate-secrets", action="store_true", help="generate a new admin password")
    general.add_argument("--check", action="store_true", help="only validate the existing configuration; change nothing")
    general.add_argument("--skip-install", action="store_true", help="do not create the virtual environment or install packages")
    general.add_argument(
        "--convert-to-local", action="store_true",
        help="switch a Core configured for the advanced Orchestrator mode (host-api) to single-server mode",
    )

    paths = parser.add_argument_group("locations")
    paths.add_argument("--install-root", type=Path, help="server folder for the game, tools and start script (default: the bundle folder)")
    paths.add_argument("--env-file", type=Path, help="Core settings file (default: <Andromeda.Core>/.env)")
    paths.add_argument("--manifest", type=Path, help=f"release pins (default: <install root>/{manifest.MANIFEST_NAME})")

    net = parser.add_argument_group("networking")
    net.add_argument("--public-ip", help="this server's public IPv4 address; skips automatic detection")
    net.add_argument("--http-mode", choices=HTTP_MODES, help="ip (default), tunnel (Cloudflare Tunnel) or domain (own DNS/proxy)")
    net.add_argument("--core-hostname", help="tunnel mode: public hostname for Core, e.g. play.example.com")
    net.add_argument("--core-url", help="domain mode: public Core URL, e.g. https://play.example.com")
    net.add_argument("--core-port", type=int)
    net.add_argument("--port-start", type=int, help="first game/voice port (default 7777)")
    net.add_argument("--port-end", type=int, help="last game/voice port (default 7977)")
    net.add_argument("--apply-firewall", action="store_true", help="Windows: add the firewall rules without asking (needs Administrator)")

    tunnel = parser.add_argument_group("cloudflare tunnel (Core HTTP only; game/voice UDP never uses it)")
    tunnel.add_argument("--cloudflared", help="path to cloudflared")
    tunnel.add_argument("--install-cloudflared", action="store_true", help="Windows: download cloudflared without asking")
    tunnel.add_argument("--tunnel-name", default=cloudflare.DEFAULT_TUNNEL_NAME)
    tunnel.add_argument("--tunnel-credentials", type=Path, help="existing named-tunnel credentials JSON")
    tunnel.add_argument("--tunnel-config", type=Path, help="where to write the generated config (default: <install root>/cloudflared/config.yml)")
    tunnel.add_argument("--use-tunnel-config", type=Path, help="use an existing cloudflared config you maintain instead of generating one")

    sessions = parser.add_argument_group("game servers")
    sessions.add_argument("--game-exe", "--game-executable", dest="game_exe", type=Path)
    sessions.add_argument("--max-sessions", type=int, help="maximum simultaneous matches (default 6)")
    sessions.add_argument("--log-server", action=argparse.BooleanOptionalAction, default=None,
                          help="optional legacy TCP client-log collector (default off)")
    sessions.add_argument("--log-port", type=int)
    sessions.add_argument("--landing-page", action=argparse.BooleanOptionalAction, default=None,
                          help="serve the public landing page at / (default off)")
    sessions.add_argument("--skip-game", action="store_true", help="do not download SteamCMD, the game, MelonLoader or Andromeda.Mod")
    sessions.add_argument("--no-game-update", action="store_true", help="do not run SteamCMD when the game is already installed")
    sessions.add_argument("--mod-version", help=f"Andromeda.Mod release tag (default: the pinned {ANDROMEDA_VERSION}; 'latest' for newest)")
    sessions.add_argument("--melonloader-version", help="MelonLoader release, e.g. 0.6.6 (default: the pinned version)")
    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _int(value: object, fallback: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return fallback


def _bool(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _port(text: str) -> int:
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError("ports must be between 1 and 65535")
    return port


def _optional_ipv4(text: str) -> str:
    return publicip.validate_manual_ipv4(text) if text else ""


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _write_file(ui: UI, path: Path, text: str, force: bool, what: str) -> bool:
    """Write a non-secret generated file unless the operator declines replacing a different one."""
    if path.exists():
        if _read_text(path) == text:
            ui.ok(f"{what} is already up to date: {path}")
            return True
        if not force and not ui.yes_no(f"{path} already exists. Replace it? A backup copy is saved first.", ui.interactive):
            ui.warn(f"Kept the existing {what}: {path} (rerun with --force to replace it)")
            return False
    backup = None
    if path.exists():
        backup = envfile.backup_path(path)
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    if backup:
        ui.ok(f"Updated {what}: {path} (previous version saved as {backup.name})")
    else:
        ui.ok(f"Created {what}: {path}")
    return True


def _locations(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    root = (args.install_root.expanduser().resolve() if args.install_root else default_install_root())
    env_path = args.env_file.expanduser().resolve() if args.env_file else CORE_DIR / ".env"
    manifest_path = args.manifest.expanduser().resolve() if args.manifest else root / manifest.MANIFEST_NAME
    return root, env_path, manifest_path


def _load_pins(args: argparse.Namespace, manifest_path: Path) -> manifest.Pins:
    if args.manifest and not manifest_path.is_file():
        raise SetupError(f"{manifest_path} does not exist")
    try:
        pins = manifest.load(manifest_path)
    except manifest.ManifestError as exc:
        raise SetupError(str(exc)) from None
    if args.mod_version:
        pins.mod_tag, pins.mod_url, pins.mod_sha256 = args.mod_version.strip(), "", ""
    if args.melonloader_version:
        pins.melonloader_version, pins.melonloader_url, pins.melonloader_sha256 = args.melonloader_version.strip(), "", ""
    return pins


# ---------------------------------------------------------------------------
# Wizard steps
# ---------------------------------------------------------------------------

class Wizard:
    def __init__(self, args: argparse.Namespace, ui: UI, deps: Deps):
        self.args = args
        self.ui = ui
        self.deps = deps
        self.root, self.env_path, self.manifest_path = _locations(args)
        self.pins = manifest.Pins()
        self.prev: dict[str, str] = {}
        self.manual_override = ""
        self.launcher: Path = self.root / "start_andromeda.bat"
        self.notes: list[str] = []

    def remembered(self, key: str, default: str = "") -> str:
        return self.prev.get(key, "") or default

    # -- step 1 ------------------------------------------------------------
    def locate(self) -> None:
        ui = self.ui
        ui.step(1, "Checking this computer")
        version = sys.version.split()[0]
        if sys.version_info < MIN_PYTHON:
            raise SetupError(f"Python {version} is too old; install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer.")
        ui.ok(f"Python {version}")
        if not ((CORE_DIR / "main.py").is_file() and (CORE_DIR / "requirements.txt").is_file()):
            raise SetupError(f"{CORE_DIR} is not a complete Andromeda.Core folder (main.py/requirements.txt missing)")
        ui.ok(f"Andromeda.Core: {CORE_DIR}")
        ui.ok(f"Server folder: {self.root}")
        self.pins = _load_pins(self.args, self.manifest_path)
        ui.ok(f"Release: {self.pins.description}")
        self.prev = envfile.read_env(self.env_path)
        if self.prev:
            ui.ok("Existing configuration found; your previous answers and passwords are kept.")
        self._advanced_mode_guard()

    def _advanced_mode_guard(self) -> None:
        """Never silently turn a hand-configured two-API (Orchestrator) deployment into single-server mode."""
        provider = self.prev.get("GAME_SESSION_PROVIDER", "").strip().lower()
        if provider != "host-api":
            return
        ui = self.ui
        ui.warn(f"{self.env_path} is set up for the ADVANCED multi-host mode (GAME_SESSION_PROVIDER=host-api,")
        ui.warn("Core + Andromeda.Orchestrator). This setup configures the standard single-server mode, where")
        ui.warn("Core is the only API and starts game servers itself.")
        if self.args.convert_to_local or ui.yes_no("Switch this server to the standard single-server mode? A backup is kept.", False):
            self.notes.append("Switched from the advanced Orchestrator mode; the Orchestrator is no longer needed on this server.")
            return
        raise SetupError(
            "Kept the advanced Orchestrator configuration unchanged. Rerun with --convert-to-local to switch to "
            "the single-server mode, or keep editing .env by hand as described in the Andromeda.Orchestrator README."
        )

    # -- step 2 ------------------------------------------------------------
    def install(self) -> None:
        ui = self.ui
        ui.step(2, "Installing Core's Python packages")
        if self.args.skip_install:
            ui.warn("Skipped (--skip-install).")
            return
        try:
            python = self.deps.ensure_venv(CORE_DIR, self.deps.windows, log=ui.say)
        except bootstrap.BootstrapError as exc:
            raise SetupError(str(exc)) from None
        ui.ok(f"Virtual environment: {python}")

    # -- game server settings (asked before networking so validation sees the ports) -----------
    def sessions(self, plan: Plan) -> None:
        ui, args = self.ui, self.args
        advanced = args.advanced and ui.interactive
        if advanced:
            ui.say("")
            ui.say("Advanced options (press Enter to keep each default)")
        generated = bool(self.remembered("ANDROMEDA_SETUP_HTTP_MODE"))
        prev_exe = self.remembered("GAME_EXECUTABLE_PATH")
        # A path copied from .env.example is not an answer given to this wizard unless it really exists.
        if prev_exe and not generated and not resolve_path(prev_exe, self.root).is_file():
            prev_exe = ""
        default = args.game_exe or prev_exe or default_game_exe(self.root)
        if advanced:
            default = ui.ask("Game server executable", str(resolve_path(default, self.root)))
        plan.game_exe = resolve_path(default, self.root)

        plan.core_port = args.core_port or _int(self.prev.get("CORE_PORT"), 8000)
        plan.port_start = args.port_start or _int(self.prev.get("GAME_PORT_RANGE_START"), 7777)
        plan.port_end = args.port_end or _int(self.prev.get("GAME_PORT_RANGE_END"), 7977)
        plan.max_sessions = args.max_sessions or _int(self.prev.get("MAX_EOB_INSTANCES"), 6)
        plan.log_server = args.log_server if args.log_server is not None else _bool(self.prev.get("ENABLE_LOG_SERVER", "false"))
        plan.log_port = args.log_port or _int(self.prev.get("LOG_SERVER_PORT"), 9090)
        plan.landing_page = (
            args.landing_page if args.landing_page is not None else _bool(self.prev.get("ENABLE_LANDING_PAGE", "false"))
        )
        if advanced:
            plan.core_port = int(ui.ask_valid("Core port", str(plan.core_port), _port))
            plan.port_start = int(ui.ask_valid("First game port", str(plan.port_start), _port))
            plan.port_end = int(ui.ask_valid("Last game port", str(plan.port_end), _port))
            plan.max_sessions = int(ui.ask_valid("Maximum simultaneous matches", str(plan.max_sessions), int))
            plan.landing_page = ui.yes_no("Serve the public landing page at / ?", plan.landing_page)
            plan.log_server = ui.yes_no("Enable the optional TCP client-log collector? (most servers do not need it)", plan.log_server)
            if plan.log_server:
                plan.log_port = int(ui.ask_valid("Log collector port", str(plan.log_port), _port))

    # -- step 3 ------------------------------------------------------------
    def public_address(self) -> str:
        ui, args = self.ui, self.args
        ui.step(3, "Finding this server's public internet address")
        ui.say("  Players' games send game and voice traffic (UDP) straight to this address, so it must")
        ui.say("  be the server's real public IPv4 address. An internet connection is required.")
        manual = args.public_ip or ""
        previous_manual = self.remembered("ANDROMEDA_SETUP_PUBLIC_IP_OVERRIDE") or self.remembered(
            "ANDROMEDA_SETUP_PUBLIC_HOST_OVERRIDE"
        )
        if not manual and previous_manual:
            if ui.yes_no(f"Last time you entered {previous_manual} manually. Keep using it?", True):
                manual = previous_manual
        if manual:
            return self._use_manual(manual)
        try:
            found = self.deps.detect_ip()
        except publicip.PublicIPError as exc:
            for line in str(exc).splitlines():
                ui.fail(line.strip())
            if ui.interactive:
                ui.say("  Without internet access players cannot connect, so there is no offline/LAN fallback.")
                answer = ui.ask_valid(
                    "Type this server's public IPv4 address to continue anyway (Enter to stop)", "", _optional_ipv4
                )
                if answer:
                    return self._use_manual(str(answer))
            raise SetupError(
                "The public IPv4 address is required for game hosting and could not be detected. "
                "Check the internet connection, or rerun with --public-ip <address> if you know it."
            ) from None
        ip = found.ip
        ui.ok(f"Public IPv4 address: {ip} (confirmed by {found.source_text})")
        for url, other in found.disagreements.items():
            ui.warn(f"{url} reported {other} instead. If players cannot connect, rerun with --public-ip.")
        previous = envfile.real_secret(self.prev.get("SERVER_HOST"))
        if previous and previous != ip:
            ui.warn(f"This changed since the last setup ({previous} -> {ip}); the configuration will be updated.")
        if not ui.yes_no(f"Should players connect to {ip}?", True):
            return self._use_manual(str(ui.ask_valid("Public IPv4 address players should use", "", publicip.validate_manual_ipv4)))
        self._nat_hint(ip)
        return ip

    def _use_manual(self, value: str) -> str:
        try:
            address = publicip.validate_manual_ipv4(value)
        except ValueError as exc:
            raise SetupError(f"Invalid public IPv4 address: {exc}") from None
        self.ui.ok(f"Using manually supplied public IPv4 address: {address}")
        self.manual_override = address
        self._nat_hint(address)
        return address

    def _nat_hint(self, public: str) -> None:
        local = self.deps.local_ip()
        if local and local != public:
            self.notes.append(
                f"This server's own network address is {local}, not {public}: it is behind a router or cloud "
                f"firewall. Forward/allow the game ports there too (to {local})."
            )

    # -- step 4 ------------------------------------------------------------
    def http_mode(self, plan: Plan) -> None:
        ui, args = self.ui, self.args
        ui.step(4, "How players reach the Core website and API")
        default_mode = args.http_mode or self.remembered("ANDROMEDA_SETUP_HTTP_MODE") or (
            "tunnel" if args.core_hostname else "domain" if args.core_url else "ip"
        )
        if args.http_mode:
            plan.http_mode = args.http_mode
        else:
            plan.http_mode = ui.choose(
                "Game and voice traffic always uses the public IP above. For Core's website/API choose:",
                [
                    ("ip", f"Directly by IP address, http://{plan.public_host}:{plan.core_port}  (simplest)"),
                    ("tunnel", "Cloudflare Tunnel with your own domain (HTTPS, no open web port)"),
                    ("domain", "My own domain, DNS record or reverse proxy (advanced)"),
                ],
                default_mode if default_mode in HTTP_MODES else "ip",
            )
        if plan.http_mode == "tunnel":
            def hostname(text: str) -> str:
                if not text:
                    raise ValueError("a hostname is required")
                return cloudflare.normalize_hostname(text)

            plan.core_hostname = str(ui.ask_valid(
                "Hostname for Core (example: play.example.com)",
                args.core_hostname or self.remembered("ANDROMEDA_SETUP_CORE_HOSTNAME"),
                hostname,
            ))
        elif plan.http_mode == "domain":
            default = args.core_url or self.remembered("ANDROMEDA_SETUP_CORE_URL")
            plan.core_url = str(ui.ask_valid("Public Core URL (example: https://play.example.com)", default, normalize_url))
            self.notes.append("Domain mode: point your DNS record or reverse proxy at this server's Core port. "
                              "Keep any DNS record for game traffic DNS-only (not proxied).")
        ui.ok(f"Core will be reachable at {plan.core_public_url}")

    # -- step 5 ------------------------------------------------------------
    def tunnel(self, plan: Plan) -> None:
        ui, args = self.ui, self.args
        ui.step(5, "Cloudflare Tunnel (optional, Core website/API only)")
        if plan.http_mode != "tunnel":
            ui.ok("Not used.")
            return
        exe = Path(args.cloudflared) if args.cloudflared else self.deps.find_cloudflared(self.root)
        if exe is not None:
            version = self.deps.cloudflared_version(exe)
            if version is None:
                raise SetupError(f"{exe} did not run correctly; reinstall cloudflared or pass --cloudflared <path>")
            ui.ok(f"cloudflared found: {exe} ({version})")
        elif self.deps.windows:
            ui.warn("cloudflared (Cloudflare's tunnel program) is not installed.")
            target = self.root / "tools" / "cloudflared.exe"
            if args.install_cloudflared or ui.yes_no(
                f"Download the official Windows build from github.com/cloudflare/cloudflared to {target}?", False
            ):
                try:
                    exe = self.deps.download_cloudflared(target)
                except Exception as exc:
                    raise SetupError(f"Downloading cloudflared failed: {exc}") from None
                ui.ok(f"Downloaded cloudflared to {exe}")
            else:
                ui.say("  Install it later with:  winget install --id Cloudflare.cloudflared  (then rerun setup)")
        else:
            ui.warn(f"cloudflared is not installed. {cloudflare.non_windows_install_hint()}")
        plan.cloudflared = str(exe) if exe else ""
        if not exe:
            self.notes.append("Install cloudflared and rerun setup so the start script also starts the tunnel.")
        shown_exe = plan.cloudflared or "cloudflared"

        if args.use_tunnel_config:
            config = args.use_tunnel_config.expanduser().resolve()
            if not config.is_file():
                raise SetupError(f"{config} does not exist")
            if plan.core_hostname not in cloudflare.config_hostnames(config.read_text(encoding="utf-8")):
                ui.warn(f"{config} has no ingress rule for {plan.core_hostname}; add one pointing to http://localhost:{plan.core_port}")
            plan.tunnel_config = config
            ui.ok(f"Using your existing tunnel config: {config}")
        else:
            creds = self._tunnel_credentials()
            if creds:
                ui.ok(f"Using existing tunnel {creds.tunnel_id} (credentials file {creds.path})")
                ui.say("  Make sure the hostname points at this tunnel (safe to run again):")
                ui.say(f"    {cloudflare.quote_arg(shown_exe)} tunnel route dns {creds.tunnel_id} {plan.core_hostname}")
                tunnel_ref = creds.tunnel_id
            else:
                try:
                    tunnel_ref = cloudflare.validate_tunnel_name(args.tunnel_name)
                except ValueError as exc:
                    raise SetupError(str(exc)) from None
                steps = cloudflare.setup_steps(shown_exe, tunnel_ref, [plan.core_hostname])
                if cloudflare.has_origin_cert(self.deps.credential_dirs()):
                    steps = steps[1:]  # already logged in
                ui.warn("No tunnel credentials were found yet. Run these commands once on this server")
                ui.say("  (the login command opens a browser where you pick your Cloudflare domain):")
                for command in steps:
                    ui.say(f"    {command}")
                ui.say("  The generated config below works as soon as the tunnel exists; rerunning setup")
                ui.say("  afterwards also records the exact credentials file.")
                self.notes.append("Finish the Cloudflare Tunnel commands shown in step 5 before starting Andromeda.")
            text = cloudflare.render_config(tunnel_ref, plan.tunnel_routes, creds.path if creds else None)
            previous = self.remembered("ANDROMEDA_SETUP_TUNNEL_CONFIG")
            config = (args.tunnel_config or (Path(previous) if previous else self.root / "cloudflared" / "config.yml"))
            config = config.expanduser().resolve()
            _write_file(ui, config, text, args.force, what="tunnel config")
            plan.tunnel_config = config
        ui.say("")
        ui.say("  IMPORTANT: a Cloudflare Tunnel only carries Core's website and API (HTTP).")
        ui.say(f"  Game and voice traffic uses UDP ports {plan.port_start}-{plan.port_end}, which an ordinary Cloudflare")
        ui.say(f"  Tunnel does NOT carry. Players' games connect to {plan.public_host} directly, so those ports must")
        ui.say("  be open in Windows Firewall and in your hosting provider's firewall / router (see step 8).")

    def _tunnel_credentials(self) -> cloudflare.TunnelCredentials | None:
        if self.args.tunnel_credentials:
            try:
                return cloudflare.read_tunnel_credentials(self.args.tunnel_credentials.expanduser().resolve())
            except ValueError as exc:
                raise SetupError(str(exc)) from None
        found = cloudflare.find_tunnel_credentials(self.deps.credential_dirs())
        if len(found) <= 1:
            return found[0] if found else None
        options = [(str(i), f"{c.tunnel_id}  ({c.path})") for i, c in enumerate(found)]
        if not self.ui.interactive:
            self.ui.warn(f"Several tunnels found; using the newest ({found[0].tunnel_id}). Use --tunnel-credentials to pick.")
            return found[0]
        return found[int(self.ui.choose("Several tunnels were found. Which one should Andromeda use?", options, "0"))]

    def validate(self, plan: Plan) -> None:
        errors = validate(plan)
        if errors:
            raise SetupError("The configuration is not valid:\n" + "\n".join(f"  - {e}" for e in errors))

    # -- secrets -------------------------------------------------------------
    def resolve_secrets(self) -> tuple[str, str | None]:
        ui = self.ui
        admin = os.environ.get("ANDROMEDA_ADMIN_PASSWORD") or (
            None if self.args.rotate_secrets
            else envfile.real_secret(self.prev.get("ADMIN_PASSWORD") or self.prev.get("ADMIN_TOKEN"))
        )
        admin = admin or secrets.token_urlsafe(18)
        steam = os.environ.get("STEAM_API_KEY") or envfile.real_secret(self.prev.get("STEAM_API_KEY")) or self._legacy_api_key()
        if not steam and ui.interactive:
            ui.say("")
            ui.say("  Optional: a Steam Web API key (https://steamcommunity.com/dev/apikey) lets Core verify")
            ui.say("  that players really own their Steam accounts. Without it logins are not verified.")
            steam = ui.secret("Steam Web API key (input hidden; press Enter to skip)") or None
        return admin, steam

    @staticmethod
    def _legacy_api_key() -> str | None:
        """Core's old run_server.bat kept the Steam Web API key in apikey.txt."""
        text = _read_text(CORE_DIR / "apikey.txt")
        return envfile.real_secret(text.strip()) if text else None

    # -- step 6 ------------------------------------------------------------
    def write(self, plan: Plan) -> None:
        ui = self.ui
        ui.step(6, "Writing configuration")
        for label, port in (("Core", plan.core_port),) + ((("Log collector", plan.log_port),) if plan.log_server else ()):
            if not self.deps.port_free(port):
                ui.warn(f"Port {port} ({label}) is in use. That is expected if Andromeda is already running; otherwise pick another port.")

        admin, steam = self.resolve_secrets()
        managed, defaults = core_env(plan, admin, steam)
        managed.update({
            "ANDROMEDA_SETUP_HTTP_MODE": plan.http_mode,
            "ANDROMEDA_SETUP_CORE_HOSTNAME": plan.core_hostname,
            "ANDROMEDA_SETUP_CORE_URL": plan.core_url,
            "ANDROMEDA_SETUP_PUBLIC_IP_OVERRIDE": self.manual_override,
            "ANDROMEDA_SETUP_TUNNEL_CONFIG": str(plan.tunnel_config or ""),
        })
        removed = [key for key in ORCHESTRATOR_KEYS if key in self.prev]
        managed_values, default_values, preserved = envfile.merge(
            self.prev, managed, defaults, remove=ORCHESTRATOR_KEYS + LEGACY_META_KEYS
        )
        text = envfile.render_env(ENV_HEADER, managed_values, default_values, preserved)
        if _read_text(self.env_path) == text:
            ui.ok(f"Core settings are already up to date: {self.env_path}")
        else:
            if self.env_path.exists() and not self.args.force:
                if not ui.interactive:
                    raise SetupError(
                        "The existing Core configuration differs from these settings. Rerun with --force to replace "
                        "it (a backup is kept and the admin password is reused)."
                    )
                keep = "" if self.args.rotate_secrets else " The admin password is kept."
                if not ui.yes_no(f"Save these settings? The previous file is backed up first.{keep}", True):
                    raise SetupCancelled("Kept the existing configuration.")
            backup = envfile.write_env(self.env_path, text, overwrite=True)
            if backup:
                ui.ok(f"Updated Core settings: {self.env_path} (previous version saved as {backup.name})")
            else:
                ui.ok(f"Created Core settings: {self.env_path}")
            if removed:
                ui.ok(f"Removed Orchestrator settings ({', '.join(removed)}); single-server mode does not use them.")
        ui.ok("Passwords and keys were saved to that file and were not printed.")

        svcs = services(plan, self.deps.windows)
        if self.deps.windows:
            launcher = self.root / "start_andromeda.bat"
            launcher.write_text(render_windows_launcher(plan, svcs), encoding="utf-8", newline="")
        else:
            launcher = self.root / "start_andromeda.sh"
            launcher.write_text(render_posix_launcher(plan, svcs), encoding="utf-8")
            launcher.chmod(0o755)
        ui.ok(f"Start script: {launcher}")
        self.launcher = launcher

    # -- step 7 ------------------------------------------------------------
    def game(self, plan: Plan) -> None:
        ui, args = self.ui, self.args
        ui.step(7, "Game server files (SteamCMD, Enemy On Board, MelonLoader, Andromeda.Mod)")
        exe = plan.game_exe
        assert exe is not None
        if args.skip_game:
            ui.warn("Skipped (--skip-game).")
        elif not self.deps.windows:
            ui.warn("Automatic game download only works on Windows; point --game-exe at a modded game install.")
        else:
            update = not args.no_game_update
            if exe.is_file() and update and ui.interactive:
                update = ui.yes_no("The game is already installed. Check for game updates now?", True)
            installer = self.deps.game_installer(
                data_dir=game_data_dir(self.root), exe=exe, pins=self.pins, log=ui.say,
                ask=ui.ask if ui.interactive else None,
            )
            try:
                installer.install_all(update)
            except game.GameInstallError as exc:
                raise SetupError(str(exc)) from None
        if exe.is_file():
            ui.ok(f"Game server executable: {exe}")
        else:
            ui.warn(f"The game was not found at {exe}.")
            self.notes.append("Install the game (run the setup again) before players start matches.")

    # -- step 8 ------------------------------------------------------------
    def firewall(self, plan: Plan) -> None:
        ui = self.ui
        ui.step(8, "Firewall")
        rules = firewall_rules(plan)
        for rule in rules:
            ui.say(f"  Allow inbound {rule.protocol} {rule.ports}: {rule.reason}")
        if plan.http_mode == "tunnel":
            ui.say(f"  Core's port {plan.core_port} stays closed: the Cloudflare Tunnel reaches it over localhost.")
        if self.deps.windows:
            commands = [cmd for rule in rules for cmd in netsh_commands(rule)]
            cleanup = [netsh_delete(name) for name in stale_rules(plan)]
            ui.say("")
            ui.say("  Windows Firewall commands (Administrator Command Prompt):")
            for cmd in commands:
                ui.say(f"    {format_command(cmd)}")
            ui.say("  Remove rules this configuration does not need (errors about missing rules are harmless):")
            for cmd in cleanup:
                ui.say(f"    {format_command(cmd)}")
            if self.args.apply_firewall or ui.yes_no("Add these Windows Firewall rules now?", False):
                if not self.deps.is_admin():
                    ui.warn("Administrator rights are needed. Right-click Setup-Andromeda-Server.bat and choose")
                    ui.warn("'Run as administrator', or paste the commands above into an Administrator Command Prompt.")
                    self.notes.append("The Windows Firewall rules from step 8 have not been added yet.")
                else:
                    for cmd in cleanup + commands:
                        result = self.deps.run(cmd, capture_output=True, text=True)
                        # Deleting a rule that does not exist yet fails harmlessly.
                        if result.returncode != 0 and "add" in cmd:
                            raise SetupError(f"Firewall command failed: {format_command(cmd)}")
                    ui.ok("Windows Firewall rules added.")
        else:
            ui.say("")
            ui.say("  Example (ufw):")
            for rule in rules:
                ui.say(f"    sudo ufw allow {rule.ports.replace('-', ':')}/{rule.protocol.lower()}")
        ui.say("")
        ui.say("  Also allow the same ports in your hosting provider's firewall / security group, or forward")
        ui.say("  them on your router. Without this, players can log in but cannot join matches.")

    # -- summary -------------------------------------------------------------
    def summary(self, plan: Plan) -> None:
        ui = self.ui
        ui.say("")
        ui.say("=" * 66)
        ui.say("Setup finished - single-server mode (one API: Andromeda.Core)")
        ui.say("=" * 66)
        ui.say(f"  Players' Andromeda API address : {plan.core_public_url}   (in game: F10 menu -> API URL)")
        ui.say(f"  Operator console               : {plan.core_public_url}/admin")
        ui.say(f"  Game servers advertised at     : {plan.public_host} (UDP+TCP {plan.port_start}-{plan.port_end})")
        ui.say(f"  Admin password                 : open {self.env_path} and look for ADMIN_PASSWORD")
        ui.say("  Core starts and stops the game server processes itself; no Orchestrator is needed.")
        ui.say("")
        ui.say(f"  START: {'double-click ' if self.deps.windows else ''}{self.launcher}")
        ui.say("  Or start it yourself:")
        for svc in services(plan, self.deps.windows):
            prefix = "cd /d" if self.deps.windows else "cd"
            ui.say(f"    [{svc.title}]  {prefix} \"{svc.cwd}\" && {format_command(svc.argv)}")
        ui.say("")
        ui.say(f"  Health check (on this server): {plan.core_local_url}/health")
        if plan.http_mode == "ip":
            ui.say("  The Core website is served over plain HTTP. Choose the Cloudflare Tunnel option to get HTTPS.")
        for note in self.notes:
            ui.say(f"  NOTE: {note}")
        ui.say("")
        ui.say("  Run the setup again at any time; to only verify the configuration use:  setup_server.py --check")


def run_setup(args: argparse.Namespace, ui: UI, deps: Deps) -> int:
    wizard = Wizard(args, ui, deps)
    ui.say(f"Andromeda {ANDROMEDA_VERSION} server setup - single server, one API (Andromeda.Core)")
    ui.say("Core handles accounts, matchmaking and the website, and starts the game servers itself.")
    ui.say("It is safe to run this again.")
    wizard.locate()
    wizard.install()
    plan = Plan(core_dir=CORE_DIR, install_root=wizard.root)
    wizard.sessions(plan)
    plan.public_host = wizard.public_address()
    wizard.http_mode(plan)
    wizard.validate(plan)
    wizard.tunnel(plan)
    wizard.write(plan)
    wizard.game(plan)
    wizard.firewall(plan)
    wizard.summary(plan)
    return 0


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------

def run_check(args: argparse.Namespace, ui: UI, deps: Deps) -> int:
    failures = 0

    def check(ok: bool, good: str, bad: str, warn_only: bool = False) -> None:
        nonlocal failures
        if ok:
            ui.ok(good)
        elif warn_only:
            ui.warn(bad)
        else:
            ui.fail(bad)
            failures += 1

    root, env_path, _ = _locations(args)
    ui.say("Andromeda configuration check (single-server mode)")
    check(sys.version_info >= MIN_PYTHON, f"Python {sys.version.split()[0]}", "Python 3.10 or newer is required")
    env = envfile.read_env(env_path)
    check(bool(env), f"Core settings: {env_path}", f"Missing {env_path}; run the setup first")
    if not env:
        return 1

    provider = env.get("GAME_SESSION_PROVIDER", "local").strip().lower()
    check(provider == "local", "Core starts game servers itself (GAME_SESSION_PROVIDER=local)",
          f"GAME_SESSION_PROVIDER={provider}: this is the advanced Orchestrator mode, not the single-server setup")
    leftover = [key for key in ORCHESTRATOR_KEYS if key in env]
    check(not leftover, "No Orchestrator settings", f"Unused Orchestrator settings in .env: {', '.join(leftover)}",
          warn_only=True)
    check(bool(envfile.real_secret(env.get("ADMIN_PASSWORD") or env.get("ADMIN_TOKEN"))),
          "Admin password is set", "ADMIN_PASSWORD is empty or a placeholder")
    check(bool(envfile.real_secret(env.get("STEAM_API_KEY"))), "Steam Web API key is set",
          "STEAM_API_KEY is not set; Steam logins are not verified", warn_only=True)

    server_host = env.get("SERVER_HOST", "")
    check(bool(publicip.parse_public_ipv4(server_host)), f"SERVER_HOST {server_host} is a public IPv4 address",
          f"SERVER_HOST {server_host!r} must be this server's public IPv4 address (not a hostname); rerun setup")
    manual = env.get("ANDROMEDA_SETUP_PUBLIC_IP_OVERRIDE", "") or env.get("ANDROMEDA_SETUP_PUBLIC_HOST_OVERRIDE", "")
    try:
        detected = deps.detect_ip().ip
    except publicip.PublicIPError as exc:
        detected = ""
        # A manual override is explicit, so a failed lookup is only a warning then.
        check(False, "", f"Public IP detection failed (internet required): {str(exc).splitlines()[0]}",
              warn_only=bool(manual))
    if detected:
        check(server_host == detected or server_host == manual,
              f"SERVER_HOST {server_host} matches the public IP",
              f"SERVER_HOST is {server_host!r} but this server's public IP is {detected}; rerun setup")

    exe_text = env.get("GAME_EXECUTABLE_PATH", "")
    exe = Path(exe_text) if exe_text else None
    plan = Plan(
        core_dir=CORE_DIR,
        install_root=root,
        public_host=server_host,
        http_mode=env.get("ANDROMEDA_SETUP_HTTP_MODE", "ip"),
        core_port=_int(env.get("CORE_PORT"), 8000),
        port_start=_int(env.get("GAME_PORT_RANGE_START"), 7777),
        port_end=_int(env.get("GAME_PORT_RANGE_END"), 7977),
        max_sessions=_int(env.get("MAX_EOB_INSTANCES"), 6),
        game_exe=exe,
        core_hostname=env.get("ANDROMEDA_SETUP_CORE_HOSTNAME", ""),
        core_url=env.get("ANDROMEDA_SETUP_CORE_URL", ""),
        log_server=_bool(env.get("ENABLE_LOG_SERVER", "false")),
        log_port=_int(env.get("LOG_SERVER_PORT"), 9090),
    )
    for error in validate(plan):
        if not error.startswith("SERVER_HOST"):  # already reported above
            check(False, "", error)
    if exe is not None and exe.is_absolute():
        check(exe.is_file(), f"Game server executable: {exe}", f"Game server executable missing: {exe}")
        if exe.is_file():
            # WARN only: custom installs may use another MelonLoader layout or mod file name.
            check(game.melonloader_installed(exe.parent), "MelonLoader installed",
                  f"MelonLoader not found next to {exe}", warn_only=True)
            check(game.mod_path(exe.parent).is_file(), "Andromeda.Mod installed",
                  f"Missing {game.mod_path(exe.parent)}", warn_only=True)
    python = venv_python(CORE_DIR, deps.windows)
    check(python.exists(), f"Virtual environment: {python}", f"Missing {python}; run the setup", warn_only=True)
    if plan.http_mode == "tunnel":
        configured = env.get("ANDROMEDA_SETUP_TUNNEL_CONFIG", "")
        config = args.use_tunnel_config or args.tunnel_config or (Path(configured) if configured else root / "cloudflared" / "config.yml")
        check(config.is_file(), f"Tunnel config: {config}", f"Tunnel config missing: {config}")
        check(deps.find_cloudflared(root) is not None, "cloudflared found", "cloudflared not found", warn_only=True)
    check(deps.port_free(plan.core_port), f"Port {plan.core_port} (Core) is free",
          f"Port {plan.core_port} (Core) is in use (fine if Andromeda is already running)", warn_only=True)
    ui.say("")
    ui.say("All checks passed." if not failures else f"{failures} problem(s) found.")
    return 1 if failures else 0


def main(argv: list[str] | None = None, ui: UI | None = None, deps: Deps | None = None) -> int:
    args = build_parser().parse_args(argv)
    ui = ui or UI(interactive=not args.non_interactive)
    deps = deps or Deps()
    try:
        if args.check:
            return run_check(args, ui, deps)
        return run_setup(args, ui, deps)
    except SetupError as exc:
        ui.say("")
        for line in str(exc).splitlines():
            ui.fail(line)
        return 2
    except SetupCancelled as exc:
        ui.say("")
        ui.say(f"  {exc} Nothing else was changed.")
        return 1
    except KeyboardInterrupt:
        ui.say("\nCancelled. Nothing else was changed; run the setup again whenever you are ready.")
        return 130
