from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path

from bzr_live.provision import adapters, keys
from bzr_live.provision.adapters import (
    BridgeClient,
    BzrClient,
    ProvisionError,
    assign_bug_custom_fields,
)


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


class _FakeRun:
    """Records subprocess invocations and returns scripted results."""

    def __init__(self, results):
        self.calls = []
        self._results = list(results)

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        code, out, err = self._results.pop(0)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)


class BzrClientTests(unittest.TestCase):
    def _client(self, results):
        fake = _FakeRun(results)
        return BzrClient("/opt/bzr", "http://127.0.0.1:8080/", "k3y", run=fake), fake

    def test_read_builds_stateless_argv_and_passes_key_via_env(self) -> None:
        client, fake = self._client([(0, b'{"data": {"name": "q4-devs"}}', b"")])
        payload = client.read(["group", "view"], positionals=["q4-devs"])
        argv, kwargs = fake.calls[0]
        self.assertEqual(
            argv[:6],
            ["/opt/bzr", "--json", "--server-url", "http://127.0.0.1:8080/",
             "--server-api-key-env", "BZR_LIVE_API_KEY"],
        )
        self.assertIn("--", argv)
        self.assertEqual(argv[argv.index("--") + 1 :], ["q4-devs"])
        self.assertEqual(kwargs["env"]["BZR_LIVE_API_KEY"], "k3y")
        self.assertNotIn("k3y", " ".join(argv))
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(payload, {"name": "q4-devs"})

    def test_read_exit_two_is_absent(self) -> None:
        client, _ = self._client([(2, b"", b"not found")])
        self.assertIsNone(client.read(["group", "view"], positionals=["missing"]))

    def test_read_other_exit_is_boundary_failure(self) -> None:
        client, _ = self._client([(4, b"", b"api error")])
        with self.assertRaises(ProvisionError):
            client.read(["group", "view"], positionals=["q4-devs"])

    def test_write_exit_two_is_boundary_failure_not_absent(self) -> None:
        client, _ = self._client([(2, b"", b"bad args")])
        with self.assertRaises(ProvisionError):
            client.write(["group", "create", "--name=q4-devs"])


class BridgeClientTests(unittest.TestCase):
    def _client(self, results):
        fake = _FakeRun(results)
        prefix = ["docker", "compose", "--project-name", "bzr-live-abc",
                  "exec", "-T", "--user", "www-data", "bugzilla", "bzr-live-bridge"]
        return BridgeClient(prefix, "bzr-live-abc", run=fake), fake

    def test_only_allowlisted_operations_are_emitted(self) -> None:
        client, _ = self._client([])
        with self.assertRaises(ProvisionError):
            client.call("drop-tables", {})

    def test_call_sends_json_stdin_and_parses_ok_reply(self) -> None:
        reply = json.dumps({"ok": True, "result": {"name": "q4-hot"}}).encode()
        client, fake = self._client([(0, reply, b"")])
        result = client.call("get-keyword", {"name": "q4-hot"})
        argv, kwargs = fake.calls[0]
        self.assertEqual(argv[-1], "get-keyword")
        self.assertEqual(json.loads(kwargs["input"]), {"name": "q4-hot"})
        self.assertEqual(result, {"name": "q4-hot"})

    def test_error_reply_raises_without_secret_leak(self) -> None:
        # The scripted error smuggles key-like material; none of it may surface.
        reply = json.dumps(
            {"ok": False, "error": "insert failed for key t0psecretmaterial"}).encode()
        client, _ = self._client([(1, reply, b"")])
        with self.assertRaises(ProvisionError) as ctx:
            client.call("create-api-key", {"login": None})
        self.assertNotIn("t0psecretmaterial", str(ctx.exception))
        self.assertNotIn("api_key", str(ctx.exception))

    def test_container_not_found_names_project_root_recovery(self) -> None:
        client, _ = self._client([(1, b"", b'service "bugzilla" is not running')])
        with self.assertRaises(ProvisionError) as ctx:
            client.call("get-keyword", {"name": "q4-hot"})
        self.assertIn("bzr-live-abc", str(ctx.exception))
        self.assertIn("--project-root", str(ctx.exception))


class RestAdapterTests(unittest.TestCase):
    def test_put_shape_carries_key_in_body(self) -> None:
        captured = {}

        def fake_opener(request):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["body"] = json.loads(request.data.decode())
            return io.BytesIO(json.dumps({"bugs": [{"id": 7}]}).encode())

        assign_bug_custom_fields(
            "http://127.0.0.1:8080/", "k3y", 7, {"cf_q4_risk": "low"},
            opener=fake_opener,
        )
        self.assertEqual(captured["url"], "http://127.0.0.1:8080/rest/bug/7")
        self.assertEqual(captured["method"], "PUT")
        self.assertEqual(captured["body"], {"cf_q4_risk": "low", "api_key": "k3y"})

    def test_error_body_raises_without_key(self) -> None:
        def fake_opener(request):
            return io.BytesIO(json.dumps({"error": True, "message": "nope"}).encode())

        with self.assertRaises(ProvisionError) as ctx:
            assign_bug_custom_fields(
                "http://127.0.0.1:8080/", "k3y", 7, {"cf_x": "v"}, opener=fake_opener)
        self.assertNotIn("k3y", str(ctx.exception))

    def test_non_int_bug_id_is_rejected(self) -> None:
        with self.assertRaises(ProvisionError):
            assign_bug_custom_fields("http://127.0.0.1:8080/", "k", "7", {"cf_x": "v"})

    def test_http_error_maps_to_provision_error_without_key(self) -> None:
        def raising_opener(request):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {},
                io.BytesIO(b'{"error": true, "message": "auth"}'))

        with self.assertRaises(ProvisionError) as ctx:
            assign_bug_custom_fields(
                "http://127.0.0.1:8080/", "k3y", 7, {"cf_x": "v"},
                opener=raising_opener)
        self.assertIn("401", str(ctx.exception))
        self.assertNotIn("k3y", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
