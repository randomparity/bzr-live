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


class FakeBugzilla:
    """A stateful stand-in for the fixture's Bugzilla, driven through bzr's own argv.

    It models only the commands the five actions issue #24 names actually issue, with the
    reply shapes src/bzr_live/replay/actions.py reads, and refuses everything else. It
    proves engine recovery and nothing about bzr or Bugzilla; boundary fidelity stays
    tests/replay_smoke.sh's job (ADR 0009).
    """

    # Applied by `bug update` and then unreadable, on grounds that are Bugzilla's rather
    # than bzr's: it gates the time-tracking fields on timetrackinggroup and omits them
    # from an otherwise-successful read, and it decrements remaining_time by logged work
    # so the declared value is never the final state (ADR 0006). Keeping them off the bug
    # this double returns is what makes the fixture's update-triage reconcile as `retry`
    # rather than `advance`.
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
        """Refuse every flag *and switch* the double does not model.

        Switches count: `--private` (actions.py:456, :494) and `--reset-assigned-to`
        (actions.py:385) carry no `=`, so a check reading command.flags alone would
        accept them on a default reply -- the silence this method exists to prevent.
        """
        unknown = (set(command.flags) | set(command.switches)) - set(names)
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
        create = self._event(scenario, "create-checkout-race")
        bug_id = server.aliases[
            create.expected_postcondition["values"]["server_alias"]]
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

    def test_an_unmodelled_switch_fails_loudly(self) -> None:
        """A switch carries no "=", so a flags-only check would accept it silently."""
        server = FakeBugzilla()
        argv = ["bzr", "--json", "--server-url", "u", "--server-api-key-env", "K",
                "--server-email", "a@b.test", "bug", "update", "--reset-assigned-to",
                "--", "1"]
        with self.assertRaises(AssertionError) as caught:
            server(argv)
        self.assertIn("reset-assigned-to", str(caught.exception))


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

        update-triage declares `remaining_hours`, which never confirms -- Bugzilla
        decrements remaining_time by logged work, so the declared value is not the
        server's final state -- so it can only ever reconcile as `retry`. The `advance` resolution
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
        # The operator-facing summary counts the recovery under its own name. Rolling
        # `resumed` into `executed` would tell someone recovering a killed run that this
        # run sent a mutation, when it sent none -- the misreading
        # src/bzr_live/replay/engine.py:129-131 says the split exists to prevent. This
        # branch is the first thing in the repository producing a nonzero resumed count,
        # so nothing else pins it.
        self.assertEqual(
            self.out[-1],
            "summary: 0 executed, 0 reconciled, 1 resumed, 0 already complete")

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


if __name__ == "__main__":
    unittest.main()
