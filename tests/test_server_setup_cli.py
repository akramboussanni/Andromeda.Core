"""End-to-end tests of the single-server setup wizard with every external effect mocked.

Public IP discovery, cloudflared, SteamCMD/game downloads, venv creation and the
firewall are injected through ``Deps``; ``CORE_DIR`` points at a temporary
server-bundle layout so nothing is written into the repository.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server_setup import cli, envfile, manifest, publicip  # noqa: E402
from server_setup.plan import ORCHESTRATOR_KEYS  # noqa: E402

PUBLIC_IP = "93.184.216.34"
OTHER_IP = "93.184.216.99"
SECRET_ENV = ("ANDROMEDA_ADMIN_PASSWORD", "STEAM_API_KEY", "STEAM_USER", "STEAM_PASS")


class FakeInstaller:
    created: list["FakeInstaller"] = []

    def __init__(self, data_dir, exe, pins, log, ask):
        self.data_dir, self.exe, self.pins, self.log, self.ask = data_dir, exe, pins, log, ask
        self.calls = []
        FakeInstaller.created.append(self)

    def install_all(self, update):
        self.calls.append(update)
        game_dir = self.exe.parent
        (game_dir / "MelonLoader").mkdir(parents=True, exist_ok=True)
        (game_dir / "Mods").mkdir(exist_ok=True)
        (game_dir / "version.dll").write_bytes(b"MZ")
        (game_dir / "Mods" / "Andromeda.Mod.dll").write_bytes(b"MZ")
        self.exe.write_bytes(b"MZ")


class Result:
    def __init__(self, returncode=0):
        self.returncode, self.stdout, self.stderr = returncode, "", ""


class WizardTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        # Same layout as the downloadable server bundle: <root>/core + manifest.
        self.root = base / "Andromeda Server"
        self.core = self.root / "core"
        self.core.mkdir(parents=True)
        (self.core / "main.py").write_text("# core\n")
        (self.core / "requirements.txt").write_text("fastapi\n")
        (self.root / manifest.MANIFEST_NAME).write_text(json.dumps({
            "schema": 1, "version": "0.11.1",
            "mod": {"repo": "akramboussanni/Andromeda.Mod", "tag": "0.11.1"},
            "melonloader": {"version": "0.6.6"},
        }))
        self.creds_dir = base / "cloudflared-home"
        self.creds_dir.mkdir()

        patcher = mock.patch.object(cli, "CORE_DIR", self.core)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
        env_patch = mock.patch.dict(os.environ, env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        FakeInstaller.created = []
        self.ip = PUBLIC_IP
        self.run_calls = []
        self.output: list[str] = []

    @property
    def env(self) -> dict[str, str]:
        return envfile.read_env(self.core / ".env")

    @property
    def env_text(self) -> str:
        return (self.core / ".env").read_text()

    def detect(self):
        if isinstance(self.ip, Exception):
            raise self.ip
        return publicip.Detection(self.ip, ["https://one.example", "https://two.example"])

    def deps(self, **overrides) -> cli.Deps:
        values = dict(
            detect_ip=self.detect,
            local_ip=lambda: "10.0.0.4",
            find_cloudflared=lambda root: None,
            cloudflared_version=lambda path: "cloudflared version 2024.9.1",
            credential_dirs=lambda: [self.creds_dir],
            download_cloudflared=lambda dest: dest,
            ensure_venv=lambda *a, **k: self.fail("venv creation must be skipped"),
            game_installer=FakeInstaller,
            run=lambda cmd, **k: self.run_calls.append(cmd) or Result(),
            is_admin=lambda: False,
            port_free=lambda port: True,
            windows=True,
        )
        values.update(overrides)
        return cli.Deps(**values)

    def run_cli(self, *argv, interactive=None, deps=None, secret="") -> int:
        self.output = []
        argv = list(argv)
        if interactive is None:
            argv = ["--non-interactive", "--skip-install", *argv]
            ui = cli.UI(interactive=False, out=self.output.append)
        else:
            argv = ["--skip-install", *argv]
            ui = cli.UI(interactive=True, input_fn=interactive, secret_fn=lambda prompt: secret, out=self.output.append)
        return cli.main(argv, ui=ui, deps=deps or self.deps())

    @property
    def text(self) -> str:
        return "\n".join(self.output)


class StandardModeTests(WizardTestCase):
    def test_first_run_configures_core_as_the_only_api(self):
        self.assertEqual(self.run_cli(), 0, self.text)
        env = self.env

        self.assertEqual(env["GAME_SESSION_PROVIDER"], "local")
        self.assertEqual(env["SERVER_IP"], "auto")
        self.assertEqual(env["GAME_RUNTIME_MODE"], "process")
        self.assertEqual(env["SERVER_HOST"], PUBLIC_IP)
        self.assertEqual((env["CORE_HOST"], env["CORE_PORT"]), ("0.0.0.0", "8000"))
        self.assertEqual(env["GAME_SESSION_API_URL"], "http://127.0.0.1:8000")
        self.assertEqual((env["GAME_PORT_RANGE_START"], env["GAME_PORT_RANGE_END"], env["MAX_EOB_INSTANCES"]),
                         ("7777", "7977", "6"))
        exe = Path(env["GAME_EXECUTABLE_PATH"])
        self.assertTrue(exe.is_absolute())
        self.assertEqual(exe, self.root / "gameserver" / "game" / "enemy-on-board.exe")
        # Optional extras stay off.
        self.assertEqual(env["ENABLE_LOG_SERVER"], "false")
        self.assertEqual(env["ENABLE_LANDING_PAGE"], "false")
        self.assertTrue(env["ADMIN_PASSWORD"])

        # No Orchestrator API, URL or token anywhere in the standard output.
        everything = self.env_text + self.text + (self.root / "start_andromeda.bat").read_text()
        for key in ORCHESTRATOR_KEYS + ("ORCH_HOST", "ORCH_PORT", "HOST_RUNTIME_MODE"):
            self.assertNotIn(key, everything)
        self.assertNotIn(":9000", everything)
        self.assertNotIn("port 9000", everything)
        self.assertNotIn("uvicorn main:app", everything)

        # Secrets are written, never printed.
        self.assertNotIn(env["ADMIN_PASSWORD"], self.text)
        if os.name != "nt":
            self.assertEqual((self.core / ".env").stat().st_mode & 0o777, 0o600)

        # Game installed through the (mocked) installer into the absolute path with the bundle's pins.
        self.assertEqual(len(FakeInstaller.created), 1)
        installer = FakeInstaller.created[0]
        self.assertEqual(installer.exe, exe)
        self.assertEqual(installer.data_dir, self.root / "gameserver")
        self.assertIsNone(installer.ask)
        self.assertEqual((installer.pins.mod_tag, installer.pins.melonloader_version), ("0.11.1", "0.6.6"))
        self.assertEqual(installer.calls, [True])

        # One launcher: Core only (no tunnel configured).
        launcher = (self.root / "start_andromeda.bat").read_text()
        self.assertEqual(launcher.count('start "'), 1)
        self.assertIn(f'start "Andromeda Core" /D "{self.core}"', launcher)
        self.assertIn("main.py --host 0.0.0.0 --port 8000", launcher)
        self.assertIn("setup_server.py\" --check --install-root", launcher)
        self.assertNotIn("cloudflared", launcher)

        # Exact firewall rules: Core HTTP (not tunnelled) + direct game/voice ports; nothing applied.
        self.assertIn('netsh advfirewall firewall add rule name="Andromeda game+voice UDP" dir=in action=allow '
                      "protocol=UDP localport=7777-7977 profile=any", self.text)
        self.assertIn('netsh advfirewall firewall add rule name="Andromeda game TCP" dir=in action=allow '
                      "protocol=TCP localport=7777-7977 profile=any", self.text)
        self.assertIn('netsh advfirewall firewall add rule name="Andromeda Core API" dir=in action=allow '
                      "protocol=TCP localport=8000 profile=any", self.text)
        self.assertIn('netsh advfirewall firewall delete rule name="Andromeda Orchestrator API"', self.text)
        self.assertEqual(self.run_calls, [])
        self.assertIn("10.0.0.4", self.text, "NAT hint mentions the local address")
        self.assertIn("no Orchestrator is needed", self.text)

    def test_relative_game_path_is_made_absolute_against_the_server_folder(self):
        os.chdir(self.creds_dir)
        self.addCleanup(os.chdir, Path(__file__).resolve().parent)
        self.assertEqual(self.run_cli("--game-exe", "custom/game.exe", "--skip-game"), 0, self.text)
        self.assertEqual(Path(self.env["GAME_EXECUTABLE_PATH"]), self.root / "custom" / "game.exe")
        self.assertIn("Skipped (--skip-game)", self.text)
        self.assertEqual(FakeInstaller.created, [])

    def test_plain_checkout_without_bundle_uses_the_core_folder(self):
        (self.root / manifest.MANIFEST_NAME).unlink()
        self.assertEqual(self.run_cli("--skip-game"), 0, self.text)
        self.assertEqual(Path(self.env["GAME_EXECUTABLE_PATH"]), self.core / "gameserver" / "game" / "enemy-on-board.exe")
        self.assertTrue((self.core / "start_andromeda.bat").is_file())
        self.assertIn("built-in defaults", self.text)

    def test_linux_writes_posix_launcher_and_skips_game_download(self):
        self.assertEqual(self.run_cli(deps=self.deps(windows=False)), 0, self.text)
        launcher = (self.root / "start_andromeda.sh").read_text()
        self.assertTrue(launcher.startswith("#!/usr/bin/env bash"))
        self.assertIn("main.py", launcher)
        self.assertIn("sudo ufw allow 7777:7977/udp", self.text)
        self.assertEqual(FakeInstaller.created, [])
        self.assertEqual(self.env["GAME_SESSION_PROVIDER"], "local")

    def test_legacy_steam_api_key_file_is_imported_and_not_printed(self):
        (self.core / "apikey.txt").write_text("0123456789ABCDEF0123456789ABCDEF \n")
        self.assertEqual(self.run_cli(), 0, self.text)
        self.assertEqual(self.env["STEAM_API_KEY"], "0123456789ABCDEF0123456789ABCDEF")
        self.assertNotIn("0123456789ABCDEF0123456789ABCDEF", self.text)

    def test_env_copied_from_example_is_replaced_with_local_mode(self):
        example = Path(__file__).resolve().parent.parent / ".env.example"
        (self.core / ".env").write_text(example.read_text())
        self.assertEqual(self.run_cli("--force"), 0, self.text)
        env = self.env
        self.assertEqual(env["GAME_SESSION_PROVIDER"], "local")
        self.assertNotEqual(env["ADMIN_PASSWORD"], "change-me")
        self.assertEqual(Path(env["GAME_EXECUTABLE_PATH"]), self.root / "gameserver" / "game" / "enemy-on-board.exe")

    def test_pinned_mod_version_can_be_overridden(self):
        self.assertEqual(self.run_cli("--mod-version", "0.11.0"), 0, self.text)
        self.assertEqual(FakeInstaller.created[0].pins.mod_tag, "0.11.0")

    def test_landing_page_and_log_server_are_opt_in(self):
        self.assertEqual(self.run_cli("--landing-page", "--log-server", "--log-port", "9191"), 0, self.text)
        env = self.env
        self.assertEqual((env["ENABLE_LANDING_PAGE"], env["ENABLE_LOG_SERVER"], env["LOG_SERVER_PORT"]),
                         ("true", "true", "9191"))
        self.assertIn("localport=9191", self.text)
        self.assertEqual(self.run_cli("--no-log-server", "--force"), 0, self.text)
        self.assertEqual(self.env["ENABLE_LOG_SERVER"], "false")
        self.assertIn('delete rule name="Andromeda log collector"', self.text)


class PublicAddressTests(WizardTestCase):
    def test_detection_failure_stops_without_writing_anything(self):
        self.ip = publicip.PublicIPError("Could not determine this server's public IPv4 address.\n    - x: timeout")
        self.assertEqual(self.run_cli(), 2)
        self.assertIn("public IPv4 address is required", self.text)
        self.assertIn("--public-ip", self.text)
        self.assertFalse((self.core / ".env").exists())
        self.assertFalse((self.root / "start_andromeda.bat").exists())

    def test_manual_public_ip_override(self):
        self.ip = publicip.PublicIPError("offline")
        self.assertEqual(self.run_cli("--public-ip", OTHER_IP), 0, self.text)
        self.assertEqual(self.env["SERVER_HOST"], OTHER_IP)
        self.assertEqual(self.env["ANDROMEDA_SETUP_PUBLIC_IP_OVERRIDE"], OTHER_IP)

    def test_manual_hostnames_and_private_addresses_are_rejected(self):
        for value in ("play.example.com", "192.168.1.5", "127.0.0.1", "100.64.0.1"):
            with self.subTest(value=value):
                self.assertEqual(self.run_cli("--public-ip", value), 2)
                self.assertIn("Invalid public IPv4 address", self.text)
                self.assertFalse((self.core / ".env").exists())

    def test_interactive_failure_asks_for_a_public_ipv4_and_rejects_lan_addresses(self):
        self.ip = publicip.PublicIPError("offline")
        typed = iter(["10.0.0.5", "play.example.com", PUBLIC_IP])

        def answer(prompt):
            return next(typed) if "public IPv4" in prompt else ""

        self.assertEqual(self.run_cli(interactive=answer), 0, self.text)
        self.assertEqual(self.env["SERVER_HOST"], PUBLIC_IP)
        self.assertIn("private/LAN", self.text)
        self.assertIn("hostname", self.text)
        self.assertIn("no offline/LAN fallback", self.text)
        self.assertIsNotNone(FakeInstaller.created[0].ask)

    def test_interactive_empty_answer_stops(self):
        self.ip = publicip.PublicIPError("offline")
        self.assertEqual(self.run_cli(interactive=lambda prompt: ""), 2)
        self.assertFalse((self.core / ".env").exists())

    def test_disagreeing_providers_are_reported(self):
        def detect():
            return publicip.Detection(PUBLIC_IP, ["https://a"], {"https://b": OTHER_IP})

        self.assertEqual(self.run_cli(deps=self.deps(detect_ip=detect)), 0, self.text)
        self.assertIn(f"https://b reported {OTHER_IP}", self.text)
        self.assertEqual(self.env["SERVER_HOST"], PUBLIC_IP)


class TunnelTests(WizardTestCase):
    def test_tunnel_carries_core_http_only_and_server_host_stays_the_ip(self):
        (self.creds_dir / "0f0e-tunnel.json").write_text(json.dumps({"TunnelID": "0f0e-tunnel", "TunnelSecret": "TOPSECRET"}))
        cloudflared = self.root / "tools" / "cloudflared.exe"
        deps = self.deps(find_cloudflared=lambda root: cloudflared)
        rc = self.run_cli("--http-mode", "tunnel", "--core-hostname", "https://Play.Example.com/", deps=deps)
        self.assertEqual(rc, 0, self.text)

        env = self.env
        self.assertEqual(env["SERVER_HOST"], PUBLIC_IP)
        self.assertEqual(env["CORE_HOST"], "127.0.0.1")
        self.assertEqual(env["GAME_SESSION_PROVIDER"], "local")
        self.assertEqual(env["ANDROMEDA_SETUP_CORE_HOSTNAME"], "play.example.com")
        config = Path(env["ANDROMEDA_SETUP_TUNNEL_CONFIG"])
        self.assertEqual(config, self.root / "cloudflared" / "config.yml")
        text = config.read_text()
        self.assertIn('tunnel: "0f0e-tunnel"', text)
        self.assertIn(str(self.creds_dir / "0f0e-tunnel.json"), text)
        self.assertIn("service: http://localhost:8000", text)
        self.assertEqual(text.count("hostname:"), 1, "only Core is routed")
        self.assertNotIn("9000", text)
        self.assertNotIn("TOPSECRET", text + self.text)

        self.assertIn("does NOT carry", self.text)
        self.assertIn("https://play.example.com", self.text)
        self.assertIn("tunnel route dns 0f0e-tunnel play.example.com", self.text)
        # Core's HTTP port is closed when tunnelled; game/voice ports are always open.
        self.assertNotIn("add rule name=\"Andromeda Core API\"", self.text)
        self.assertIn('delete rule name="Andromeda Core API"', self.text)
        self.assertIn("localport=7777-7977", self.text)

        launcher = (self.root / "start_andromeda.bat").read_text()
        self.assertEqual(launcher.count('start "'), 2)
        self.assertIn("main.py --host 127.0.0.1 --port 8000", launcher)
        self.assertIn(f'cmd /k ""{cloudflared}" tunnel --config "{config}" run"', launcher)

        # Rerun: identical settings are already up to date, so no --force is needed.
        self.assertEqual(self.run_cli(deps=deps), 0, self.text)
        self.assertIn("already up to date", self.text)

    def test_missing_cloudflared_prints_one_time_commands(self):
        rc = self.run_cli("--http-mode", "tunnel", "--core-hostname", "play.example.com", deps=self.deps(windows=False))
        self.assertEqual(rc, 0, self.text)
        self.assertIn("cloudflared tunnel login", self.text)
        self.assertIn("cloudflared tunnel create andromeda", self.text)
        self.assertIn("cloudflared tunnel route dns andromeda play.example.com", self.text)
        self.assertNotIn("cloudflared", (self.root / "start_andromeda.sh").read_text())

    def test_windows_downloads_cloudflared_on_request(self):
        downloaded = []

        def download(dest):
            downloaded.append(dest)
            return dest

        rc = self.run_cli("--http-mode", "tunnel", "--core-hostname", "play.example.com", "--install-cloudflared",
                          deps=self.deps(download_cloudflared=download))
        self.assertEqual(rc, 0, self.text)
        self.assertEqual(downloaded, [self.root / "tools" / "cloudflared.exe"])
        self.assertIn("cloudflared.exe", (self.root / "start_andromeda.bat").read_text())

    def test_tunnel_needs_a_hostname(self):
        self.assertEqual(self.run_cli("--http-mode", "tunnel"), 2)
        self.assertIn("hostname is required", self.text)

    def test_switching_back_from_tunnel_reopens_core_port(self):
        self.assertEqual(self.run_cli("--http-mode", "tunnel", "--core-hostname", "play.example.com"), 0, self.text)
        self.assertEqual(self.run_cli("--http-mode", "ip", "--force"), 0, self.text)
        self.assertEqual(self.env["CORE_HOST"], "0.0.0.0")
        self.assertIn('add rule name="Andromeda Core API"', self.text)


class RerunTests(WizardTestCase):
    def test_rerun_keeps_secrets_custom_keys_and_backs_up(self):
        self.assertEqual(self.run_cli(), 0, self.text)
        admin = self.env["ADMIN_PASSWORD"]
        with open(self.core / ".env", "a", encoding="utf-8") as handle:
            handle.write("MANAGED_ZIP_TOKEN=keep-this-zip-token\nENABLE_UPNP=true\n")

        # A changed answer + an edited file: non-interactive runs refuse to replace without --force.
        self.assertEqual(self.run_cli("--max-sessions", "8"), 2)
        self.assertIn("--force", self.text)
        self.assertEqual(self.env["MAX_EOB_INSTANCES"], "6")

        self.assertEqual(self.run_cli("--max-sessions", "8", "--force"), 0, self.text)
        env = self.env
        self.assertEqual(env["MAX_EOB_INSTANCES"], "8")
        self.assertEqual(env["ADMIN_PASSWORD"], admin)
        self.assertEqual(env["MANAGED_ZIP_TOKEN"], "keep-this-zip-token")
        self.assertEqual(env["ENABLE_UPNP"], "true", "tuned defaults are kept")
        self.assertTrue(list(self.core.glob(".env.backup-*")))
        self.assertNotIn("keep-this-zip-token", self.text)

    def test_identical_rerun_changes_nothing(self):
        self.assertEqual(self.run_cli(), 0, self.text)
        before = self.env_text
        self.assertEqual(self.run_cli(), 0, self.text)
        self.assertEqual(before, self.env_text)
        self.assertEqual(list(self.core.glob(".env.backup-*")), [])

    def test_rotate_secrets(self):
        self.assertEqual(self.run_cli(), 0, self.text)
        admin = self.env["ADMIN_PASSWORD"]
        self.assertEqual(self.run_cli("--rotate-secrets", "--force"), 0, self.text)
        self.assertNotEqual(self.env["ADMIN_PASSWORD"], admin)
        self.assertNotIn(self.env["ADMIN_PASSWORD"], self.text)

    def test_interactive_decline_keeps_file(self):
        self.assertEqual(self.run_cli(), 0, self.text)
        before = self.env_text

        def answer(prompt):
            return "n" if "Save these settings" in prompt else ""

        self.assertEqual(self.run_cli("--max-sessions", "9", interactive=answer), 1)
        self.assertEqual(self.env_text, before)
        self.assertIn("Kept the existing configuration", self.text)

    def test_previous_manual_ip_is_reused_when_detection_fails(self):
        self.assertEqual(self.run_cli("--public-ip", OTHER_IP), 0, self.text)
        self.ip = publicip.PublicIPError("offline")
        self.assertEqual(self.run_cli(), 0, self.text)
        self.assertEqual(self.env["SERVER_HOST"], OTHER_IP)

    def test_changed_public_ip_updates_server_host(self):
        self.assertEqual(self.run_cli(), 0, self.text)
        self.ip = OTHER_IP
        self.assertEqual(self.run_cli("--force"), 0, self.text)
        self.assertIn(f"{PUBLIC_IP} -> {OTHER_IP}", self.text)
        self.assertEqual(self.env["SERVER_HOST"], OTHER_IP)

    def test_earlier_two_api_setup_is_migrated_without_its_token(self):
        # The file the earlier Core + Orchestrator setup produced.
        (self.core / ".env").write_text(
            "# Generated by Andromeda.Orchestrator/setup.py.\n"
            f"SERVER_HOST={PUBLIC_IP}\nGAME_SESSION_PROVIDER=host-api\n"
            "GAME_HOST_API_BASE_URL=http://127.0.0.1:9000\nGAME_HOST_API_TOKEN=old-orchestrator-token-value\n"
            "ADMIN_PASSWORD=keep-admin-password\nANDROMEDA_SETUP_ORCH_HOSTNAME=orch.example.com\n"
        )
        # Never converted silently.
        self.assertEqual(self.run_cli("--force"), 2)
        self.assertIn("--convert-to-local", self.text)
        self.assertIn("GAME_HOST_API_TOKEN=old-orchestrator-token-value", self.env_text)

        self.assertEqual(self.run_cli("--force", "--convert-to-local"), 0, self.text)
        env = self.env
        self.assertEqual(env["GAME_SESSION_PROVIDER"], "local")
        self.assertEqual(env["ADMIN_PASSWORD"], "keep-admin-password")
        for key in ORCHESTRATOR_KEYS + ("ANDROMEDA_SETUP_ORCH_HOSTNAME",):
            self.assertNotIn(key, env)
        self.assertNotIn("old-orchestrator-token-value", self.env_text + self.text)
        self.assertIn("Removed Orchestrator settings", self.text)
        backups = list(self.core.glob(".env.backup-*"))
        self.assertTrue(backups and "old-orchestrator-token-value" in backups[0].read_text())


class FirewallTests(WizardTestCase):
    def test_apply_firewall_as_administrator(self):
        rc = self.run_cli("--apply-firewall", deps=self.deps(is_admin=lambda: True))
        self.assertEqual(rc, 0, self.text)
        adds = [cmd for cmd in self.run_calls if "add" in cmd]
        self.assertEqual(len(adds), 3)
        self.assertEqual(
            [next(a for a in cmd if a.startswith("localport=")) for cmd in adds],
            ["localport=7777-7977", "localport=7777-7977", "localport=8000"],
        )
        self.assertIn("protocol=UDP", adds[0])
        deleted = [next(a for a in cmd if a.startswith("name=")) for cmd in self.run_calls if "delete" in cmd]
        self.assertIn("name=Andromeda Orchestrator API", deleted)
        self.assertIn("Windows Firewall rules added", self.text)

    def test_tunnel_firewall_never_opens_core(self):
        rc = self.run_cli("--apply-firewall", "--http-mode", "tunnel", "--core-hostname", "play.example.com",
                          deps=self.deps(is_admin=lambda: True))
        self.assertEqual(rc, 0, self.text)
        added = [next(a for a in cmd if a.startswith("localport=")) for cmd in self.run_calls if "add" in cmd]
        self.assertEqual(added, ["localport=7777-7977", "localport=7777-7977"])

    def test_apply_firewall_without_admin_only_explains(self):
        self.assertEqual(self.run_cli("--apply-firewall"), 0, self.text)
        self.assertEqual(self.run_calls, [])
        self.assertIn("Run as administrator", self.text)

    def test_failed_firewall_add_is_an_error(self):
        deps = self.deps(is_admin=lambda: True, run=lambda cmd, **k: Result(1))
        self.assertEqual(self.run_cli("--apply-firewall", deps=deps), 2)
        self.assertIn("Firewall command failed", self.text)


class CheckTests(WizardTestCase):
    def test_check_passes_after_setup(self):
        self.assertEqual(self.run_cli(), 0, self.text)
        self.assertEqual(self.run_cli("--check"), 0, self.text)
        self.assertIn("All checks passed", self.text)
        self.assertIn("GAME_SESSION_PROVIDER=local", self.text)
        self.assertIn("MelonLoader installed", self.text)

    def test_check_fails_when_public_ip_changed(self):
        self.assertEqual(self.run_cli(), 0, self.text)
        self.ip = OTHER_IP
        self.assertEqual(self.run_cli("--check"), 1)
        self.assertIn(f"this server's public IP is {OTHER_IP}", self.text)

    def test_check_fails_when_detection_fails_without_manual_ip(self):
        self.assertEqual(self.run_cli(), 0, self.text)
        self.ip = publicip.PublicIPError("Could not determine\n detail")
        self.assertEqual(self.run_cli("--check"), 1)
        self.assertIn("[FAIL] Public IP detection failed", self.text)

    def test_check_only_warns_on_detection_failure_with_manual_ip(self):
        self.assertEqual(self.run_cli("--public-ip", PUBLIC_IP), 0, self.text)
        self.ip = publicip.PublicIPError("offline")
        self.assertEqual(self.run_cli("--check"), 0, self.text)
        self.assertIn("[WARN] Public IP detection failed", self.text)

    def test_check_rejects_hostname_server_host_relative_exe_and_host_api(self):
        self.assertEqual(self.run_cli(), 0, self.text)
        text = self.env_text.replace(f"SERVER_HOST={PUBLIC_IP}", "SERVER_HOST=play.example.com")
        text = text.replace("GAME_SESSION_PROVIDER=local", "GAME_SESSION_PROVIDER=host-api")
        text += "GAME_EXECUTABLE_PATH=game/enemy-on-board.exe\nGAME_HOST_API_TOKEN=leftover-token-value\n"
        (self.core / ".env").write_text(text)
        self.assertEqual(self.run_cli("--check"), 1)
        self.assertIn("must be this server's public IPv4 address", self.text)
        self.assertIn("advanced Orchestrator mode", self.text)
        self.assertIn("must be an absolute path", self.text)
        self.assertIn("Unused Orchestrator settings", self.text)
        self.assertNotIn("leftover-token-value", self.text)

    def test_check_reports_missing_game_and_config(self):
        self.assertEqual(self.run_cli("--check"), 1)
        self.assertIn("run the setup first", self.text)
        self.assertEqual(self.run_cli("--skip-game"), 0, self.text)
        self.assertEqual(self.run_cli("--check"), 1)
        self.assertIn("Game server executable missing", self.text)


if __name__ == "__main__":
    unittest.main()
