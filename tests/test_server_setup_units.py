"""Unit tests for the server_setup helpers (standard library only, no network)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server_setup import cloudflare, envfile, game, manifest, plan, publicip  # noqa: E402

PUBLIC_IP = "93.184.216.34"
OTHER_IP = "93.184.216.99"


def fetcher(answers):
    def fetch(url, timeout):
        value = answers[url]
        if isinstance(value, Exception):
            raise value
        return value
    return fetch


class PublicIPTests(unittest.TestCase):
    def test_two_agreeing_providers_confirm_the_address(self):
        answers = {"a": OSError("offline"), "b": "10.0.0.7\n", "c": f"{PUBLIC_IP}\n", "d": f" {PUBLIC_IP}", "e": "x"}
        found = publicip.detect_public_ipv4(["a", "b", "c", "d", "e"], fetch=fetcher(answers))
        self.assertEqual((found.ip, found.sources, found.disagreements), (PUBLIC_IP, ["c", "d"], {}))

    def test_single_answer_is_accepted_when_others_are_down(self):
        answers = {"a": TimeoutError("t"), "b": PUBLIC_IP}
        self.assertEqual(publicip.detect_public_ipv4(["a", "b"], fetch=fetcher(answers)).ip, PUBLIC_IP)

    def test_disagreement_is_reported_and_majority_wins(self):
        answers = {"a": OTHER_IP, "b": PUBLIC_IP, "c": PUBLIC_IP}
        found = publicip.detect_public_ipv4(["a", "b", "c"], fetch=fetcher(answers))
        self.assertEqual(found.ip, PUBLIC_IP)
        self.assertEqual(found.disagreements, {"a": OTHER_IP})

    def test_failure_is_an_error_not_a_lan_fallback(self):
        with self.assertRaises(publicip.PublicIPError) as ctx:
            publicip.detect_public_ipv4(["https://one", "https://two"], fetch=fetcher(
                {"https://one": TimeoutError("timed out"), "https://two": "192.168.1.20"}))
        message = str(ctx.exception)
        self.assertIn("internet connection is required", message)
        self.assertIn("https://one", message)
        self.assertIn("https://two", message)

    def test_default_providers_are_https(self):
        self.assertGreaterEqual(len(publicip.PROVIDERS), 3)
        self.assertTrue(all(url.startswith("https://") for url in publicip.PROVIDERS))

    def test_manual_ipv4_validation(self):
        self.assertEqual(publicip.validate_manual_ipv4(f" {PUBLIC_IP} "), PUBLIC_IP)
        for bad, fragment in (
            ("play.example.com", "hostname"),
            ("192.168.1.5", "private"),
            ("127.0.0.1", "private"),
            ("100.64.0.1", "private"),  # carrier-grade NAT
            ("2606:4700::1", "IPv4"),
            ("not an ip", "not an IPv4"),
            ("", "required"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    publicip.validate_manual_ipv4(bad)
                self.assertIn(fragment, str(ctx.exception))


class EnvFileTests(unittest.TestCase):
    def test_round_trip_keeps_windows_paths_literal(self):
        values = {
            "PLAIN": r"C:\AndromedaServer\gameserver\game\enemy-on-board.exe",
            "SPACED": r"C:\Andromeda Server\gameserver\game\enemy-on-board.exe",
            "FLAG": True,
            "EMPTY": "",
        }
        text = envfile.render_env("header", values)
        self.assertIn("SPACED='C:\\Andromeda Server\\gameserver\\game\\enemy-on-board.exe'", text)
        parsed = envfile.parse_env(text)
        self.assertEqual(parsed["PLAIN"], values["PLAIN"])
        self.assertEqual(parsed["SPACED"], values["SPACED"])
        self.assertEqual(parsed["FLAG"], "true")
        self.assertEqual(parsed["EMPTY"], "")

    def test_parse_handles_comments_export_and_quotes(self):
        parsed = envfile.parse_env('# c\nexport A=1\nB="two words"\nC=3 # note\nnot a line\n')
        self.assertEqual(parsed, {"A": "1", "B": "two words", "C": "3"})

    def test_newlines_and_single_quotes_are_refused(self):
        with self.assertRaises(ValueError):
            envfile.format_value("a\nb")
        with self.assertRaises(ValueError):
            envfile.format_value("it's here")

    def test_placeholders_are_not_secrets(self):
        for value in ("", "  ", "change-me", "CHANGE-ME", "changeme", "YOUR_API_KEY_HERE", None):
            self.assertIsNone(envfile.real_secret(value))
        self.assertEqual(envfile.real_secret(" real "), "real")

    def test_merge_preserves_unknown_and_tuned_keys_and_drops_removed(self):
        managed, defaults, preserved = envfile.merge(
            {"A": "old", "TUNED": "mine", "CUSTOM": "keep", "GAME_HOST_API_TOKEN": "t"},
            {"A": "new"}, {"TUNED": "default", "OTHER": "d"}, remove=("GAME_HOST_API_TOKEN",),
        )
        self.assertEqual(managed, {"A": "new"})
        self.assertEqual(defaults, {"TUNED": "mine", "OTHER": "d"})
        self.assertEqual(preserved, {"CUSTOM": "keep"})

    def test_write_env_backs_up_and_restricts_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            self.assertIsNone(envfile.write_env(path, "A=1\n", overwrite=False))
            with self.assertRaises(FileExistsError):
                envfile.write_env(path, "A=2\n", overwrite=False)
            first = envfile.write_env(path, "A=2\n", overwrite=True)
            second = envfile.write_env(path, "A=3\n", overwrite=True)
            self.assertNotEqual(first, second, "backups made within one second must not collide")
            self.assertEqual(first.read_text(), "A=1\n")
            self.assertEqual(second.read_text(), "A=2\n")
            self.assertEqual(path.read_text(), "A=3\n")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            self.assertEqual([p.name for p in Path(tmp).glob(".*.tmp-*")], [])


class PlanTests(unittest.TestCase):
    def make(self, **overrides) -> plan.Plan:
        values = dict(core_dir=Path("/srv/andromeda/core"), install_root=Path("/srv/andromeda"), public_host=PUBLIC_IP,
                      game_exe=Path("/srv/andromeda/gameserver/game/enemy-on-board.exe"))
        values.update(overrides)
        return plan.Plan(**values)

    def test_default_plan_is_valid(self):
        self.assertEqual(plan.validate(self.make()), [])

    def test_core_env_is_local_process_mode_without_orchestrator_settings(self):
        managed, defaults = plan.core_env(self.make(), "admin", None)
        self.assertEqual(managed["GAME_SESSION_PROVIDER"], "local")
        self.assertEqual(managed["SERVER_IP"], "auto")
        self.assertEqual(managed["SERVER_IP_CACHE_SECONDS"], 30)
        self.assertEqual(managed["GAME_RUNTIME_MODE"], "process")
        self.assertEqual(managed["GAME_EXECUTABLE_PATH"], str(Path("/srv/andromeda/gameserver/game/enemy-on-board.exe")))
        self.assertEqual(managed["GAME_SESSION_API_URL"], "http://127.0.0.1:8000")
        self.assertEqual(managed["SERVER_HOST"], PUBLIC_IP)
        self.assertIs(managed["ENABLE_LOG_SERVER"], False)
        self.assertIs(managed["ENABLE_LANDING_PAGE"], False)
        self.assertNotIn("STEAM_API_KEY", managed)
        for key in plan.ORCHESTRATOR_KEYS:
            self.assertNotIn(key, {**managed, **defaults})

    def test_server_host_must_be_public_ipv4(self):
        for host in ("play.example.com", "192.168.0.10", "::1"):
            with self.subTest(host=host):
                errors = plan.validate(self.make(public_host=host))
                self.assertTrue(any("SERVER_HOST" in e and "public IPv4" in e for e in errors), errors)

    def test_game_executable_must_be_absolute(self):
        errors = plan.validate(self.make(game_exe=Path("gameserver/game/enemy-on-board.exe")))
        self.assertTrue(any("absolute" in e for e in errors), errors)
        self.assertTrue(any("not set" in e for e in plan.validate(self.make(game_exe=None))))

    def test_port_conflicts_and_capacity(self):
        errors = plan.validate(self.make(core_port=7780, max_sessions=500, log_server=True, log_port=7780))
        joined = "\n".join(errors)
        self.assertIn("overlaps the game port range", joined)
        self.assertIn("both use port 7780", joined)
        self.assertIn("only fits", joined)

    def test_tunnel_mode_never_puts_the_hostname_in_server_host(self):
        p = self.make(http_mode="tunnel", core_hostname="play.example.com")
        managed, _ = plan.core_env(p, "admin", None)
        self.assertEqual(managed["SERVER_HOST"], PUBLIC_IP)
        self.assertEqual(managed["CORE_HOST"], "127.0.0.1")
        self.assertEqual(p.core_public_url, "https://play.example.com")
        self.assertEqual(p.tunnel_routes, [("play.example.com", 8000)])
        rules = plan.firewall_rules(p)
        self.assertEqual({(r.protocol, r.ports) for r in rules}, {("UDP", "7777-7977"), ("TCP", "7777-7977")})
        self.assertIn(plan.CORE_RULE, plan.stale_rules(p))

    def test_ip_mode_opens_core_port_and_never_an_orchestrator_port(self):
        p = self.make()
        rules = plan.firewall_rules(p)
        self.assertIn(("TCP", "8000"), {(r.protocol, r.ports) for r in rules})
        self.assertNotIn("9000", [r.ports for r in rules])
        self.assertEqual(plan.stale_rules(p), [plan.ORCHESTRATOR_RULE, plan.LOG_RULE])

    def test_netsh_commands_are_exact(self):
        rule = plan.firewall_rules(self.make())[0]
        delete, add = plan.netsh_commands(rule)
        self.assertEqual(plan.format_command(delete), 'netsh advfirewall firewall delete rule name="Andromeda game+voice UDP"')
        self.assertEqual(
            plan.format_command(add),
            'netsh advfirewall firewall add rule name="Andromeda game+voice UDP" dir=in action=allow '
            "protocol=UDP localport=7777-7977 profile=any",
        )

    def test_windows_launcher_quotes_paths_with_spaces_and_checks_first(self):
        p = self.make(core_dir=Path("C:/Andromeda Server/core"), install_root=Path("C:/Andromeda Server"),
                      http_mode="tunnel", core_hostname="play.example.com",
                      cloudflared="C:/Program Files/cloudflared/cloudflared.exe",
                      tunnel_config=Path("C:/Andromeda Server/cloudflared/config.yml"))
        text = plan.render_windows_launcher(p, plan.services(p, windows=True))
        lines = text.split("\r\n")
        self.assertTrue(text.endswith("\r\n"))
        check = lines.index(next(line for line in lines if "--check" in line))
        core = lines.index(next(line for line in lines if line.startswith('start "Andromeda Core"')))
        self.assertLess(check, core, "the configuration is checked before Core starts")
        self.assertIn('"C:/Andromeda Server/core/venv/Scripts/python.exe" "C:/Andromeda Server/core/setup_server.py" '
                      '--check --install-root "C:/Andromeda Server"', lines[check])
        self.assertIn('/D "C:/Andromeda Server/core" cmd /k ""C:/Andromeda Server/core/venv/Scripts/python.exe" '
                      'main.py --host 127.0.0.1 --port 8000"', lines[core])
        tunnel = next(line for line in lines if line.startswith('start "Andromeda Cloudflare Tunnel"'))
        self.assertIn('""C:/Program Files/cloudflared/cloudflared.exe" tunnel --config '
                      '"C:/Andromeda Server/cloudflared/config.yml" run"', tunnel)
        self.assertNotIn("uvicorn", text)

    def test_windows_launcher_uses_tunnel_token_file(self):
        p = self.make(core_dir=Path("C:/Andromeda Server/core"), install_root=Path("C:/Andromeda Server"),
                      http_mode="tunnel", core_hostname="play.example.com",
                      cloudflared="C:/Program Files/cloudflared/cloudflared.exe",
                      tunnel_token_file=Path("C:/Andromeda Server/cloudflared/tunnel.token"))
        text = plan.render_windows_launcher(p, plan.services(p, windows=True))
        self.assertIn('tunnel run --token-file "C:/Andromeda Server/cloudflared/tunnel.token"', text)

    def test_posix_launcher(self):
        p = self.make()
        text = plan.render_posix_launcher(p, plan.services(p, windows=False))
        self.assertIn("'/srv/andromeda/core/venv/bin/python' 'main.py' '--host' '0.0.0.0' '--port' '8000'", text)
        self.assertIn("--check", text)


class CloudflareTests(unittest.TestCase):
    def test_normalize_hostname(self):
        self.assertEqual(cloudflare.normalize_hostname("https://Play.Example.com/path"), "play.example.com")
        for bad in ("localhost", "bad host.com", "-x.example.com"):
            with self.assertRaises(ValueError):
                cloudflare.normalize_hostname(bad)

    def test_render_config_routes_core_http_only(self):
        text = cloudflare.render_config("abc-123", [("play.example.com", 8000)], Path("/home/u/.cloudflared/abc-123.json"))
        self.assertIn('tunnel: "abc-123"', text)
        self.assertIn("service: http://localhost:8000", text)
        self.assertIn("http_status:404", text)
        self.assertIn("UDP", text)
        self.assertEqual(cloudflare.config_hostnames(text), ["play.example.com"])

    def test_credentials_discovery_never_returns_the_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "abc.json").write_text(json.dumps({"TunnelID": "abc", "TunnelSecret": "s3cr3t"}))
            (d / "junk.json").write_text("{not json")
            (d / "other.json").write_text(json.dumps({"AccountTag": "x"}))
            found = cloudflare.find_tunnel_credentials([d, d / "missing"])
            self.assertEqual([(c.tunnel_id, c.path.name) for c in found], [("abc", "abc.json")])
            self.assertNotIn("s3cr3t", repr(found))
            self.assertFalse(cloudflare.has_origin_cert([d]))
            (d / "cert.pem").write_text("x")
            self.assertTrue(cloudflare.has_origin_cert([d]))

    def test_find_cloudflared_prefers_path_then_known_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp) / "cloudflared.exe"
            self.assertEqual(cloudflare.find_cloudflared(Path(tmp), which=lambda n: "/usr/bin/cloudflared",
                                                         candidates=[candidate]), Path("/usr/bin/cloudflared"))
            self.assertIsNone(cloudflare.find_cloudflared(Path(tmp), which=lambda n: None, candidates=[candidate]))
            candidate.write_bytes(b"MZ")
            self.assertEqual(cloudflare.find_cloudflared(Path(tmp), which=lambda n: None, candidates=[candidate]), candidate)

    def test_download_cloudflared_validates_the_binary(self):
        class Opener:
            def __init__(self, payload):
                self.payload = payload

            def __call__(self, req, timeout):
                return io.BytesIO(self.payload)

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "tools" / "cloudflared.exe"
            self.assertEqual(cloudflare.download_cloudflared(dest, opener=Opener(b"MZ" + b"\0" * 1_100_000)), dest)
            bad = Path(tmp) / "bad.exe"
            with self.assertRaises(RuntimeError):
                cloudflare.download_cloudflared(bad, opener=Opener(b"<html>error</html>"))
            self.assertFalse(bad.exists())
            self.assertFalse(bad.with_name("bad.exe.part").exists())

    def test_setup_steps(self):
        steps = cloudflare.setup_steps("C:/Program Files/cf.exe", "andromeda", ["play.example.com"])
        self.assertEqual(steps, [
            '"C:/Program Files/cf.exe" tunnel login',
            '"C:/Program Files/cf.exe" tunnel create andromeda',
            '"C:/Program Files/cf.exe" tunnel route dns andromeda play.example.com',
        ])


class ManifestTests(unittest.TestCase):
    def test_defaults_match_this_release(self):
        pins = manifest.load(None)
        self.assertEqual((pins.version, pins.mod_tag), ("0.11.4", "0.11.1"))
        self.assertEqual(pins.mod_repo, "akramboussanni/Andromeda.Mod")

    def test_load_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / manifest.MANIFEST_NAME
            path.write_text(json.dumps({"schema": 1, "version": "0.11.1",
                                        "mod": {"tag": "0.11.1", "url": "https://x/Andromeda.Mod.dll", "sha256": "A" * 64}}))
            pins = manifest.load(path)
            self.assertEqual((pins.mod_url, pins.mod_sha256, pins.source), ("https://x/Andromeda.Mod.dll", "a" * 64, path))
            path.write_text(json.dumps({"schema": 1, "mod": {"sha256": "nothex"}}))
            with self.assertRaises(manifest.ManifestError):
                manifest.load(path)
            path.write_text(json.dumps({"schema": 99}))
            with self.assertRaises(manifest.ManifestError):
                manifest.load(path)


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


MOD_BYTES = b"MZ-andromeda-mod-0.11.1"
MOD_URL = "https://github.com/akramboussanni/Andromeda.Mod/releases/download/0.11.1/Andromeda.Mod.dll"
RELEASE = {"tag_name": "0.11.1", "assets": [
    {"name": "Andromeda.Mod.pdb", "browser_download_url": "https://x/pdb"},
    {"name": "Andromeda.Mod.zip", "browser_download_url": "https://x/zip"},
    {"name": "Andromeda.Mod.dll", "browser_download_url": MOD_URL},
]}


class GameTests(unittest.TestCase):
    def test_steamcmd_command_only_includes_password_when_given(self):
        cmd = game.steamcmd_command(Path("steamcmd.exe"), Path("game"), "999860", "bob")
        self.assertEqual(cmd[3:5], ["+login", "bob"])
        self.assertEqual(cmd[5], "+app_update")
        cmd = game.steamcmd_command(Path("s"), Path("g"), "999860", "bob", password="pw", guard_code="X1", branch="beta")
        self.assertEqual(cmd[3:7], ["+login", "bob", "pw", "X1"])
        self.assertIn("-beta", cmd)
        self.assertEqual(game.steamcmd_command(Path("s"), Path("g"), "1", "anonymous", password="pw")[3:5], ["+login", "anonymous"])

    def test_release_asset_selection_and_urls(self):
        self.assertEqual(game.pick_release_asset(RELEASE, manifest.MOD_ASSET_PATTERN), MOD_URL)
        self.assertIsNone(game.pick_release_asset({"assets": []}, manifest.MOD_ASSET_PATTERN))
        self.assertEqual(game.release_api_url("o/r", "0.11.1"), "https://api.github.com/repos/o/r/releases/tags/0.11.1")
        self.assertTrue(game.release_api_url("o/r", "latest").endswith("/releases/latest"))
        self.assertTrue(game.mod_fallback_urls("o/r", "0.11.1")[0].endswith("/download/0.11.1/Andromeda.Mod.dll"))
        pins = manifest.Pins(melonloader_version="0.6.6")
        self.assertTrue(game.melonloader_url(pins, {}).endswith("/download/v0.6.6/MelonLoader.x64.zip"))
        # The Orchestrator's Docker default points at the Linux build; it must not leak into Windows installs.
        linux = {"MELONLOADER_URL": "https://github.com/LavaGang/MelonLoader/releases/latest/download/MelonLoader.Linux.x64.zip"}
        self.assertNotIn("Linux", game.melonloader_url(manifest.Pins(), linux))

    def test_zip_slip_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = make_zip(Path(tmp) / "evil.zip", {"../escape.txt": b"x"})
            with self.assertRaises(game.GameInstallError):
                game.safe_extract_zip(archive, Path(tmp) / "out")
            self.assertFalse((Path(tmp) / "escape.txt").exists())

    def installer(self, tmp: str, pins: manifest.Pins | None = None, **kwargs):
        data = Path(tmp) / "gameserver"
        exe = data / "game" / "enemy-on-board.exe"
        record = {"fetched": [], "ran": [], "logs": [], "api": []}

        def fetch(url, dest):
            record["fetched"].append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if "steamcmd" in url:
                return make_zip(dest, {"steamcmd.exe": b"MZ"})
            if "MelonLoader" in url:
                return make_zip(dest, {"version.dll": b"MZ", "MelonLoader/net6/MelonLoader.dll": b"MZ"})
            if url == MOD_URL:
                dest.write_bytes(MOD_BYTES)
                return dest
            raise game.GameInstallError("404")

        def fetch_json(url):
            record["api"].append(url)
            return RELEASE

        def run(cmd):
            record["ran"].append(cmd)
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_bytes(b"MZ")
            return 0

        values = dict(data_dir=data, exe=exe, pins=pins or manifest.Pins(), log=record["logs"].append, ask=None,
                      fetch=fetch, fetch_json=fetch_json, run=run, env={"STEAM_USER": "bob", "STEAM_PASS": "hunter2"})
        values.update(kwargs)
        return game.GameInstaller(**values), exe, record

    def test_full_install_with_mocked_downloads_and_steamcmd(self):
        with tempfile.TemporaryDirectory() as tmp:
            installer, exe, record = self.installer(tmp)
            installer.install_all(update=True)

            self.assertTrue(exe.is_file())
            self.assertTrue(game.melonloader_installed(exe.parent))
            self.assertEqual(game.mod_path(exe.parent).read_bytes(), MOD_BYTES)
            self.assertEqual(record["api"], ["https://api.github.com/repos/akramboussanni/Andromeda.Mod/releases/tags/0.11.1"])
            self.assertIn(MOD_URL, record["fetched"])
            self.assertEqual(len(record["ran"]), 1)
            self.assertIn("hunter2", record["ran"][0])  # unattended login still works...
            self.assertFalse(any("hunter2" in line for line in record["logs"]))  # ...but the password is never printed
            self.assertEqual((exe.parent.parent / "steamcmd" / game.STEAM_USER_FILE).read_text(), "bob")
            self.assertEqual(game.read_mod_stamp(exe.parent)["tag"], "0.11.1")
            self.assertEqual(sorted(p.name for p in exe.parent.parent.iterdir()), ["game", "steamcmd"], "downloads are cleaned up")

            # Rerun without update: the pinned mod is already installed, so nothing is downloaded or run.
            record["fetched"].clear()
            record["ran"].clear()
            installer.install_all(update=False)
            self.assertEqual((record["ran"], record["fetched"]), ([], []))
            self.assertTrue(any("already installed" in line for line in record["logs"]))

    def test_pinned_sha256_is_enforced(self):
        good = hashlib.sha256(MOD_BYTES).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            installer, exe, record = self.installer(tmp, manifest.Pins(mod_url=MOD_URL, mod_sha256=good))
            installer.install_mod()
            self.assertEqual(game.mod_path(exe.parent).read_bytes(), MOD_BYTES)
            self.assertEqual(record["api"], [], "a pinned URL needs no GitHub lookup")
        with tempfile.TemporaryDirectory() as tmp:
            installer, exe, _ = self.installer(tmp, manifest.Pins(mod_url=MOD_URL, mod_sha256="0" * 64))
            with self.assertRaises(game.GameInstallError) as ctx:
                installer.install_mod()
            self.assertIn("integrity check", str(ctx.exception))
            self.assertFalse(game.mod_path(exe.parent).exists())

    def test_missing_pinned_release_fails_clearly_instead_of_installing_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            installer, exe, record = self.installer(tmp, manifest.Pins(mod_tag="9.9.9"),
                                                    fetch_json=lambda url: {"assets": []})
            with self.assertRaises(game.GameInstallError) as ctx:
                installer.install_mod()
            self.assertIn("9.9.9", str(ctx.exception))
            self.assertIn("--mod-version", str(ctx.exception))
            self.assertFalse(any("latest" in url for url in record["fetched"]))

    def test_github_api_outage_falls_back_to_direct_links(self):
        def api_down(url):
            raise OSError("rate limited")

        with tempfile.TemporaryDirectory() as tmp:
            installer, exe, record = self.installer(tmp, fetch_json=api_down)
            installer.install_mod()
            self.assertEqual(record["fetched"], [MOD_URL])

    def test_interactive_anonymous_failure_asks_for_an_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            steamcmd = Path(tmp) / "steamcmd" / "steamcmd.exe"
            steamcmd.parent.mkdir()
            steamcmd.write_bytes(b"MZ")
            exe = Path(tmp) / "game" / "enemy-on-board.exe"
            ran = []

            def run(cmd):
                ran.append(cmd)
                if "alice" in cmd:
                    exe.parent.mkdir(parents=True, exist_ok=True)
                    exe.write_bytes(b"MZ")
                return 0

            answers = iter(["anonymous", "alice"])
            installer = game.GameInstaller(data_dir=Path(tmp), exe=exe, log=lambda s: None,
                                           ask=lambda prompt, default: next(answers), run=run, env={})
            installer.install_game(steamcmd, update=True)
            self.assertEqual([cmd[4] for cmd in ran], ["anonymous", "alice"])
            self.assertTrue(all(len(cmd) == cmd.index("+app_update") + 4 for cmd in ran), "no password in argv")

    def test_missing_game_after_steamcmd_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            steamcmd = Path(tmp) / "steamcmd.exe"
            steamcmd.write_bytes(b"MZ")
            installer = game.GameInstaller(data_dir=Path(tmp), exe=Path(tmp) / "g" / "x.exe", log=lambda s: None,
                                           run=lambda cmd: 0, env={})
            with self.assertRaises(game.GameInstallError):
                installer.install_game(steamcmd, update=True)


if __name__ == "__main__":
    unittest.main()
