# Implementation plan: Bugzilla resource provisioning

Goal: provision every `ValidatedScenario.resource_plan` entry into the local Bugzilla
fixture in plan order with stable-name reconciliation, clear divergence failures, and
owner-readable actor API-key files, per
[the design spec](../specs/2026-08-31-bugzilla-resource-provisioning-design.md) and
[ADR 0004](../../adr/0004-bugzilla-resource-provisioning.md).

Architecture: a new stdlib-only package `bzr_live.provision` routes nine resource
kinds to three fixed boundaries (bzr subprocess, container-local Perl bridge over
`docker compose exec`, one narrow REST call), runs a read-compare pass then a
create-and-readback pass, and stores keys under a 0700 state root. Tech stack:
Python 3.11 stdlib, `unittest`, Bash + shellcheck for the smoke, Perl (container-side
Bugzilla object layer) for the bridge.

## Global constraints

- Python `>=3.11` (pyproject `requires-python`); **zero runtime dependencies** — the
  `dependencies = []` list in pyproject.toml must stay empty (ADR 0002 consequence).
- Tests are stdlib `unittest`, discovered by `uv run --python 3.11 python -m unittest
  discover -s tests -v` (the scenario-contract CI command). Test files must be
  importable without Docker, network, or a bzr binary.
- Guardrails that must stay green: `make test`, `make check` (adds `bash -n` +
  `shellcheck` for shell files listed in the Makefile, `compileall` over `src`,
  compose config), CI's `uv build` + installed-wheel smoke.
- Shell scripts pass `shellcheck` and `bash -n`; new shell files must be added to the
  `check` target's file lists.
- The bridge uses only the Bugzilla Perl object layer at the pinned source
  (`BZ_SOURCE_SHA=644c66f45ce0b1b2746a31a061fbd96886278225` in
  `containers/bugzilla/Dockerfile`); **never raw SQL** (issue #4 boundary).
- The bzr executable is caller-supplied (`--bzr PATH`); nothing may read or write bzr
  config files — every invocation is stateless via `--server-url` and
  `--server-api-key-env`. The validation binary is the authorized candidate
  `bzr 0.8.3-dev (c1efef6a)`.
- API keys never appear in argv, stdout, stderr, exception text, or logs.
- Line length ≤ 100 characters; no new lint suppressions.

## File map

| File | Responsibility |
|---|---|
| `src/bzr_live/provision/__init__.py` | public exports |
| `src/bzr_live/provision/adapters.py` | boundary routing table, `BzrClient`, `BridgeClient`, `assign_bug_custom_fields`, `compose_project_name` |
| `src/bzr_live/provision/keys.py` | `KeyStore` (state root, admin + actor key files) |
| `src/bzr_live/provision/executor.py` | `Provisioner` two-pass reconciliation, comparison semantics, report |
| `src/bzr_live/provision/__main__.py` | CLI |
| `containers/bugzilla/bridge.pl` | fixed-operation Perl bridge |
| `containers/bugzilla/Dockerfile` | one COPY line installing the bridge |
| `tests/fixtures/provision-scenario/` | all-kinds scenario fixture |
| `tests/test_provision.py` | focused unit tests (fake boundaries) |
| `tests/provision_smoke.sh` | operator-run live two-run proof |
| `Makefile` | `check` file lists gain `tests/provision_smoke.sh` |
| `README.md`, `.gitignore` | state-root documentation; ignore `/state/` |

Interfaces consumed from the existing codebase (verified present):
`bzr_live.scenario.load_scenario(path) -> ValidatedScenario` with
`.resource_plan: tuple[PlannedResource, ...]`, `.resources`, and
`PlannedResource(kind, name, data, dependencies)` where `data` is a frozen mapping
(`Reference(kind, name)` values for refs) — `src/bzr_live/scenario/model.py:37-66`.

## Task 1 — fixture scenario, routing table, project name, key store

Creates: `tests/fixtures/provision-scenario/scenario.json`,
`tests/fixtures/provision-scenario/resources.json`,
`tests/fixtures/provision-scenario/events.jsonl`,
`src/bzr_live/provision/__init__.py`, `src/bzr_live/provision/adapters.py` (partial:
errors, routing, project name), `src/bzr_live/provision/keys.py`,
`tests/test_provision.py` (initial cases).

Interfaces provided to later tasks:
- `adapters.ProvisionError(Exception)`; `adapters.BOUNDARIES: dict[str, str]`;
  `adapters.BUG_CUSTOM_FIELD_BOUNDARY = "bugzilla-rest-custom-field"`;
  `adapters.compose_project_name(root: str) -> str`.
- `keys.KeyStore(state_root: str | Path)` with `.admin_key() -> str | None`,
  `.store_admin_key(key: str) -> None`, `.actor_key(name: str) -> str | None`,
  `.store_actor_key(name: str, key: str) -> None`.

Steps:

1. Write the fixture scenario. `tests/fixtures/provision-scenario/scenario.json`:

```json
{"format_version": 1, "name": "q4-provision", "description": "Provisioning fixture"}
```

`tests/fixtures/provision-scenario/events.jsonl`: an empty file (zero bytes).

`tests/fixtures/provision-scenario/resources.json` — one of every kind, one
component without `default_assignee`, one flag type with no products/components:

```json
{"format_version": 1, "resources": [
  {"kind": "group", "name": "q4-devs", "description": "Q4 developers"},
  {"kind": "actor", "name": "q4-ada", "email": "q4-ada@example.test",
   "display_name": "Ada Q4", "groups": [{"ref": "group:q4-devs"}]},
  {"kind": "product", "name": "q4-checkout", "description": "Checkout product"},
  {"kind": "component", "name": "q4-cart", "product": {"ref": "product:q4-checkout"},
   "description": "Cart component", "default_assignee": {"ref": "actor:q4-ada"}},
  {"kind": "component", "name": "q4-docs", "product": {"ref": "product:q4-checkout"},
   "description": "Docs component"},
  {"kind": "version", "name": "q4-v1", "product": {"ref": "product:q4-checkout"}},
  {"kind": "milestone", "name": "q4-m1", "product": {"ref": "product:q4-checkout"}},
  {"kind": "custom-field", "name": "q4-risk", "field_type": "single-select",
   "values": ["low", "high"]},
  {"kind": "custom-field", "name": "q4-notes", "field_type": "text"},
  {"kind": "keyword", "name": "q4-hot", "description": "Hot issue"},
  {"kind": "flag-type", "name": "q4-review", "description": "Review flag",
   "target": "bug", "products": [{"ref": "product:q4-checkout"}]},
  {"kind": "flag-type", "name": "q4-any", "description": "Unscoped flag",
   "target": "bug"}
]}
```

2. Write the failing test file skeleton `tests/test_provision.py` with the first
   cases (they must fail with `ModuleNotFoundError` before step 3 exists — run
   `uv run --python 3.11 python -m unittest tests.test_provision -v` and confirm the
   import error):

```python
from __future__ import annotations

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
            "bzr-live-" + __import__("hashlib").sha256(b"/tmp/example").hexdigest()[:12],
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
```

3. Implement. `src/bzr_live/provision/__init__.py` — Task 1 writes exactly this
   (later tasks extend it):

```python
"""Host-side Bugzilla fixture provisioning (issue #4, ADR 0004)."""

from .adapters import (
    BOUNDARIES,
    BUG_CUSTOM_FIELD_BOUNDARY,
    ProvisionError,
    compose_project_name,
)
from .keys import KeyStore

__all__ = [
    "BOUNDARIES",
    "BUG_CUSTOM_FIELD_BOUNDARY",
    "KeyStore",
    "ProvisionError",
    "compose_project_name",
]
```

Task 2 adds `BridgeClient`, `BzrClient`, and `assign_bug_custom_fields` to the
`.adapters` import and to `__all__`; Task 3 adds
`from .executor import ProvisionConflictError, Provisioner` and those two names to
`__all__` (keep `__all__` sorted).

`src/bzr_live/provision/adapters.py` (Task 1 portion):

```python
from __future__ import annotations

import hashlib


class ProvisionError(Exception):
    """Actionable provisioning failure; str(exc) is the operator-facing message."""


# Resource kind -> mutation boundary. Fixed by issue #4's implementation boundaries.
BOUNDARIES: dict[str, str] = {
    "group": "bzr",
    "actor": "bzr",
    "product": "bzr",
    "component": "bzr",
    "version": "bridge",
    "milestone": "bridge",
    "custom-field": "bridge",
    "keyword": "bridge",
    "flag-type": "bridge",
}

# Per-bug custom-field assignment is the sole stock-REST operation (journal boundary
# vocabulary, ADR 0002).
BUG_CUSTOM_FIELD_BOUNDARY = "bugzilla-rest-custom-field"


def compose_project_name(root: str) -> str:
    """Derive the compose project name exactly as scripts/lifecycle does.

    `root` must already be the physical path (os.path.realpath), matching the
    script's `pwd -P`.
    """
    return "bzr-live-" + hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]
```

`src/bzr_live/provision/keys.py`:

```python
from __future__ import annotations

import os
import stat
from pathlib import Path

from .adapters import ProvisionError

_ACTOR_DIR = "actor-keys"
_ADMIN_FILE = "admin.key"


def _require_private_dir(path: Path) -> None:
    details = os.lstat(path)
    if not stat.S_ISDIR(details.st_mode):
        raise ProvisionError(f"{path} is not a directory")
    if stat.S_IMODE(details.st_mode) != 0o700:
        raise ProvisionError(f"{path} must have mode 0700; run: chmod 700 {path}")
    if details.st_uid != os.getuid():
        raise ProvisionError(f"{path} must be owned by the current user")


def _ensure_private_dir(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    except FileExistsError:
        pass
    _require_private_dir(path)


class KeyStore:
    """Owner-only key files: <root>/admin.key and <root>/actor-keys/<name>.key.

    The admin key lives outside actor-keys/ because "admin" is a legal actor slug
    (spec: Actor API keys). Ordinary local fixture hygiene only — 0700/0600 modes,
    no symlinks, owner check (issue #4 boundary).
    """

    def __init__(self, state_root: str | Path) -> None:
        self._root = Path(state_root)
        self._root.parent.mkdir(parents=True, exist_ok=True)
        _ensure_private_dir(self._root)
        _ensure_private_dir(self._root / _ACTOR_DIR)

    def _read(self, path: Path) -> str | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProvisionError(f"cannot open key file {path}: {exc.strerror}") from None
        try:
            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode):
                raise ProvisionError(f"key file {path} is not a regular file")
            if stat.S_IMODE(details.st_mode) != 0o600:
                raise ProvisionError(f"key file {path} must have mode 0600")
            if details.st_uid != os.getuid():
                raise ProvisionError(f"key file {path} must be owned by the current user")
            content = os.read(fd, 4096).decode("utf-8").strip()
        finally:
            os.close(fd)
        if not content:
            raise ProvisionError(f"key file {path} is empty; remove it and rerun")
        return content

    def _write(self, path: Path, key: str) -> None:
        tmp = path.parent / f".tmp-{path.name}"
        tmp.unlink(missing_ok=True)  # a stale tmp from a killed run must not block us
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, (key + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.rename(tmp, path)

    def admin_key(self) -> str | None:
        return self._read(self._root / _ADMIN_FILE)

    def store_admin_key(self, key: str) -> None:
        self._write(self._root / _ADMIN_FILE, key)

    def admin_key_path(self) -> Path:
        return self._root / _ADMIN_FILE

    def actor_key(self, name: str) -> str | None:
        return self._read(self._root / _ACTOR_DIR / f"{name}.key")

    def store_actor_key(self, name: str, key: str) -> None:
        self._write(self._root / _ACTOR_DIR / f"{name}.key", key)
```

4. Run `uv run --python 3.11 python -m unittest tests.test_provision -v` — expect all
   Task 1 tests to pass (`OK`).
5. Run `uv run --python 3.11 python -m unittest discover -s tests -v` — expect the
   whole suite green (the fixture scenario must load cleanly if any existing test
   sweeps fixtures).
6. Commit: `feat(provision): add routing table, key store, and fixture scenario`.

Acceptance: routing table matches the spec's boundary table verbatim; key modes are
0700/0600 with owner checks; the fixture declares all nine kinds plus both edge
resources.

## Task 2 — boundary clients (bzr, bridge, REST)

Modifies: `src/bzr_live/provision/adapters.py`, `tests/test_provision.py`.

Interfaces provided:
- `BzrClient(bzr_path: str, base_url: str, api_key: str, run=subprocess.run)` with
  `.read(args: list[str], positionals: list[str] | None = None) -> object | None`
  (None = absent per the not-found contract),
  `.write(args: list[str], positionals: list[str] | None = None) -> object`, and
  `.whoami() -> str` (returns the admin login).
- `BridgeClient(argv_prefix: list[str], project: str, run=subprocess.run)` with
  `.call(operation: str, payload: dict) -> object`; allowlist
  `BridgeClient.OPERATIONS`.
- `assign_bug_custom_fields(base_url, api_key, bug_id, values, opener=urllib.request.urlopen)`.

Steps:

1. Add failing tests to `tests/test_provision.py`:

```python
import io
import json
import subprocess
import urllib.error
import urllib.request

from bzr_live.provision.adapters import (
    BridgeClient,
    BzrClient,
    ProvisionError,
    assign_bug_custom_fields,
)


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
```

2. Run `uv run --python 3.11 python -m unittest tests.test_provision -v` — the new
   tests must fail (`AttributeError`/`ImportError` for the missing names).
3. Append to `src/bzr_live/provision/adapters.py`:

```python
import json
import os
import subprocess
import urllib.error
import urllib.request

_KEY_ENV = "BZR_LIVE_API_KEY"

# docker compose's messages when the exec target does not exist. Matching them maps
# a wrong --project-root to an actionable message instead of a generic failure.
_NO_CONTAINER_MARKERS = ("is not running", "no such service", "no container found")


def _payload(raw: bytes, context: str) -> object:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProvisionError(f"{context}: reply was not JSON") from None
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value


class BzrClient:
    """Stateless bzr subprocess seam. The key rides the environment, never argv."""

    def __init__(self, bzr_path: str, base_url: str, api_key: str,
                 run=subprocess.run) -> None:
        self._bzr = bzr_path
        self._base_url = base_url
        self._api_key = api_key
        self._run = run

    def _invoke(self, args: list[str], positionals: list[str] | None):
        argv = [self._bzr, "--json", "--server-url", self._base_url,
                "--server-api-key-env", _KEY_ENV, *args]
        if positionals:
            argv += ["--", *positionals]
        env = dict(os.environ)
        env[_KEY_ENV] = self._api_key
        return argv, self._run(argv, capture_output=True, env=env, shell=False)

    def read(self, args: list[str], positionals: list[str] | None = None):
        """A read exiting 2 is absent (not-found contract); other failures raise."""
        argv, done = self._invoke(args, positionals)
        if done.returncode == 0:
            return _payload(done.stdout, f"bzr {' '.join(args)}")
        if done.returncode == 2:
            return None
        raise ProvisionError(
            f"bzr boundary failure (exit {done.returncode}) running "
            f"{' '.join(args)}: {done.stderr.decode('utf-8', 'replace').strip()}")

    def write(self, args: list[str], positionals: list[str] | None = None):
        argv, done = self._invoke(args, positionals)
        if done.returncode != 0:
            raise ProvisionError(
                f"bzr boundary failure (exit {done.returncode}) running "
                f"{' '.join(args)}: {done.stderr.decode('utf-8', 'replace').strip()}")
        if done.stdout.strip():
            return _payload(done.stdout, f"bzr {' '.join(args)}")
        return {}

    def whoami(self) -> str:
        payload = self.read(["whoami"])
        if payload is None:
            raise ProvisionError("bzr whoami reported not-found; fixture unreachable")
        for key in ("name", "login", "email"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, str) and value:
                return value
        raise ProvisionError("bzr whoami reply carried no login")


class BridgeClient:
    """Fixed-operation container-local Perl bridge over docker compose exec."""

    OPERATIONS = frozenset({
        "create-version", "create-milestone", "create-custom-field",
        "create-keyword", "create-flag-type", "create-api-key",
        "get-custom-field", "get-keyword", "get-flag-type",
    })

    def __init__(self, argv_prefix: list[str], project: str,
                 run=subprocess.run) -> None:
        self._prefix = list(argv_prefix)
        self._project = project
        self._run = run

    def call(self, operation: str, payload: dict) -> object:
        if operation not in self.OPERATIONS:
            raise ProvisionError(f"bridge operation {operation!r} is not allowlisted")
        done = self._run(
            [*self._prefix, operation],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True, shell=False,
        )
        stderr = done.stderr.decode("utf-8", "replace")
        if any(marker in stderr.lower() for marker in _NO_CONTAINER_MARKERS):
            raise ProvisionError(
                f"bridge cannot reach the fixture: compose project {self._project} has "
                "no running bugzilla container. Pass --project-root <checkout root> "
                "(or run from the checkout) so the derived project matches make up.")
        secret = operation == "create-api-key"
        if done.returncode not in (0, 1):
            detail = "" if secret else f": {stderr.strip()}"
            raise ProvisionError(
                f"bridge boundary failure (exit {done.returncode}) in {operation}{detail}")
        try:
            reply = json.loads(done.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = "" if secret else f": {stderr.strip()}"
            raise ProvisionError(f"bridge reply for {operation} was not JSON{detail}") from None
        if not isinstance(reply, dict) or "ok" not in reply:
            raise ProvisionError(f"bridge reply for {operation} was malformed")
        if not reply["ok"]:
            if secret:
                # never echo any part of a create-api-key reply (spec: failure modes)
                raise ProvisionError(
                    "bridge create-api-key failed; check the fixture and rerun")
            raise ProvisionError(f"bridge {operation} failed: {reply.get('error', 'unknown')}")
        return reply.get("result")


def assign_bug_custom_fields(base_url: str, api_key: str, bug_id: int,
                             values: dict, opener=urllib.request.urlopen) -> object:
    """The sole stock-REST operation: per-bug custom-field assignment.

    The key rides the JSON body (`api_key` param) — the pinned Bugzilla has no auth
    header, and a query-string key would land in the container's access log.
    """
    if not isinstance(bug_id, int) or isinstance(bug_id, bool):
        raise ProvisionError("bug id must be an integer")
    body = dict(values)
    body["api_key"] = api_key
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/rest/bug/{bug_id}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with opener(request) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # Bugzilla returns error bodies with 4xx/5xx; read the body, never the key.
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise ProvisionError(
            f"REST custom-field assignment failed (HTTP {exc.code}): {detail}") from None
    except urllib.error.URLError as exc:
        raise ProvisionError(
            f"REST custom-field assignment failed: {exc.reason}") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProvisionError("REST custom-field reply was not JSON") from None
    if isinstance(payload, dict) and payload.get("error"):
        raise ProvisionError(
            f"REST custom-field assignment failed: {payload.get('message', 'unknown')}")
    return payload
```

4. Run `uv run --python 3.11 python -m unittest tests.test_provision -v` — expect
   `OK`.
5. Commit: `feat(provision): add bzr, bridge, and REST boundary clients`.

Acceptance: key never in argv or exception text; exit-2 absent only on reads; bridge
allowlist closed; container-not-found mapped to the `--project-root` hint; REST body
carries `api_key`.

## Task 3 — the two-pass executor

Creates: `src/bzr_live/provision/executor.py`. Modifies: `tests/test_provision.py`,
`src/bzr_live/provision/__init__.py` (add `Provisioner`, `ProvisionConflictError`).

Interfaces provided:
- `executor.Provisioner(scenario, bzr_factory, bridge, keys, out=print)` where
  `bzr_factory: Callable[[str], BzrClient]` maps the resolved admin key to a
  client (the executor acquires the key before any bzr call; tests pass
  `lambda key: fake_bzr`), with `.run() -> list[tuple[str, str]]` (ordered
  `(status, "kind:name")`, status in `{"created", "unchanged"}`).
- `executor.ProvisionConflictError(ProvisionError)` carrying
  `.identity`, `.field`, `.declared`, `.observed`.
- `executor.SYSTEM_GROUPS: frozenset[str]`.
- `executor.custom_field_name(slug: str) -> str` (`cf_` + `-`→`_`).

Behavior contract (from the spec, normative for this task):
- Pre-flight: reject a declared custom-field value `---` (actionable message naming
  the field). Acquire the admin key (reuse `keys.admin_key()`, else bridge
  `create-api-key {"login": None}` and store). Verify with `whoami()`; on
  `ProvisionError` from whoami, re-raise naming `keys.admin_key_path()` and the
  recovery choice (remove the state root or `make reset`). Record the returned admin
  login for the component fallback.
- Pass 1 classifies every plan entry `absent`/`unchanged`/`divergent` without any
  mutation; the first divergence raises `ProvisionConflictError` before pass 2.
  Product views are cached in pass 1 only.
- Pass 2 creates absent entries in plan order and reads each creation back with
  fresh reads: the product-view cache exists only during pass 1, and no pass-2
  readback ever consults it. The bzr `field list` readback applies to select
  custom fields; a text field's pass-2 readback is the bridge `get-custom-field`
  (its bzr observability is asserted by the smoke). Pass 2 also ensures an actor
  key file for every actor entry (created or unchanged).
- Comparison semantics per kind exactly as the spec's table (declared fields only;
  case-insensitive emails; system-group carve-out reported `unchanged`; custom-field
  values as sets ignoring `---`; flag-type canonical inclusion pairs; version and
  milestone are existence-only).
- Every bridge custom-field payload (`create-custom-field`, `get-custom-field`)
  carries `custom_field_name(slug)` — `cf_q4_risk`, never the raw slug `q4-risk`:
  Bugzilla rejects hyphens in field names and stores the `cf_` form, so a raw-slug
  probe would misclassify every provisioned field as absent on rerun.
- Readback verification failures and mid-create divergence raise `ProvisionError`
  naming resource and field; the conflict message for a divergent *multi-step*
  partial (actor groups, select values, flag inclusions) appends the
  `make reset` recovery hint.

Fake-based tests to add (all use a `_FakeBzr`/`_FakeBridge` pair implementing the
client interfaces with scripted per-identity state and a call log; the fakes live in
`tests/test_provision.py`):

```python
from bzr_live.provision.executor import (
    ProvisionConflictError,
    Provisioner,
    SYSTEM_GROUPS,
    custom_field_name,
)
from bzr_live.scenario import load_scenario

FIXTURE = Path(__file__).parent / "fixtures" / "provision-scenario"


class _FakeBzr:
    """Dict-backed stand-in for BzrClient; mirrors read/write/whoami."""

    def __init__(self, state):
        self.state = state          # e.g. {"group:q4-devs": {...}, "user:q4-ada@..": {...}}
        self.writes = []
        self.reads = []

    def whoami(self):
        return "admin@bugzilla.test"

    def read(self, args, positionals=None):
        self.reads.append((tuple(args), tuple(positionals or ())))
        key = self._key(args, positionals)
        return self.state.get(key)

    def write(self, args, positionals=None):
        self.writes.append((tuple(args), tuple(positionals or ())))
        self._apply(args, positionals)
        return {}

    # _key/_apply translate the executor's documented invocations into dict state;
    # implement exactly the invocations the executor emits (group view/create,
    # user search/create, group add-user, product view/create, component
    # view/create, field list).
```

Test cases (names are normative; write each, watch it fail, implement, watch it
pass):

- `test_plan_order_is_respected` — fresh empty fake state; run; assert the combined
  bzr+bridge write log creates in exactly `scenario.resource_plan` order and the
  report is all-`created`.
- `test_identical_rerun_is_all_unchanged_with_no_writes` — pre-populate fakes with
  the exact declared state; run; report all-`unchanged`; write logs empty; no
  actor-key mint when the key file already exists.
- `test_divergent_description_fails_before_any_mutation` — pre-populate with one
  changed product description; run raises `ProvisionConflictError` naming
  `product:q4-checkout`, field `description`, both values; assert zero writes.
- `test_partial_run_rerun_creates_only_missing_suffix` — pre-populate the first
  half of the plan; run; assert only the missing entries are created.
- `test_system_group_reconciles_without_description_compare` — declare a group named
  `editbugs` (build a scenario in-test via a temp dir); fake state has it with
  Bugzilla's own description; run classifies it `unchanged`, never creates.
- `test_declared_placeholder_value_rejected` — in-test scenario with a custom field
  value `---`; run raises `ProvisionError` naming the field before any boundary
  call.
- `test_actor_search_superset_resolves_by_exact_login` — fake search returns the
  declared actor plus a `dr-q4-ada@example.test` noise row; classification is
  `unchanged`, not divergent.
- `test_component_without_assignee_uses_admin_and_skips_compare` — `q4-docs` is
  created with the whoami login as `--default-assignee` and its readback ignores the
  server-side assignee.
- `test_flag_type_inclusions_canonicalize` — `q4-any` maps to `[(None, None)]`;
  `q4-review` to `[("q4-checkout", None)]`; stored pairs in a different order
  compare equal; a missing pair is divergent.
- `test_custom_field_readback_via_field_list` — after create, the executor calls
  `field list` with the mapped `cf_q4_risk` name and compares value sets ignoring
  `---`; the fake bridge also asserts both custom-field payloads carried
  `{"name": "cf_q4_risk"}`, never the raw slug.
- `test_actor_keys_ensured_in_pass_two_only` — conflict run mints no keys; clean
  run mints exactly the missing actor keys via `create-api-key` with the actor
  email.
- `test_multi_step_partial_conflict_hints_reset` — fake state has the actor without
  its declared group; the raised conflict message contains `make reset`.
- `test_report_and_output_never_carry_keys` — capture `out=`; assert no fake key
  material in any line.
- `test_custom_field_name_mapping` — `custom_field_name("risk-level") ==
  "cf_risk_level"`.

Implementation sketch for `executor.py` (complete the bodies to satisfy the tests;
every comparison lives in one `_compare_<kind>` function; keep functions ≤ 100
lines):

```python
from __future__ import annotations

from typing import Callable

from .adapters import BOUNDARIES, ProvisionError

SYSTEM_GROUPS = frozenset({
    "admin", "tweakparams", "editusers", "creategroups", "editclassifications",
    "editcomponents", "editkeywords", "editbugs", "canconfirm",
})

_MULTI_STEP_HINT = (
    " If an earlier run was interrupted mid-create, the fixture holds a partial "
    "resource; recover with a fixture reset (CONFIRM_RESET=1 make reset)."
)


class ProvisionConflictError(ProvisionError):
    def __init__(self, identity, field, declared, observed, *, multi_step=False):
        self.identity, self.field = identity, field
        self.declared, self.observed = declared, observed
        hint = _MULTI_STEP_HINT if multi_step else ""
        super().__init__(
            f"{identity} differs in declared field {field!r}: declared "
            f"{declared!r}, observed {observed!r}.{hint}")


def custom_field_name(slug: str) -> str:
    return "cf_" + slug.replace("-", "_")


class Provisioner:
    def __init__(self, scenario, bzr_factory, bridge, keys,
                 out: Callable[[str], None] = print):
        # bzr_factory: Callable[[str], BzrClient] — called once with the admin key
        # after _ensure_admin resolves it; self._bzr is set there.
        ...

    def run(self) -> list[tuple[str, str]]:
        self._reject_reserved_values()
        admin_login = self._ensure_admin()
        states = {}
        cache = {}                      # pass-1 product views only
        for resource in self._scenario.resource_plan:
            states[self._identity(resource)] = self._classify(resource, cache)
        report = []
        for resource in self._scenario.resource_plan:
            identity = self._identity(resource)
            if states[identity] == "absent":
                self._create(resource, admin_login)
                self._verify_readback(resource, admin_login)
                report.append(("created", identity))
            else:
                report.append(("unchanged", identity))
            if resource.kind == "actor":
                self._ensure_actor_key(resource)
        for status, identity in report:
            self._out(f"{status} {identity}")
        created = sum(1 for status, _ in report if status == "created")
        self._out(f"summary: {created} created, {len(report) - created} unchanged")
        return report
```

Steps: write the tests (red), implement `executor.py` (green), extend
`__init__.py` exports, run
`uv run --python 3.11 python -m unittest tests.test_provision -v` then the full
discover command, commit
`feat(provision): add two-pass reconciliation executor`.

Acceptance: the six spec failure modes behave as specified; no mutation precedes the
conflict gate except admin-key bootstrap; report lines match
`created|unchanged kind:name`.

## Task 4 — CLI

Creates: `src/bzr_live/provision/__main__.py`. Modifies: `tests/test_provision.py`.

Interfaces: `python -m bzr_live.provision SCENARIO_DIR [--state-root PATH]
[--base-url URL] [--bzr PATH] [--project-root PATH]`; exit 0 success, 1 conflict or
failure (single-line actionable stderr message), 2 usage.

```python
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..scenario import ScenarioValidationError, load_scenario
from .adapters import BridgeClient, BzrClient, ProvisionError, compose_project_name
from .executor import Provisioner
from .keys import KeyStore


def _compose_prefix(project_root: str) -> tuple[list[str], str]:
    root = os.path.realpath(project_root)
    project = compose_project_name(root)
    prefix = [
        "docker", "compose", "--project-name", project,
        "--project-directory", root, "--file", str(Path(root) / "compose.yaml"),
        "exec", "-T", "--user", "www-data", "bugzilla", "bzr-live-bridge",
    ]
    return prefix, project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bzr_live.provision",
        description="Provision scenario resources into the local Bugzilla fixture.")
    parser.add_argument("scenario_dir")
    parser.add_argument("--state-root", default="./state")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/")
    parser.add_argument("--bzr", default="bzr")
    parser.add_argument("--project-root", default=".")
    options = parser.parse_args(argv)
    try:
        scenario = load_scenario(options.scenario_dir)
        store = KeyStore(options.state_root)
        prefix, project = _compose_prefix(options.project_root)
        bridge = BridgeClient(prefix, project)
        provisioner = Provisioner(
            scenario,
            lambda key: BzrClient(options.bzr, options.base_url, key),
            bridge,
            store,
        )
        provisioner.run()
    except (ProvisionError, ScenarioValidationError) as exc:
        print(f"provision failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Wiring note: `BzrClient` needs the admin key, which the executor acquires via the
bridge — so `Provisioner` takes the factory (`lambda key: BzrClient(...)`) rather
than a client instance, exactly as Task 3 defines it.

Tests: `test_cli_reports_conflict_as_exit_one` (monkeypatch `Provisioner.run` to
raise; assert exit 1 and message on stderr), `test_cli_project_root_is_resolved`
(symlinked temp dir → prefix uses realpath), `test_cli_defaults` (parse only).

Verification: unit run + `uv run --python 3.11 python -m bzr_live.provision --help`
prints usage, exit 0. Commit: `feat(provision): add provisioning CLI`.

## Task 5 — the Perl bridge and container integration

Creates: `containers/bugzilla/bridge.pl`. Modifies: `containers/bugzilla/Dockerfile`
(one COPY line), `tests/test_provision.py` (protocol-shape tests only — the bridge
itself is proven by the live smoke, per the operator's recorded decision).

Dockerfile change — add beside the existing entrypoint COPY:

```dockerfile
COPY --chmod=0755 containers/bugzilla/bridge.pl /usr/local/bin/bzr-live-bridge
```

`containers/bugzilla/bridge.pl` — complete file:

```perl
#!/usr/bin/perl
# bzr-live fixed-operation provisioning bridge (issue #4, ADR 0004).
# One allowlisted operation per invocation: argv[0] = operation, stdin = one JSON
# request, stdout = one JSON reply {"ok": true, "result": ...} or
# {"ok": false, "error": "..."}. Bugzilla object layer only — never raw SQL.
use strict;
use warnings;
use lib qw(/var/www/html /var/www/html/lib);

use JSON::XS qw(decode_json encode_json);

BEGIN { chdir '/var/www/html' or die "cannot chdir to bugzilla root: $!\n"; }

use Bugzilla;
use Bugzilla::Constants;
use Bugzilla::Component;
use Bugzilla::Field;
use Bugzilla::Field::Choice;
use Bugzilla::FlagType;
use Bugzilla::Keyword;
use Bugzilla::Milestone;
use Bugzilla::Product;
use Bugzilla::User;
use Bugzilla::User::APIKey;
use Bugzilla::Version;

my %FIELD_TYPES = (
  'text'          => FIELD_TYPE_FREETEXT,
  'single-select' => FIELD_TYPE_SINGLE_SELECT,
  'multi-select'  => FIELD_TYPE_MULTI_SELECT,
);
my %FIELD_TYPE_NAMES = reverse %FIELD_TYPES;

sub reply_ok    { print encode_json({ok => JSON::XS::true, result => $_[0]}); exit 0 }
sub reply_error { print encode_json({ok => JSON::XS::false, error => "$_[0]"}); exit 1 }

my %OPERATIONS = map { $_ => 1 } qw(
  create-version create-milestone create-custom-field create-keyword
  create-flag-type create-api-key get-custom-field get-keyword get-flag-type
);

my $operation = $ARGV[0] // '';
unless ($OPERATIONS{$operation}) {
  print STDERR "unknown operation\n";
  exit 2;
}

my $request = eval {
  local $/;
  my $raw = <STDIN> // '';
  length $raw ? decode_json($raw) : {};
};
reply_error("request was not valid JSON") if $@;

my $result = eval {
  Bugzilla->usage_mode(USAGE_MODE_CMDLINE);
  my $admin = Bugzilla::User->check({name => $ENV{BZ_ADMIN_EMAIL}});
  Bugzilla->set_user($admin);
  dispatch($operation, $request, $admin);
};
reply_error($@) if $@;
reply_ok($result);

sub product_of { Bugzilla::Product->check({name => $_[0]}) }

sub inclusion_pairs {
  # canonical [{product, component}] with nulls meaning any -> "pid:cid" strings
  my ($pairs) = @_;
  my @clusions;
  for my $pair (@{$pairs // []}) {
    my ($pid, $cid) = ('', '');
    if (defined $pair->{product}) {
      my $product = product_of($pair->{product});
      $pid = $product->id;
      if (defined $pair->{component}) {
        my $component = Bugzilla::Component->check(
          {product => $product, name => $pair->{component}});
        $cid = $component->id;
      }
    }
    push @clusions, "$pid:$cid";
  }
  @clusions = (':') unless @clusions;
  return \@clusions;
}

sub stored_inclusions {
  # FlagType->inclusions: {"Product:Component" => "pid:cid"}; __Any__ -> null
  my ($flagtype) = @_;
  my @pairs;
  for my $name (keys %{$flagtype->inclusions}) {
    my ($product, $component) = split /:/, $name, 2;
    push @pairs, {
      product   => ($product eq '__Any__' ? undef : $product),
      component => (!defined $component || $component eq '__Any__'
                    ? undef : $component),
    };
  }
  return \@pairs;
}

sub dispatch {
  my ($operation, $request, $admin) = @_;

  if ($operation eq 'create-version') {
    # Version/Milestone use NAME_FIELD 'value'; passing 'name' is an invalid column
    # at the pinned SHA (Version.pm:30-56, Object.pm create).
    my $version = Bugzilla::Version->create(
      {value => $request->{name}, product => product_of($request->{product})});
    return {name => $version->name};
  }
  if ($operation eq 'create-milestone') {
    my $milestone = Bugzilla::Milestone->create(
      {value => $request->{name}, product => product_of($request->{product})});
    return {name => $milestone->name};
  }
  if ($operation eq 'create-keyword') {
    my $keyword = Bugzilla::Keyword->create(
      {name => $request->{name}, description => $request->{description}});
    return {name => $keyword->name};
  }
  if ($operation eq 'create-custom-field') {
    my $type = $FIELD_TYPES{$request->{field_type} // ''}
      // die "unsupported field_type\n";
    my $field = Bugzilla::Field->create({
      name        => $request->{name},
      description => $request->{name},
      type        => $type,
      custom      => 1,
      enter_bug   => 1,
    });
    if ($type != FIELD_TYPE_FREETEXT) {
      Bugzilla::Field::Choice->type($field)->create({value => $_})
        for @{$request->{values} // []};
    }
    return {name => $field->name};
  }
  if ($operation eq 'create-flag-type') {
    my $flagtype = Bugzilla::FlagType->create({
      name             => $request->{name},
      description      => $request->{description},
      target_type      => $request->{target},
      cc_list          => '',
      sortkey          => 1,
      is_active        => 1,
      is_requestable   => 1,
      is_requesteeble  => 1,
      is_multiplicable => 1,
      inclusions       => inclusion_pairs($request->{inclusions}),
    });
    return {name => $flagtype->name};
  }
  if ($operation eq 'create-api-key') {
    my $login = $request->{login} // $ENV{BZ_ADMIN_EMAIL};
    my $user  = Bugzilla::User->check({name => $login});
    my $key   = Bugzilla::User::APIKey->create(
      {user_id => $user->id, description => 'bzr-live provisioning'});
    return {login => $user->login, api_key => $key->api_key};
  }
  if ($operation eq 'get-custom-field') {
    my $field = Bugzilla::Field->new({name => $request->{name}});
    return undef unless $field && $field->custom;
    my $values = [];
    if ($field->is_select) {
      $values = [grep { $_ ne '---' }
                 map { $_->name } @{$field->legal_values}];
    }
    return {
      name       => $field->name,
      field_type => $FIELD_TYPE_NAMES{$field->type} // 'unknown',
      values     => $values,
    };
  }
  if ($operation eq 'get-keyword') {
    my $keyword = Bugzilla::Keyword->new({name => $request->{name}});
    return undef unless $keyword;
    return {name => $keyword->name, description => $keyword->description};
  }
  if ($operation eq 'get-flag-type') {
    my $matches = Bugzilla::FlagType::match({name => $request->{name}});
    return undef unless @$matches;
    die "flag type name is not unique in the fixture\n" if @$matches > 1;
    my $flagtype = $matches->[0];
    return {
      name        => $flagtype->name,
      description => $flagtype->description,
      # the accessor already maps the stored 'b'/'a' to 'bug'/'attachment'
      # (FlagType.pm:272 at the pinned SHA)
      target      => $flagtype->target_type,
      inclusions  => stored_inclusions($flagtype),
    };
  }
  die "unreachable operation\n";
}
```

Notes for the implementer: `Bugzilla::FlagType::match` and `target_type` returning
the single-character stored form (`b`/`a`), `legal_values`, and `is_select` all
exist at the pinned SHA; if the live smoke shows a different accessor shape, fix the
bridge — never the executor's canonical contract. `perl -c` cannot run on the host
(no Bugzilla libs); syntax-check inside the image:
`docker compose ... exec -T bugzilla perl -c /usr/local/bin/bzr-live-bridge` during
the smoke.

Host-side tests added in this task: extend `BridgeClientTests` asserting the client
sends `create-flag-type` inclusions in the canonical pair shape and that
`get-flag-type` `null` result maps to absent in the executor (fake bridge returning
`None`).

Verification: `make check` (compose config still valid; Dockerfile unchanged except
COPY), unit suite green. Commit:
`feat(provision): add container-local provisioning bridge`.

## Task 6 — smoke script, docs, guardrail wiring

Creates: `tests/provision_smoke.sh`. Modifies: `README.md`, `.gitignore`,
`Makefile` (`check` lists), `tests/test_provision.py` (none — this task is script +
docs).

`tests/provision_smoke.sh` — complete file:

```bash
#!/usr/bin/env bash
# Operator-run live proof for issue #4: two provisioning runs against a fresh
# fixture — first all created, second all unchanged — plus bzr custom-field
# readback. Requires: make up already healthy, and BZR_LIVE_BZR pointing at a bzr
# binary carrying the component default_assigned_to fix.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"  # uv resolves the project from cwd
BZR=${BZR_LIVE_BZR:?set BZR_LIVE_BZR to the bzr binary to validate with}
SCENARIO="$ROOT/tests/fixtures/provision-scenario"
STATE=$(mktemp -d "${TMPDIR:-/tmp}/bzr-live-provision-smoke.XXXXXX")
trap 'rm -rf "$STATE"' EXIT
chmod 700 "$STATE"

BASE_URL="http://127.0.0.1:${BZ_PORT:-8080}/"

run() {
  uv run --python 3.11 python -m bzr_live.provision "$SCENARIO" \
    --state-root "$STATE/state" --bzr "$BZR" --project-root "$ROOT" \
    --base-url "$BASE_URL"
}

echo "provision smoke: bridge syntax check inside the image"
PROJECT="bzr-live-$(printf '%s' "$ROOT" | openssl dgst -sha256 -r | cut -c1-12)"
docker compose --project-name "$PROJECT" --project-directory "$ROOT" \
  --file "$ROOT/compose.yaml" exec -T bugzilla \
  perl -c /usr/local/bin/bzr-live-bridge

echo "provision smoke: first run (expect all created)"
first=$(run)
echo "$first"
if printf '%s\n' "$first" | grep -q '^unchanged '; then
  echo "smoke failed: first run reported unchanged resources" >&2
  exit 1
fi

echo "provision smoke: second run (expect all unchanged)"
second=$(run)
echo "$second"
if printf '%s\n' "$second" | grep -q '^created '; then
  echo "smoke failed: second run created resources" >&2
  exit 1
fi

echo "provision smoke: custom-field readback through bzr"
key=$(cat "$STATE/state/admin.key")
values=$(BZR_LIVE_API_KEY=$key "$BZR" --json \
  --server-url "$BASE_URL" \
  --server-api-key-env BZR_LIVE_API_KEY \
  field list cf_q4_risk)
grep -q 'low' <<<"$values" || {
  echo "smoke failed: cf_q4_risk legal values not observable through bzr" >&2
  echo "$values" >&2
  exit 1
}
# The text field's definition must also be observable through bzr (criterion 5):
# field list on a freetext field should succeed (its value list may be empty).
BZR_LIVE_API_KEY=$key "$BZR" --json \
  --server-url "$BASE_URL" \
  --server-api-key-env BZR_LIVE_API_KEY \
  field list cf_q4_notes >/dev/null || {
  echo "smoke failed: cf_q4_notes definition not observable through bzr" >&2
  exit 1
}

echo "provision smoke: OK"
```

Makefile `check` target: append `tests/provision_smoke.sh` to both the `bash -n`
and `shellcheck` file lists (keep the existing backslash-list style). Do **not**
add a smoke invocation to `test` or CI — the live smoke is operator-run by the
recorded decision.

`.gitignore` — append:

```
/state/
```

`README.md` — append a section at the end of the file, using the README's existing
setext heading style (`Scenario provisioning` underlined with `---`), with this
content:

```markdown
Scenario provisioning
---------------------

Provision a validated scenario's resources into the running fixture:

    uv run python -m bzr_live.provision tests/fixtures/provision-scenario \
      --state-root ./state --bzr /path/to/bzr

Reruns are idempotent: existing resources that match their declaration are
reported `unchanged`, and a resource that differs in a declared field fails with
a conflict naming the field. Actor API keys are written under the state root
(default `./state`, gitignored) as owner-only files — `admin.key` for the
fixture admin and `actor-keys/<actor>.key` per scenario actor. The state root is
disposable local fixture state; deleting it re-mints keys on the next run.

`tests/provision_smoke.sh` runs the live two-run proof against a fresh fixture
(`BZR_LIVE_BZR=<bzr binary> bash tests/provision_smoke.sh`).
```

Verification: `bash -n tests/provision_smoke.sh`, `shellcheck
tests/provision_smoke.sh`, `make check`, full unit discover. Commit:
`feat(provision): add live smoke, state-root docs, and guardrail wiring`.

## Task 7 — full-suite verification and live proof

No new files. Run, in order, each expected green:

1. `uv run --python 3.11 python -m unittest discover -s tests -v` — all tests pass.
2. `make check` — exit 0.
3. `make test` — exit 0 (shell lifecycle suite + Python).
4. `uv build` then the installed-wheel smoke exactly as CI:
   `uv run --isolated --no-project --with ./dist/bzr_live-0.1.0-py3-none-any.whl
   python tests/smoke_installed.py tests/fixtures/minimal-scenario`.
5. Live proof (operator-run, Docker required). Freshness first — the smoke's
   first-run assertion needs an unprovisioned fixture, and `make up` reuses
   existing volumes: `CONFIRM_RESET=1 make reset` (fine on a never-initialized
   fixture), then `make up`, then
   `BZR_LIVE_BZR="/Volumes/Source Code Volume/src/bzr-worktrees/bzr-live-issue-4/target/release/bzr"
   bash tests/provision_smoke.sh`, then `CONFIRM_RESET=1 make reset` again.
   Expected: `provision smoke: OK`; first run lines all `created`, second all
   `unchanged`. A re-run after any smoke failure starts over from the leading
   reset.
6. `make build-multiarch` — container builds for both platforms with the new COPY.

Record actual durations beside each command in the task plan when run. Commit any
fixes discovered as their own conventional commits.
