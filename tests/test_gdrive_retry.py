"""Tests for SSL/connection-reset retry handling in gdrive.scan and gdrive.fetch."""

from __future__ import annotations

import ssl
import unittest
from unittest.mock import MagicMock, patch

from gdrive.scan import _list_children


class _FlakyFilesList:
    """Raises ssl.SSLError once, then returns a canned response."""

    def __init__(self, response: dict) -> None:
        self._response = response
        self._raised = False

    def execute(self):
        if not self._raised:
            self._raised = True
            raise ssl.SSLError("EOF occurred in violation of protocol")
        return self._response


class TestListChildrenNetworkRetry(unittest.TestCase):
    def test_retries_on_ssl_error_and_resets_connections(self) -> None:
        response = {"files": [{"id": "f1", "name": "a.txt", "mimeType": "text/plain"}]}
        service = MagicMock()
        service.files.return_value.list.return_value = _FlakyFilesList(response)
        connections = {"stale": object()}
        service._http.http.connections = connections

        with patch("gdrive.scan._sleep_backoff_network"):
            out = _list_children(service, "folder123")

        self.assertEqual(out, response["files"])
        self.assertEqual(connections, {})  # dead socket dropped before retry

    def test_raises_after_exhausting_retries(self) -> None:
        service = MagicMock()

        class _AlwaysFails:
            def execute(self):
                raise ConnectionResetError("Connection reset by peer")

        service.files.return_value.list.return_value = _AlwaysFails()

        with patch("gdrive.scan._sleep_backoff_network"):
            with self.assertRaises(ConnectionResetError):
                _list_children(service, "folder123", max_retries=1)


if __name__ == "__main__":
    unittest.main()
