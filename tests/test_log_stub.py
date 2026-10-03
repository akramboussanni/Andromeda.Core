import os
import socket
import unittest
from unittest.mock import patch

import log_server


class LogStubTests(unittest.TestCase):
    def test_stub_accepts_multiple_clients_without_enqueuing_logs(self):
        with patch.dict(os.environ, {"LOG_SERVER_MODE": "stub"}), patch.object(log_server, "_enqueue") as enqueue:
            thread = log_server.start_log_server(0)
            try:
                with socket.create_connection(("127.0.0.1", thread.port), timeout=2) as first:
                    first.sendall(b"no newline" * 10000)
                    with socket.create_connection(("127.0.0.1", thread.port), timeout=2) as second:
                        second.sendall(b"[error] discarded\n")
                thread.server.shutdown()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                enqueue.assert_not_called()
                self.assertIs(thread.server.RequestHandlerClass, log_server.StubLogHandler)
            finally:
                thread.server.server_close()

    def test_default_keeps_collector_handler(self):
        with patch.dict(os.environ, {}, clear=True):
            thread = log_server.start_log_server(0)
            try:
                self.assertIs(thread.server.RequestHandlerClass, log_server.LogHandler)
            finally:
                thread.server.shutdown()
                thread.server.server_close()
                thread.join(timeout=2)

    def test_invalid_mode_does_not_bind(self):
        with patch.dict(os.environ, {"LOG_SERVER_MODE": "typo"}):
            with self.assertRaises(ValueError):
                log_server.start_log_server(0)
