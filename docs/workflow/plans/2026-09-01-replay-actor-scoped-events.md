# Implementation plan — replay actor-scoped bzr events with safe resume

**Goal.** Add a `bzr_live.replay` package that executes a validated scenario's ordered,
actor-scoped events against the local Bugzilla fixture and resumes an interrupted run to
exactly one semantic result per event or refuses with an actionable message.

**Architecture.** A `ReplayContext` owns credentials, the symbolic-reference → server-ID
table, and a temporary workspace. One `ActionHandler` per action owns everything about that
action: whether its payload is expressible in one mutation (`check_supported`), how to build
the invocation (`build`), how to read IDs out of a reply (`resolved_ids`), and how to
reconcile it against the server (`reconcile`). A `ReplayEngine` walks
`ValidatedScenario.events` in order, consults `JournalStore` for each event's latest record,
and writes the in-flight and completed records around every invocation. `__main__` wires the
CLI.

**Tech stack.** Python 3.11+, standard library only, run through `uv`. Tests are
`unittest`, discovered by `python -m unittest discover -s tests`.

Expected implementation size: **~2,000–2,500 changed lines (L)**, dominated by two components.
About 900 lines of module body, summed from the inline bodies in the file map below:
`context.py` ~145, `actions.py` ~530 (a ~158-line `check_supported` half, a ~220-line `build`
half, and eight `reconcile` methods), `engine.py` ~180, `__main__.py` ~50, `__init__.py` ~8.
Then ~1,050–1,550 lines of test body: 62 new tests at this repository's measured 17–25 lines
per test, the floor being `tests/test_provision.py` at 17.1. The remainder is the eight-action
scenario fixture (~55), `tests/replay_smoke.sh` (~100, against `provision_smoke.sh`'s 87 for
strictly less work), the `adapters.py` and `checksetup_answers.txt` edits (~10), and the
Makefile/CI/README wiring (~25).

Excludes these design documents, and excludes `docs/bzr-findings.md` — the register is already
committed on this branch, so it is not remaining work. An earlier draft of this line said
1350–1750 and counted the register; both were wrong.

## Global Constraints

Transcribed from
[the design spec](../specs/2026-09-01-replay-actor-scoped-events-design.md) and
[ADR 0006](../../adr/0006-actor-scoped-event-replay.md):

- **Python 3.11+**, no runtime dependencies. The package under `src/bzr_live/` imports only
  the standard library and its own siblings.
- **Guardrails**: `make check` (`bash -n`, `shellcheck`, `compileall`, `docker compose
  config`) and `make test` (`tests/lifecycle_test.sh` plus `unittest discover -s tests`).
  Both must exit 0 before every commit. Baseline before this work: 154 tests, `make test`
  ≈1.2 s, `make check` ≈0.3 s.
- **Style**: match the surrounding code — `from __future__ import annotations`, module-level
  private helpers prefixed `_`, one actionable exception class per package whose `str(exc)`
  is the operator-facing message, comments only for non-obvious invariants. Lines ≤100
  characters.
- **Mutation boundaries** are fixed by ADR 0004 and may not grow: `bzr` subprocess
  invocations through `bzr_live.provision.adapters.BzrClient`, and
  `bzr_live.provision.adapters.assign_bug_custom_fields` for per-bug custom fields. No new
  REST route, no raw SQL, no bridge call from the replay path.
- **Secrets**: an API key reaches `bzr` only through the `BZR_LIVE_API_KEY` environment
  variable (`BzrClient` already does this) and reaches REST only in the JSON body. Every
  journal write passes `known_secrets` so redaction applies. No key in argv, in a message,
  or in ordinary output.
- **Journal contract** is ADR 0002's and is not modified: `next_safe_action` written by this
  code is only `advance`, `retry`, or `stop`; a reconciliation-derived record carries
  `exit_status = -1`.
- **Refusals are preconditions.** Every check that can run before a mutation runs before the
  first mutation.
- **Scenario name and event names** are loader-validated slugs matching
  `[a-z][a-z0-9-]{0,62}`, so they are safe path components.

## File map

| File | New/changed | Responsibility |
|---|---|---|
| `src/bzr_live/replay/__init__.py` | new | package exports |
| `src/bzr_live/replay/context.py` | new | `ReplayError`, `ReplayContext` — credentials, ID table, workspace |
| `src/bzr_live/replay/actions.py` | new | `Invocation`, `Reconciliation`, `ActionHandler` and the eight handlers, `HANDLERS` |
| `src/bzr_live/replay/engine.py` | new | `ReplayEngine` — preconditions, ordered loop, journal, resume decisions |
| `src/bzr_live/replay/__main__.py` | new | CLI and exit codes |
| `tests/test_replay.py` | new | unit suite over mocked `subprocess.run` and URL opener |
| `tests/fixtures/replay-scenario/` | new | scenario exercising all eight actions (built in Task 1; every later task's tests load it) |
| `src/bzr_live/provision/adapters.py` | changed | `BzrClient.read` gains keyword-only `absent_codes`; new `BUG_ABSENT_CODES` |
| `docs/bzr-findings.md` | already committed | the register of bzr limitations this work surfaced; no task changes it |
| `containers/bugzilla/checksetup_answers.txt` | changed | `defaultplatform` / `defaultopsys`, so an honest create succeeds (Task 0); `defaultpriority` corrected to `'---'` after the live run (see Task 0's acceptance criteria) |
| `tests/replay_smoke.sh` | new | operator-run live proof: the create succeeds and its alias round-trips |
| `Makefile` | changed | widen `compileall` from two named test files to `tests`; add `tests/replay_smoke.sh` to the shell checks and a `replay-smoke` target |
| `.github/workflows/scenario-contract.yml` | changed | add the new ADR, spec and plan to both `paths` lists |
| `README.md` | changed | document `replay` and `resume` |

## Task 0 — the fixture's create defaults

Changes `containers/bugzilla/checksetup_answers.txt`. No code, no tests.

**Where this fits.** First, because the fixture must be able to accept an honest create before
any replay step can be verified against it.

**The findings register is not part of this task.** `docs/bzr-findings.md` is already complete
and committed on this branch, and its entries have been checked against bzr's own ADRs and
open issues, with bzr#640 and bzr#641 filed and cross-linked. Do **not** rewrite it from a
description — that discards verified upstream work. Its set is D1, D3, D4, D5, G1–G7 and G9. There is
no D2 (reclassified to G7 once bzr's accepted ADR 0015 turned out to govern it) and no G8:
that entry charged bzr for the absent `--reset-dupe-of`, and the fixture's own Bugzilla image
showed the constraint is Bugzilla's, so it was withdrawn. G9 (bzr's silent `"unspecified"`
version default) is already there.

### Step 0.1 — give the fixture create defaults

`bzr` documents `op_sys` and `rep_platform` as "required by some Bugzilla installations"
(`src/cli/bug/create.rs:138,141`) and passes both on every functional create. Bugzilla
supplies them from its own `defaultplatform`/`defaultopsys` parameters when set, and this
fixture sets neither. Add to `containers/bugzilla/checksetup_answers.txt`, beside the other
parameter answers:

```perl
$answer{'defaultplatform'} = 'PC';
$answer{'defaultopsys'} = 'Linux';
```

The alternative — injecting a fixed pair into every create document — sends a value the
scenario never declared so the run appears to succeed, which `AGENTS.md` forbids. Fixing the
fixture keeps the create payload a faithful record of what the scenario asked for.

### Step 0.2 — verify

```
make check
```

Exit 0. The parameter change only takes effect on a fresh install, so the operator must run
`CONFIRM_RESET=1 make reset && make up` before `make replay-smoke`; that requirement is
load-bearing for Step 6.1b, whose preamble repeats it.

### Step 0.3 — commit

```
git add containers/bugzilla/checksetup_answers.txt
git commit -m "fix(fixture): declare default platform and OS for bug creates"
```

**Acceptance criteria.** `checksetup_answers.txt` sets `defaultplatform` and `defaultopsys`,
so a create declaring neither succeeds without the runner substituting anything. Every
bzr-grounded refusal message a later task writes has a register entry to cite — satisfied by
the committed register, not by this task.

**Amended after the live run.** This task planned two answers; the file carries three. The
first `make replay-smoke` failed at the create with api_code 51, and the cause was a
pre-existing `defaultpriority = '--'` answer — not a legal priority, accepted unchecked at
install (`Bugzilla/Config.pm:235-236`), and substituted into every create that declares none
(`Bugzilla/Bug.pm:713-714`). Corrected to Bugzilla's own `'---'` in commit `a9627fe`. It is
recorded here rather than folded into Step 0.1 because the plan did not foresee it: nothing
in this repository had created a bug through the fixture before replay did.

## Task 1 — `ReplayContext`

Creates `tests/fixtures/replay-scenario/`, `src/bzr_live/replay/__init__.py`,
`src/bzr_live/replay/context.py`; changes `src/bzr_live/provision/adapters.py`.
Tests in `tests/test_replay.py`.

**Where this fits.** Everything else consumes `ReplayContext`. It is the only unit that
touches the key store, the temporary workspace, and the ID table. The fixture is built here
rather than last because every later task's tests load it, and Tasks 3 and 4 assert markers
that name it.

### Step 1.0 — write the fixture

`tests/fixtures/replay-scenario/scenario.json`, `resources.json`, `events.jsonl` and
`assets/notes.txt`, declaring one product, one component, one version, one milestone, one
keyword, one flag type, one custom field, two actors (`triager` and `reporter`), and eight
events — one per action, in dependency order: `create-checkout-race` (`bug.create`),
`update-triage` (`bug.update`), `comment-triage` (`bug.comment`), `attach-notes`
(`bug.attach`), `worktime-triage` (`bug.worktime`), `custom-field-triage`
(`bug.custom-field-set`), `flag-review` (`bug.flag`), `obsolete-notes`
(`attachment.update`). Its `scenario.json` `name` is `replay-demo`, which the marker
assertions in Tasks 3 and 4 depend on, and its `triager` actor is the one Task 1's context
tests use. The repository's other two fixtures declare no actors at all
(`tests/fixtures/minimal-scenario/resources.json` is `{"format_version":1,"resources":[]}`),
so this fixture is the first one these tests can run against.

Verify it loads before writing any test against it:

```
uv run --python 3.11 python -c "from bzr_live.scenario import load_scenario; s = load_scenario('tests/fixtures/replay-scenario'); print(s.name, len(s.events), s.digest[:12])"
```

Expect `replay-demo 8` and a 12-character digest prefix.

**Interfaces produced** (later tasks rely on these exact signatures):

```python
class ReplayError(Exception): ...

class ReplayContext:
    def __init__(self, scenario: ValidatedScenario, keys: KeyStore, *, bzr_path: str,
                 base_url: str, workspace: str | Path, run=subprocess.run,
                 opener=urllib.request.urlopen) -> None
    def actor_email(self, actor: Reference) -> str
    def client(self, actor: Reference) -> BzrClient
    def read_bug(self, actor: Reference, positionals: list[str]) -> object | None
    def actor_key(self, actor: Reference) -> str
    @property
    def known_secrets(self) -> frozenset[str]
    def resolve(self, ref: Reference) -> int
    def resolve_all(self, refs: tuple[Reference, ...]) -> list[int]
    def adopt(self, resolved_ids: Mapping[str, int]) -> None
    def resource(self, kind: str, name: str) -> PlannedResource
    def text_file(self, stem: str, text: str) -> str
    def json_file(self, stem: str, document: dict) -> str
    def asset_file(self, name: str, expected_sha256: str) -> str
    def rest(self, actor: Reference, bug_id: int, values: dict) -> object
```

**Interfaces consumed** (confirmed present in this repository at
`bb3a189`): `bzr_live.scenario.ValidatedScenario` (fields `name`, `resources`,
`resource_plan`, `events`, `assets`, `digest`), `bzr_live.scenario.Reference(kind, name)`,
`bzr_live.scenario.PlannedResource(kind, name, data, dependencies)`,
`bzr_live.scenario.Asset(name, path, sha256, content)`,
`bzr_live.provision.adapters.BzrClient(bzr_path, base_url, api_key, admin_email, run=subprocess.run)`
with `.read(args, positionals=None)` / `.write(args, positionals=None)` / `.whoami()`,
`bzr_live.provision.adapters.assign_bug_custom_fields(base_url, api_key, bug_id, values, opener=urllib.request.urlopen)`,
`bzr_live.provision.adapters.ProvisionError`, `bzr_live.provision.adapters._KEY_ENV`
(the string `"BZR_LIVE_API_KEY"`), `bzr_live.provision.keys.KeyStore(state_root)` with
`.actor_key(name)` / `.admin_key()`.

### Step 1.1 — write the failing test

Create `tests/test_replay.py`:

```python
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from bzr_live.provision import KeyStore, ProvisionError
from bzr_live.provision.adapters import BUG_ABSENT_CODES, BzrClient
from bzr_live.replay import ReplayContext, ReplayError
from bzr_live.scenario import (
    CompletedRecord,
    InFlightRecord,
    Reference,
    load_scenario,
)

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
            # Asset *names* are loader slugs (loader.py:25, applied at :390), so the
            # key is "notes"; the declared path is assets/notes.txt.
            context.asset_file("notes", "0" * 64)
        self.assertIn("notes.txt", str(caught.exception))
        self.assertIn("hashes to", str(caught.exception))
```

Add three tests for the boundary change Step 1.3a makes:

```python
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
```

### Step 1.2 — confirm it fails

```
uv run --python 3.11 python -m unittest tests.test_replay -v
```

Expect `ModuleNotFoundError: No module named 'bzr_live.replay'`.

### Step 1.3 — write `context.py`

```python
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from ..provision.adapters import (
    BUG_ABSENT_CODES,
    BUG_CUSTOM_FIELD_BOUNDARY,
    BzrClient,
    _KEY_ENV,
    assign_bug_custom_fields,
)
from ..scenario import PlannedResource, Reference, ValidatedScenario

KEY_ENV = _KEY_ENV
REST_BOUNDARY = BUG_CUSTOM_FIELD_BOUNDARY


class ReplayError(Exception):
    """Actionable replay failure; str(exc) is the operator-facing message."""


class ReplayContext:
    """Credentials, symbolic identity, and the scratch workspace for one run."""

    def __init__(self, scenario, keys, *, bzr_path, base_url, workspace,
                 run=subprocess.run, opener=urllib.request.urlopen) -> None:
        self._scenario = scenario
        self._keys = keys
        self._bzr_path = bzr_path
        self._base_url = base_url
        self._workspace = Path(workspace)
        self._run = run
        self._opener = opener
        self._clients: dict[str, BzrClient] = {}
        self._keys_seen: dict[str, str] = {}
        self._ids: dict[str, int] = {}
        self._resources = {f"{r.kind}:{r.name}": r for r in scenario.resources}
        self._files = 0

    # --- resources and credentials ---------------------------------------

    def resource(self, kind: str, name: str) -> PlannedResource:
        try:
            return self._resources[f"{kind}:{name}"]
        except KeyError:
            raise ReplayError(f"{kind}:{name} is not declared in this scenario") from None

    def actor_email(self, actor: Reference) -> str:
        return self.resource("actor", actor.name).data["email"]

    def actor_key(self, actor: Reference) -> str:
        if actor.name not in self._keys_seen:
            key = self._keys.actor_key(actor.name)
            if key is None:
                raise ReplayError(
                    f"no API key for actor {actor.name!r}; run "
                    f"python -m bzr_live.provision <scenario_dir> first")
            self._keys_seen[actor.name] = key
        return self._keys_seen[actor.name]

    def client(self, actor: Reference) -> BzrClient:
        key = self.actor_key(actor)
        if actor.name not in self._clients:
            self._clients[actor.name] = BzrClient(
                self._bzr_path, self._base_url, key,
                admin_email=self.actor_email(actor), run=self._run)
        return self._clients[actor.name]

    @property
    def known_secrets(self) -> frozenset[str]:
        return frozenset(self._keys_seen.values())

    # --- identity ---------------------------------------------------------

    def read_bug(self, actor: Reference, positionals: list[str]):
        """Read a bug by id or alias; None means absent, 102 (access denied) raises."""
        return self.client(actor).read(
            ["bug", "view"], positionals=positionals, absent_codes=BUG_ABSENT_CODES)

    def resolve(self, ref: Reference) -> int:
        try:
            return self._ids[f"{ref.kind}:{ref.name}"]
        except KeyError:
            raise ReplayError(
                f"{ref.kind}:{ref.name} has no server id; the event that creates it has "
                "not completed") from None

    def resolve_all(self, refs) -> list[int]:
        return [self.resolve(ref) for ref in refs]

    def adopt(self, resolved_ids: Mapping[str, int]) -> None:
        self._ids.update(resolved_ids)

    # --- workspace --------------------------------------------------------

    def _write_private(self, name: str, content: bytes) -> str:
        path = self._workspace / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(fd, content[offset:])
        finally:
            os.close(fd)
        return str(path)

    def text_file(self, stem: str, text: str) -> str:
        self._files += 1
        return self._write_private(
            f"{self._files:04d}-{stem}.txt", text.encode("utf-8"))

    def json_file(self, stem: str, document: dict) -> str:
        self._files += 1
        encoded = json.dumps(document, ensure_ascii=False).encode("utf-8")
        return self._write_private(f"{self._files:04d}-{stem}.json", encoded)

    def asset_file(self, name: str, expected_sha256: str) -> str:
        asset = self._scenario.assets[name]
        if asset.sha256 != expected_sha256:
            raise ReplayError(
                f"asset {name!r} hashes to {asset.sha256} but the event expects "
                f"{expected_sha256}; the scenario and its journal disagree")
        self._files += 1
        directory = self._workspace / f"{self._files:04d}-asset"
        os.mkdir(directory, 0o700)
        return self._write_private(
            f"{directory.name}/{Path(asset.path).name}", asset.content)

    # --- the REST boundary -------------------------------------------------

    def rest(self, actor: Reference, bug_id: int, values: dict) -> object:
        return assign_bug_custom_fields(
            self._base_url, self.actor_key(actor), bug_id, values, opener=self._opener)
```

### Step 1.3a — widen the boundary client's absence contract

`BzrClient.read` reports absent only for `api_code` 51, 105 or 106 — the product and
component codes issue #4 needed. A bug lookup answers 100 (invalid alias), 101 (invalid ID)
or 102 (access denied), so every one of those currently raises, which would make the pristine
sweep abort against a genuinely pristine fixture. In `src/bzr_live/provision/adapters.py`,
add beside the existing constants:

```python
# Bugzilla Bug.get per-resource fault codes. 100/101 mean the bug is not there; 102 means
# the caller may not see it, which is deliberately NOT absence — see ADR 0006.
_NOT_FOUND_CODES = frozenset({51, 105, 106})
BUG_ABSENT_CODES = frozenset({100, 101})
```

and change `BzrClient.read`'s signature and its exit-4 branch:

```python
    def read(self, args: list[str], positionals: list[str] | None = None, *,
             absent_codes: frozenset[int] = _NOT_FOUND_CODES):
        ...
        if done.returncode == 4 and _api_error_code(stderr) in absent_codes:
            return None
```

The parameter is keyword-only with today's set as its default, so every existing
provisioning call site is unaffected. The change stays strictly inside `adapters.py`:
do **not** re-export `BUG_ABSENT_CODES` from `src/bzr_live/provision/__init__.py`. That
file is issue #4's, the surface amendment names only `adapters.py`, and every consumer
here imports from `bzr_live.provision.adapters` directly, so the re-export would be an
unread public name added outside the approved surface.

Create `src/bzr_live/replay/__init__.py`:

```python
"""Actor-scoped event replay with journal-backed safe resume (issue #6, ADR 0006)."""

from .context import ReplayContext, ReplayError

__all__ = ["ReplayContext", "ReplayError"]
```

### Step 1.4 — confirm it passes

```
uv run --python 3.11 python -m unittest tests.test_replay -v
```

Expect `OK`, nine tests.

### Step 1.5 — commit

```
make check && make test
git add src/bzr_live/replay src/bzr_live/provision tests/test_replay.py tests/fixtures/replay-scenario
git commit -m "feat(replay): add the replay context for credentials and identity"
```

Both guardrails exit 0. `make test` reports 163 tests.

**Acceptance criteria.** `bzr bug view` of an absent alias reads as absent while an
access-denied answer still raises, and provisioning's default absent-code set is unchanged.
A missing actor key raises `ReplayError` naming the actor and the
provisioning command. Clients are cached per actor. `known_secrets` holds every loaded key.
`resolve` raises for an unresolved reference and returns the adopted ID afterwards. Workspace
files are mode 0600 and uniquely named. An asset whose checksum disagrees with the event's
`asset_sha256` raises.

## Task 2 — the supported-payload table

Creates `src/bzr_live/replay/actions.py` (the `check_supported` half). Tests appended to
`tests/test_replay.py`.

**Where this fits.** `ReplayEngine` runs `check_supported` over every event as precondition
2, before any mutation. Task 3 fills in `build` on the same classes.

**Interfaces produced:**

```python
class ActionHandler:
    action: str
    boundary: str = "bzr"
    @staticmethod
    def check_supported(event: PlannedEvent) -> None      # raises ReplayError

HANDLERS: dict[str, ActionHandler]        # keyed by PlannedEvent.action, all eight actions
ATTACHMENT_SUMMARY_BYTE_LIMIT = 255      # Bugzilla 5.2 attachments.description is TINYTEXT
def render_marker(text: str, marker: str) -> str
def render_attachment_summary(description: str, marker: str, sha256: str) -> str
```

**Interfaces consumed:** `bzr_live.scenario.PlannedEvent` (fields `name`, `actor`, `action`,
`action_class`, `payload`, `dependencies`, `reconciliation_marker`, `expected_postcondition`,
`creates`), and `ReplayError` from Task 1. `event.expected_postcondition` is a mapping with
keys `action`, `target`, `values`, `marker`; `values` is the per-action mapping the loader
built.

### Step 2.1 — write the failing tests

Append to `tests/test_replay.py`:

```python
from bzr_live.replay import HANDLERS, ReplayError
from bzr_live.scenario import ScenarioValidationError, load_scenario


class SupportedPayloadTest(unittest.TestCase):
    def _event(self, action: str, **values):
        """A minimal stand-in carrying only what check_supported reads."""
        class Event:
            pass
        event = Event()
        event.name = "sample"
        event.action = action
        event.reconciliation_marker = "bzr-live:demo:sample"
        event.expected_postcondition = {"action": action, "values": values}
        return event

    def test_create_rejects_custom_fields(self) -> None:
        event = self._event("bug.create", version="1.0", custom_fields=({"field": 1},))
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.create"].check_supported(event)
        self.assertIn("custom_fields", str(caught.exception))
        self.assertIn("finding G4", str(caught.exception))

    def test_create_rejects_estimated_hours(self) -> None:
        event = self._event("bug.create", version="1.0", estimated_hours="3.5")
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.create"].check_supported(event)
        self.assertIn("estimated_hours", str(caught.exception))
        self.assertIn("finding G1", str(caught.exception))

    def test_create_rejects_remaining_hours(self) -> None:
        event = self._event("bug.create", version="1.0", remaining_hours="1.0")
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.create"].check_supported(event)
        self.assertIn("remaining_hours", str(caught.exception))
        self.assertIn("finding G1", str(caught.exception))

    def test_update_rejects_version(self) -> None:
        event = self._event("bug.update", version=object())
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.update"].check_supported(event)
        self.assertIn("version", str(caught.exception))
        self.assertIn("finding G2", str(caught.exception))

    def test_update_rejects_a_null_milestone(self) -> None:
        event = self._event("bug.update", milestone=None)
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.update"].check_supported(event)
        self.assertIn("milestone", str(caught.exception))
        self.assertIn("finding G3", str(caught.exception))

    def test_flag_rejects_a_hyphenated_flag_type_name(self) -> None:
        event = self._event("bug.flag", flag_type=Reference("flag-type", "needs-info"))
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.flag"].check_supported(event)
        self.assertIn("needs-info", str(caught.exception))
        self.assertIn("finding D1", str(caught.exception))

    def test_create_rejects_a_null_version(self) -> None:
        event = self._event("bug.create", version=None)
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.create"].check_supported(event)
        self.assertIn("version", str(caught.exception))
        self.assertIn("finding G9", str(caught.exception))

    def test_create_accepts_a_supported_payload(self) -> None:
        HANDLERS["bug.create"].check_supported(self._event("bug.create", version="1.0"))

    def test_update_rejects_groups(self) -> None:
        event = self._event("bug.update", groups=())
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.update"].check_supported(event)
        self.assertIn("groups", str(caught.exception))
        self.assertIn("finding D3", str(caught.exception))

    def test_update_rejects_a_null_resolution(self) -> None:
        event = self._event("bug.update", resolution=None)
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.update"].check_supported(event)
        self.assertIn("resolution", str(caught.exception))
        # Bugzilla clears the resolution on transition to an open status, so the
        # message names Bugzilla and links no findings entry.
        self.assertIn("Bugzilla", str(caught.exception))
        self.assertNotIn("(finding ", str(caught.exception))

    def test_update_rejects_status_with_duplicate_of(self) -> None:
        event = self._event("bug.update", status="RESOLVED", duplicate_of=object())
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.update"].check_supported(event)
        self.assertIn("status", str(caught.exception))
        self.assertIn("conflicts_with", str(caught.exception))
        self.assertIn("finding G5", str(caught.exception))

    def test_update_rejects_resolution_with_duplicate_of(self) -> None:
        event = self._event("bug.update", resolution="DUPLICATE", duplicate_of=object())
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.update"].check_supported(event)
        self.assertIn("resolution", str(caught.exception))
        self.assertIn("conflicts_with", str(caught.exception))
        self.assertIn("finding G5", str(caught.exception))

    def test_update_rejects_a_null_duplicate_of(self) -> None:
        event = self._event("bug.update", duplicate_of=None)
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.update"].check_supported(event)
        self.assertIn("duplicate_of", str(caught.exception))
        self.assertIn("Bugzilla", str(caught.exception))
        self.assertNotIn("(finding ", str(caught.exception))

    def test_create_rejects_duplicate_of(self) -> None:
        event = self._event("bug.create", version="1.0", duplicate_of=object())
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.create"].check_supported(event)
        self.assertIn("duplicate_of", str(caught.exception))
        # Bugzilla's own Bug.create has no dupe_of, so the message names Bugzilla,
        # not bzr, and links no findings entry.
        self.assertIn("Bugzilla", str(caught.exception))
        self.assertNotIn("(finding ", str(caught.exception))

    def test_attachment_summary_limit(self) -> None:
        event = self._event(
            "bug.attach", description="x" * 250, asset_sha256="a" * 64)
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.attach"].check_supported(event)
        self.assertIn("255", str(caught.exception))

    def test_attachment_summary_limit_counts_bytes_not_characters(self) -> None:
        # 120 CJK characters render well under 255 code points and well over 255 bytes.
        event = self._event("bug.attach", description="\u6f22" * 120, asset_sha256="a" * 64)
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.attach"].check_supported(event)
        self.assertIn("bytes", str(caught.exception))

    def test_every_action_has_a_handler(self) -> None:
        self.assertEqual(set(HANDLERS), {
            "bug.create", "bug.update", "bug.comment", "bug.attach", "bug.worktime",
            "bug.custom-field-set", "bug.flag", "attachment.update"})
```

### Step 2.2 — confirm it fails

```
uv run --python 3.11 python -m unittest tests.test_replay -v
```

Expect `ImportError: cannot import name 'HANDLERS' from 'bzr_live.replay'`.

### Step 2.3 — write the `check_supported` half of `actions.py`

```python
from __future__ import annotations

from ..scenario import PlannedEvent
from .context import ReplayError

# Bugzilla declares attachments.description TINYTEXT at Bugzilla/DB/Schema.pm:505 -- read
# from this fixture's own image (BUGZILLA_VERSION "5.2+") -- a MySQL 255-BYTE column, so the
# check measures encoded bytes and not code points.
ATTACHMENT_SUMMARY_BYTE_LIMIT = 255

# Grounds for the refusals below, at bzr b80303b7: create_json.rs:150 defaults an omitted
# version to "unspecified"; update.rs:85 and :92 both carry conflicts_with = "dupe_of".
# Kept here rather than in the operator-facing messages, which outlive any line number.
#
# A message naming a *bzr* limitation cites its docs/bzr-findings.md entry as "(finding X)".
# A message whose ground is Bugzilla's own model names Bugzilla and cites no entry, because
# charging bzr for a constraint it did not impose corrupts the register as surely as hiding
# a real gap does. Neither kind tells the author how to route around the boundary.
_CREATE_UNSUPPORTED = {
    "custom_fields": "bzr excludes cf_* from bug create by design (finding G4)",
    "estimated_hours": "bzr bug create --from-json has no estimated_time field, "
                       "though Bugzilla accepts one (finding G1)",
    "remaining_hours": "bzr bug create --from-json has no remaining_time field, "
                       "though Bugzilla accepts one (finding G1)",
    "duplicate_of": "Bugzilla's own Bug.create has no dupe_of field",
}
_UPDATE_UNSUPPORTED = {
    "groups": "bzr bug view does not return groups, so no delta can be computed and no "
              "result confirmed (finding D3)",
    "version": "bzr bug update has no version flag, though Bugzilla accepts one "
               "(finding G2)",
}
_UPDATE_NO_CLEAR = {
    "resolution": "Bugzilla clears the resolution on transition to an open status, "
                  "so declare that status change instead",
    "milestone": "bzr offers --reset-assigned-to but no milestone reset (finding G3)",
    # Bugzilla's, not bzr's: Bug.update types dupe_of as int with no null form, and
    # clear_resolution -- which calls _clear_dup_id -- throws unless the bug is already
    # open (Bugzilla/Bug.pm:2933-2939, WebService/Bug.pm:3935-3942, verified against the
    # fixture image). bzr's dupe_of: Option<u64> mirrors that exactly.
    "duplicate_of": "Bugzilla clears a duplicate through a status transition, not by "
                    "nulling dupe_of, so declare that status change instead",
}
# Both flags carry `conflicts_with = "dupe_of"` at bzr b80303b7
# (src/cli/bug/update.rs:85 and :92), so clap rejects either pairing at parse time.
_UPDATE_DUPE_CONFLICTS = ("status", "resolution")


def render_marker(text: str, marker: str) -> str:
    return f"{text}\n\n[{marker}]"


def render_attachment_summary(description: str, marker: str, sha256: str) -> str:
    return f"{description} [{marker}] sha256={sha256}".strip()


def _unsupported(event: PlannedEvent, field: str, limitation: str) -> ReplayError:
    # The register pointer is appended only for a bzr-grounded limitation, which is
    # exactly the set that names an entry. A Bugzilla-grounded refusal has no entry to
    # point at, and sending an operator to look for one would be its own small lie.
    pointer = " See docs/bzr-findings.md." if "(finding " in limitation else ""
    return ReplayError(
        f"event {event.name!r} ({event.action}) declares {field!r}, which the boundary "
        f"cannot apply: {limitation}.{pointer}")


class ActionHandler:
    action = ""
    boundary = "bzr"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        return


class BugCreateHandler(ActionHandler):
    action = "bug.create"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        values = event.expected_postcondition["values"]
        for field, fix in _CREATE_UNSUPPORTED.items():
            declared = values.get(field)
            if declared:                      # () and None are both "not declared"
                raise _unsupported(event, field, fix)
        if values.get("version") is None:
            raise _unsupported(
                event, "version",
                "bzr silently substitutes the version 'unspecified', which this "
                "fixture's products do not declare (finding G9)")


class BugUpdateHandler(ActionHandler):
    action = "bug.update"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        values = event.expected_postcondition["values"]
        for field, fix in _UPDATE_UNSUPPORTED.items():
            if field in values:
                raise _unsupported(event, field, fix)
        for field, fix in _UPDATE_NO_CLEAR.items():
            if field in values and values[field] is None:
                raise _unsupported(event, field, fix)
        if values.get("duplicate_of") is not None:
            for field in _UPDATE_DUPE_CONFLICTS:
                if field in values:
                    raise _unsupported(
                        event, field,
                        f"bzr's --{field} carries conflicts_with = \"dupe_of\", "
                        "deliberately (finding G5)")


class BugCommentHandler(ActionHandler):
    action = "bug.comment"


class BugAttachHandler(ActionHandler):
    action = "bug.attach"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        values = event.expected_postcondition["values"]
        rendered = render_attachment_summary(
            values["description"], event.reconciliation_marker, values["asset_sha256"])
        size = len(rendered.encode("utf-8"))
        if size > ATTACHMENT_SUMMARY_BYTE_LIMIT:
            raise _unsupported(
                event, "description",
                f"the rendered attachment summary is {size} bytes and Bugzilla's "
                f"attachments.description holds {ATTACHMENT_SUMMARY_BYTE_LIMIT}; "
                "shorten the description")


class BugWorktimeHandler(ActionHandler):
    action = "bug.worktime"


class BugCustomFieldHandler(ActionHandler):
    action = "bug.custom-field-set"
    boundary = "bugzilla-rest-custom-field"


class BugFlagHandler(ActionHandler):
    action = "bug.flag"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        # bzr's parse_single_flag takes the FIRST of + - ? X as the status character, so a
        # hyphenated slug like `needs-info` renders `--flag=needs-info?` and parses as name
        # `needs`. Uppercase X cannot occur in a slug, so the hyphen is the live hazard.
        name = event.expected_postcondition["values"]["flag_type"].name
        if any(character in name for character in "+-?X"):
            raise _unsupported(
                event, "flag_type",
                f"bzr cannot address the flag type name {name!r}: its flag parser takes "
                "the first of + - ? X as the status character, so the name is truncated. "
                "Bugzilla permits such names (finding D1)")


class AttachmentUpdateHandler(ActionHandler):
    action = "attachment.update"


HANDLERS: dict[str, ActionHandler] = {
    handler.action: handler()
    for handler in (
        BugCreateHandler, BugUpdateHandler, BugCommentHandler, BugAttachHandler,
        BugWorktimeHandler, BugCustomFieldHandler, BugFlagHandler, AttachmentUpdateHandler,
    )
}
```

Extend `src/bzr_live/replay/__init__.py`:

```python
"""Actor-scoped event replay with journal-backed safe resume (issue #6, ADR 0006)."""

from .actions import HANDLERS, ActionHandler
from .context import ReplayContext, ReplayError

__all__ = ["HANDLERS", "ActionHandler", "ReplayContext", "ReplayError"]
```

### Step 2.4 — confirm it passes

```
uv run --python 3.11 python -m unittest tests.test_replay -v
```

Expect `OK`, twenty-six tests.

### Step 2.5 — commit

```
make check && make test
git add src/bzr_live/replay tests/test_replay.py
git commit -m "feat(replay): refuse payloads the bzr boundary cannot apply"
```

**Acceptance criteria.** Every row of the spec's supported-payload "Refused" column raises
`ReplayError` naming the field and its fix, both `duplicate_of` conflicts included. The
attachment ceiling is enforced in UTF-8 bytes, pinned by a non-ASCII test. A supported payload raises nothing. `HANDLERS`
covers exactly the loader's eight actions.

## Task 3 — building invocations

Adds `Invocation` and `build`/`resolved_ids` to
`src/bzr_live/replay/actions.py`. Tests appended to `tests/test_replay.py`.

**Where this fits.** `ReplayEngine` calls `build` before writing the in-flight record, so the
`InvocationMetadata` recorded there is the one that is about to run.

**Interfaces produced:**

```python
@dataclass(frozen=True, slots=True)
class Invocation:
    metadata: InvocationMetadata
    args: tuple[str, ...]
    positionals: tuple[str, ...]
    target_id: int | None = None            # bug id, for the REST boundary
    values: Mapping[str, object] | None = None   # REST body, without the api_key

class ActionHandler:
    def build(self, context: ReplayContext, event: PlannedEvent) -> Invocation
    def resolved_ids(self, event: PlannedEvent, output) -> dict[str, int]
```

**Interfaces consumed:** `ReplayContext` from Task 1 (`client`, `actor_email`, `resolve`,
`resolve_all`, `text_file`, `json_file`, `asset_file`, `rest`, `resource`),
`bzr_live.scenario.InvocationMetadata(mutation_boundary, operation, arguments, environment_names)`,
`bzr_live.replay.context.KEY_ENV` (`"BZR_LIVE_API_KEY"`),
`bzr_live.provision.executor.custom_field_name(slug) -> str` (maps `a-b` to `cf_a_b`).

### Step 3.1 — write the failing tests

Append to `tests/test_replay.py`. `_FakeRun` records every argv and returns a queued reply;
it is reused by Tasks 4 and 5.

```python
import subprocess


class _FakeRun:
    """Stands in for subprocess.run: records calls, returns queued CompletedProcess."""

    def __init__(self, replies=None):
        self.calls: list[dict] = []
        # Each reply is (exit_code, stdout_payload_or_None, api_code_or_None). The third
        # slot builds the stderr JSON line BzrClient.read reads the api_code out of.
        self.replies = list(replies or [])

    def __call__(self, argv, capture_output=False, env=None, shell=False, input=None):
        self.calls.append({"argv": list(argv), "env": dict(env or {}), "input": input})
        code, payload, api_code = (
            self.replies.pop(0) if self.replies else (0, {}, None))
        stdout = json.dumps(payload).encode("utf-8") if payload is not None else b""
        stderr = b"" if api_code is None else json.dumps(
            {"error": {"api_code": api_code}}).encode("utf-8")
        return subprocess.CompletedProcess(argv, code, stdout, stderr)


class BuildTest(unittest.TestCase):
    # setUp mirrors ContextTest: a 0700 state root, a workspace, the replay fixture,
    # and an actor key stored for every actor the fixture declares.
    ...

    def test_comment_writes_the_body_to_a_private_file_with_the_marker(self) -> None:
        event = self._event("comment-triage")     # a bug.comment event in the fixture
        self.context.adopt({"bug:checkout-race": 41})
        invocation = HANDLERS["bug.comment"].build(self.context, event)
        self.assertEqual(invocation.positionals, ("41",))
        body_arg = [a for a in invocation.args if a.startswith("--body-file=")][0]
        body = Path(body_arg.split("=", 1)[1]).read_text(encoding="utf-8")
        self.assertTrue(body.endswith("\n\n[bzr-live:replay-demo:comment-triage]"))
        self.assertEqual(invocation.metadata.mutation_boundary, "bzr")
        self.assertEqual(invocation.metadata.environment_names, ("BZR_LIVE_API_KEY",))

    def test_create_json_carries_the_server_alias_and_no_cf_fields(self) -> None:
        event = self._event("create-checkout-race")
        invocation = HANDLERS["bug.create"].build(self.context, event)
        path = [a for a in invocation.args if a.startswith("--from-json=")][0].split("=", 1)[1]
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertTrue(document["alias"].startswith("bzr-live-"))
        self.assertNotIn("estimated_time", document)
        self.assertFalse([k for k in document if k.startswith("cf_")])

    def test_create_json_carries_only_declared_fields(self) -> None:
        # The regression guard for the rework AGENTS.md ("Purpose: prove bzr") forced: an
        # earlier draft injected a fixed op_sys/rep_platform pair the scenario never
        # declared, so the run would appear to succeed. The fixture's checksetup answers
        # supply those defaults now, and the document carries only what was declared.
        event = self._event("create-checkout-race")
        invocation = HANDLERS["bug.create"].build(self.context, event)
        path = [a for a in invocation.args if a.startswith("--from-json=")][0].split("=", 1)[1]
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertNotIn("op_sys", document)
        self.assertNotIn("rep_platform", document)
        self.assertLessEqual(set(document), {
            "alias", "product", "component", "summary", "description", "version",
            "target_milestone", "assignee", "cc", "keywords", "groups", "blocks",
            "depends_on",
        })

    def test_update_computes_add_and_remove_deltas(self) -> None:
        # bug view reports cc [keep@x, drop@x]; the event declares [keep@x, join@x]
        ...
        self.assertIn("--cc-add=join@x", invocation.args)
        self.assertIn("--cc-remove=drop@x", invocation.args)

    def test_flag_renders_bugzilla_syntax(self) -> None:
        # The fixture's flag-review event is actor triager requesting review of
        # reporter, so this asserts the suffix comes from `requestee` and not from
        # `event.actor` -- a discrimination the assertion loses if the two coincide.
        invocation = HANDLERS["bug.flag"].build(self.context, self._event("flag-review"))
        self.assertIn("--flag=review?(reporter@example.test)", invocation.args)

    def test_create_resolves_the_new_bug_id(self) -> None:
        ids = HANDLERS["bug.create"].resolved_ids(
            self._event("create-checkout-race"), {"id": 41})
        self.assertEqual(ids, {"bug:checkout-race": 41})

    # Any test that builds a bug.update must queue a bug-view reply first: build reads
    # the bug through _bug_object, and _FakeRun's empty-queue default of (0, {}, None)
    # has no "id", so build would refuse before the assertion runs.
    def test_no_argument_ever_carries_the_key(self) -> None:
        for event in self.scenario.events:
            invocation = HANDLERS[event.action].build(self.context, event)
            for argument in invocation.args + invocation.positionals:
                self.assertNotIn("SECRET-KEY", argument)
```

### Step 3.2 — confirm it fails

```
uv run --python 3.11 python -m unittest tests.test_replay -v
```

Expect `AttributeError: 'BugCommentHandler' object has no attribute 'build'`.

### Step 3.3 — implement `build` and `resolved_ids`

Add to `actions.py` (imports first):

```python
from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path

from ..provision.executor import custom_field_name
from ..scenario import InvocationMetadata, JsonValue
from .context import KEY_ENV, REST_BOUNDARY, ReplayContext


@dataclass(frozen=True, slots=True)
class Invocation:
    metadata: InvocationMetadata
    args: tuple[str, ...] = ()
    positionals: tuple[str, ...] = ()
    target_id: int | None = None
    values: Mapping[str, object] | None = None


def _bzr(operation: str, args, positionals=()) -> Invocation:
    args, positionals = tuple(args), tuple(str(p) for p in positionals)
    return Invocation(
        InvocationMetadata("bzr", operation, args + positionals, (KEY_ENV,)),
        args, positionals)


def _delta(declared: list, observed: list) -> tuple[list, list]:
    """Bugzilla list fields are edited by add/remove, so a declared set is a delta."""
    add = [item for item in declared if item not in observed]
    remove = [item for item in observed if item not in declared]
    return add, remove


def _bug_object(payload) -> dict | None:
    """The one bug object `bzr bug view` returns for a single ID, or None.

    Exactly one shape is accepted. Every read this engine issues names one bug, and
    bzr short-circuits a single ID to `view_single` before any batch handling
    (`src/commands/bug/view.rs:89-92` at b80303b7), so the batch wrapper is
    unreachable here; `BzrClient._payload` has already unwrapped the `data` envelope.
    Accepting a second shape would let a future change in bzr's reply be absorbed
    silently instead of failing loudly, which is the accommodation this repository
    reverted in 04c3ac7. Anything else returns None and the caller refuses.
    """
    if isinstance(payload, dict) and "id" in payload:
        return payload
    return None
```

`BugCreateHandler.build` — assemble the `--from-json` document, dropping absent keys:

```python
    def build(self, context, event):
        values = event.expected_postcondition["values"]
        document: dict[str, object] = {
            "alias": values["server_alias"],
            "product": values["product"].name,
            "component": values["component"].name,
            "summary": values["summary"],
            "description": values["description"],
            "version": values["version"].name,
        }
        # Nothing else goes in. bzr wants op_sys/rep_platform on installations that set no
        # defaults; the honest fix is the fixture's checksetup answers (Task 0), not a value
        # the scenario never declared. See AGENTS.md, "Purpose: prove bzr".
        if values["milestone"] is not None:
            document["target_milestone"] = values["milestone"].name
        if values["assignee"] is not None:
            document["assignee"] = context.actor_email(values["assignee"])
        cc = [context.actor_email(ref) for ref in values["cc"]]
        keywords = [ref.name for ref in values["keywords"]]
        groups = [ref.name for ref in values["groups"]]
        for key, collection in (
            ("cc", cc), ("keywords", keywords), ("groups", groups),
            ("blocks", context.resolve_all(values["blocks"])),
            ("depends_on", context.resolve_all(values["depends_on"])),
        ):
            if collection:
                document[key] = collection
        path = context.json_file(f"create-{event.name}", document)
        return _bzr("bug create", ["bug", "create", f"--from-json={path}"])

    def resolved_ids(self, event, output):
        bug = _bug_object(output)
        if bug is None or not isinstance(bug.get("id"), int) or bug["id"] <= 0:
            raise ReplayError(
                f"event {event.name!r}: bzr bug create returned no bug id")
        return {f"bug:{event.creates.name}": bug["id"]}
```

`BugUpdateHandler.build` — read the bug, then emit set flags and deltas:

```python
    def build(self, context, event):
        values = event.expected_postcondition["values"]
        bug_ref = event.expected_postcondition["target"]
        bug_id = context.resolve(bug_ref)
        observed = _bug_object(
            context.client(event.actor).read(["bug", "view"], positionals=[str(bug_id)]))
        if observed is None:
            raise ReplayError(
                f"event {event.name!r}: cannot read bug {bug_id} to compute the update")
        args = ["bug", "update"]
        for key, flag in (
            ("summary", "--summary"), ("status", "--status"),
            ("resolution", "--resolution"),
        ):
            if key in values:
                args.append(f"{flag}={values[key]}")
        if "assignee" in values:
            args.append(
                "--reset-assigned-to" if values["assignee"] is None
                else f"--assignee={context.actor_email(values['assignee'])}")
        if values.get("duplicate_of") is not None:
            args.append(f"--dupe-of={context.resolve(values['duplicate_of'])}")
        if values.get("milestone") is not None:
            args.append(f"--target-milestone={values['milestone'].name}")
        for key, flag in (
            ("estimated_hours", "--estimated-time"),
            ("remaining_hours", "--remaining-time"),
        ):
            if key in values:
                args.append(f"{flag}={values[key]}")
        for key, add_flag, remove_flag, project in (
            ("cc", "--cc-add", "--cc-remove", lambda r: context.actor_email(r)),
            ("keywords", "--keywords-add", "--keywords-remove", lambda r: r.name),
            ("depends_on", "--depends-on-add", "--depends-on-remove",
             lambda r: context.resolve(r)),
            ("blocks", "--blocks-add", "--blocks-remove", lambda r: context.resolve(r)),
        ):
            if key not in values:
                continue
            declared = [project(ref) for ref in values[key]]
            add, remove = _delta(declared, list(observed.get(key) or []))
            args += [f"{add_flag}={item}" for item in add]
            args += [f"{remove_flag}={item}" for item in remove]
        return _bzr("bug update", args, [bug_id])
```

The remaining six `build` methods, each two to eight lines:

```python
# BugCommentHandler
    def build(self, context, event):
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(event.expected_postcondition["target"])
        path = context.text_file(
            f"comment-{event.name}",
            render_marker(values["body"], event.reconciliation_marker))
        args = ["comment", "add", f"--body-file={path}"]
        if values["private"]:
            args.append("--private")
        return _bzr("comment add", args, [bug_id])

# BugAttachHandler
    def build(self, context, event):
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(values["bug"])
        path = context.asset_file(values["asset"].name, values["asset_sha256"])
        summary = render_attachment_summary(
            values["description"], event.reconciliation_marker, values["asset_sha256"])
        args = ["attachment", "upload", f"--summary={summary}",
                f"--content-type={values['content_type']}"]
        if values["private"]:
            args.append("--private")
        return _bzr("attachment upload", args, [bug_id, path])

    def resolved_ids(self, event, output):
        if not isinstance(output, dict) or not isinstance(output.get("id"), int):
            raise ReplayError(
                f"event {event.name!r}: bzr attachment upload returned no attachment id")
        return {f"attachment:{event.creates.name}": output["id"]}

# BugWorktimeHandler
    def build(self, context, event):
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(values["bug"])
        path = context.text_file(
            f"worktime-{event.name}",
            render_marker(values["comment"], event.reconciliation_marker))
        return _bzr("bug update", [
            "bug", "update", f"--work-time={values['hours']}",
            f"--comment-file={path}"], [bug_id])

# BugCustomFieldHandler
    def build(self, context, event):
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(values["bug"])
        body = {
            custom_field_name(assignment["field"].name):
                list(assignment["value"]) if isinstance(assignment["value"], tuple)
                else assignment["value"]
            for assignment in values["values"]
        }
        return Invocation(
            InvocationMetadata(
                REST_BOUNDARY, f"PUT rest/bug/{bug_id}", tuple(sorted(body)), ()),
            target_id=bug_id, values=body)

# BugFlagHandler
    def build(self, context, event):
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(values["bug"])
        spec = f"{values['flag_type'].name}{values['status']}"
        if values["requestee"] is not None:
            spec += f"({context.actor_email(values['requestee'])})"
        return _bzr("bug update", ["bug", "update", f"--flag={spec}"], [bug_id])

# AttachmentUpdateHandler
    def build(self, context, event):
        values = event.expected_postcondition["values"]
        attachment_id = context.resolve(values["attachment"])
        args = ["attachment", "update",
                "--obsolete" if values["obsolete"] else "--no-obsolete"]
        if "description" in values:
            args.append(f"--summary={values['description']}")
        return _bzr("attachment update", args, [attachment_id])
```

Base-class defaults on `ActionHandler`:

```python
    def build(self, context, event) -> Invocation:
        raise NotImplementedError(self.action)

    def resolved_ids(self, event, output) -> dict[str, int]:
        return {}
```

Export `Invocation` from `__init__.py` alongside `HANDLERS`.

### Step 3.4 — confirm it passes

```
uv run --python 3.11 python -m unittest tests.test_replay -v
```

Expect `OK`, thirty-three tests.

### Step 3.5 — commit

```
make check && make test
git add src/bzr_live/replay tests/test_replay.py
git commit -m "feat(replay): build one bzr or REST invocation per event"
```

**Acceptance criteria.** Every action builds an `Invocation` whose `metadata` names the right
boundary and, for bzr, `("BZR_LIVE_API_KEY",)`. Comment bodies and the create JSON go to
mode-0600 workspace files. Append-class text carries the loader's marker; set-class text does
not. Update deltas are computed from a `bzr bug view` read. No argument or positional
contains an API key.

## Task 4 — reconciliation

Adds `Reconciliation` and `reconcile` to `src/bzr_live/replay/actions.py`. Tests appended
to `tests/test_replay.py`.

**Where this fits.** `ReplayEngine` calls `reconcile` whenever an invocation did not clearly
succeed and whenever it resumes an in-flight record.

**Interfaces produced:**

```python
@dataclass(frozen=True, slots=True)
class Reconciliation:
    next_action: str                 # "advance" | "retry" | "stop"
    output: JsonValue
    resolved_ids: Mapping[str, int]
    detail: str

class ActionHandler:
    def reconcile(self, context: ReplayContext, event: PlannedEvent) -> Reconciliation
```

**Interfaces consumed:** `ReplayContext.client(actor).read(args, positionals)` returning the
decoded payload or `None` when the object is absent, and `context.resolve`. Reply shapes
confirmed against bzr `b80303b7`: `bug view` yields the bug object with `id`, `summary`,
`status`, `resolution`, `dupe_of`, `assigned_to`, `target_milestone`, `keywords`, `blocks`,
`depends_on`, `cc`, `flags`, and any `cf_*`; `comment list` yields comments with `id` and
`text`; `attachment list` yields attachments with `id`, `summary`, `is_obsolete`.

### Step 4.1 — write the failing tests

```python
class ReconcileTest(unittest.TestCase):
    def test_create_adopts_an_existing_alias(self) -> None:
        run = _FakeRun([(0, {"id": 41, "summary": "Checkout race"}, None)])
        result = HANDLERS["bug.create"].reconcile(self._context(run), self._create_event())
        self.assertEqual(result.next_action, "advance")
        self.assertEqual(result.resolved_ids, {"bug:checkout-race": 41})

    def test_create_retries_when_the_alias_is_absent(self) -> None:
        # An unknown alias is exit 4 + api_code 100, asserted against a live Bugzilla in
        # bzr's tests/functional/phases/08e-bugs-restricted-access.sh:289-292 (101 for a
        # numeric id); ReplayContext.read_bug is what maps it to absent.
        run = _FakeRun([(4, None, 100)])
        result = HANDLERS["bug.create"].reconcile(self._context(run), self._create_event())
        self.assertEqual(result.next_action, "retry")

    def test_create_raises_when_the_bug_is_access_denied(self) -> None:
        run = _FakeRun([(4, None, 102)])     # not absence: the actor may not see it
        with self.assertRaises(ProvisionError):
            HANDLERS["bug.create"].reconcile(self._context(run), self._create_event())

    def test_append_adopts_exactly_one_marker(self) -> None:
        run = _FakeRun([(0, [{"id": 5, "text": "body\n\n[bzr-live:replay-demo:comment-triage]"}], None)])
        result = HANDLERS["bug.comment"].reconcile(self._context(run), self._comment_event())
        self.assertEqual(result.next_action, "advance")

    def test_append_retries_when_no_marker_is_present(self) -> None:
        run = _FakeRun([(0, [{"id": 5, "text": "unrelated"}], None)])
        result = HANDLERS["bug.comment"].reconcile(self._context(run), self._comment_event())
        self.assertEqual(result.next_action, "retry")

    def test_append_stops_on_two_markers(self) -> None:
        marked = {"id": 5, "text": "[bzr-live:replay-demo:comment-triage]"}
        run = _FakeRun([(0, [marked, dict(marked, id=6)], None)])
        result = HANDLERS["bug.comment"].reconcile(self._context(run), self._comment_event())
        self.assertEqual(result.next_action, "stop")
        self.assertIn("CONFIRM_RESET=1 make reset", result.detail)

    def test_set_advances_when_the_postcondition_matches(self) -> None: ...
    def test_set_retries_when_the_postcondition_differs(self) -> None: ...
    def test_set_retries_when_a_declared_field_is_unreadable(self) -> None: ...

    def test_flag_clear_advances_on_absence(self) -> None:
        # Status X is the one flag postcondition proved by absence: the declared clear
        # committed exactly when no entry with that type name is present.
        run = _FakeRun([(0, {"id": 41, "flags": []}, None)])
        result = HANDLERS["bug.flag"].reconcile(self._context(run), self._flag_event("X"))
        self.assertEqual(result.next_action, "advance")

    def test_attachment_update_reads_the_attachment_by_id(self) -> None:
        # The summary must equal the description obsolete-notes declares, because
        # reconcile compares it: build emits --summary=<description>, so a reply whose
        # summary still held the old value is a declared change that did not commit.
        run = _FakeRun([
            (0, {"id": 7, "summary": "Superseded triage notes", "is_obsolete": True},
             None)])
        context = self._context(run)
        # The id table is keyed by the *attachment alias* the attach event declares
        # (`triage-notes`), not by the asset name (`notes`) -- the same distinction that
        # governs asset_file's key. obsolete-notes resolves attachment:triage-notes.
        context.adopt({"attachment:triage-notes": 7})
        result = HANDLERS["attachment.update"].reconcile(context, self._obsolete_event())
        self.assertEqual(result.next_action, "advance")
        self.assertEqual(run.calls[0]["argv"][-2:], ["--", "7"])
        self.assertIn("view", run.calls[0]["argv"])
```

### Step 4.2 — confirm it fails

Expect `AttributeError: 'BugCreateHandler' object has no attribute 'reconcile'`.

### Step 4.3 — implement `reconcile`

Three shared helpers plus one method per handler:

```python
AMBIGUOUS_HINT = (
    "the fixture cannot be reconciled automatically; reset it "
    "(CONFIRM_RESET=1 make reset) and replay")


def _marker_count(entries, field: str, marker: str) -> tuple[int, int | None]:
    token = f"[{marker}]"
    hits = [entry for entry in entries or []
            if isinstance(entry, dict) and token in (entry.get(field) or "")]
    if len(hits) != 1:
        return len(hits), None
    return 1, hits[0].get("id")


def _append_result(event, count, entry_id, output):
    if count == 1:
        return Reconciliation("advance", output, {}, "")
    if count == 0:
        return Reconciliation(
            "retry", output, {}, f"event {event.name!r} did not commit")
    return Reconciliation(
        "stop", output, {},
        f"event {event.name!r} matches {count} results for marker "
        f"{event.reconciliation_marker!r}; {AMBIGUOUS_HINT}")


def _entries(payload) -> list:
    """The list `comment list` / `attachment list` returns, or an empty one.

    One shape, for the same reason as `_bug_object`: a wrapper branch here would be
    dead code that silently absorbs a reply-shape change instead of surfacing it.
    """
    return payload if isinstance(payload, list) else []
```

- `BugCreateHandler.reconcile` calls `context.read_bug(event.actor, [server_alias])`, which
  passes `absent_codes=BUG_ABSENT_CODES` so an unknown alias reads as absent and an
  access-denied answer still raises. `None` (absent) yields `retry`; an object with a
  positive integer `id` yields `advance` plus `{f"bug:{event.creates.name}": id}`; anything
  else yields `stop` with "returned no usable bug id; " + `AMBIGUOUS_HINT`.
- `BugCommentHandler.reconcile` and `BugWorktimeHandler.reconcile` read
  `["comment", "list"]` positional `<bug id>`, then
  `_marker_count(_entries(payload), "text", event.reconciliation_marker)` and
  `_append_result`.
- `BugAttachHandler.reconcile` reads `["attachment", "list"]` positional `<bug id>`, matches
  on `summary`, and on a single hit returns `advance` with
  `{f"attachment:{event.creates.name}": hit_id}`.
- `BugUpdateHandler.reconcile` calls `context.read_bug(event.actor, [str(bug_id)])` and compares
  each declared value against the mapping in the spec's "Reconciliation" section:
  `summary`/`status`/`resolution` as strings, `assignee` against `assigned_to` by email,
  `duplicate_of` against `dupe_of` by resolved id, `milestone` against `target_milestone` by
  name, and `cc`/`keywords`/`depends_on`/`blocks` as sets. `estimated_hours`,
  `remaining_hours`, and a null `assignee` have nothing to compare and force `retry`. All
  comparable values equal yields `advance`; anything else yields `retry` naming the first
  field that differed.
- `BugCustomFieldHandler.reconcile` calls `context.read_bug` and compares each
  `custom_field_name(slug)` key.
- `BugFlagHandler.reconcile` calls `context.read_bug` and searches `flags` for an entry whose
  `name` equals the flag type and whose `status` equals the declared status. Status `X` is the
  exception: it clears the flag, so no entry with that status can ever exist. Its declared
  postcondition is the *absence* of an entry with that type name, and absence is what yields
  `advance`; comparing for an `X` entry would make a flag-clear unable to reconcile at all.
- `AttachmentUpdateHandler.reconcile` reads `["attachment", "view"]` with the resolved
  attachment id as its positional and compares `is_obsolete` and, where declared, `summary`
  off that single object. It cannot list the bug's attachments: the loader normalizes this
  event to `{attachment, obsolete, description?}` with the attachment as the postcondition
  target, so no bug reference exists, and `bzr attachment list` takes only a bug id.
  `bzr attachment view <id>` returns `id`, `summary` and `is_obsolete`
  (src/cli/attachment.rs:178-181, src/types/attachment.rs:8-38 at bzr b80303b7).

Every set-class `reconcile` returns only `advance` or `retry`, never `stop`.

### Step 4.4 — confirm it passes

```
uv run --python 3.11 python -m unittest tests.test_replay -v
```

Expect `OK`, forty-four tests.

### Step 4.5 — commit

```
make check && make test
git add src/bzr_live/replay tests/test_replay.py
git commit -m "feat(replay): reconcile each recovery class against the fixture"
```

**Acceptance criteria.** A create reconciles by alias through `read_bug`, treating api_code
100/101 as absent and letting 102 raise; an append by marker count, with two or more yielding
`stop` and a message naming `CONFIRM_RESET=1 make reset`; a set by comparing readable declared
values, never yielding `stop`; `attachment.update` by `bzr attachment view <attachment id>`.

## Task 5 — `ReplayEngine`

Creates `src/bzr_live/replay/engine.py`. Tests appended to `tests/test_replay.py`.

**Where this fits.** The engine is what `__main__` calls. It owns preconditions, the ordered
loop, and every journal write.

**Interfaces produced:**

```python
class ReplayEngine:
    def __init__(self, scenario: ValidatedScenario, context: ReplayContext,
                 store: JournalStore, journal_dir: str | Path, *,
                 out: Callable[[str], None] = print) -> None
    def replay(self) -> list[tuple[str, str]]     # (status, event name)
    def resume(self) -> list[tuple[str, str]]
```

**Interfaces consumed:** `bzr_live.scenario.JournalStore(state_dir)` with
`.read(event, attempt=None)`, `.write_in_flight(record, known_secrets=())`,
`.replace_completed(record, known_secrets=())`, and context-manager support;
`bzr_live.scenario.InFlightRecord(scenario_digest, event, attempt, actor, action_class,
expected_postcondition, reconciliation_marker)`; `bzr_live.scenario.CompletedRecord(...same
seven..., invocation, handler_output, exit_status, resolved_ids, next_safe_action)`;
`HANDLERS`, `Invocation`, `Reconciliation` from Tasks 2–4; `ProvisionError`.

### Step 5.1 — write the failing tests

```python
class EngineTest(unittest.TestCase):
    def test_replay_refuses_a_non_empty_journal(self) -> None: ...
    def test_replay_refuses_when_a_server_alias_already_exists(self) -> None: ...
    def test_replay_executes_every_event_in_order(self) -> None: ...
    def test_resume_refuses_a_changed_digest(self) -> None: ...
    def test_resume_adopts_a_committed_in_flight_create(self) -> None: ...
    def test_resume_retries_an_in_flight_create_the_server_never_saw(self) -> None: ...
    def test_resume_refuses_a_recorded_stop_without_querying(self) -> None: ...
    def test_resume_skips_completed_events_and_rebuilds_the_id_table(self) -> None: ...
    def test_an_unsupported_payload_stops_before_any_mutation(self) -> None: ...
    def test_no_journal_record_contains_an_api_key(self) -> None: ...
    def test_replay_refuses_a_record_for_an_event_the_scenario_dropped(self) -> None: ...
    def test_resume_refuses_a_record_for_an_event_the_scenario_dropped(self) -> None: ...
    def test_pristine_sweep_refuses_a_bug_the_actor_cannot_see(self) -> None: ...
    def test_resume_refuses_a_pre_existing_alias_for_an_unjournalled_event(self) -> None: ...

    def test_an_exit_zero_reply_with_no_usable_id_reconciles(self) -> None:
        """ADR 0006's third reconciliation trigger, distinct from the other two.

        The create exits 0 but the reply carries no id, so `resolved_ids` raises; the
        alias read that follows finds the bug, proving the mutation committed. The run
        continues rather than aborting on "returned no bug id".
        """
        run = _FakeRun([(0, {}, None), (0, {"id": 41}, None)])
        ...
        self.assertEqual(engine.replay(), ...)
        record = store.read("create-checkout-race")
        self.assertEqual(record.next_safe_action, "advance")
        self.assertEqual(record.resolved_ids, {"bug:checkout-race": 41})

    def test_a_failing_reconciliation_read_leaves_the_in_flight_record(self) -> None:
        """ADR 0006: no completed record is written over a read that failed."""
        # Queue a failing mutation, then a reconciliation read that also fails.
        ...
        with self.assertRaises(ProvisionError):
            engine.replay()
        record = store.read("create-checkout-race")
        self.assertIsInstance(record, InFlightRecord)
        self.assertNotIsInstance(record, CompletedRecord)
```

`test_resume_refuses_a_recorded_stop_without_querying` asserts the fake `run` recorded zero
calls after the refusal, which is what "without re-querying" means.

### Step 5.2 — confirm it fails

Expect `ImportError: cannot import name 'ReplayEngine'`.

### Step 5.3 — write `engine.py`

```python
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from ..provision.adapters import ProvisionError
from ..scenario import CompletedRecord, InFlightRecord, JournalStore, ValidatedScenario
from ..scenario.journal import _ATTEMPT_FILE
from .actions import AMBIGUOUS_HINT, HANDLERS, Reconciliation
from .context import ReplayContext, ReplayError

_RECONCILED_EXIT = -1     # the invocation's status was never observed (ADR 0006)


class ReplayEngine:
    def __init__(self, scenario, context, store, journal_dir, *,
                 out: Callable[[str], None] = print):
        self._scenario = scenario
        self._context = context
        self._store = store
        self._journal_dir = journal_dir
        self._out = out

    # --- entry points ------------------------------------------------------

    def replay(self) -> list[tuple[str, str]]:
        latest = self._check_local_preconditions()
        self._require_empty_journal()
        self._sweep_pristine()
        return self._run(latest)

    def _require_empty_journal(self) -> None:
        """Any attempt file at all, not just one this scenario still names.

        Scanning only the current event names would let a journal survive an event
        rename unseen — and the digest check cannot fire on a record no event name
        reaches, because it reads records through those same names.
        """
        for name in sorted(os.listdir(self._journal_dir)):
            if _ATTEMPT_FILE.fullmatch(name) is not None:
                raise ReplayError(
                    f"this scenario has already been replayed under this state root "
                    f"(found journal record {name!r}); use resume, or reset the fixture "
                    "and remove the journal directory")

    def resume(self) -> list[tuple[str, str]]:
        return self._run(self._check_local_preconditions())

    # --- preconditions -----------------------------------------------------

    def _check_local_preconditions(self) -> dict[str, object]:
        for event in self._scenario.events:
            HANDLERS[event.action].check_supported(event)
        self._require_no_stray_records()
        latest: dict[str, object] = {}
        for event in self._scenario.events:
            record = self._store.read(event.name)
            if record is not None and record.scenario_digest != self._scenario.digest:
                raise ReplayError(
                    f"event {event.name!r} was journalled under scenario digest "
                    f"{record.scenario_digest} but this scenario hashes to "
                    f"{self._scenario.digest}; restore the scenario, or reset the "
                    "fixture (CONFIRM_RESET=1 make reset) and replay")
            latest[event.name] = record
        return latest

    def _require_no_stray_records(self) -> None:
        """A record the scenario's event names no longer reach is still binding.

        The digest check below reads records *through* current event names, so a
        journalled event since renamed is unreachable and its digest never compared --
        the same blind spot `_require_empty_journal` closes for `replay`. Completion
        criterion 4 says `resume` refuses unless the digest matches, unqualified, so
        the scan belongs on both paths. `_ATTEMPT_FILE`'s group 1 is the event name
        (`src/bzr_live/scenario/journal.py:45`).
        """
        known = {event.name for event in self._scenario.events}
        for name in sorted(os.listdir(self._journal_dir)):
            match = _ATTEMPT_FILE.fullmatch(name)
            if match is not None and match.group(1) not in known:
                raise ReplayError(
                    f"journal record {name!r} belongs to an event this scenario no "
                    "longer names, so its digest cannot be checked; restore the "
                    "scenario, or reset the fixture (CONFIRM_RESET=1 make reset) and "
                    "remove the journal directory")

    def _require_absent(self, event) -> None:
        """Refuse if this event's bug already exists. No-op for non-create events."""
        if event.action != "bug.create":
            return
        alias = event.expected_postcondition["values"]["server_alias"]
        # read_bug lets api_code 100/101 mean absent; 102 (access denied) still raises,
        # so an invisible pre-existing bug refuses instead of passing.
        if self._context.read_bug(event.actor, [alias]) is not None:
            raise ReplayError(
                f"bug alias {alias} (event {event.name!r}) already exists in the "
                "fixture; replay requires the pristine baseline — run "
                "scripts/checkpoint restore pristine, then replay")

    def _sweep_pristine(self) -> None:
        for event in self._scenario.events:
            self._require_absent(event)

    # --- the loop ----------------------------------------------------------

    def _run(self, latest) -> list[tuple[str, str]]:
        report: list[tuple[str, str]] = []
        for event in self._scenario.events:
            status = self._advance(event, latest[event.name])
            report.append((status, event.name))
            self._out(f"{status} {event.name}")
        done = sum(1 for status, _ in report if status != "skipped")
        self._out(f"summary: {done} executed, {len(report) - done} already complete")
        return report

    def _advance(self, event, record) -> str:
        if record is None:
            # No journal record means no proof this run attempted the event, under either
            # subcommand — so adoption is not available to it and a present alias refuses.
            # replay's up-front sweep is the same check in fail-fast form.
            self._require_absent(event)
            return self._execute(event, 1)
        if isinstance(record, CompletedRecord):
            if record.next_safe_action == "advance":
                self._context.adopt(record.resolved_ids)
                return "skipped"
            if record.next_safe_action == "stop":
                raise ReplayError(
                    f"event {event.name!r} was recorded as ambiguous by an earlier run; "
                    f"{AMBIGUOUS_HINT}")
            return self._execute(event, record.attempt + 1)
        # An in-flight record from an earlier run: settle it, then continue per its answer.
        result = self._settle(event, record.attempt)
        if result.next_action == "advance":
            return "resumed"
        if result.next_action == "stop":
            raise ReplayError(f"event {event.name!r}: {result.detail}")
        return self._execute(event, record.attempt + 1)

    def _execute(self, event, attempt) -> str:
        self._context.actor_key(event.actor)      # fail fast, and register the secret
        invocation = HANDLERS[event.action].build(self._context, event)
        self._store.write_in_flight(
            InFlightRecord(
                self._scenario.digest, event.name, attempt, event.actor,
                event.action_class, event.expected_postcondition,
                event.reconciliation_marker),
            known_secrets=self._context.known_secrets)
        # ADR 0006 names three triggers for an in-run reconciliation, and all three land
        # here: a non-zero exit and an unparseable reply both raise ProvisionError out of
        # BzrClient, and an exit-0 reply carrying no usable identifier raises ReplayError
        # out of resolved_ids. The third is inside the `try` for that reason — leaving it
        # out would abort the run with "returned no bug id" on a create that may well have
        # committed, and leave the answer to the operator's next `resume`.
        try:
            output = self._context.invoke(event.actor, invocation)
            ids = HANDLERS[event.action].resolved_ids(event, output)
        except (ProvisionError, ReplayError) as exc:
            result = self._settle(event, attempt, invocation=invocation)
            if result.next_action == "advance":
                return "reconciled"
            # One execution per event per run: the next attempt belongs to `resume`.
            raise ReplayError(
                f"event {event.name!r} failed: {exc}. {result.detail}") from None
        self._write_completed(event, attempt, invocation, output, 0, ids, "advance")
        self._context.adopt(ids)
        return "executed"

    def _settle(self, event, attempt, invocation=None) -> Reconciliation:
        """Reconcile against the fixture and record the outcome.

        Returns `retry` or `stop` rather than raising, so the caller decides what each
        means. A failing reconciliation *read* still propagates: ADR 0006 requires that
        no completed record be written over a read that failed, leaving the in-flight
        record for a later `resume`.
        """
        result = HANDLERS[event.action].reconcile(self._context, event)
        invocation = invocation or HANDLERS[event.action].build(self._context, event)
        self._write_completed(
            event, attempt, invocation, result.output, _RECONCILED_EXIT,
            result.resolved_ids, result.next_action)
        if result.next_action == "advance":
            self._context.adopt(result.resolved_ids)
        return result

    def _write_completed(self, event, attempt, invocation, output, exit_status,
                         resolved_ids, next_action) -> None:
        self._store.replace_completed(
            CompletedRecord(
                self._scenario.digest, event.name, attempt, event.actor,
                event.action_class, event.expected_postcondition,
                event.reconciliation_marker, invocation.metadata, output,
                exit_status, dict(resolved_ids), next_action),
            known_secrets=self._context.known_secrets)
```

Two details the implementer must get right:

- `_settle` rebuilds the invocation when it was not supplied, because a resumed in-flight
  record needs an `InvocationMetadata` for its completed record and the original run's is
  gone. Rebuilding is deterministic for every handler *except* `bug.update`, whose delta
  depends on a fresh read; that is correct, because the recorded metadata then describes what
  the resumed state actually implies.
- `_settle` turns a reconciliation *answer* into a return value rather than an exception, so
  the caller controls what a `retry` means; a failing reconciliation *read* still propagates
  out of it, which is what leaves the in-flight record for `resume`. On a
  resumed in-flight record it means "execute attempt *n+1* now", and after a failure this run
  already caused it means "abort — the next attempt belongs to `resume`". Folding the raise
  into `_settle` would collapse those two and would also swallow the boundary's own error
  message.
- `ReplayContext.invoke(actor, invocation)` is added in this task:

```python
    def invoke(self, actor, invocation):
        if invocation.metadata.mutation_boundary == REST_BOUNDARY:
            return self.rest(actor, invocation.target_id, dict(invocation.values))
        return self.client(actor).write(
            list(invocation.args), list(invocation.positionals) or None)
```

Rename `actions._AMBIGUOUS` to the public `AMBIGUOUS_HINT` in Task 4's code and import it
here. The refusal wording exists once, in `actions.py`.

### Step 5.4 — confirm it passes

```
uv run --python 3.11 python -m unittest tests.test_replay -v
```

Expect `OK`, sixty tests.

### Step 5.5 — commit

```
make check && make test
git add src/bzr_live/replay tests/test_replay.py
git commit -m "feat(replay): drive the event loop with journalled resume"
```

**Acceptance criteria.** `replay` refuses any attempt file in the journal directory —
including one for an event the scenario no longer names — and a pre-existing server alias,
both before any mutation; a bug the sweeping actor cannot see (api_code 102) refuses rather
than reading as absent. `resume` refuses a digest mismatch and a recorded `stop` without
querying the server. An in-flight record reconciles to adoption or a fresh attempt. Completed
`advance` records are skipped and their IDs adopted. No journal record contains an API key.

## Task 6 — CLI, fixture, and wiring

Creates `src/bzr_live/replay/__main__.py` and `tests/replay_smoke.sh`; changes `Makefile`,
`.github/workflows/scenario-contract.yml`, `README.md`.

**Where this fits.** Last task: the operator-facing surface and the gate coverage.

**Interfaces produced:** `bzr_live.replay.__main__.main(argv=None) -> int`.

**Interfaces consumed:** `ReplayEngine`, `ReplayContext`, `KeyStore`, `JournalStore`,
`load_scenario`, `ReplayError`, `ProvisionError`, `ScenarioValidationError`.

### Step 6.1 — write the CLI

```python
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from ..provision.adapters import ProvisionError
from ..provision.keys import KeyStore
from ..scenario import JournalStore, ScenarioValidationError, load_scenario
from .context import ReplayContext, ReplayError
from .engine import ReplayEngine


def _parse(argv):
    parser = argparse.ArgumentParser(
        prog="python -m bzr_live.replay",
        description="Replay a scenario's events into the local Bugzilla fixture.")
    parser.add_argument("command", choices=("replay", "resume"))
    parser.add_argument("scenario_dir")
    parser.add_argument("--state-root", default="./state")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/")
    parser.add_argument("--bzr", default="bzr")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    options = _parse(argv)
    try:
        scenario = load_scenario(options.scenario_dir)
        keys = KeyStore(options.state_root)
        journal = Path(options.state_root) / "journal" / scenario.name
        journal.parent.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory() as workspace:
            Path(workspace).chmod(0o700)
            context = ReplayContext(
                scenario, keys, bzr_path=options.bzr, base_url=options.base_url,
                workspace=workspace)
            with JournalStore(journal) as store:
                engine = ReplayEngine(scenario, context, store, journal)
                getattr(engine, options.command)()
    except (ReplayError, ProvisionError, ScenarioValidationError) as exc:
        print(f"replay failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### Step 6.1a — test `main()`

The repository already tests a package `__main__` directly — `tests/test_provision.py:645-690`
imports `bzr_live.provision.__main__` and calls `cli.main([...])`. Copy that shape rather than
relying on the manual command in Step 6.3:

```python
class MainTest(unittest.TestCase):
    def test_missing_actor_key_exits_one_with_a_replay_failed_message(self) -> None:
        from bzr_live.replay import __main__ as cli
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = cli.main([
                "replay", str(FIXTURE), "--state-root", str(root / "state")])
        self.assertEqual(code, 1)
        self.assertTrue(errors.getvalue().startswith("replay failed:"))
        self.assertIn("no API key for actor", errors.getvalue())

    def test_the_journal_lands_under_the_scenario_name(self) -> None:
        from bzr_live.replay import __main__ as cli
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        with contextlib.redirect_stderr(io.StringIO()):
            cli.main(["replay", str(FIXTURE), "--state-root", str(root / "state")])
        self.assertTrue((root / "state" / "journal" / "replay-demo").is_dir())
```

`contextlib`, `io` and `shutil` are already in Step 1.1's import block; no new imports.

### Step 6.1b — write the operator-run live smoke

`tests/replay_smoke.sh`, modelled on `tests/provision_smoke.sh` (same `BZR_LIVE_BZR`
requirement, same `.env` port and admin-email fallbacks, same `mktemp -d` state root under a
cleanup trap). It provisions the replay fixture, replays it, and then asserts the three facts
the unit suite cannot reach:

1. the first `bug.create` succeeded while declaring no `op_sys`/`rep_platform` — proving
   Task 0's `defaultplatform`/`defaultopsys` answers make an honest create work;
2. `bzr --json bug view <server_alias>` resolves to the id that create returned — proving the
   `alias` key round-trips here rather than silently no-opping as it does on bzr's
   alias-disabled containers; and
3. a second create declaring that same alias **fails** — proving Bugzilla enforces alias
   uniqueness. It also uploads an attachment whose rendered summary is 256 bytes and records
   the observed exit code and message, settling the `TINYTEXT` ceiling against the running
   server rather than against a schema file alone. That is the property the whole `unique-create` recovery class rests on: it is
   what sends a duplicate create into reconciliation instead of quietly producing two bugs
   under one alias, and ADR 0006 records it as inferred until this assertion runs.

Read the expected `server_alias` out of the loaded scenario rather than recomputing the hash:

```
uv run --python 3.11 python -c "
from bzr_live.scenario import load_scenario
s = load_scenario('tests/fixtures/replay-scenario')
print(next(e.expected_postcondition['values']['server_alias']
           for e in s.events if e.action == 'bug.create'))"
```

It then probes each `docs/bzr-findings.md` entry still marked *read from source* — most
usefully D1, by attempting a hyphenated flag type — and **prints** each probe's observed exit
code and message under a `findings probe:` prefix. That probe is the fixture doing its actual
job. The script asserts nothing about the register and never writes to it: the operator reads
the output and transcribes the promotion from *read* to *observed* as an ordinary edit. A test
script that rewrote a committed deliverable would dirty the tree on every run and would move
authorship of the register — whose classifications are judgement calls, as D2→G7 and the
withdrawal of G8 both showed — from the operator to an automated writer.

The smoke requires a fixture installed **after** Task 0's checksetup change:
`CONFIRM_RESET=1 make reset && make up` first, since Bugzilla reads those answers only at
install.

If either assertion fails, ADR 0006's "Considered & rejected" already names the fallback —
reconcile creates by a namespaced marker in the description — and taking it is a design
change, not an implementation fix.

### Step 6.2 — widen the guardrail and gate the docs

In `Makefile`, add `tests/replay_smoke.sh` to both the `bash -n` and the `shellcheck` file
lists, add a `replay-smoke` target beside `checkpoint-smoke`, add it to `.PHONY`, and change
the `compileall` line from

```
	@uv run --python 3.11 python -m compileall -q src tests/test_checkpoint.py tests/test_provision.py
```

to

```
	@uv run --python 3.11 python -m compileall -q src tests
```

`tests/` already holds five test modules the named list omitted, so this closes an existing
blind spot rather than only making room for `tests/test_replay.py`.

In `.github/workflows/scenario-contract.yml`, add these three lines to **both** the
`pull_request.paths` and `push.paths` lists, beside the existing ADR and spec entries:

```yaml
      - docs/adr/0006-actor-scoped-event-replay.md
      - docs/workflow/specs/2026-09-01-replay-actor-scoped-events-design.md
      - docs/workflow/plans/2026-09-01-replay-actor-scoped-events.md
```

`src/**` and `tests/**` are already listed, so the new package and tests gate without further
edits.

In `README.md`, add `replay` and `resume` to the command list with the pristine precondition
and the provisioning prerequisite stated in one sentence each.

### Step 6.3 — verify

```
uv run --python 3.11 python -m bzr_live.replay --help
uv run --python 3.11 python -m bzr_live.replay replay tests/fixtures/replay-scenario --state-root "$(mktemp -d)/state"
make check
make test
```

`--help` lists `replay` and `resume` and the four options. The replay run exits 1 with
`replay failed: no API key for actor ...` on stderr, proving the precondition path reaches
the operator. `make check` and `make test` both exit 0; `make test` reports 62 more tests
than the 154-test baseline, i.e. 216 — the per-task figures being 9, 17, 7, 11, 16 and 2. `make replay-smoke` is operator-run against a healthy
`make up` and is not part of either guardrail.

### Step 6.4 — commit

```
git add src/bzr_live/replay tests/replay_smoke.sh tests/test_replay.py Makefile \
    .github/workflows/scenario-contract.yml README.md
git commit -m "feat(replay): add the replay and resume commands"
```

**Acceptance criteria.** `python -m bzr_live.replay replay|resume <dir>` runs and maps every
failure to exit 1 with a `replay failed:` message on stderr. The journal lands in
`<state-root>/journal/<scenario name>/`. The workspace is a mode-0700 temporary directory
removed on exit. `make check` compiles every module under `tests/`. The new ADR, spec and
plan gate the `scenario-contract` workflow.

## Requirement-to-task map

| Spec requirement | Task |
|---|---|
| Actor credential resolution, missing-key message | 1 |
| Symbolic reference → server ID table, rebuild on resume | 1, 5 |
| Workspace files, asset materialization and its checksum re-assertion | 1 |
| Supported-payload table (all refusals) | 2 |
| Reading absence: `absent_codes`, 102 is not absence | 1, 4, 5 |
| Marker rendering for append-class events | 2, 3 |
| One invocation per event, per-action argv and payload | 3 |
| Delta computation for Bugzilla list fields | 3 |
| Reconciliation per recovery class | 4 |
| Ambiguity refusal with a reset instruction | 4 |
| In-flight before, completed after, `exit_status = -1` on reconciliation | 5 |
| Digest binding | 5 |
| Empty-journal (directory-wide) and pristine-sweep preconditions | 5 |
| One execution per event per run | 5 |
| Recorded `stop` refuses on a later resume | 5 |
| Secrets absent from journal records and argv | 1, 3, 5 |
| CLI, exit codes, journal location | 6 |
| Create carries only what the scenario declared; fixture supplies the defaults | 0, 3, 6 |
| A bzr-grounded refusal names the limitation and cites its register entry; a Bugzilla-grounded one names Bugzilla and cites none | 2 |
| Flag-type name refusal, and flag-clear reconciling by absence | 2, 4 |
| Pristine check binds any first execution, not just `replay` | 5 |
| Failing reconciliation read leaves the in-flight record | 5 |
| All three reconciliation triggers, including an exit-0 reply with no usable id | 5 |
| Live proof of the create, the alias round-trip, and alias uniqueness | 6 |
| Gate coverage for the new module, tests, and docs | 6 |

## Deferrals carried into this plan

None yet. Any deferral a `$trial-loop` run disposes of during the design or branch review is
appended here with its owning record path or tracker issue.
