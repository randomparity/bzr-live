from __future__ import annotations

import contextlib
import dataclasses
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
from bzr_live.replay import HANDLERS, ReplayContext, ReplayEngine, ReplayError
from bzr_live.replay.context import KEY_ENV
from bzr_live.scenario import (
    CompletedRecord,
    InFlightRecord,
    InvocationMetadata,
    JournalStore,
    PlannedEvent,
    PlannedResource,
    Reference,
    freeze_planned,
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
        event = self._event("bug.attach", description="漢" * 120, asset_sha256="a" * 64)
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.attach"].check_supported(event)
        self.assertIn("bytes", str(caught.exception))

    def test_attachment_update_refuses_an_over_long_description(self) -> None:
        # The same TINYTEXT column, reached by the second writer. Bugzilla applies no
        # length validator here (Bugzilla/Attachment.pm:578-584) and disables strict
        # sql_mode (Bugzilla/DB/MariaDB.pm:87-100), so an unchecked write is truncated
        # by MariaDB and reported as success -- the shape AGENTS.md forbids.
        event = self._event(
            "attachment.update", obsolete=True, description="x" * 256)
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["attachment.update"].check_supported(event)
        self.assertIn("255", str(caught.exception))
        self.assertIn("Bugzilla", str(caught.exception))
        self.assertNotIn("(finding ", str(caught.exception))

    def test_attachment_update_without_a_description_is_supported(self) -> None:
        event = self._event("attachment.update", obsolete=True)
        self.assertIsNone(HANDLERS["attachment.update"].check_supported(event))

    def test_append_text_carrying_a_marker_token_is_refused(self) -> None:
        # Reconciliation matches an append by looking for its bracketed marker in
        # server-visible text, so a declared body carrying one could satisfy another
        # event's reconciliation -- advance for a mutation that never happened.
        event = self._event(
            "bug.comment", body="see [bzr-live:demo:other] above", private=False)
        with self.assertRaises(ReplayError) as caught:
            HANDLERS["bug.comment"].check_supported(event)
        self.assertIn("[bzr-live:", str(caught.exception))

    def test_every_append_action_refuses_an_embedded_marker(self) -> None:
        embedded = "text with [bzr-live:demo:other] in it"
        for action, values in (
            ("bug.comment", {"body": embedded, "private": False}),
            ("bug.worktime", {"comment": embedded, "hours": "1.0"}),
            ("bug.attach", {"description": embedded, "asset_sha256": "a" * 64}),
            ("attachment.update", {"obsolete": True, "description": embedded}),
        ):
            with self.subTest(action=action):
                with self.assertRaises(ReplayError):
                    HANDLERS[action].check_supported(self._event(action, **values))

    def test_every_action_has_a_handler(self) -> None:
        self.assertEqual(set(HANDLERS), {
            "bug.create", "bug.update", "bug.comment", "bug.attach", "bug.worktime",
            "bug.custom-field-set", "bug.flag", "attachment.update"})


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
    def setUp(self) -> None:
        self.root = _state_root(self)
        self.keys = KeyStore(self.root)
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = workspace.name
        self.scenario = load_scenario(FIXTURE)
        for resource in self.scenario.resources:
            if resource.kind == "actor":
                self.keys.store_actor_key(resource.name, "SECRET-KEY")
        self.run = _FakeRun()
        self.context = ReplayContext(
            self.scenario, self.keys, bzr_path="bzr",
            base_url="http://127.0.0.1:8080/", workspace=self.workspace, run=self.run)

    def _event(self, name: str):
        return next(event for event in self.scenario.events if event.name == name)

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
        keep = PlannedResource("actor", "keep", {"email": "keep@x"}, ())
        join = PlannedResource("actor", "join", {"email": "join@x"}, ())
        scenario = dataclasses.replace(
            self.scenario, resources=self.scenario.resources + (keep, join))
        context = ReplayContext(
            scenario, self.keys, bzr_path="bzr",
            base_url="http://127.0.0.1:8080/", workspace=self.workspace, run=self.run)
        context.adopt({"bug:checkout-race": 41})
        event = PlannedEvent(
            name="update-cc-delta",
            actor=Reference("actor", "triager"),
            action="bug.update",
            action_class="idempotent-set",
            payload={},
            dependencies=(),
            reconciliation_marker="bzr-live:replay-demo:update-cc-delta",
            expected_postcondition={
                "action": "bug.update",
                "target": Reference("bug", "checkout-race"),
                "values": {"cc": (Reference("actor", "keep"), Reference("actor", "join"))},
                "marker": "bzr-live:replay-demo:update-cc-delta",
            },
            creates=None,
        )
        self.run.replies.append((0, {"id": 41, "cc": ["keep@x", "drop@x"]}, None))
        invocation = HANDLERS["bug.update"].build(context, event)
        self.assertIn("--cc-add=join@x", invocation.args)
        self.assertIn("--cc-remove=drop@x", invocation.args)

    def test_flag_renders_bugzilla_syntax(self) -> None:
        # The fixture's flag-review event is actor triager requesting review of
        # reporter, so this asserts the suffix comes from `requestee` and not from
        # `event.actor` -- a discrimination the assertion loses if the two coincide.
        self.context.adopt({"bug:checkout-race": 41})
        invocation = HANDLERS["bug.flag"].build(self.context, self._event("flag-review"))
        self.assertIn("--flag=review?(reporter@example.test)", invocation.args)

    def test_create_resolves_the_new_bug_id(self) -> None:
        ids = HANDLERS["bug.create"].resolved_ids(
            self._event("create-checkout-race"), {"id": 41})
        self.assertEqual(ids, {"bug:checkout-race": 41})

    def test_an_unusable_id_is_refused_by_the_handler_not_the_journal(self) -> None:
        # Both handlers guard the same way, and `isinstance(True, int)` holds -- so a
        # bool would pass an isinstance check here and be refused later by
        # CompletedRecord, blaming the record for a boundary reply after the in-flight
        # record has landed and the mutation may already have committed.
        create, attach = self._event("create-checkout-race"), self._event("attach-notes")
        for output in ({"id": 0}, {"id": -1}, {"id": True}, {"id": "41"}):
            with self.subTest(output=output):
                with self.assertRaises(ReplayError):
                    HANDLERS["bug.create"].resolved_ids(create, output)
                with self.assertRaises(ReplayError):
                    HANDLERS["bug.attach"].resolved_ids(attach, output)

    # Any test that builds a bug.update must queue a bug-view reply first: build reads
    # the bug through _bug_object, and _FakeRun's empty-queue default of (0, {}, None)
    # has no "id", so build would refuse before the assertion runs.
    def test_no_argument_ever_carries_the_key(self) -> None:
        self.context.adopt({"bug:checkout-race": 41, "attachment:triage-notes": 7})
        self.run.replies.append((0, {"id": 41}, None))
        for event in self.scenario.events:
            invocation = HANDLERS[event.action].build(self.context, event)
            for argument in invocation.args + invocation.positionals:
                self.assertNotIn("SECRET-KEY", argument)


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _state_root(self)
        self.keys = KeyStore(self.root)
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = workspace.name
        self.scenario = load_scenario(FIXTURE)
        for resource in self.scenario.resources:
            if resource.kind == "actor":
                self.keys.store_actor_key(resource.name, "SECRET-KEY")

    def _context(self, run) -> ReplayContext:
        context = ReplayContext(
            self.scenario, self.keys, bzr_path="bzr",
            base_url="http://127.0.0.1:8080/", workspace=self.workspace, run=run)
        # Every reconcile test below but the bug.create ones targets the bug the
        # fixture's create-checkout-race event creates; adopt the same id BuildTest
        # uses throughout so each test can call reconcile without repeating this.
        context.adopt({"bug:checkout-race": 41})
        return context

    def _event(self, name: str):
        return next(event for event in self.scenario.events if event.name == name)

    def _create_event(self):
        return self._event("create-checkout-race")

    def _comment_event(self):
        return self._event("comment-triage")

    def _obsolete_event(self):
        return self._event("obsolete-notes")

    def _set_event(self, values):
        # Hand-built the way Task 3 hand-built a PlannedEvent for its delta test: no
        # fixture bug.update event declares a postcondition free of remaining_hours,
        # which bzr bug view never serializes and would force retry regardless of
        # what this checks.
        marker = "bzr-live:replay-demo:update-status"
        return PlannedEvent(
            name="update-status",
            actor=Reference("actor", "triager"),
            action="bug.update",
            action_class="idempotent-set",
            payload={},
            dependencies=(),
            reconciliation_marker=marker,
            expected_postcondition={
                "action": "bug.update",
                "target": Reference("bug", "checkout-race"),
                "values": values,
                "marker": marker,
            },
            creates=None,
        )

    def _flag_event(self, status):
        # Hand-built: the fixture's only bug.flag event, flag-review, declares status
        # "?", so a status-X (clear) postcondition has no fixture event to adopt.
        marker = "bzr-live:replay-demo:flag-status"
        return PlannedEvent(
            name="flag-status",
            actor=Reference("actor", "triager"),
            action="bug.flag",
            action_class="idempotent-set",
            payload={},
            dependencies=(),
            reconciliation_marker=marker,
            expected_postcondition={
                "action": "bug.flag",
                "target": Reference("bug", "checkout-race"),
                "values": {
                    "bug": Reference("bug", "checkout-race"),
                    "flag_type": Reference("flag-type", "review"),
                    "status": status,
                    "requestee": None,
                },
                "marker": marker,
            },
            creates=None,
        )

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
        run = _FakeRun(
            [(0, [{"id": 5, "text": "body\n\n[bzr-live:replay-demo:comment-triage]"}], None)])
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

    def test_append_stops_when_the_reply_does_not_answer(self) -> None:
        # An unanswered read is not proof of absence. It used to collapse into the empty
        # list, which retries -- re-sending the comment, upload or --work-time whose
        # marker rides it, on the one class that must never be blindly repeated.
        run = _FakeRun([(2, None, None)])
        result = HANDLERS["bug.comment"].reconcile(self._context(run), self._comment_event())
        self.assertEqual(result.next_action, "stop")
        self.assertIn("did not answer", result.detail)
        self.assertIn("CONFIRM_RESET=1 make reset", result.detail)

    def test_append_raises_when_the_bug_cannot_be_listed(self) -> None:
        # 51 is Bugzilla's generic object_does_not_exist and is in BzrClient.read's
        # default absent set, which would silently answer None here. The append
        # reconcilers pass an empty set instead, so an unreadable bug raises.
        run = _FakeRun([(4, None, 51)])
        with self.assertRaises(ProvisionError):
            HANDLERS["bug.comment"].reconcile(self._context(run), self._comment_event())

    def test_attach_adopts_the_matched_attachment_id(self) -> None:
        event = self._event("attach-notes")
        run = _FakeRun(
            [(0, [{"id": 7, "summary": f"notes [{event.reconciliation_marker}]"}], None)])
        result = HANDLERS["bug.attach"].reconcile(self._context(run), event)
        self.assertEqual(result.next_action, "advance")
        self.assertEqual(result.resolved_ids, {"attachment:triage-notes": 7})

    def test_attach_stops_when_the_matched_entry_carries_no_id(self) -> None:
        # An entry without a usable `id` used to be adopted unchecked, and the None
        # then died in CompletedRecord's validation -- blaming the journal for a
        # boundary reply, after the in-flight record had landed.
        event = self._event("attach-notes")
        run = _FakeRun(
            [(0, [{"summary": f"notes [{event.reconciliation_marker}]"}], None)])
        result = HANDLERS["bug.attach"].reconcile(self._context(run), event)
        self.assertEqual(result.next_action, "stop")
        self.assertEqual(result.resolved_ids, {})
        self.assertIn("carries no usable id", result.detail)
        self.assertIn(event.reconciliation_marker, result.detail)
        self.assertIn("CONFIRM_RESET=1 make reset", result.detail)

    def test_set_advances_when_the_postcondition_matches(self) -> None:
        run = _FakeRun(
            [(0, {"id": 41, "status": "CONFIRMED", "target_milestone": "m1"}, None)])
        event = self._set_event(
            {"status": "CONFIRMED", "milestone": Reference("milestone", "m1")})
        result = HANDLERS["bug.update"].reconcile(self._context(run), event)
        self.assertEqual(result.next_action, "advance")

    def test_set_retries_when_the_postcondition_differs(self) -> None:
        run = _FakeRun(
            [(0, {"id": 41, "status": "IN_PROGRESS", "target_milestone": "m1"}, None)])
        event = self._set_event(
            {"status": "CONFIRMED", "milestone": Reference("milestone", "m1")})
        result = HANDLERS["bug.update"].reconcile(self._context(run), event)
        self.assertEqual(result.next_action, "retry")
        self.assertIn("status", result.detail)

    def test_set_retries_when_a_declared_field_is_unreadable(self) -> None:
        # update-triage is the fixture's own bug.update event; it declares
        # remaining_hours, which bzr bug view never serializes, so even a reply that
        # matches every other declared field still forces retry.
        run = _FakeRun(
            [(0, {"id": 41, "status": "CONFIRMED", "target_milestone": "m1"}, None)])
        result = HANDLERS["bug.update"].reconcile(
            self._context(run), self._event("update-triage"))
        self.assertEqual(result.next_action, "retry")

    def test_flag_compares_the_requestee_not_just_the_name_and_status(self) -> None:
        # flag-review declares status "?" with requestee actor:reporter. Bugzilla emits
        # `requestee` as the requestee's login and only when one is set
        # (Bugzilla/WebService/Bug.pm:1425-1428 in this fixture's own image); bzr
        # deserializes it as Option<String> (src/types/flag.rs:65 at b80303b7). A flag of
        # the right name and status held by the *wrong* requestee is a different request,
        # so it must not advance -- that mismatch is what this test rests on.
        event = self._event("flag-review")
        held_by_someone_else = {
            "id": 41,
            "flags": [{"name": "review", "status": "?",
                       "requestee": "triager@example.test"}],
        }
        run = _FakeRun([(0, held_by_someone_else, None)])
        result = HANDLERS["bug.flag"].reconcile(self._context(run), event)
        self.assertEqual(result.next_action, "retry")
        self.assertIn("review", result.detail)
        matching = {
            "id": 41,
            "flags": [{"name": "review", "status": "?",
                       "requestee": "reporter@example.test"}],
        }
        run = _FakeRun([(0, matching, None)])
        result = HANDLERS["bug.flag"].reconcile(self._context(run), event)
        self.assertEqual(result.next_action, "advance")

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


class _FakeOpener:
    """Stands in for urllib.request.urlopen: records requests, returns a JSON body."""

    def __init__(self, payload=None):
        self.requests: list[dict] = []
        self._payload = {"bugs": [{"id": 41}]} if payload is None else payload

    def __call__(self, request):
        self.requests.append({
            "url": request.full_url,
            "body": json.loads(request.data.decode("utf-8")),
        })
        return io.BytesIO(json.dumps(self._payload).encode("utf-8"))


class _RecordingStore:
    """Delegates to a real JournalStore, recording every write's known_secrets.

    The in-flight write's `known_secrets` guards structural metadata only, so its
    absence leaves no trace in the written file; recording the argument is the only
    way to hold the "every journal write passes known_secrets" line for that write.
    """

    def __init__(self, store: JournalStore) -> None:
        self._store = store
        self.writes: list[frozenset[str]] = []

    def read(self, event, attempt=None):
        return self._store.read(event, attempt)

    def write_in_flight(self, record, *, known_secrets=()):
        self.writes.append(frozenset(known_secrets))
        return self._store.write_in_flight(record, known_secrets=known_secrets)

    def replace_completed(self, record, *, known_secrets=()):
        self.writes.append(frozenset(known_secrets))
        return self._store.replace_completed(record, known_secrets=known_secrets)


class EngineTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.root = _state_root(self)
        self.keys = KeyStore(self.root)
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = workspace.name
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
        self.store = JournalStore(self.journal_dir)
        self.addCleanup(self.store.close)
        self.out: list[str] = []

    # --- scenarios ---------------------------------------------------------

    def _trimmed(self, *names: str):
        """The fixture scenario, cut down to the named events in fixture order.

        Naming no event keeps all eight. This is how the two "the scenario no longer
        names this event" tests drop an event whose record is already journalled, and
        how the resume tests narrow a run to the events their assertions are about.
        """
        events = tuple(
            event for event in self.scenario.events
            if not names or event.name in names)
        return dataclasses.replace(self.scenario, events=events)

    def _unsupported(self):
        """The fixture scenario with a create bzr's --from-json cannot carry.

        `estimated_hours` is finding G1: bzr's create JSON has no `estimated_time`.
        The fixture itself declares none, because it must be replayable end to end,
        so precondition 2's wiring needs a scenario that puts one back.
        """
        create = self._event(self.scenario, "create-checkout-race")
        postcondition = dict(create.expected_postcondition)
        postcondition["values"] = dict(
            postcondition["values"], estimated_hours="4.5")
        create = dataclasses.replace(
            create, expected_postcondition=freeze_planned(postcondition))
        return dataclasses.replace(
            self.scenario,
            events=(create,) + tuple(self.scenario.events[1:]))

    def _engine(self, run, scenario=None, *, opener=None, store=None):
        scenario = self._trimmed() if scenario is None else scenario
        context = ReplayContext(
            scenario, self.keys, bzr_path="bzr", base_url="http://127.0.0.1:8080/",
            workspace=self.workspace, run=run, opener=opener or _FakeOpener())
        return ReplayEngine(
            scenario, context, self.store if store is None else store,
            self.journal_dir, out=self.out.append)

    # --- journal fixtures --------------------------------------------------

    def _event(self, scenario, name: str):
        return next(event for event in scenario.events if event.name == name)

    def _in_flight(self, event, *, attempt: int = 1, digest: str | None = None):
        """Journal an in-flight record exactly as an interrupted earlier run would."""
        record = InFlightRecord(
            self.scenario.digest if digest is None else digest, event.name, attempt,
            event.actor, event.action_class, event.expected_postcondition,
            event.reconciliation_marker)
        self.store.write_in_flight(record)
        return record

    def _completed(self, event, next_action: str, resolved_ids=(), *,
                   attempt: int = 1, exit_status: int = 0):
        self._in_flight(event, attempt=attempt)
        boundary = (
            "bugzilla-rest-custom-field" if event.action == "bug.custom-field-set"
            else "bzr")
        record = CompletedRecord(
            self.scenario.digest, event.name, attempt, event.actor, event.action_class,
            event.expected_postcondition, event.reconciliation_marker,
            InvocationMetadata(boundary, "earlier run", (), (KEY_ENV,)),
            {}, exit_status, dict(resolved_ids), next_action)
        self.store.replace_completed(record)
        return record

    # --- the full run ------------------------------------------------------

    def _full_run_replies(self, create_reply=None):
        """The ten bzr replies one clean replay of the eight-event fixture consumes."""
        return [
            (4, None, 100),                      # pristine sweep: the alias is absent
            (4, None, 100),                      # the create's own pre-execution check
            (0, create_reply or {"id": 41}, None),   # bug create
            (0, {"id": 41}, None),               # bug view, read to compute the update
            (0, {}, None),                       # bug update
            (0, {"id": 5}, None),                # comment add
            (0, {"id": 7}, None),                # attachment upload
            (0, {}, None),                       # bug update --work-time
            (0, {}, None),                       # bug update --flag
            (0, {}, None),                       # attachment update
        ]

    def test_replay_executes_every_event_in_order(self) -> None:
        run = _FakeRun(self._full_run_replies())
        opener = _FakeOpener()
        engine = self._engine(run, opener=opener)
        self.assertEqual(engine.replay(), [
            ("executed", "create-checkout-race"),
            ("executed", "update-triage"),
            ("executed", "comment-triage"),
            ("executed", "attach-notes"),
            ("executed", "worktime-triage"),
            ("executed", "custom-field-triage"),
            ("executed", "flag-review"),
            ("executed", "obsolete-notes"),
        ])
        # Every event reached its own boundary operation, in scenario order, and the
        # one REST event addressed the bug the create resolved.
        self.assertEqual(
            [self.store.read(event.name).invocation.operation
             for event in self.scenario.events],
            ["bug create", "bug update", "comment add", "attachment upload",
             "bug update", "PUT rest/bug/41", "bug update", "attachment update"])
        for event in self.scenario.events:
            record = self.store.read(event.name)
            self.assertIsInstance(record, CompletedRecord)
            self.assertEqual(record.next_safe_action, "advance")
            self.assertEqual(record.exit_status, 0)
            self.assertEqual(record.attempt, 1)
        self.assertEqual(len(run.calls), 10)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(
            self.out[-1],
            "summary: 8 executed, 0 reconciled, 0 resumed, 0 already complete")

    # --- preconditions -----------------------------------------------------

    def test_replay_refuses_a_non_empty_journal(self) -> None:
        scenario = self._trimmed()
        self._in_flight(self._event(scenario, "create-checkout-race"))
        run = _FakeRun()
        with self.assertRaises(ReplayError) as caught:
            self._engine(run, scenario).replay()
        self.assertIn("create-checkout-race.000001.json", str(caught.exception))
        self.assertIn("use resume", str(caught.exception))
        self.assertEqual(run.calls, [])

    def test_replay_refuses_when_a_server_alias_already_exists(self) -> None:
        scenario = self._trimmed()
        alias = self._event(
            scenario, "create-checkout-race").expected_postcondition["values"][
                "server_alias"]
        run = _FakeRun([(0, {"id": 41}, None)])
        with self.assertRaises(ReplayError) as caught:
            self._engine(run, scenario).replay()
        self.assertIn(alias, str(caught.exception))
        self.assertIn("pristine baseline", str(caught.exception))
        self.assertIn("scripts/checkpoint", str(caught.exception))
        # Refused by the up-front sweep: one read, no mutation, nothing journalled.
        self.assertEqual(len(run.calls), 1)
        self.assertIn("view", run.calls[0]["argv"])
        self.assertIsNone(self.store.read("create-checkout-race"))

    def test_pristine_sweep_refuses_a_bug_the_actor_cannot_see(self) -> None:
        # api_code 102 is access denied, and BUG_ABSENT_CODES is {100, 101} only: a bug
        # the sweeping actor cannot see is not a bug that is not there. The run stops
        # with the boundary's own ProvisionError rather than reconciling (ADR 0006).
        run = _FakeRun([(4, None, 102)])
        with self.assertRaises(ProvisionError) as caught:
            self._engine(run).replay()
        self.assertIn("exit 4", str(caught.exception))
        self.assertEqual(len(run.calls), 1)
        self.assertIsNone(self.store.read("create-checkout-race"))

    def test_an_unsupported_payload_stops_before_any_mutation(self) -> None:
        # A create declaring estimated_hours, which bzr's create JSON cannot carry.
        # Precondition 2 binds both subcommands, so neither may reach the network.
        run = _FakeRun()
        for replay_or_resume in ("replay", "resume"):
            with self.assertRaises(ReplayError) as caught:
                getattr(self._engine(run, self._unsupported()), replay_or_resume)()
            self.assertIn("estimated_hours", str(caught.exception))
            self.assertIn("finding G1", str(caught.exception))
        self.assertEqual(run.calls, [])
        self.assertIsNone(self.store.read("create-checkout-race"))

    def test_replay_refuses_a_record_for_an_event_the_scenario_dropped(self) -> None:
        # A record whose event the scenario no longer names is unreachable by the
        # digest check, which reads records *through* current event names.
        self._in_flight(self._event(self.scenario, "comment-triage"))
        run = _FakeRun()
        with self.assertRaises(ReplayError) as caught:
            self._engine(run, self._trimmed("create-checkout-race")).replay()
        self.assertIn("comment-triage.000001.json", str(caught.exception))
        self.assertIn("no longer names", str(caught.exception))
        self.assertEqual(run.calls, [])

    def test_resume_refuses_a_record_for_an_event_the_scenario_dropped(self) -> None:
        self._in_flight(self._event(self.scenario, "comment-triage"))
        run = _FakeRun()
        with self.assertRaises(ReplayError) as caught:
            self._engine(run, self._trimmed("create-checkout-race")).resume()
        self.assertIn("comment-triage.000001.json", str(caught.exception))
        self.assertIn("no longer names", str(caught.exception))
        self.assertEqual(run.calls, [])

    def test_resume_refuses_a_changed_digest(self) -> None:
        scenario = self._trimmed()
        self._in_flight(
            self._event(scenario, "create-checkout-race"), digest="b" * 64)
        run = _FakeRun()
        with self.assertRaises(ReplayError) as caught:
            self._engine(run, scenario).resume()
        self.assertIn("b" * 64, str(caught.exception))
        self.assertIn(self.scenario.digest, str(caught.exception))
        self.assertIn("CONFIRM_RESET=1 make reset", str(caught.exception))
        self.assertEqual(run.calls, [])

    def test_resume_refuses_a_pre_existing_alias_for_an_unjournalled_event(self) -> None:
        # resume runs no up-front sweep, so this is the per-event check in _advance:
        # an event with no journal record has no proof this run attempted it, so a
        # present alias refuses instead of being adopted by a reconciliation.
        scenario = self._trimmed("create-checkout-race")
        alias = self._event(
            scenario, "create-checkout-race").expected_postcondition["values"][
                "server_alias"]
        run = _FakeRun([(0, {"id": 41}, None)])
        with self.assertRaises(ReplayError) as caught:
            self._engine(run, scenario).resume()
        self.assertIn(alias, str(caught.exception))
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(len(run.calls), 1)
        self.assertIsNone(self.store.read("create-checkout-race"))

    # --- resume ------------------------------------------------------------

    def test_resume_adopts_a_committed_in_flight_create(self) -> None:
        scenario = self._trimmed("create-checkout-race")
        self._in_flight(self._event(scenario, "create-checkout-race"))
        run = _FakeRun([(0, {"id": 41, "summary": "Checkout races"}, None)])
        engine = self._engine(run, scenario)
        self.assertEqual(engine.resume(), [("resumed", "create-checkout-race")])
        record = self.store.read("create-checkout-race")
        self.assertIsInstance(record, CompletedRecord)
        self.assertEqual(record.next_safe_action, "advance")
        self.assertEqual(record.resolved_ids, {"bug:checkout-race": 41})
        # exit_status -1 means the invocation's own status was never observed.
        self.assertEqual(record.exit_status, -1)
        # The create is never repeated: the one call is the alias read.
        self.assertEqual(len(run.calls), 1)
        self.assertIn("view", run.calls[0]["argv"])
        self.assertNotIn("create", run.calls[0]["argv"])
        self.assertIsNone(self.store.read("create-checkout-race", 2))

    def test_resume_retries_an_in_flight_create_the_server_never_saw(self) -> None:
        scenario = self._trimmed("create-checkout-race")
        self._in_flight(self._event(scenario, "create-checkout-race"))
        run = _FakeRun([(4, None, 100), (0, {"id": 41}, None)])
        self.assertEqual(
            self._engine(run, scenario).resume(),
            [("executed", "create-checkout-race")])
        first = self.store.read("create-checkout-race", 1)
        self.assertEqual(first.next_safe_action, "retry")
        self.assertEqual(first.exit_status, -1)
        second = self.store.read("create-checkout-race", 2)
        self.assertIsInstance(second, CompletedRecord)
        self.assertEqual(second.attempt, 2)
        self.assertEqual(second.next_safe_action, "advance")
        self.assertEqual(second.exit_status, 0)
        self.assertEqual(second.resolved_ids, {"bug:checkout-race": 41})
        self.assertEqual(len(run.calls), 2)
        self.assertIn("create", run.calls[1]["argv"])

    def test_resume_executes_a_completed_retry_at_the_next_attempt(self) -> None:
        # The record an aborted run leaves behind: reconciliation settled attempt 1 as
        # `retry`, so the operator's next resume owns attempt 2. Nothing is re-read --
        # the journalled answer is what makes the re-execution safe.
        scenario = self._trimmed("create-checkout-race")
        self._completed(
            self._event(scenario, "create-checkout-race"), "retry", exit_status=-1)
        run = _FakeRun([(0, {"id": 41}, None)])
        self.assertEqual(
            self._engine(run, scenario).resume(),
            [("executed", "create-checkout-race")])
        second = self.store.read("create-checkout-race", 2)
        self.assertIsInstance(second, CompletedRecord)
        self.assertEqual(second.attempt, 2)
        self.assertEqual(second.next_safe_action, "advance")
        self.assertEqual(second.exit_status, 0)
        self.assertEqual(second.resolved_ids, {"bug:checkout-race": 41})
        self.assertEqual(len(run.calls), 1)
        self.assertIn("create", run.calls[0]["argv"])

    def test_resume_raises_the_reconciliation_detail_for_an_in_flight_stop(self) -> None:
        # An in-flight record whose reconciliation cannot tell one commit from two:
        # the halt has to carry the reconciliation's own detail, not a generic message,
        # because the detail is the only place the ambiguity is described.
        scenario = self._trimmed("create-checkout-race", "comment-triage")
        self._completed(
            self._event(scenario, "create-checkout-race"), "advance",
            {"bug:checkout-race": 41})
        comment = self._event(scenario, "comment-triage")
        self._in_flight(comment)
        marker = comment.reconciliation_marker
        run = _FakeRun([
            (0, [{"id": 5, "text": f"triaged [{marker}]"},
                 {"id": 6, "text": f"triaged again [{marker}]"}], None),
        ])
        with self.assertRaises(ReplayError) as caught:
            self._engine(run, scenario).resume()
        message = str(caught.exception)
        self.assertIn("comment-triage", message)
        self.assertIn("matches 2 results", message)
        self.assertIn(marker, message)
        self.assertIn("CONFIRM_RESET=1 make reset", message)
        record = self.store.read("comment-triage")
        self.assertIsInstance(record, CompletedRecord)
        self.assertEqual(record.next_safe_action, "stop")
        self.assertEqual(record.exit_status, -1)
        # The comment was reconciled, never re-sent: the one call is the list read.
        self.assertEqual(len(run.calls), 1)
        self.assertIn("list", run.calls[0]["argv"])

    def test_resume_refuses_a_completed_record_it_cannot_interpret(self) -> None:
        # "reconcile" is legal in CompletedRecord's Literal and this engine never
        # writes it, so a record carrying it came from somewhere else; refusing beats
        # re-executing a mutation on an unreadable instruction.
        scenario = self._trimmed("create-checkout-race")
        self._completed(
            self._event(scenario, "create-checkout-race"), "reconcile", exit_status=-1)
        run = _FakeRun()
        with self.assertRaises(ReplayError) as caught:
            self._engine(run, scenario).resume()
        message = str(caught.exception)
        self.assertIn("create-checkout-race", message)
        self.assertIn("reconcile", message)
        self.assertIn("CONFIRM_RESET=1 make reset", message)
        self.assertEqual(run.calls, [])

    def test_resume_refuses_a_recorded_stop_without_querying(self) -> None:
        # A stop is terminal in the journal: the refusal is durable rather than
        # dependent on the server still looking ambiguous, so nothing is re-queried.
        scenario = self._trimmed("comment-triage")
        # A stop is only ever reached through reconciliation, hence exit_status -1.
        self._completed(
            self._event(scenario, "comment-triage"), "stop", exit_status=-1)
        run = _FakeRun()
        with self.assertRaises(ReplayError) as caught:
            self._engine(run, scenario).resume()
        self.assertIn("comment-triage", str(caught.exception))
        self.assertIn("CONFIRM_RESET=1 make reset", str(caught.exception))
        self.assertEqual(run.calls, [])

    def test_resume_skips_completed_events_and_rebuilds_the_id_table(self) -> None:
        scenario = self._trimmed("create-checkout-race", "comment-triage")
        self._completed(
            self._event(scenario, "create-checkout-race"), "advance",
            {"bug:checkout-race": 41})
        run = _FakeRun([(0, {"id": 5}, None)])
        self.assertEqual(self._engine(run, scenario).resume(), [
            ("skipped", "create-checkout-race"),
            ("executed", "comment-triage"),
        ])
        # The completed create was neither re-read nor re-executed, and the comment
        # resolved bug:checkout-race from the record's ids rather than from a read.
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(run.calls[0]["argv"][-2:], ["--", "41"])
        self.assertEqual(
            self.store.read("comment-triage").invocation.arguments[-1], "41")
        self.assertEqual(
            self.out[-1],
            "summary: 1 executed, 0 reconciled, 0 resumed, 1 already complete")

    # --- reconciliation inside a run ---------------------------------------

    def test_an_exit_zero_reply_with_no_usable_id_reconciles(self) -> None:
        """ADR 0006's third reconciliation trigger, distinct from the other two.

        The create exits 0 but the reply carries no id, so `resolved_ids` raises; the
        alias read that follows finds the bug, proving the mutation committed. The run
        continues rather than aborting on "returned no bug id".
        """
        scenario = self._trimmed("create-checkout-race", "comment-triage")
        run = _FakeRun([
            (4, None, 100),            # pristine sweep: the alias is absent
            (4, None, 100),            # the create's own pre-execution check
            (0, {}, None),             # bug create exits 0 carrying no id
            (0, {"id": 41}, None),     # the reconciliation read finds the bug
            (0, {"id": 5}, None),      # ... and the run continues into the comment
        ])
        engine = self._engine(run, scenario)
        self.assertEqual(engine.replay(), [
            ("reconciled", "create-checkout-race"),
            ("executed", "comment-triage"),
        ])
        record = self.store.read("create-checkout-race")
        self.assertEqual(record.next_safe_action, "advance")
        self.assertEqual(record.resolved_ids, {"bug:checkout-race": 41})
        self.assertEqual(record.exit_status, -1)
        # The reconciled id was adopted, so the next event addressed the right bug.
        self.assertEqual(run.calls[-1]["argv"][-2:], ["--", "41"])
        # A reconciled result is not a mutation this run sent: the summary says so.
        self.assertEqual(
            self.out[-1],
            "summary: 1 executed, 1 reconciled, 0 resumed, 0 already complete")

    def test_a_failing_mutation_aborts_the_run_and_journals_the_retry(self) -> None:
        """One execution per event per run: attempt 2 belongs to the operator's resume.

        The mutation fails, reconciliation answers "absent" (so nothing committed), and
        the run aborts carrying both errors -- the boundary's own, which says what broke,
        and the reconciliation's detail, which says what the fixture now holds.
        """
        scenario = self._trimmed("create-checkout-race")
        run = _FakeRun([
            (4, None, 100),            # pristine sweep: the alias is absent
            (4, None, 100),            # the create's own pre-execution check
            (1, None, None),           # the create fails
            (4, None, 100),            # the reconciliation read: still absent
        ])
        with self.assertRaises(ReplayError) as caught:
            self._engine(run, scenario).replay()
        message = str(caught.exception)
        self.assertIn("create-checkout-race", message)
        self.assertIn("bzr boundary failure (exit 1)", message)   # the boundary's own
        self.assertIn("did not commit", message)                  # result.detail
        record = self.store.read("create-checkout-race")
        self.assertIsInstance(record, CompletedRecord)
        self.assertEqual(record.next_safe_action, "retry")
        self.assertEqual(record.exit_status, -1)
        # The retry is journalled, not taken: this run stops at attempt 1.
        self.assertIsNone(self.store.read("create-checkout-race", 2))
        self.assertEqual(len(run.calls), 4)

    def test_a_failing_reconciliation_read_leaves_the_in_flight_record(self) -> None:
        """ADR 0006: no completed record is written over a read that failed."""
        scenario = self._trimmed("create-checkout-race")
        run = _FakeRun([
            (4, None, 100),            # pristine sweep: the alias is absent
            (4, None, 100),            # the create's own pre-execution check
            (1, None, None),           # the create fails
            (1, None, None),           # and so does the reconciliation read
        ])
        engine = self._engine(run, scenario)
        with self.assertRaises(ProvisionError):
            engine.replay()
        record = self.store.read("create-checkout-race")
        self.assertIsInstance(record, InFlightRecord)
        self.assertNotIsInstance(record, CompletedRecord)
        self.assertEqual(record.attempt, 1)
        self.assertIsNone(self.store.read("create-checkout-race", 2))

    # --- secrets -----------------------------------------------------------

    def test_no_journal_record_contains_an_api_key(self) -> None:
        # The create's reply embeds the reporter's key in an ordinary field, so a
        # journal write that dropped known_secrets would leave it in the record file.
        run = _FakeRun(self._full_run_replies(
            create_reply={"id": 41, "note": "created by SECRET-KEY-reporter"}))
        opener = _FakeOpener()
        store = _RecordingStore(self.store)
        engine = self._engine(run, opener=opener, store=store)
        self.assertEqual(len(engine.replay()), 8)
        keys = {"SECRET-KEY-reporter", "SECRET-KEY-triager"}
        written = sorted(path for path in os.listdir(self.journal_dir)
                         if path.endswith(".json"))
        self.assertEqual(len(written), 8)
        for name in written:
            content = (self.journal_dir / name).read_text(encoding="utf-8")
            for key in keys:
                self.assertNotIn(key, content, f"{name} carries {key}")
        # The redaction ran rather than the key merely never arriving: the whole
        # "SECRET-KEY-reporter" is the secret, so removing it leaves the prefix alone.
        self.assertEqual(
            self.store.read("create-checkout-race").handler_output["note"],
            "created by ")
        # Every write -- in-flight as well as completed -- carried the run's secrets.
        self.assertEqual(len(store.writes), 16)
        self.assertTrue(all(store.writes))
        # The key reaches bzr only through the environment, never through argv.
        for call in run.calls:
            for argument in call["argv"]:
                self.assertNotIn("SECRET-KEY", argument)
            self.assertTrue(call["env"][KEY_ENV].startswith("SECRET-KEY-"))
        # ... and reaches REST only in the JSON body, never the query string.
        self.assertEqual(opener.requests[0]["body"]["api_key"], "SECRET-KEY-triager")
        self.assertNotIn("SECRET-KEY", opener.requests[0]["url"])


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


if __name__ == "__main__":
    unittest.main()
