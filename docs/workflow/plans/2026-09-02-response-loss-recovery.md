# Implementation plan — response-loss recovery for the five replay actions

**Goal.** Prove in `make test` that a response lost after the fixture applied a mutation
recovers through `resume` to exactly one semantic result — or refuses with a reset/replay
instruction — for `bug.create`, `bug.update`, `bug.comment`, `bug.attach`, and
`bug.worktime`.

**Architecture.** One new test module, `tests/test_fault_injection.py`. It holds a
stateful in-memory Bugzilla double driven through `bzr`'s own argv, a wrapper that lets one
mutation commit and then raises a `BaseException` (the killed runner), and tests that run
one replay process into that fault and a second, independent `resume` process over the same
journal directory. Nothing under `src/` changes: the double is passed to the
`ReplayContext(run=...)` seam that already exists, so the engine, handlers, and journal
under test are the shipped ones.

**Tech stack.** Python 3.11, `unittest`, no runtime dependencies. `uv` runs it.

Spec: `docs/workflow/specs/2026-09-02-response-loss-recovery-design.md`.
Decision record: `docs/adr/0009-response-loss-fault-injection.md`.
Prior decision this proves: `docs/adr/0006-actor-scoped-event-replay.md`.

Expected implementation size: 560–620 changed lines (M) — counted from this plan's own
transcribed module: imports and the shared fixture base (~150), the parser and fault
wrappers (~100), the double (~175), and the ten tests (~185). An earlier draft of this
line guessed 290–380 before the code was transcribed; the file map is what settles it.

## Global Constraints

- Python 3.11 or newer, run through `uv` (`uv run --python 3.11 …`). The package under
  `src/bzr_live/` has no runtime dependencies and none may be added.
- Prefer ≤100 lines per function, cyclomatic complexity ≤8, and 100-character lines.
- No file under `src/` may be modified. Issue #24's "Proposed approach" requires the
  shipped replay path to be the code under test.
- `src/bzr_live/verify/` belongs to concurrent issue #20 and must not be created, read, or
  referenced.
- `Makefile` is not edited. `make test` runs
  `uv run --python 3.11 python -m unittest discover -s tests -v` (Makefile:34-36), whose
  default pattern already collects `tests/test_*.py`.
- Guardrails: `make check` and `make test`, both run from the worktree root.
- Repository policy (`AGENTS.md`): this is a disposable local fixture. No crash-consistency
  protocols, no fsync choreography, no subprocess supervision. Never silently substitute a
  value a scenario did not declare.
- Deferrals carried from the design review: none at authoring time; any recorded during the
  review is appended to this section with its owning record path or tracker issue.

## File map

| File | Status | Answerable for |
|---|---|---|
| `tests/test_fault_injection.py` | created | the double, the fault wrappers, and every response-loss recovery test |
| `docs/adr/0009-response-loss-fault-injection.md` | created (already written) | the decision |
| `docs/workflow/specs/2026-09-02-response-loss-recovery-design.md` | created (already written) | the design |
| `docs/workflow/plans/2026-09-02-response-loss-recovery.md` | created (this file) | the plan |

Nothing else is created or modified.

## Names borrowed from the existing codebase

Each was confirmed to exist with the signature used here, at
`origin/main` (555b7de):

- `bzr_live.provision.KeyStore(state_root)`, `.store_actor_key(name, key)` —
  `src/bzr_live/provision/keys.py`, used the same way at `tests/test_replay.py:752-754`.
- `bzr_live.replay.ReplayContext(scenario, keys, *, bzr_path, base_url, workspace, run=subprocess.run, opener=urllib.request.urlopen)`
  — `src/bzr_live/replay/context.py:31-33`.
- `bzr_live.replay.ReplayEngine(scenario, context, store, journal_dir, *, out=print)`,
  `.replay()`, `.resume()` — `src/bzr_live/replay/engine.py:32-50`.
- `bzr_live.replay.ReplayError` — `src/bzr_live/replay/context.py:24`.
- `bzr_live.scenario.JournalStore(state_dir)`, used as a context manager,
  `.read(event, attempt=None)` — `src/bzr_live/scenario/journal.py:696`, and
  `src/bzr_live/replay/__main__.py:39` for the `with` form.
- `bzr_live.scenario.InFlightRecord`, `CompletedRecord`, `PlannedEvent`, `Reference`,
  `freeze_planned`, `load_scenario` — all exported from `src/bzr_live/scenario/__init__.py`
  and all imported the same way at `tests/test_replay.py:19-29`.
- `CompletedRecord.next_safe_action`, `.exit_status`, `.resolved_ids`, `.attempt` — asserted
  on at `tests/test_replay.py:1007-1015`.
- `BzrClient._invoke`'s argv shape,
  `[bzr, "--json", "--server-url", url, "--server-api-key-env", "BZR_LIVE_API_KEY", "--server-email", email, *args]`
  followed by `["--", *positionals]` — `src/bzr_live/provision/adapters.py:93-97`. The
  eight-element prefix is what `_ARGV_PREFIX` skips.
- `BzrClient.read`'s absence contract: exit 2 is "no answer", exit 4 with an `api_code` in
  `absent_codes` is "absent", anything else raises — `src/bzr_live/provision/adapters.py:118-131`.

---

## Task 1 — the boundary double and the fault wrappers

**Where this fits.** Everything in Task 2 asserts on state this double holds. Built and
proved first, so a double that silently drops mutations cannot make Task 2's
"not duplicated" assertions pass vacuously.

**Creates:** `tests/test_fault_injection.py`.
**Modifies:** nothing.
**Tests:** `ReplayThroughTheDoubleTest` in the same file.

### Interfaces

Consumed from the existing codebase: everything in *Names borrowed* above.

Provided to Task 2:

```python
_ARGV_PREFIX: int                                  # 8

class _RunnerKilled(BaseException): ...
class _Absent(Exception):
    def __init__(self, api_code: int) -> None: ...
    api_code: int

@dataclasses.dataclass(frozen=True)
class _Command:
    operation: str
    flags: Mapping[str, tuple[str, ...]]
    switches: frozenset
    positionals: tuple
    def one(self, name: str) -> str | None: ...

def _parse(argv) -> _Command: ...

class FakeBugzilla:
    bugs: dict[int, dict]
    aliases: dict[str, int]
    comments: dict[int, list[dict]]
    attachments: dict[int, list[dict]]
    mutations: list[tuple[str, str]]
    def __call__(self, argv, capture_output=False, env=None, shell=False,
                 input=None) -> subprocess.CompletedProcess: ...
    def marked_comments(self, marker: str) -> int: ...
    def marked_attachments(self, marker: str) -> int: ...
    def sent(self, operation: str) -> int: ...

class _ResponseLoss:
    def __init__(self, server: FakeBugzilla, operation: str) -> None: ...
    fired: bool
    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess: ...

class _Unanswerable:
    def __init__(self, server: FakeBugzilla, operation: str) -> None: ...
    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess: ...

FIXTURE: Path                                      # tests/fixtures/replay-scenario
```

### Steps

**1.1 — write the failing test first.** Create `tests/test_fault_injection.py` containing
only the imports and this test class:

```python
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from bzr_live.provision import KeyStore
from bzr_live.replay import ReplayContext, ReplayEngine, ReplayError
from bzr_live.scenario import (
    CompletedRecord,
    InFlightRecord,
    JournalStore,
    PlannedEvent,
    Reference,
    freeze_planned,
    load_scenario,
)

# Anchored to this file, not the CWD, matching tests/test_replay.py:33.
FIXTURE = Path(__file__).parent / "fixtures" / "replay-scenario"


class _Fixture(unittest.TestCase):
    """The state root, keys, journal directory and per-process runner every test needs."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "state"
        self.keys = KeyStore(self.root)
        self.scenario = load_scenario(FIXTURE)
        # One distinct key per actor, so a sweep for a key cannot pass by finding the
        # other actor's value where this actor's was expected.
        for resource in self.scenario.resources:
            if resource.kind == "actor":
                self.keys.store_actor_key(resource.name, f"SECRET-KEY-{resource.name}")
        # The spec puts the journal at <state-root>/journal/<scenario name>/;
        # JournalStore creates only the leaf, so the parent is made here.
        self.journal_dir = self.root / "journal" / self.scenario.name
        os.mkdir(self.journal_dir.parent, 0o700)
        self.out: list[str] = []

    def _process(self, scenario, run, command: str):
        """One replay process, shaped like the CLI's own.

        src/bzr_live/replay/__main__.py:33-41 gives each run a fresh workspace directory
        and a fresh JournalStore. Both matter here: the store takes an exclusive flock, so
        a second run cannot open one the first still holds, and ReplayContext's file
        counter restarts at 1 in a fresh context, so a shared workspace would collide on
        _write_private's O_EXCL open.
        """
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        Path(workspace.name).chmod(0o700)
        context = ReplayContext(
            scenario, self.keys, bzr_path="bzr", base_url="http://127.0.0.1:8080/",
            workspace=workspace.name, run=run)
        with JournalStore(self.journal_dir) as store:
            engine = ReplayEngine(
                scenario, context, store, self.journal_dir, out=self.out.append)
            return getattr(engine, command)()

    def _record(self, event: str, attempt: int | None = None):
        with JournalStore(self.journal_dir) as store:
            return store.read(event, attempt)

    def _event(self, scenario, name: str):
        return next(event for event in scenario.events if event.name == name)

    def _trimmed(self, *names: str):
        """The fixture scenario cut down to the named events, in fixture order."""
        events = tuple(event for event in self.scenario.events if event.name in names)
        return dataclasses.replace(self.scenario, events=events)

    def _alias(self, scenario) -> str:
        return self._event(
            scenario, "create-checkout-race").expected_postcondition["values"][
                "server_alias"]


class ReplayThroughTheDoubleTest(_Fixture):
    """The double records what replay sends.

    Without this, every "not duplicated" assertion in ResponseLossTest could pass
    vacuously: a double that dropped every mutation would hold one of nothing.
    """

    def test_a_clean_replay_lands_in_the_double(self) -> None:
        scenario = self._trimmed(
            "create-checkout-race", "comment-triage", "attach-notes")
        server = FakeBugzilla()
        self.assertEqual(
            [status for status, _ in self._process(scenario, server, "replay")],
            ["executed", "executed", "executed"])
        self.assertEqual(len(server.bugs), 1)
        bug_id = server.aliases[self._alias(scenario)]
        # Two comments: Bugzilla stores the create's description as the bug's first
        # comment, which is why it is in the corpus the append reconcilers search.
        self.assertEqual(len(server.comments[bug_id]), 2)
        self.assertEqual(len(server.attachments[bug_id]), 1)
        # The create's declared fields landed, so `bug view` reads back what was sent.
        bug = server.bugs[bug_id]
        self.assertEqual(bug["status"], "UNCONFIRMED")
        self.assertEqual(bug["target_milestone"], "m1")
        self.assertEqual(bug["assigned_to"], "triager@example.test")
        self.assertEqual(bug["cc"], ["reporter@example.test"])
        self.assertEqual(bug["keywords"], ["regression"])
        self.assertEqual(
            server.marked_comments(
                self._event(scenario, "comment-triage").reconciliation_marker), 1)
        self.assertEqual(
            server.marked_attachments(
                self._event(scenario, "attach-notes").reconciliation_marker), 1)
        self.assertEqual(server.sent("bug create"), 1)
        self.assertEqual(server.sent("comment add"), 1)
        self.assertEqual(server.sent("attachment upload"), 1)

    def test_the_injector_lets_the_mutation_commit_before_it_kills(self) -> None:
        """The fault lands after write_in_flight and before any completed write."""
        scenario = self._trimmed("create-checkout-race")
        server = FakeBugzilla()
        loss = _ResponseLoss(server, "bug create")
        with self.assertRaises(_RunnerKilled):
            self._process(scenario, loss, "replay")
        self.assertTrue(loss.fired)
        self.assertEqual(server.sent("bug create"), 1)
        self.assertEqual(len(server.bugs), 1)
        record = self._record("create-checkout-race")
        self.assertIsInstance(record, InFlightRecord)
        self.assertNotIsInstance(record, CompletedRecord)

    def test_an_unmodelled_command_fails_loudly(self) -> None:
        """A default reply for a command nobody modelled would pass silently."""
        server = FakeBugzilla()
        argv = ["bzr", "--json", "--server-url", "u", "--server-api-key-env", "K",
                "--server-email", "a@b.test", "product", "view", "--", "checkout"]
        with self.assertRaises(AssertionError) as caught:
            server(argv)
        self.assertIn("product view", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
```

**1.2 — run it and confirm it fails.** From the worktree root:

```
uv run --python 3.11 python -m unittest tests.test_fault_injection -v
```

Expect `NameError: name 'FakeBugzilla' is not defined` on all three tests. A pass here
means the file was written wrong.

**1.3 — add the parser and the fault wrappers.** Insert after the `FIXTURE` line:

```python
# bzr, --json, --server-url, URL, --server-api-key-env, ENV, --server-email, EMAIL.
# BzrClient._invoke builds exactly this prefix
# (src/bzr_live/provision/adapters.py:93-97).
_ARGV_PREFIX = 8


class _RunnerKilled(BaseException):
    """The runner died after the boundary committed, before it learned the outcome.

    BaseException, not Exception: src/bzr_live/replay/engine.py:190 catches
    (ProvisionError, ReplayError) around the mutation, and a fault that arm could absorb
    would exercise the in-run reconciliation path instead of `resume`.
    """


class _Absent(Exception):
    """The boundary's "no such object": exit 4 carrying an api_code."""

    def __init__(self, api_code: int) -> None:
        super().__init__(api_code)
        self.api_code = api_code


@dataclasses.dataclass(frozen=True)
class _Command:
    """One parsed bzr invocation."""

    operation: str                          # "bug create", "comment list", ...
    flags: Mapping[str, tuple[str, ...]]    # --summary=x  ->  {"summary": ("x",)}
    switches: frozenset                     # --private, --obsolete
    positionals: tuple

    def one(self, name: str) -> str | None:
        """The single value of a flag. Repetition is a caller error, not a last-wins."""
        values = self.flags.get(name)
        if values is None:
            return None
        if len(values) != 1:
            raise AssertionError(
                f"--{name} was given {len(values)} times on {self.operation!r}")
        return values[0]


def _parse(argv) -> _Command:
    """Split a bzr argv into its operation, flags, switches and positionals."""
    rest = list(argv[_ARGV_PREFIX:])
    positionals: tuple = ()
    if "--" in rest:
        cut = rest.index("--")
        rest, positionals = rest[:cut], tuple(rest[cut + 1:])
    flags: dict[str, list[str]] = {}
    switches: set[str] = set()
    words: list[str] = []
    for word in rest:
        if not word.startswith("--"):
            words.append(word)
            continue
        name, separator, value = word[2:].partition("=")
        if separator:
            flags.setdefault(name, []).append(value)
        else:
            switches.add(name)
    return _Command(
        " ".join(words), {name: tuple(values) for name, values in flags.items()},
        frozenset(switches), positionals)


class _ResponseLoss:
    """Lets one named mutation commit, then destroys the runner before it is told.

    This is issue #24's injection seam: ReplayContext already takes `run`, so nothing
    under src/ changes and the engine under test is the shipped one.
    """

    def __init__(self, server: "FakeBugzilla", operation: str) -> None:
        self._server = server
        self._operation = operation
        self.fired = False

    def __call__(self, argv, **kwargs):
        completed = self._server(argv, **kwargs)
        if not self.fired and _parse(argv).operation == self._operation:
            self.fired = True
            raise _RunnerKilled(self._operation)
        return completed


class _Unanswerable:
    """A boundary whose named read exits 2, which BzrClient.read maps to "no answer".

    Not an error and not an empty result: src/bzr_live/replay/actions.py:232-240 refuses
    to read it as an empty list, so the append reconcilers reach their `stop` arm.
    """

    def __init__(self, server: "FakeBugzilla", operation: str) -> None:
        self._server = server
        self._operation = operation

    def __call__(self, argv, **kwargs):
        if _parse(argv).operation == self._operation:
            return subprocess.CompletedProcess(argv, 2, b"", b"")
        return self._server(argv, **kwargs)
```

**1.4 — add the double.** Insert after `_Unanswerable`:

```python
class FakeBugzilla:
    """A stateful stand-in for the fixture's Bugzilla, driven through bzr's own argv.

    It models only the commands the five actions issue #24 names actually issue, with the
    reply shapes src/bzr_live/replay/actions.py reads, and refuses everything else. It
    proves engine recovery and nothing about bzr or Bugzilla; boundary fidelity stays
    tests/replay_smoke.sh's job (ADR 0009).
    """

    # Applied by `bug update` and then unreadable: `bzr bug view` serializes none of the
    # three (finding D3, ADR 0006). Keeping them off the bug this double returns is what
    # makes the fixture's update-triage reconcile as `retry` rather than `advance`.
    _UNREADABLE = frozenset({"estimated-time", "remaining-time", "work-time"})
    # Only the two `bug update` flags the scoped events actually send. Modelling
    # --summary, --resolution, --assignee, --dupe-of or the list deltas would add
    # branches no test exercises, which is where a wrong model hides longest (ADR 0009);
    # _require_known refuses them loudly instead.
    _SCALAR_FLAGS = {
        "status": "status",
        "target-milestone": "target_milestone",
    }

    def __init__(self) -> None:
        self.bugs: dict[int, dict] = {}
        self.aliases: dict[str, int] = {}
        self.comments: dict[int, list[dict]] = {}
        self.attachments: dict[int, list[dict]] = {}
        self.mutations: list[tuple[str, str]] = []
        self._next_bug = 1
        self._next_comment = 1
        self._next_attachment = 1

    # --- the subprocess seam ----------------------------------------------

    def __call__(self, argv, capture_output=False, env=None, shell=False, input=None):
        command = _parse(argv)
        handler = getattr(self, "_" + command.operation.replace(" ", "_"), None)
        if handler is None:
            raise AssertionError(
                f"FakeBugzilla does not model {command.operation!r}; model it, or narrow "
                "the scenario under test")
        try:
            payload = handler(command)
        except _Absent as absent:
            stderr = json.dumps({"error": {"api_code": absent.api_code}})
            return subprocess.CompletedProcess(argv, 4, b"", stderr.encode("utf-8"))
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(payload).encode("utf-8"), b"")

    # --- what the tests read ----------------------------------------------

    def marked_comments(self, marker: str) -> int:
        token = f"[{marker}]"
        return sum(token in entry["text"]
                   for entries in self.comments.values() for entry in entries)

    def marked_attachments(self, marker: str) -> int:
        token = f"[{marker}]"
        return sum(token in entry["summary"]
                   for entries in self.attachments.values() for entry in entries)

    def sent(self, operation: str) -> int:
        return sum(1 for name, _ in self.mutations if name == operation)

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _require_known(command: _Command, names) -> None:
        unknown = set(command.flags) - set(names)
        if unknown:
            raise AssertionError(
                f"FakeBugzilla does not model {sorted(unknown)} on "
                f"{command.operation!r}")

    def _bug(self, handle: str) -> dict:
        if handle in self.aliases:
            return self.bugs[self.aliases[handle]]
        if handle.isdigit() and int(handle) in self.bugs:
            return self.bugs[int(handle)]
        # 100 is the alias code and 101 the numeric-id code; both are in
        # BUG_ABSENT_CODES (src/bzr_live/provision/adapters.py:48).
        raise _Absent(101 if handle.isdigit() else 100)

    def _append_comment(self, bug_id: int, text: str) -> int:
        entry = {"id": self._next_comment, "text": text}
        self._next_comment += 1
        self.comments[bug_id].append(entry)
        return entry["id"]

    # --- the modelled commands --------------------------------------------

    def _bug_create(self, command: _Command) -> dict:
        self._require_known(command, {"from-json"})
        document = json.loads(
            Path(command.one("from-json")).read_text(encoding="utf-8"))
        alias = document["alias"]
        if alias in self.aliases:
            # Bugzilla refuses a second bug declaring a live alias (ADR 0006, observed).
            # Failing outright rather than replying exit 4 is deliberate: the only way to
            # reach this is a successful create being repeated, which is the defect these
            # tests exist to catch.
            raise AssertionError(
                f"the fixture already holds alias {alias!r}: a create was repeated")
        bug_id = self._next_bug
        self._next_bug += 1
        self.aliases[alias] = bug_id
        self.bugs[bug_id] = {
            "id": bug_id,
            "alias": alias,
            "summary": document["summary"],
            "status": "UNCONFIRMED",
            "target_milestone": document.get("target_milestone"),
            "assigned_to": document.get("assignee"),
            "cc": list(document.get("cc") or []),
            "keywords": list(document.get("keywords") or []),
        }
        self.comments[bug_id] = []
        self.attachments[bug_id] = []
        # Bugzilla stores the description as the bug's first comment and `bzr comment
        # list` filters nothing out (src/bzr_live/replay/actions.py:286-291).
        self._append_comment(bug_id, document["description"])
        self.mutations.append(("bug create", alias))
        return dict(self.bugs[bug_id])

    def _bug_view(self, command: _Command) -> dict:
        self._require_known(command, set())
        return dict(self._bug(command.positionals[0]))

    def _bug_update(self, command: _Command) -> dict:
        self._require_known(
            command,
            set(self._SCALAR_FLAGS) | set(self._UNREADABLE) | {"comment-file"})
        bug = self._bug(command.positionals[0])
        for flag, key in self._SCALAR_FLAGS.items():
            if flag in command.flags:
                bug[key] = command.one(flag)
        if "comment-file" in command.flags:
            self._append_comment(
                bug["id"],
                Path(command.one("comment-file")).read_text(encoding="utf-8"))
        self.mutations.append(("bug update", str(bug["id"])))
        return dict(bug)

    def _comment_add(self, command: _Command) -> dict:
        self._require_known(command, {"body-file"})
        bug = self._bug(command.positionals[0])
        comment_id = self._append_comment(
            bug["id"], Path(command.one("body-file")).read_text(encoding="utf-8"))
        self.mutations.append(("comment add", str(bug["id"])))
        return {"id": comment_id}

    def _comment_list(self, command: _Command) -> list:
        self._require_known(command, set())
        bug = self._bug(command.positionals[0])
        return [dict(entry) for entry in self.comments[bug["id"]]]

    def _attachment_upload(self, command: _Command) -> dict:
        self._require_known(command, {"summary", "content-type"})
        bug = self._bug(command.positionals[0])
        # Read the materialized asset, so a path the context never wrote fails here.
        Path(command.positionals[1]).read_bytes()
        entry = {
            "id": self._next_attachment,
            "summary": command.one("summary"),
            "is_obsolete": False,
        }
        self._next_attachment += 1
        self.attachments[bug["id"]].append(entry)
        self.mutations.append(("attachment upload", str(bug["id"])))
        return {"id": entry["id"]}

    def _attachment_list(self, command: _Command) -> list:
        self._require_known(command, set())
        bug = self._bug(command.positionals[0])
        return [dict(entry) for entry in self.attachments[bug["id"]]]
```

**1.5 — run the tests and confirm they pass.**

```
uv run --python 3.11 python -m unittest tests.test_fault_injection -v
```

Expect `Ran 3 tests` and `OK`.

**1.6 — confirm the fidelity test bites.** Temporarily make `_comment_add` return
`{"id": 1}` without calling `self._append_comment(...)`, re-run the command above, and
confirm `test_a_clean_replay_lands_in_the_double` fails on
`2 != 1` for `len(server.comments[bug_id])`. Then revert the edit and re-run to green.
This is the step that proves the "not duplicated" assertions in Task 2 can fail at all.

**1.7 — run the guardrails and commit.**

```
make check
```

Expect no output and exit 0. Then:

```
git add tests/test_fault_injection.py
git commit -m "test: model the replay boundary so mutations can be counted"
```

### Acceptance criteria

- `tests/test_fault_injection.py` exists and holds `FakeBugzilla`, `_ResponseLoss`,
  `_Unanswerable`, `_RunnerKilled`, `_parse`, and `ReplayThroughTheDoubleTest`.
- No file under `src/` is modified (`git diff --name-only origin/main...HEAD -- src/` is
  empty).
- A clean three-event replay through the double leaves one bug, two comments (the
  description plus the declared comment) and one attachment.
- An unmodelled command raises `AssertionError` naming the operation.
- `make check` is green.

---

## Task 2 — the response-loss recovery cases

**Where this fits.** The proof issue #24 asks for. Each case runs one replay process into
an injected response loss and a second `resume` process over the same journal, and asserts
what the fixture holds afterwards.

**Creates:** nothing.
**Modifies:** `tests/test_fault_injection.py`.
**Tests:** `ResponseLossTest` in that file.

### Interfaces

Consumed from Task 1: `_Fixture` (`_process`, `_record`, `_event`, `_trimmed`, `_alias`),
`FakeBugzilla`, `_ResponseLoss`, `_Unanswerable`, `_RunnerKilled`.
Nothing later depends on this task.

### Steps

**2.1 — write the failing tests.** Append this class to `tests/test_fault_injection.py`,
above the `if __name__ == "__main__":` block:

```python
class ResponseLossTest(_Fixture):
    """ADR 0006's guarantee: a response lost after the mutation committed resumes to
    exactly one semantic result, or refuses with a reset/replay instruction.

    Each test kills the runner after the fixture applied the mutation, then runs a second,
    independent `resume` over the same journal directory -- the operator's own recovery
    path, not a test-only one.
    """

    def _lose_response(self, scenario, operation: str) -> FakeBugzilla:
        """Replay until the named mutation commits, then destroy the runner."""
        server = FakeBugzilla()
        loss = _ResponseLoss(server, operation)
        with self.assertRaises(_RunnerKilled):
            self._process(scenario, loss, "replay")
        self.assertTrue(loss.fired, f"no {operation!r} was ever sent")
        return server

    def _readable_update(self):
        """The fixture with update-triage replaced by an update that reads back whole.

        update-triage declares `remaining_hours`, which `bzr bug view` never serializes
        (finding D3), so it can only ever reconcile as `retry`. The `advance` resolution
        needs an update whose every declared field reads back, and the fixture has none --
        the same reason tests/test_replay.py:484-505 hand-builds one.
        """
        marker = "bzr-live:replay-demo:update-status"
        update = PlannedEvent(
            name="update-status",
            actor=Reference("actor", "triager"),
            action="bug.update",
            action_class="idempotent-set",
            payload={},
            dependencies=(),
            reconciliation_marker=marker,
            expected_postcondition=freeze_planned({
                "action": "bug.update",
                "target": Reference("bug", "checkout-race"),
                "values": {
                    "status": "CONFIRMED",
                    "milestone": Reference("milestone", "m1"),
                },
                "marker": marker,
            }),
            creates=None,
        )
        create = self._event(self.scenario, "create-checkout-race")
        return dataclasses.replace(self.scenario, events=(create, update))

    # --- unique-create -----------------------------------------------------

    def test_bug_create_resumes_to_exactly_one_bug(self) -> None:
        scenario = self._trimmed("create-checkout-race")
        server = self._lose_response(scenario, "bug create")
        self.assertEqual(
            self._process(scenario, server, "resume"),
            [("resumed", "create-checkout-race")])
        # The create was adopted from the alias, never repeated.
        self.assertEqual(server.sent("bug create"), 1)
        self.assertEqual(len(server.bugs), 1)
        record = self._record("create-checkout-race")
        self.assertIsInstance(record, CompletedRecord)
        self.assertEqual(record.next_safe_action, "advance")
        # exit_status -1 means the invocation's own status was never observed.
        self.assertEqual(record.exit_status, -1)
        self.assertEqual(record.resolved_ids, {"bug:checkout-race": 1})
        self.assertIsNone(self._record("create-checkout-race", 2))

    # --- idempotent-set ----------------------------------------------------

    def test_bug_update_resumes_without_re_sending_a_readable_set(self) -> None:
        scenario = self._readable_update()
        server = self._lose_response(scenario, "bug update")
        self.assertEqual(server.sent("bug update"), 1)
        self.assertEqual(
            self._process(scenario, server, "resume"),
            [("skipped", "create-checkout-race"), ("resumed", "update-status")])
        # The declared set reads back, so reconciliation adopts it and sends nothing.
        self.assertEqual(server.sent("bug update"), 1)
        self.assertEqual(server.bugs[1]["status"], "CONFIRMED")
        self.assertEqual(server.bugs[1]["target_milestone"], "m1")
        record = self._record("update-status")
        self.assertEqual(record.next_safe_action, "advance")
        self.assertEqual(record.exit_status, -1)

    def test_bug_update_re_applies_a_set_the_boundary_cannot_read_back(self) -> None:
        """ADR 0006's "one extra idempotent invocation, never a duplicate".

        update-triage declares `remaining_hours`, which never reads back, so
        reconciliation cannot confirm the commit and records `retry`; the operator's
        resume owns attempt 2. Two invocations, one semantic result.
        """
        scenario = self._trimmed("create-checkout-race", "update-triage")
        server = self._lose_response(scenario, "bug update")
        self.assertEqual(
            self._process(scenario, server, "resume"),
            [("skipped", "create-checkout-race"), ("executed", "update-triage")])
        self.assertEqual(server.sent("bug update"), 2)
        self.assertEqual(self._record("update-triage", 1).next_safe_action, "retry")
        second = self._record("update-triage", 2)
        self.assertEqual(second.next_safe_action, "advance")
        self.assertEqual(second.exit_status, 0)
        self.assertEqual(server.bugs[1]["status"], "CONFIRMED")
        self.assertEqual(server.bugs[1]["target_milestone"], "m1")
        # Re-applying a set is not an append: the create's description is still the only
        # comment on the bug, and no attachment appeared behind it.
        self.assertEqual(len(server.comments[1]), 1)
        self.assertEqual(server.attachments[1], [])

    # --- append ------------------------------------------------------------

    def test_bug_comment_is_never_appended_twice(self) -> None:
        scenario = self._trimmed("create-checkout-race", "comment-triage")
        marker = self._event(scenario, "comment-triage").reconciliation_marker
        server = self._lose_response(scenario, "comment add")
        self.assertEqual(server.marked_comments(marker), 1)
        self.assertEqual(
            self._process(scenario, server, "resume"),
            [("skipped", "create-checkout-race"), ("resumed", "comment-triage")])
        self.assertEqual(server.marked_comments(marker), 1)
        self.assertEqual(server.sent("comment add"), 1)
        record = self._record("comment-triage")
        self.assertEqual(record.next_safe_action, "advance")
        self.assertEqual(record.exit_status, -1)

    def test_bug_attach_is_never_uploaded_twice(self) -> None:
        scenario = self._trimmed("create-checkout-race", "attach-notes")
        marker = self._event(scenario, "attach-notes").reconciliation_marker
        server = self._lose_response(scenario, "attachment upload")
        self.assertEqual(server.marked_attachments(marker), 1)
        self.assertEqual(
            self._process(scenario, server, "resume"),
            [("skipped", "create-checkout-race"), ("resumed", "attach-notes")])
        self.assertEqual(server.marked_attachments(marker), 1)
        self.assertEqual(server.sent("attachment upload"), 1)
        record = self._record("attach-notes")
        self.assertEqual(record.next_safe_action, "advance")
        # The attachment already on the bug is adopted, not re-created.
        self.assertEqual(record.resolved_ids, {"attachment:triage-notes": 1})

    def test_bug_worktime_is_never_appended_twice(self) -> None:
        scenario = self._trimmed("create-checkout-race", "worktime-triage")
        marker = self._event(scenario, "worktime-triage").reconciliation_marker
        # A worktime is a `bug update` carrying --work-time and a marked comment, so the
        # faulted operation is the same one bug.update uses -- and this scenario contains
        # no other update for the injector to fire on.
        server = self._lose_response(scenario, "bug update")
        self.assertEqual(server.marked_comments(marker), 1)
        self.assertEqual(
            self._process(scenario, server, "resume"),
            [("skipped", "create-checkout-race"), ("resumed", "worktime-triage")])
        self.assertEqual(server.marked_comments(marker), 1)
        self.assertEqual(server.sent("bug update"), 1)
        self.assertEqual(self._record("worktime-triage").next_safe_action, "advance")

    # --- the refusal arm ---------------------------------------------------

    def test_an_unanswerable_reconciliation_refuses_instead_of_re_sending(self) -> None:
        """The other half of the guarantee: refuse with a reset/replay instruction.

        An append whose commit can be neither proved nor disproved must not be retried.
        """
        scenario = self._trimmed("create-checkout-race", "comment-triage")
        marker = self._event(scenario, "comment-triage").reconciliation_marker
        server = self._lose_response(scenario, "comment add")
        with self.assertRaises(ReplayError) as caught:
            self._process(scenario, _Unanswerable(server, "comment list"), "resume")
        message = str(caught.exception)
        self.assertIn("comment-triage", message)
        self.assertIn("did not answer", message)
        self.assertIn(marker, message)
        self.assertIn("CONFIRM_RESET=1 make reset", message)
        self.assertEqual(server.marked_comments(marker), 1)
        self.assertEqual(server.sent("comment add"), 1)
        self.assertEqual(self._record("comment-triage").next_safe_action, "stop")
```

**2.2 — run them and confirm which fail.**

```
uv run --python 3.11 python -m unittest tests.test_fault_injection -v
```

These tests assert an existing guarantee, so they are expected to pass on first run — the
proof is that the guarantee holds, not that new code makes it hold. Step 2.3 is what
establishes they can fail at all. If any fails here, that is a real recovery defect in the
shipped engine: stop and investigate rather than adjusting the assertion.

**2.3 — confirm the tests bite.** Make this one controlled change to `_ResponseLoss`, so
the fault fires *before* the mutation commits rather than after:

```python
    def __call__(self, argv, **kwargs):
        if not self.fired and _parse(argv).operation == self._operation:
            self.fired = True
            raise _RunnerKilled(self._operation)
        return self._server(argv, **kwargs)
```

Re-run the command in 2.2. Expect every `ResponseLossTest` case to fail: with nothing
committed, `bug create` reconciles to `retry` (so `resume` reports `executed`, not
`resumed`), and each append's marker count is 0 before the resume rather than 1. Then
**revert the edit** and re-run to green. A case that still passes with the mutation
suppressed is asserting nothing about recovery and must be strengthened before proceeding.

**2.4 — run the guardrails.**

```
make check
```

Expect no output and exit 0.

```
make test
```

Expect the lifecycle shell tests to run, then the Python suite, ending in `OK`. Record the
observed wall-clock duration; it was previously unmeasured.

**2.5 — commit.**

```
git add tests/test_fault_injection.py
git commit -m "test: prove response-loss recovery for the five replay actions"
```

### Acceptance criteria

- Seven tests in `ResponseLossTest`, covering `bug.create`, `bug.update` (both
  resolutions), `bug.comment`, `bug.attach`, `bug.worktime`, and the refusal arm.
- Every recovery case drives recovery through `ReplayEngine.resume()` on a second
  `JournalStore` and workspace, with no engine subclass and no test-only branch.
- Every append case asserts the marker count is 1 both before and after the resume.
- `bug.create` asserts the server holds one bug and `bug create` was sent once.
- The refusal case asserts the message names the marker and
  `CONFIRM_RESET=1 make reset`, and that nothing was re-sent.
- `git diff --name-only origin/main...HEAD -- src/` is empty.
- `make check` and `make test` are green.

### Rollback

The change is one new test file. Reverting the two commits removes it entirely; no
production behaviour, fixture data, or generated artifact is touched.
