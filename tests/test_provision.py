from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path

from bzr_live.provision import adapters, keys


class RoutingTests(unittest.TestCase):
    def test_routing_table_is_complete_and_fixed(self) -> None:
        self.assertEqual(
            adapters.BOUNDARIES,
            {
                "group": "bzr",
                "actor": "bzr",
                "product": "bzr",
                "component": "bzr",
                "version": "bridge",
                "milestone": "bridge",
                "custom-field": "bridge",
                "keyword": "bridge",
                "flag-type": "bridge",
            },
        )
        self.assertEqual(adapters.BUG_CUSTOM_FIELD_BOUNDARY, "bugzilla-rest-custom-field")

    def test_project_name_matches_lifecycle_derivation(self) -> None:
        # scripts/lifecycle: printf '%s' "$ROOT" | openssl dgst -sha256; first 12 hex.
        self.assertEqual(
            adapters.compose_project_name("/tmp/example"),
            "bzr-live-" + hashlib.sha256(b"/tmp/example").hexdigest()[:12],
        )


class KeyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "state"

    def test_creates_private_layout(self) -> None:
        store = keys.KeyStore(self.root)
        store.store_actor_key("q4-ada", "secret-a")
        store.store_admin_key("secret-b")
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(os.stat(self.root / "actor-keys").st_mode), 0o700)
        actor_file = self.root / "actor-keys" / "q4-ada.key"
        admin_file = self.root / "admin.key"
        self.assertEqual(stat.S_IMODE(os.stat(actor_file).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(admin_file).st_mode), 0o600)
        self.assertEqual(store.actor_key("q4-ada"), "secret-a")
        self.assertEqual(store.admin_key(), "secret-b")

    def test_admin_key_is_separate_from_actor_namespace(self) -> None:
        store = keys.KeyStore(self.root)
        store.store_admin_key("admin-secret")
        store.store_actor_key("admin", "actor-secret")
        self.assertEqual(store.admin_key(), "admin-secret")
        self.assertEqual(store.actor_key("admin"), "actor-secret")
        self.assertTrue((self.root / "admin.key").is_file())
        self.assertTrue((self.root / "actor-keys" / "admin.key").is_file())

    def test_missing_key_returns_none(self) -> None:
        store = keys.KeyStore(self.root)
        self.assertIsNone(store.admin_key())
        self.assertIsNone(store.actor_key("q4-ada"))

    def test_rejects_bad_directory_mode(self) -> None:
        self.root.mkdir(mode=0o755)
        os.chmod(self.root, 0o755)  # mkdir's mode is umask-masked; pin it explicitly
        with self.assertRaises(adapters.ProvisionError):
            keys.KeyStore(self.root)

    def test_rejects_symlinked_key_file(self) -> None:
        store = keys.KeyStore(self.root)
        target = self.root / "elsewhere"
        target.write_text("x\n")
        os.chmod(target, 0o600)
        os.symlink(target, self.root / "admin.key")
        with self.assertRaises(adapters.ProvisionError):
            store.admin_key()


if __name__ == "__main__":
    unittest.main()
