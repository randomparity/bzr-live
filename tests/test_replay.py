from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from bzr_live.provision import KeyStore, ProvisionError
from bzr_live.provision.adapters import BUG_ABSENT_CODES, BzrClient
from bzr_live.replay import HANDLERS, ReplayContext, ReplayError
from bzr_live.scenario import PlannedEvent, PlannedResource, Reference, load_scenario

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


if __name__ == "__main__":
    unittest.main()
