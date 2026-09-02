from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from bzr_live.provision import KeyStore
from bzr_live.replay import ReplayContext
from bzr_live.replay.context import KEY_ENV
from bzr_live.scenario import (
    CompletedRecord,
    InFlightRecord,
    InvocationMetadata,
    JournalStore,
    Reference,
    load_scenario,
)
from bzr_live.verify import VerifyError
from bzr_live.verify.expected import ExpectedBug, ExpectedComment, ExpectedScenario, fold
from bzr_live.verify.observed import LINKS_MAX_NODES, VIEW_FIELDS, ServerReader
from bzr_live.verify.runner import (
    Verifier,
    check_comment_transport,
    check_link_bound,
    check_reader_keys,
    check_roles,
    resolve_ids,
)

# Anchored to this file, not the CWD, matching tests/test_replay.py:32. The fixture
# declares no actor in the insider group, which is what the missing-role cases need.
FIXTURE = Path(__file__).parent / "fixtures" / "replay-scenario"
_OTHER_DIGEST = "0" * 64


class _FakeRun:
    """Stands in for subprocess.run: records calls, returns queued CompletedProcess."""

    def __init__(self, replies=None):
        self.calls: list[dict] = []
        # (exit_code, stdout_payload_or_None, api_code_or_None), as tests/test_replay.py.
        self.replies = list(replies or [])

    def __call__(self, argv, capture_output=False, env=None, shell=False, input=None):
        self.calls.append({"argv": list(argv), "env": dict(env or {})})
        code, payload, api_code = (
            self.replies.pop(0) if self.replies else (0, {}, None))
        stdout = json.dumps(payload).encode("utf-8") if payload is not None else b""
        stderr = b"" if api_code is None else json.dumps(
            {"error": {"api_code": api_code}}).encode("utf-8")
        return subprocess.CompletedProcess(argv, code, stdout, stderr)


def _bug(alias: str, *, edges=None, comments=()) -> ExpectedBug:
    """An ExpectedBug carrying only the fields the check under test reads."""
    return ExpectedBug(
        alias=alias, creator="reporter", description="", scalars={}, names={},
        edges=edges or {}, duplicate_of=None, custom_fields={}, flags=(),
        comments=tuple(comments), attachments=(), history=(), unverifiable=(),
        unasserted=frozenset())


def _star(leaves: int) -> ExpectedScenario:
    """One root depending on `leaves` bugs, with the inverse edge fold materializes."""
    names = [f"leaf-{index:04d}" for index in range(leaves)]
    bugs = {"root": _bug("root", edges={"depends_on": frozenset(names)})}
    for name in names:
        bugs[name] = _bug(name, edges={"blocks": frozenset({"root"})})
    return ExpectedScenario(
        bugs=bugs, insider="admin-ops", outsider="triager", custom_field_keys=(),
        actor_emails={})


class _JournalFixture(unittest.TestCase):
    """The replay fixture with a real journal, as tests/test_replay.py builds one."""

    def setUp(self) -> None:
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.root = Path(state.name) / "state"
        self.keys = KeyStore(self.root)
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = workspace.name
        self.scenario = load_scenario(FIXTURE)
        self.expected = fold(self.scenario)
        # The spec puts the journal at <state-root>/journal/<scenario name>/;
        # JournalStore creates only the leaf, so the parent is made here.
        self.journal_dir = self.root / "journal" / self.scenario.name
        os.mkdir(self.journal_dir.parent, 0o700)
        self.store = JournalStore(self.journal_dir)
        self.addCleanup(self.store.close)

    def _event(self, name: str):
        return next(event for event in self.scenario.events if event.name == name)

    def _complete(self, event, *, next_action: str = "advance", resolved_ids=(),
                  digest: str | None = None) -> None:
        """Journal one event exactly as a completed earlier run would have."""
        digest = self.scenario.digest if digest is None else digest
        boundary = (
            "bugzilla-rest-custom-field" if event.action == "bug.custom-field-set"
            else "bzr")
        self.store.write_in_flight(InFlightRecord(
            digest, event.name, 1, event.actor, event.action_class,
            event.expected_postcondition, event.reconciliation_marker))
        self.store.replace_completed(CompletedRecord(
            digest, event.name, 1, event.actor, event.action_class,
            event.expected_postcondition, event.reconciliation_marker,
            InvocationMetadata(boundary, "earlier run", (), (KEY_ENV,)),
            {}, 0, dict(resolved_ids), next_action))

    def _journal_every_event(self) -> None:
        identities = {
            "create-checkout-race": {"bug:checkout-race": 41},
            "attach-notes": {"attachment:triage-notes": 7},
        }
        for event in self.scenario.events:
            self._complete(event, resolved_ids=identities.get(event.name, ()))


class ResolveIdsTest(_JournalFixture):
    def test_a_complete_journal_resolves_every_created_identity(self) -> None:
        self._journal_every_event()
        self.assertEqual(
            resolve_ids(self.scenario, self.store),
            {"bug:checkout-race": 41, "attachment:triage-notes": 7})

    def test_an_absent_record_names_the_event_and_the_state_root(self) -> None:
        self._complete(self._event("create-checkout-race"),
                       resolved_ids={"bug:checkout-race": 41})
        with self.assertRaises(VerifyError) as caught:
            resolve_ids(self.scenario, self.store)
        message = str(caught.exception)
        self.assertIn("'update-triage'", message)
        self.assertIn("no completed journal record under this state root", message)
        self.assertIn("replay the scenario here", message)

    def test_a_foreign_digest_names_both_digests(self) -> None:
        self._complete(self._event("create-checkout-race"), digest=_OTHER_DIGEST,
                       resolved_ids={"bug:checkout-race": 41})
        with self.assertRaises(VerifyError) as caught:
            resolve_ids(self.scenario, self.store)
        message = str(caught.exception)
        self.assertIn("'create-checkout-race'", message)
        self.assertIn(_OTHER_DIGEST, message)
        self.assertIn(self.scenario.digest, message)

    def test_a_retry_record_names_the_event_and_the_recorded_action(self) -> None:
        self._complete(self._event("create-checkout-race"), next_action="retry",
                       resolved_ids={"bug:checkout-race": 41})
        with self.assertRaises(VerifyError) as caught:
            resolve_ids(self.scenario, self.store)
        message = str(caught.exception)
        self.assertIn("'create-checkout-race'", message)
        self.assertIn("'retry'", message)
        self.assertIn("did not complete", message)


class ReaderKeysTest(_JournalFixture):
    def test_a_declared_reader_without_a_key_names_the_actor_in_one_line(self) -> None:
        # The fixture declares no insider, so the outsider is the only role checked.
        self.assertIsNone(self.expected.insider)
        self.assertEqual(self.expected.outsider, "triager")
        with self.assertRaises(VerifyError) as caught:
            check_reader_keys(self.expected, self.keys)
        message = str(caught.exception)
        self.assertIn("'triager'", message)
        self.assertIn("has no API key under this state root", message)
        # The CLI prints one `verify failed: <reason>` line, so the reason is one line.
        self.assertNotIn("\n", message)

    def test_a_declared_reader_with_a_key_passes(self) -> None:
        self.keys.store_actor_key("triager", "SECRET-KEY-triager")
        self.assertIsNone(check_reader_keys(self.expected, self.keys))

    def test_an_undeclared_role_is_not_a_precondition_failure(self) -> None:
        findings = check_roles(self.expected)
        self.assertEqual([finding.kind for finding in findings], ["unverifiable"])
        self.assertEqual(findings[0].subject, "insider")


class LinkBoundTest(unittest.TestCase):
    def test_a_graph_at_the_bound_passes(self) -> None:
        self.assertIsNone(check_link_bound(_star(LINKS_MAX_NODES)))

    def test_a_graph_above_the_bound_cites_the_constant(self) -> None:
        with self.assertRaises(VerifyError) as caught:
            check_link_bound(_star(LINKS_MAX_NODES + 1))
        message = str(caught.exception)
        self.assertIn("'root'", message)
        self.assertIn(str(LINKS_MAX_NODES + 1), message)
        self.assertIn(f"LINKS_MAX_NODES of {LINKS_MAX_NODES}", message)


class CommentTransportTest(unittest.TestCase):
    ALIAS = "pay-token-leak"
    MARKER = "bzr-live:smoke:comment-token-leak"

    def _bug_with_private_comment(self) -> ExpectedBug:
        return _bug(self.ALIAS, comments=(
            ExpectedComment(None, "reporter", False, "The token leaks."),
            ExpectedComment(self.MARKER, "admin-ops", True, None)))

    def test_a_thread_carrying_every_declared_marker_passes(self) -> None:
        reply = [
            {"text": "The token leaks."},
            {"text": f"[{self.MARKER}] rotated, restricted to the insider group"},
        ]
        self.assertIsNone(check_comment_transport(
            self.ALIAS, self._bug_with_private_comment(), reply))

    def test_a_missing_private_marker_names_the_missing_package(self) -> None:
        with self.assertRaises(VerifyError) as caught:
            check_comment_transport(
                self.ALIAS, self._bug_with_private_comment(),
                [{"text": "The token leaks."}])
        message = str(caught.exception)
        self.assertIn(f"'{self.ALIAS}'", message)
        self.assertIn(f"[{self.MARKER}]", message)
        # The whole value of this precondition is that the operator is sent to the
        # image's missing package, not to debug the replay.
        self.assertIn("XMLRPC::Lite", message)
        self.assertIn("libxmlrpc-lite-perl", message)
        self.assertIn("make up", message)

    def test_a_missing_public_marker_is_not_a_transport_gap(self) -> None:
        # A public comment the insider cannot see is check 4's divergence to report;
        # reporting it here would claim a bzr defect that does not exist.
        public = _bug(self.ALIAS, comments=(
            ExpectedComment(None, "reporter", False, "The token leaks."),
            ExpectedComment(self.MARKER, "triager", False, None)))
        self.assertIsNone(check_comment_transport(
            self.ALIAS, public, [{"text": "The token leaks."}]))


class ServerReaderTest(_JournalFixture):
    ACTOR = Reference("actor", "triager")
    KEY = "SECRET-KEY-triager"

    def _reader(self, replies) -> tuple[ServerReader, _FakeRun]:
        self.keys.store_actor_key(self.ACTOR.name, self.KEY)
        run = _FakeRun(replies)
        context = ReplayContext(
            self.scenario, self.keys, bzr_path="bzr",
            base_url="http://127.0.0.1:8080/", workspace=self.workspace, run=run)
        return ServerReader(context, self.ACTOR), run

    def test_view_fields_carry_the_two_fields_bzr_a7f6ab70_added(self) -> None:
        self.assertIn("groups", VIEW_FIELDS)
        self.assertIn("estimated_time", VIEW_FIELDS)

    def test_a_bug_read_requests_the_declared_fields_with_the_key_off_argv(self) -> None:
        reader, run = self._reader([(0, {"id": 41, "summary": "s"}, None)])
        self.assertEqual(reader.bug(41, VIEW_FIELDS), {"id": 41, "summary": "s"})
        argv = run.calls[0]["argv"]
        self.assertIn(f"--fields={','.join(VIEW_FIELDS)}", argv)
        self.assertEqual(argv[-2:], ["--", "41"])
        self.assertNotIn(self.KEY, " ".join(argv))
        self.assertEqual(run.calls[0]["env"][KEY_ENV], self.KEY)

    def test_an_absent_bug_names_the_journal_and_the_actor(self) -> None:
        reader, _ = self._reader([(4, None, 100)])
        with self.assertRaises(VerifyError) as caught:
            reader.bug(41, VIEW_FIELDS)
        message = str(caught.exception)
        self.assertIn("as triager reported not-found", message)
        self.assertIn("the fixture does not hold", message)

    def test_a_list_read_refuses_a_non_list_reply(self) -> None:
        reader, _ = self._reader([(0, {"id": 1}, None)])
        with self.assertRaises(VerifyError) as caught:
            reader.comments(41)
        self.assertIn("unrecognised shape", str(caught.exception))

    def test_a_recursive_link_read_carries_its_depth(self) -> None:
        reader, run = self._reader([(0, [], None)])
        self.assertEqual(reader.links(41, depth=2), [])
        argv = run.calls[0]["argv"]
        self.assertIn("--recursive", argv)
        self.assertIn("--depth=2", argv)

    def test_a_direct_link_read_is_not_recursive(self) -> None:
        reader, run = self._reader([(0, [], None)])
        self.assertEqual(reader.links(41), [])
        self.assertNotIn("--recursive", run.calls[0]["argv"])


class VerifierPreconditionTest(_JournalFixture):
    def _verifier(self, out: list[str]) -> Verifier:
        def forbid(*args, **kwargs):
            raise AssertionError("a read was issued before the preconditions held")

        context = ReplayContext(
            self.scenario, self.keys, bzr_path="bzr",
            base_url="http://127.0.0.1:8080/", workspace=self.workspace, run=forbid)
        return Verifier(self.scenario, context, self.store, str(FIXTURE), self.keys,
                        out=out.append)

    def test_a_precondition_refusal_names_the_scenario_directory(self) -> None:
        # Issue #20 requires every failure to name the scenario path.
        self.keys.store_actor_key("triager", "SECRET-KEY-triager")
        with self.assertRaises(VerifyError) as caught:
            self._verifier([]).run()
        message = str(caught.exception)
        self.assertIn(str(FIXTURE), message)
        self.assertIn("'create-checkout-race'", message)

    def test_a_missing_insider_is_unverifiable_and_the_run_still_succeeds(self) -> None:
        self.keys.store_actor_key("triager", "SECRET-KEY-triager")
        self._journal_every_event()
        out: list[str] = []
        self.assertEqual(self._verifier(out).run(), 0)
        self.assertEqual(len(out), 1)
        self.assertIn(str(FIXTURE), out[0])
        self.assertIn("insider", out[0])


if __name__ == "__main__":
    unittest.main()
