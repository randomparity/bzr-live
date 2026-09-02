from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from bzr_live.provision import KeyStore, ProvisionError
from bzr_live.provision.adapters import BUG_ABSENT_CODES, BzrClient
from bzr_live.replay import ReplayContext, ReplayError
from bzr_live.scenario import Reference, load_scenario

# Anchored to this file, not the CWD, matching tests/test_scenario_resources.py:13 and
# tests/test_provision.py:29; the relative form only works from the repo root.
FIXTURE = Path(__file__).parent / "fixtures" / "replay-scenario"


def _state_root(stack: unittest.TestCase) -> Path:
    """A 0700 state root that is removed when the test ends."""
    directory = tempfile.TemporaryDirectory()
    stack.addCleanup(directory.cleanup)
    root = Path(directory.name) / "state"
    return root


class ContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _state_root(self)
        self.keys = KeyStore(self.root)
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = workspace.name
        self.scenario = load_scenario(FIXTURE)

    def _context(self, **kwargs) -> ReplayContext:
        return ReplayContext(
            self.scenario, self.keys, bzr_path="bzr",
            base_url="http://127.0.0.1:8080/", workspace=self.workspace, **kwargs)

    def test_missing_actor_key_names_provisioning(self) -> None:
        context = self._context()
        with self.assertRaises(ReplayError) as caught:
            context.client(Reference("actor", "triager"))
        self.assertIn("no API key for actor 'triager'", str(caught.exception))
        self.assertIn("bzr_live.provision", str(caught.exception))

    def test_client_is_cached_and_registers_the_secret(self) -> None:
        self.keys.store_actor_key("triager", "SECRET-KEY")
        context = self._context()
        first = context.client(Reference("actor", "triager"))
        second = context.client(Reference("actor", "triager"))
        self.assertIs(first, second)
        self.assertEqual(context.known_secrets, frozenset({"SECRET-KEY"}))

    def test_resolve_reports_an_unresolved_reference(self) -> None:
        context = self._context()
        with self.assertRaises(ReplayError) as caught:
            context.resolve(Reference("bug", "absent"))
        self.assertIn("bug:absent", str(caught.exception))
        context.adopt({"bug:absent": 12})
        self.assertEqual(context.resolve(Reference("bug", "absent")), 12)

    def test_text_file_is_private_and_holds_the_body(self) -> None:
        context = self._context()
        path = context.text_file("body", "hello")
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "hello")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_text_file_names_are_unique(self) -> None:
        context = self._context()
        self.assertNotEqual(context.text_file("body", "a"), context.text_file("body", "b"))

    def test_asset_checksum_mismatch_refuses(self) -> None:
        context = self._context()
        with self.assertRaises(ReplayError) as caught:
            context.asset_file("notes", "0" * 64)
        self.assertIn("notes.txt", str(caught.exception))
        self.assertIn("hashes to", str(caught.exception))


class AbsentCodeTest(unittest.TestCase):
    def _client(self, code: int, api_code: int | None) -> BzrClient:
        stderr = b"" if api_code is None else json.dumps(
            {"error": {"api_code": api_code}}).encode("utf-8")

        def run(argv, capture_output=False, env=None, shell=False):
            return subprocess.CompletedProcess(argv, code, b"", stderr)

        return BzrClient("bzr", "http://127.0.0.1:8080/", "K", "a@b.test", run=run)

    def test_bug_absent_codes_read_as_absent(self) -> None:
        for api_code in (100, 101):
            client = self._client(4, api_code)
            self.assertIsNone(client.read(
                ["bug", "view"], positionals=["x"], absent_codes=BUG_ABSENT_CODES))

    def test_access_denied_still_raises(self) -> None:
        client = self._client(4, 102)
        with self.assertRaises(ProvisionError):
            client.read(["bug", "view"], positionals=["x"], absent_codes=BUG_ABSENT_CODES)

    def test_default_absent_codes_are_unchanged(self) -> None:
        self.assertIsNone(self._client(4, 51).read(["product", "view"], positionals=["p"]))
        with self.assertRaises(ProvisionError):
            self._client(4, 100).read(["product", "view"], positionals=["p"])


if __name__ == "__main__":
    unittest.main()
