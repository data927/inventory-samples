"""Tests for the shared SSL/connection-reset retry helper and its callers."""

from __future__ import annotations

import ssl
import unittest
from unittest.mock import MagicMock, patch

from gdrive.fetch import call_with_retry
from gdrive.scan import _list_children
from tools.export_ai_labs_samples import _drive_copy


class _FlakyExecute:
    """Raises a given exception once, then returns a canned response."""

    def __init__(self, response, exc: Exception) -> None:
        self._response = response
        self._exc = exc
        self._raised = False

    def execute(self):
        if not self._raised:
            self._raised = True
            raise self._exc
        return self._response


class TestCallWithRetry(unittest.TestCase):
    def test_retries_on_ssl_error_and_resets_connections(self) -> None:
        service = MagicMock()
        connections = {"stale": object()}
        service._http.http.connections = connections
        flaky = _FlakyExecute("ok", ssl.SSLError("RECORD_LAYER_FAILURE"))

        with patch("gdrive.fetch._sleep_backoff_network"):
            result = call_with_retry(service, flaky.execute)

        self.assertEqual(result, "ok")
        self.assertEqual(connections, {})  # dead socket dropped before retry

    def test_retries_on_connection_reset(self) -> None:
        service = MagicMock()
        flaky = _FlakyExecute("ok", ConnectionResetError("Connection reset by peer"))

        with patch("gdrive.fetch._sleep_backoff_network"):
            result = call_with_retry(service, flaky.execute)

        self.assertEqual(result, "ok")

    def test_retries_on_timeout_error(self) -> None:
        service = MagicMock()
        flaky = _FlakyExecute("ok", TimeoutError("The read operation timed out"))

        with patch("gdrive.fetch._sleep_backoff_network"):
            result = call_with_retry(service, flaky.execute)

        self.assertEqual(result, "ok")

    def test_raises_after_exhausting_retries(self) -> None:
        service = MagicMock()

        def always_fails():
            raise ConnectionResetError("Connection reset by peer")

        with patch("gdrive.fetch._sleep_backoff_network"):
            with self.assertRaises(ConnectionResetError):
                call_with_retry(service, always_fails, max_retries=1)


class TestListChildrenUsesRetry(unittest.TestCase):
    def test_recovers_from_ssl_error(self) -> None:
        response = {"files": [{"id": "f1", "name": "a.txt", "mimeType": "text/plain"}]}
        service = MagicMock()
        service.files.return_value.list.return_value = _FlakyExecute(
            response, ssl.SSLError("EOF occurred in violation of protocol")
        )

        with patch("gdrive.fetch._sleep_backoff_network"):
            out = _list_children(service, "folder123")

        self.assertEqual(out, response["files"])


class TestDriveCopyUsesRetry(unittest.TestCase):
    def test_recovers_from_ssl_error(self) -> None:
        service = MagicMock()
        service.files.return_value.copy.return_value = _FlakyExecute(
            {"id": "new123"}, ssl.SSLError("RECORD_LAYER_FAILURE")
        )

        with patch("gdrive.fetch._sleep_backoff_network"):
            new_id = _drive_copy(service, "file1", "parent1", "name.txt")

        self.assertEqual(new_id, "new123")


if __name__ == "__main__":
    unittest.main()
