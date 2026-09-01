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
from bzr_live.provision.executor import (
    SYSTEM_GROUPS,
    ProvisionConflictError,
    Provisioner,
    custom_field_name,
)
from bzr_live.scenario import load_scenario

FIXTURE = Path(__file__).parent / "fixtures" / "provision-scenario"


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
        client = BzrClient(
            "/opt/bzr", "http://127.0.0.1:8080/", "k3y",
            admin_email="admin@bugzilla.test", run=fake)
        return client, fake

    def test_read_builds_stateless_argv_and_passes_key_via_env(self) -> None:
        client, fake = self._client([(0, b'{"data": {"name": "q4-devs"}}', b"")])
        payload = client.read(["group", "view"], positionals=["q4-devs"])
        argv, kwargs = fake.calls[0]
        self.assertEqual(
            argv[:8],
            ["/opt/bzr", "--json", "--server-url", "http://127.0.0.1:8080/",
             "--server-api-key-env", "BZR_LIVE_API_KEY",
             "--server-email", "admin@bugzilla.test"],
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

    def test_read_api_not_found_codes_are_absent(self) -> None:
        # Live fact: a missing object is a server-side API error (exit 4) with a
        # structured code on stderr's last line; 51/105/106 are the pinned
        # not-found codes (Bugzilla WebService/Constants.pm at BZ_SOURCE_SHA).
        for code in (51, 105, 106):
            err = json.dumps({"schema_version": "1.0.0", "error": {
                "api_code": code, "type": "api", "message": "x", "exit_code": 4}})
            client, _ = self._client([(4, b"", b"WARN noise\n" + err.encode())])
            self.assertIsNone(
                client.read(["group", "view"], positionals=["missing"]),
                f"api_code {code} should classify absent")

    def test_read_other_api_error_is_boundary_failure(self) -> None:
        err = json.dumps({"schema_version": "1.0.0", "error": {
            "api_code": 32000, "type": "api", "message": "boom", "exit_code": 4}})
        client, _ = self._client([(4, b"", err.encode())])
        with self.assertRaises(ProvisionError):
            client.read(["group", "view"], positionals=["q4-devs"])

    def test_read_other_exit_is_boundary_failure(self) -> None:
        client, _ = self._client([(4, b"", b"api error")])
        with self.assertRaises(ProvisionError):
            client.read(["group", "view"], positionals=["q4-devs"])

    def test_write_exit_two_is_boundary_failure_not_absent(self) -> None:
        client, _ = self._client([(2, b"", b"bad args")])
        with self.assertRaises(ProvisionError):
            client.write(["group", "create", "--name=q4-devs"])

    def test_missing_binary_is_actionable_not_a_traceback(self) -> None:
        def missing_run(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", argv[0])

        client = BzrClient(
            "/nonexistent-bzr", "http://127.0.0.1:8080/", "k3y",
            admin_email="admin@bugzilla.test", run=missing_run)
        with self.assertRaises(ProvisionError) as ctx:
            client.read(["whoami"])
        self.assertIn("/nonexistent-bzr", str(ctx.exception))
        self.assertIn("--bzr", str(ctx.exception))


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

    def test_missing_docker_is_actionable_not_a_traceback(self) -> None:
        def missing_run(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", argv[0])

        client = BridgeClient(["docker", "compose"], "bzr-live-abc",
                              run=missing_run)
        with self.assertRaises(ProvisionError) as ctx:
            client.call("get-keyword", {"name": "q4-hot"})
        self.assertIn("docker", str(ctx.exception))

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


def _flags(args):
    return {
        token.split("=", 1)[0]: token.split("=", 1)[1]
        for token in args
        if token.startswith("--") and "=" in token
    }


class _FakeBzr:
    """Dict-backed stand-in for BzrClient; mirrors read/write/whoami."""

    def __init__(self, state=None):
        self.state = dict(state or {})
        self.reads = []
        self.writes = []

    def whoami(self):
        return "admin@bugzilla.test"

    def read(self, args, positionals=None):
        pos = list(positionals or [])
        self.reads.append((tuple(args), tuple(pos)))
        head = tuple(args[:2])
        if head == ("group", "view"):
            return self.state.get(f"group:{pos[0]}")
        if head == ("user", "search"):
            return self.state.get(f"user:{pos[0]}")
        if head == ("product", "view"):
            return self.state.get(f"product:{pos[0]}")
        if head == ("component", "view"):
            return self.state.get(f"component:{pos[0]}:{pos[1]}")
        if head == ("field", "list"):
            return self.state.get(f"field:{pos[0]}")
        raise AssertionError(f"unexpected read: {args} {pos}")

    def write(self, args, positionals=None):
        self.writes.append((tuple(args), tuple(positionals or ())))
        head, flags = tuple(args[:2]), _flags(args)
        if head == ("group", "create"):
            self.state[f"group:{flags['--name']}"] = {
                "name": flags["--name"], "description": flags["--description"]}
        elif head == ("user", "create"):
            self.state[f"user:{flags['--email']}"] = [{
                "email": flags["--email"], "real_name": flags["--full-name"],
                "groups": []}]
        elif head == ("group", "add-user"):
            self.state[f"user:{flags['--user']}"][0]["groups"].append(flags["--group"])
        elif head == ("product", "create"):
            self.state[f"product:{flags['--name']}"] = {
                "name": flags["--name"], "description": flags["--description"],
                "versions": [{"name": "unspecified"}], "milestones": [{"name": "---"}]}
        elif head == ("component", "create"):
            key = f"component:{flags['--product']}:{flags['--name']}"
            self.state[key] = {
                "name": flags["--name"], "description": flags["--description"],
                "default_assignee": flags["--default-assignee"]}
        else:
            raise AssertionError(f"unexpected write: {args}")
        return {}


class _FakeBridge:
    """Dict-backed stand-in for BridgeClient; shares product state with _FakeBzr."""

    def __init__(self, bzr, state=None):
        self._bzr = bzr
        self.state = dict(state or {})
        self.calls = []
        self._minted = 0

    def call(self, operation, payload):
        self.calls.append((operation, dict(payload)))
        if operation == "create-api-key":
            login = payload.get("login") or "admin@bugzilla.test"
            self._minted += 1
            return {"login": login, "api_key": f"key-{login}-{self._minted}"}
        if operation == "get-custom-field":
            return self.state.get(f"custom-field:{payload['name']}")
        if operation == "get-keyword":
            return self.state.get(f"keyword:{payload['name']}")
        if operation == "get-flag-type":
            return self.state.get(f"flag-type:{payload['name']}")
        if operation in ("create-version", "create-milestone"):
            product = self._bzr.state[f"product:{payload['product']}"]
            kind = "versions" if operation == "create-version" else "milestones"
            product[kind].append({"name": payload["name"]})
            return {"name": payload["name"]}
        if operation == "create-custom-field":
            self.state[f"custom-field:{payload['name']}"] = {
                "name": payload["name"], "field_type": payload["field_type"],
                "values": list(payload["values"])}
            listed = [{"name": value} for value in payload["values"]]
            if payload["field_type"] == "single-select":
                listed.append({"name": "---"})
            self._bzr.state[f"field:{payload['name']}"] = listed
            return {"name": payload["name"]}
        if operation == "create-keyword":
            self.state[f"keyword:{payload['name']}"] = {
                "name": payload["name"], "description": payload["description"]}
            return {"name": payload["name"]}
        if operation == "create-flag-type":
            self.state[f"flag-type:{payload['name']}"] = {
                "name": payload["name"], "description": payload["description"],
                "target": payload["target"],
                "inclusions": [dict(pair) for pair in payload["inclusions"]]}
            return {"name": payload["name"]}
        raise AssertionError(f"unexpected bridge call: {operation}")


def _declared_bzr_state():
    return {
        "group:q4-devs": {"name": "q4-devs", "description": "Q4 developers"},
        "user:q4-ada@example.test": [{
            "email": "q4-ada@example.test", "real_name": "Ada Q4",
            "groups": ["q4-devs"]}],
        "product:q4-checkout": {
            "name": "q4-checkout", "description": "Checkout product",
            "versions": [{"name": "unspecified"}, {"name": "q4-v1"}],
            "milestones": [{"name": "---"}, {"name": "q4-m1"}]},
        "component:q4-checkout:q4-cart": {
            "name": "q4-cart", "description": "Cart component",
            "default_assignee": "Q4-ADA@example.test"},
        "component:q4-checkout:q4-docs": {
            "name": "q4-docs", "description": "Docs component",
            "default_assignee": "someone-else@example.test"},
        "field:cf_q4_risk": [{"name": "low"}, {"name": "high"}, {"name": "---"}],
        "field:cf_q4_tags": [{"name": "perf"}, {"name": "ui"}],
    }


def _declared_bridge_state():
    return {
        "custom-field:cf_q4_risk": {
            "name": "cf_q4_risk", "field_type": "single-select",
            "values": ["high", "low"]},
        "custom-field:cf_q4_notes": {
            "name": "cf_q4_notes", "field_type": "text", "values": []},
        "custom-field:cf_q4_tags": {
            "name": "cf_q4_tags", "field_type": "multi-select",
            "values": ["perf", "ui"]},
        "keyword:q4-hot": {"name": "q4-hot", "description": "Hot issue"},
        "flag-type:q4-review": {
            "name": "q4-review", "description": "Review flag", "target": "bug",
            "inclusions": [{"product": "q4-checkout", "component": None}]},
        "flag-type:q4-any": {
            "name": "q4-any", "description": "Unscoped flag", "target": "bug",
            "inclusions": [{"product": None, "component": None}]},
    }


class ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = keys.KeyStore(Path(self._tmp.name) / "state")
        self.store.store_admin_key("k3y")  # conflict-path tests need an inert bootstrap
        self.scenario = load_scenario(FIXTURE)
        self.output = []

    def _provisioner(self, bzr, bridge):
        return Provisioner(
            self.scenario, lambda key: bzr, bridge, self.store,
            out=self.output.append)

    def _scenario_from(self, resources):
        root = Path(self._tmp.name) / f"scenario-{len(self.output)}"
        root.mkdir()
        (root / "scenario.json").write_text(
            json.dumps({"format_version": 1, "name": "q4-test"}))
        (root / "resources.json").write_text(
            json.dumps({"format_version": 1, "resources": resources}))
        (root / "events.jsonl").write_text("")
        return load_scenario(root)

    def test_plan_order_is_respected(self) -> None:
        bzr = _FakeBzr()
        bridge = _FakeBridge(bzr)
        report = self._provisioner(bzr, bridge).run()
        self.assertEqual([status for status, _ in report], ["created"] * 13)
        self.assertEqual(
            [identity for _, identity in report],
            [f"{r.kind}:{r.name}" for r in self.scenario.resource_plan])
        creates = [args for args, _ in bzr.writes if args[1] == "create"]
        self.assertEqual(len(creates), 5)  # group, user, product, two components

    def test_identical_rerun_is_all_unchanged_with_no_writes(self) -> None:
        self.store.store_actor_key("q4-ada", "key-existing")
        bzr = _FakeBzr(_declared_bzr_state())
        bridge = _FakeBridge(bzr, _declared_bridge_state())
        report = self._provisioner(bzr, bridge).run()
        self.assertEqual([status for status, _ in report], ["unchanged"] * 13)
        self.assertEqual(bzr.writes, [])
        self.assertEqual(
            [op for op, _ in bridge.calls if op.startswith("create-")], [])

    def test_divergent_description_fails_before_any_mutation(self) -> None:
        state = _declared_bzr_state()
        state["product:q4-checkout"]["description"] = "Edited by someone"
        bzr = _FakeBzr(state)
        bridge = _FakeBridge(bzr, _declared_bridge_state())
        with self.assertRaises(ProvisionConflictError) as ctx:
            self._provisioner(bzr, bridge).run()
        self.assertIn("product:q4-checkout", str(ctx.exception))
        self.assertIn("description", str(ctx.exception))
        self.assertEqual(bzr.writes, [])
        self.assertEqual(
            [op for op, _ in bridge.calls if op.startswith("create-")], [])

    def test_partial_run_rerun_creates_only_missing_suffix(self) -> None:
        state = _declared_bzr_state()
        for key in list(state):
            if key.startswith(("component:", "field:")):
                del state[key]
        bzr = _FakeBzr(state)
        bridge = _FakeBridge(bzr)  # bridge-owned kinds all absent
        # versions/milestones exist in the product payload, so they stay unchanged
        report = self._provisioner(bzr, bridge).run()
        statuses = dict((identity, status) for status, identity in report)
        self.assertEqual(statuses["group:q4-devs"], "unchanged")
        self.assertEqual(statuses["version:q4-v1"], "unchanged")
        self.assertEqual(statuses["component:q4-cart"], "created")
        self.assertEqual(statuses["custom-field:q4-risk"], "created")
        self.assertEqual(statuses["flag-type:q4-any"], "created")

    def test_system_group_reconciles_without_description_compare(self) -> None:
        scenario = self._scenario_from([
            {"kind": "group", "name": "editbugs", "description": "Anything at all"}])
        self.scenario = scenario
        bzr = _FakeBzr()
        bridge = _FakeBridge(bzr)
        report = self._provisioner(bzr, bridge).run()
        self.assertEqual(report, [("unchanged", "group:editbugs")])
        self.assertEqual(bzr.writes, [])
        self.assertIn("editbugs", SYSTEM_GROUPS)

    def test_declared_placeholder_value_rejected(self) -> None:
        scenario = self._scenario_from([
            {"kind": "custom-field", "name": "q4-bad",
             "field_type": "single-select", "values": ["---", "x"]}])
        self.scenario = scenario
        bzr = _FakeBzr()
        bridge = _FakeBridge(bzr)
        with self.assertRaises(ProvisionError) as ctx:
            self._provisioner(bzr, bridge).run()
        self.assertIn("q4-bad", str(ctx.exception))
        self.assertEqual(bzr.reads, [])
        self.assertEqual(bridge.calls, [])

    def test_actor_search_superset_resolves_by_exact_login(self) -> None:
        state = _declared_bzr_state()
        state["user:q4-ada@example.test"] = [
            {"email": "dr-q4-ada@example.test", "real_name": "Wrong Person",
             "groups": []},
            {"email": "Q4-Ada@example.test", "real_name": "Ada Q4",
             "groups": ["q4-devs"]},
        ]
        self.store.store_actor_key("q4-ada", "key-existing")
        bzr = _FakeBzr(state)
        bridge = _FakeBridge(bzr, _declared_bridge_state())
        report = self._provisioner(bzr, bridge).run()
        self.assertEqual(dict((i, s) for s, i in report)["actor:q4-ada"], "unchanged")

    def test_component_without_assignee_uses_admin_and_skips_compare(self) -> None:
        bzr = _FakeBzr()
        bridge = _FakeBridge(bzr)
        self._provisioner(bzr, bridge).run()
        docs_create = next(
            _flags(args) for args, _ in bzr.writes
            if args[1] == "create" and _flags(args).get("--name") == "q4-docs")
        self.assertEqual(docs_create["--default-assignee"], "admin@bugzilla.test")
        cart_create = next(
            _flags(args) for args, _ in bzr.writes
            if args[1] == "create" and _flags(args).get("--name") == "q4-cart")
        self.assertEqual(cart_create["--default-assignee"], "q4-ada@example.test")

    def test_flag_type_inclusions_canonicalize(self) -> None:
        bzr = _FakeBzr()
        bridge = _FakeBridge(bzr)
        self._provisioner(bzr, bridge).run()
        payloads = {p["name"]: p for op, p in bridge.calls
                    if op == "create-flag-type"}
        self.assertEqual(
            payloads["q4-review"]["inclusions"],
            [{"product": "q4-checkout", "component": None}])
        self.assertEqual(
            payloads["q4-any"]["inclusions"],
            [{"product": None, "component": None}])
        # a stored inclusion set missing a declared pair is divergent
        state = _declared_bridge_state()
        state["flag-type:q4-review"]["inclusions"] = []
        bzr2 = _FakeBzr(_declared_bzr_state())
        bridge2 = _FakeBridge(bzr2, state)
        self.store.store_actor_key("q4-ada", "key-existing")
        with self.assertRaises(ProvisionConflictError):
            self._provisioner(bzr2, bridge2).run()

    def test_custom_field_readback_via_field_list(self) -> None:
        bzr = _FakeBzr()
        bridge = _FakeBridge(bzr)
        self._provisioner(bzr, bridge).run()
        field_reads = [pos for args, pos in bzr.reads if tuple(args[:2]) == ("field", "list")]
        self.assertIn(("cf_q4_risk",), field_reads)
        self.assertIn(("cf_q4_tags",), field_reads)  # multi-select reads back too
        created_types = {p["name"]: p["field_type"] for op, p in bridge.calls
                         if op == "create-custom-field"}
        self.assertEqual(created_types["cf_q4_tags"], "multi-select")
        for operation, payload in bridge.calls:
            if operation in ("get-custom-field", "create-custom-field"):
                self.assertTrue(payload["name"].startswith("cf_q4_"))
                self.assertNotIn("q4-risk", payload["name"])

    def test_actor_keys_ensured_in_pass_two_only(self) -> None:
        state = _declared_bzr_state()
        state["product:q4-checkout"]["description"] = "Edited"
        bzr = _FakeBzr(state)
        bridge = _FakeBridge(bzr, _declared_bridge_state())
        with self.assertRaises(ProvisionConflictError):
            self._provisioner(bzr, bridge).run()
        self.assertEqual(
            [op for op, _ in bridge.calls if op == "create-api-key"], [])
        bzr2 = _FakeBzr(_declared_bzr_state())
        bridge2 = _FakeBridge(bzr2, _declared_bridge_state())
        self._provisioner(bzr2, bridge2).run()
        mints = [p for op, p in bridge2.calls if op == "create-api-key"]
        self.assertEqual(mints, [{"login": "q4-ada@example.test"}])
        self.assertEqual(self.store.actor_key("q4-ada"), "key-q4-ada@example.test-1")

    def test_multi_step_partial_conflict_hints_reset(self) -> None:
        state = _declared_bzr_state()
        state["user:q4-ada@example.test"][0]["groups"] = []
        bzr = _FakeBzr(state)
        bridge = _FakeBridge(bzr, _declared_bridge_state())
        with self.assertRaises(ProvisionConflictError) as ctx:
            self._provisioner(bzr, bridge).run()
        self.assertIn("make reset", str(ctx.exception))

    def test_report_and_output_never_carry_keys(self) -> None:
        bzr = _FakeBzr()
        bridge = _FakeBridge(bzr)
        self._provisioner(bzr, bridge).run()
        joined = "\n".join(self.output)
        self.assertNotIn("key-", joined)
        self.assertNotIn("k3y", joined)
        self.assertIn("created product:q4-checkout", joined)
        self.assertIn("summary: 13 created, 0 unchanged", joined)

    def test_custom_field_name_mapping(self) -> None:
        self.assertEqual(custom_field_name("risk-level"), "cf_risk_level")


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_cli_reports_conflict_as_exit_one(self) -> None:
        import contextlib

        from bzr_live.provision import __main__ as cli

        original = Provisioner.run
        Provisioner.run = lambda self: (_ for _ in ()).throw(
            ProvisionError("boom message"))
        self.addCleanup(lambda: setattr(Provisioner, "run", original))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.main([
                str(FIXTURE), "--state-root", str(self.tmp / "state"),
                "--project-root", str(self.tmp)])
        self.assertEqual(code, 1)
        self.assertIn("provision failed: boom message", stderr.getvalue())

    def test_cli_project_root_is_resolved(self) -> None:
        from bzr_live.provision import __main__ as cli

        real = self.tmp / "checkout"
        real.mkdir()
        link = self.tmp / "link"
        link.symlink_to(real)
        prefix, project = cli._compose_prefix(str(link))
        resolved = os.path.realpath(str(real))
        self.assertEqual(project, adapters.compose_project_name(resolved))
        self.assertIn("--project-directory", prefix)
        self.assertEqual(prefix[prefix.index("--project-directory") + 1], resolved)
        self.assertEqual(prefix[-4:], ["--user", "www-data", "bugzilla",
                                       "bzr-live-bridge"])

    def test_cli_defaults(self) -> None:
        from bzr_live.provision import __main__ as cli

        options = cli._parse([str(FIXTURE)])
        self.assertEqual(options.state_root, "./state")
        self.assertEqual(options.base_url, "http://127.0.0.1:8080/")
        self.assertEqual(options.bzr, "bzr")
        self.assertEqual(options.project_root, ".")

    def test_cli_admin_email_resolution(self) -> None:
        from bzr_live.provision import __main__ as cli

        root = self.tmp / "checkout"
        root.mkdir()
        # no .env, no environment -> compose default
        self.assertEqual(
            cli._admin_email(str(root), env={}), "admin@bugzilla.test")
        # the lifecycle-generated .env wins over the default
        (root / ".env").write_text("BZ_PORT=8080\nBZ_ADMIN_EMAIL=ops@example.test\n")
        self.assertEqual(
            cli._admin_email(str(root), env={}), "ops@example.test")
        # an exported environment value outranks the .env
        self.assertEqual(
            cli._admin_email(str(root), env={"BZ_ADMIN_EMAIL": "env@example.test"}),
            "env@example.test")


if __name__ == "__main__":
    unittest.main()
