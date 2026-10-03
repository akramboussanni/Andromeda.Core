import unittest
from unittest.mock import patch

from server_setup.publicip import Detection
from services import game_server_service as service


class DynamicServerIPTests(unittest.TestCase):
    def setUp(self):
        self.values = (
            service.SERVER_IP_AUTO,
            service.SERVER_IP_CACHE_SECONDS,
            service._resolved_server_host,
            service._resolved_server_host_at,
            service.BOOT_PROVIDER,
        )
        service.SERVER_IP_AUTO = True
        service.SERVER_IP_CACHE_SECONDS = 30
        service._resolved_server_host = ""
        service._resolved_server_host_at = 0
        service.BOOT_PROVIDER = "none"

    def tearDown(self):
        (
            service.SERVER_IP_AUTO,
            service.SERVER_IP_CACHE_SECONDS,
            service._resolved_server_host,
            service._resolved_server_host_at,
            service.BOOT_PROVIDER,
        ) = self.values

    @patch("server_setup.publicip.detect_public_ipv4")
    def test_auto_resolves_on_demand_and_uses_short_cache(self, detect):
        detect.return_value = Detection("8.8.8.8", ["mock"])
        self.assertEqual(service.get_server_host(), "8.8.8.8")
        self.assertEqual(service.get_server_host(), "8.8.8.8")
        detect.assert_called_once()

    @patch("server_setup.publicip.detect_public_ipv4")
    def test_force_refresh_picks_up_router_change(self, detect):
        detect.side_effect = [Detection("8.8.8.8", ["mock"]), Detection("1.1.1.1", ["mock"])]
        self.assertEqual(service.get_server_host(), "8.8.8.8")
        self.assertEqual(service.get_server_host(force_refresh=True), "1.1.1.1")

    @patch("server_setup.publicip.detect_public_ipv4", side_effect=RuntimeError("offline"))
    def test_auto_failure_is_explicit(self, _detect):
        with self.assertRaisesRegex(RuntimeError, "SERVER_IP=auto"):
            service.get_server_host()


if __name__ == "__main__":
    unittest.main()
