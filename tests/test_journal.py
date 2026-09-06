from __future__ import annotations

import json
import os
import subprocess
import sys
import stat
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from bzr_live.scenario import (
    CompletedRecord,
    InFlightRecord,
    InvocationMetadata,
    JournalStore,
    Reference,
    ScenarioValidationError,
    freeze_planned,
)


class JournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.state = Path(self._temporary.name) / "state"
        self.digest = "a" * 64
        self.postcondition = freeze_planned(
            {
                "action": "bug.comment",
                "target": Reference("bug", "race"),
                "values": {"body": "safe", "private": False},
                "marker": "bzr-live:smoke:comment",
            }
        )

    def in_flight(self, attempt: int = 1) -> InFlightRecord:
        return InFlightRecord(
            scenario_digest=self.digest,
            event="comment",
            attempt=attempt,
            actor=Reference("actor", "ada"),
            action_class="append",
            expected_postcondition=self.postcondition,
            reconciliation_marker="bzr-live:smoke:comment",
        )

    def completed(
        self,
        attempt: int = 1,
        next_action: str = "advance",
        handler_output: object | None = None,
    ) -> CompletedRecord:
        return CompletedRecord(
            scenario_digest=self.digest,
            event="comment",
            attempt=attempt,
            actor=Reference("actor", "ada"),
            action_class="append",
            expected_postcondition=self.postcondition,
            reconciliation_marker="bzr-live:smoke:comment",
            invocation=InvocationMetadata(
                mutation_boundary="bzr",
                operation="bug comment",
                arguments=("--body", "safe"),
                environment_names=("BUGZILLA_API_KEY",),
            ),
            handler_output=freeze_planned({"ok": True}) if handler_output is None else handler_output,
            exit_status=0,
            resolved_ids=MappingProxyType({"bug:race": 42, "actor:ada": 7}),
            next_safe_action=next_action,
        )

    def test_creates_owner_only_state_lock_and_attempt_files_under_restrictive_umask(self) -> None:
        previous = os.umask(0o777)
        try:
            with JournalStore(self.state) as store:
                path = store.write_in_flight(self.in_flight())
                self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((self.state / ".lock").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        finally:
            os.umask(previous)

    def test_existing_wrong_modes_and_symlinks_fail_without_repair(self) -> None:
        self.state.mkdir(mode=0o755)
        with self.assertRaises(ScenarioValidationError):
            JournalStore(self.state)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o755)
        self.state.chmod(0o700)
        (self.state / ".lock").write_text("", encoding="utf-8")
        (self.state / ".lock").chmod(0o644)
        with self.assertRaises(ScenarioValidationError):
            JournalStore(self.state)
        self.assertEqual(stat.S_IMODE((self.state / ".lock").stat().st_mode), 0o644)

    @unittest.skipUnless(sys.platform == "darwin", "macOS ACL semantics")
    def test_existing_access_acl_fails_without_repair(self) -> None:
        self.state.mkdir(mode=0o700)
        subprocess.run(
            ["chmod", "+a", "everyone allow read", str(self.state)],
            check=True,
        )
        with self.assertRaises(ScenarioValidationError):
            JournalStore(self.state)

    @unittest.skipUnless(sys.platform == "darwin", "macOS ACL semantics")
    def test_new_state_clears_inherited_access_acl(self) -> None:
        subprocess.run(
            [
                "chmod",
                "+a",
                "everyone allow read,search,file_inherit,directory_inherit",
                str(self.state.parent),
            ],
            check=True,
        )
        with JournalStore(self.state) as store:
            attempt = store.write_in_flight(self.in_flight())
        for path in (self.state, self.state / ".lock", attempt):
            listing = subprocess.run(
                ["ls", "-lde", str(path)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertFalse(listing.split()[0].endswith("+"))

    @unittest.skipUnless(sys.platform == "darwin", "macOS ACL semantics")
    def test_retained_files_for_other_events_and_temporary_files_reject_acls(self) -> None:
        with JournalStore(self.state) as store:
            attempt = store.write_in_flight(self.in_flight())
        attempt_document = json.loads(attempt.read_text(encoding="utf-8"))
        attempt_document["event"] = "other"
        candidates = {
            ".tmp-crash": "{}",
            "other.000001.json": json.dumps(attempt_document),
        }
        for name, content in candidates.items():
            with self.subTest(name=name):
                candidate = self.state / name
                candidate.write_text(content, encoding="utf-8")
                candidate.chmod(0o600)
                subprocess.run(
                    ["chmod", "+a", "everyone allow read", str(candidate)],
                    check=True,
                )
                with JournalStore(self.state) as store:
                    with self.assertRaises(ScenarioValidationError):
                        store.read("comment")
                candidate.unlink()

    def test_second_store_fails_while_lock_is_held(self) -> None:
        with JournalStore(self.state):
            with self.assertRaises(ScenarioValidationError):
                JournalStore(self.state)

    def test_path_replacement_does_not_redirect_operations(self) -> None:
        moved = self.state.with_name("moved")
        with JournalStore(self.state) as store:
            self.state.rename(moved)
            self.state.mkdir(mode=0o700)
            path = store.write_in_flight(self.in_flight())
            self.assertEqual(path.name, "comment.000001.json")
            self.assertTrue((moved / path.name).is_file())
            self.assertFalse((self.state / path.name).exists())

    def test_replaces_in_flight_and_preserves_retry_history(self) -> None:
        with JournalStore(self.state) as store:
            store.write_in_flight(self.in_flight())
            store.replace_completed(self.completed(next_action="retry"))
            store.write_in_flight(self.in_flight(attempt=2))
            store.replace_completed(self.completed(attempt=2))
            first = store.read("comment", 1)
            latest = store.read("comment")
        self.assertIsInstance(first, CompletedRecord)
        self.assertEqual(latest.attempt, 2)  # type: ignore[union-attr]
        self.assertEqual(first.next_safe_action, "retry")  # type: ignore[union-attr]

    def test_rejects_invalid_retry_history_when_reopened(self) -> None:
        with JournalStore(self.state) as store:
            store.write_in_flight(self.in_flight())
            store.replace_completed(self.completed(next_action="retry"))
            store.write_in_flight(self.in_flight(attempt=2))
            store.replace_completed(self.completed(attempt=2, next_action="retry"))
        first_path = self.state / "comment.000001.json"
        document = json.loads(first_path.read_text(encoding="utf-8"))
        document["next_safe_action"] = "advance"
        first_path.write_text(json.dumps(document), encoding="utf-8")
        first_path.chmod(0o600)
        with JournalStore(self.state) as store:
            with self.assertRaises(ScenarioValidationError):
                store.read("comment")
        document["next_safe_action"] = "retry"
        first_path.write_text(json.dumps(document), encoding="utf-8")
        first_path.chmod(0o600)
        second_path = self.state / "comment.000002.json"
        second_document = json.loads(second_path.read_text(encoding="utf-8"))
        second_document["scenario_digest"] = "b" * 64
        second_path.write_text(json.dumps(second_document), encoding="utf-8")
        second_path.chmod(0o600)
        with JournalStore(self.state) as store:
            with self.assertRaises(ScenarioValidationError):
                store.read("comment")


    def test_rejects_gaps_mismatches_and_completed_overwrite(self) -> None:
        with JournalStore(self.state) as store:
            with self.assertRaises(ScenarioValidationError):
                store.write_in_flight(self.in_flight(attempt=2))
            store.write_in_flight(self.in_flight())
            wrong = self.completed()
            object.__setattr__(wrong, "scenario_digest", "b" * 64)
            with self.assertRaises(ScenarioValidationError):
                store.replace_completed(wrong)
            store.replace_completed(self.completed())
            with self.assertRaises(ScenarioValidationError):
                store.replace_completed(self.completed())
            with self.assertRaises(ScenarioValidationError):
                store.write_in_flight(self.in_flight(attempt=2))

    def test_failed_install_preserves_prior_state(self) -> None:
        with JournalStore(self.state) as store:
            with mock.patch("bzr_live.scenario.journal.os.link", side_effect=OSError("fault")):
                with self.assertRaises(OSError):
                    store.write_in_flight(self.in_flight())
            self.assertIsNone(store.read("comment"))
            self.assertEqual([path.name for path in self.state.iterdir()], [".lock"])

    def test_post_replace_fsync_failure_preserves_completion(self) -> None:
        with JournalStore(self.state) as store:
            store.write_in_flight(self.in_flight())
            real_fsync = os.fsync
            calls = 0

            def fail_directory(fd: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("directory fsync fault")
                real_fsync(fd)

            with mock.patch("bzr_live.scenario.journal.os.fsync", side_effect=fail_directory):
                with self.assertRaises(OSError):
                    store.replace_completed(self.completed())
            self.assertIsInstance(store.read("comment"), CompletedRecord)
            self.assertFalse(any(path.name.startswith(".tmp-") for path in self.state.iterdir()))

    def test_redacts_sensitive_keys_and_known_secret_substrings(self) -> None:
        output = {
            "API_KEY": "key-value",
            "AUTHORIZATION": "auth-value",
            "clientAPIKey": "client-value",
            "proxyAuthorization": "proxy-value",
            "APIToken": "token-value",
            "APISecret": "secret-value",
            "HTTPAuthorization": "auth-value",
            "access_token": "token-value",
            "refreshToken": "refresh-value",
            "client-secret": "secret-value",
            "set-cookie": "cookie-value",
            "message": "redacted<OPAQUEredacted",
            "nested": ["safe-OPAQUE-value"],
            "prefix-LEAK": "key text",
            "toLEAKken": "credential-value",
        }
        completed = self.completed()
        object.__setattr__(completed, "handler_output", freeze_planned(output))
        with JournalStore(self.state) as store:
            store.write_in_flight(
                self.in_flight(), known_secrets=("redacted", "<", "OPAQUE", "LEAK")
            )
            store.replace_completed(
                completed, known_secrets=("redacted", "<", "OPAQUE", "LEAK")
            )
            stored = store.read("comment")
        self.assertIsInstance(stored, CompletedRecord)
        redacted = stored.handler_output  # type: ignore[union-attr]
        for key in output.keys() - {"message", "nested", "prefix-LEAK", "toLEAKken"}:
            self.assertIsNone(redacted[key])
        self.assertEqual(redacted["message"], "")
        self.assertEqual(redacted["nested"], ("safe--value",))
        self.assertNotIn("prefix-LEAK", redacted)
        self.assertEqual(redacted["prefix-"], "key text")
        self.assertIsNone(redacted["token"])
        self.assertEqual(stored.event, "comment")  # type: ignore[union-attr]

    def test_rejects_known_secrets_in_structural_fields_without_echoing(self) -> None:
        secret = "smoke"
        with JournalStore(self.state) as store:
            with self.assertRaises(ScenarioValidationError) as caught:
                store.write_in_flight(self.in_flight(), known_secrets=(secret,))
        self.assertNotIn(secret, str(caught.exception))
        structural = freeze_planned(
            {
                "action": "bug.comment",
                "target": Reference("bug", "race"),
                "values": {"prefix-SECRET": "value"},
                "marker": "bzr-live:smoke:comment",
            }
        )
        record = self.in_flight()
        object.__setattr__(record, "expected_postcondition", structural)
        with JournalStore(self.state) as store:
            with self.assertRaises(ScenarioValidationError):
                store.write_in_flight(record, known_secrets=("SECRET",))
        with JournalStore(self.state) as store:
            with self.assertRaises(ScenarioValidationError):
                store.write_in_flight(self.in_flight(), known_secrets=("actor",))

    def test_rejects_opaque_key_collisions_created_by_redaction(self) -> None:
        completed = self.completed()
        object.__setattr__(
            completed,
            "handler_output",
            freeze_planned({"prefix-LEAK": 1, "prefix-": 2}),
        )
        with JournalStore(self.state) as store:
            store.write_in_flight(self.in_flight())
            with self.assertRaises(ScenarioValidationError):
                store.replace_completed(completed, known_secrets=("LEAK",))
            self.assertIsInstance(store.read("comment"), InFlightRecord)

    def test_rejects_invalid_constructor_values(self) -> None:
        with self.assertRaises(ScenarioValidationError):
            InFlightRecord(
                scenario_digest="bad",
                event="comment",
                attempt=True,
                actor=Reference("actor", "ada"),
                action_class="unknown",  # type: ignore[arg-type]
                expected_postcondition=self.postcondition,
                reconciliation_marker="marker",
            )
        with self.assertRaises(ScenarioValidationError):
            InvocationMetadata("shell", "op", (), ())  # type: ignore[arg-type]
        completed = self.completed()
        object.__setattr__(
            completed,
            "invocation",
            InvocationMetadata("bugzilla-rest-custom-field", "op", (), ()),
        )
        with self.assertRaises(ScenarioValidationError):
            completed.__post_init__()
        custom_postcondition = freeze_planned(
            {
                "action": "bug.custom-field-set",
                "target": Reference("bug", "race"),
                "values": {
                    "bug": Reference("bug", "race"),
                    "values": (
                        {
                            "field": Reference("custom-field", "severity"),
                            "value": "major",
                        },
                    ),
                },
                "marker": "bzr-live:smoke:custom-field",
            }
        )
        custom_completed = CompletedRecord(
            scenario_digest=self.digest,
            event="custom-field",
            attempt=1,
            actor=Reference("actor", "ada"),
            action_class="idempotent-set",
            expected_postcondition=custom_postcondition,
            reconciliation_marker="bzr-live:smoke:custom-field",
            invocation=InvocationMetadata(
                "bugzilla-rest-custom-field", "custom-field-set", (), ()
            ),
            handler_output={},
            exit_status=0,
            resolved_ids={},
            next_safe_action="advance",
        )
        self.assertEqual(
            custom_completed.invocation.mutation_boundary,
            "bugzilla-rest-custom-field",
        )
        with self.assertRaises(ScenarioValidationError):
            InFlightRecord(
                scenario_digest=self.digest,
                event="comment",
                attempt=1,
                actor=Reference("actor", "ada"),
                action_class="append",
                expected_postcondition=freeze_planned({"action": "bug.comment"}),
                reconciliation_marker="bzr-live:smoke:comment",
            )
        invalid_values = freeze_planned(
            {
                "action": "bug.comment",
                "target": Reference("bug", "race"),
                "values": {"body": {"ref": "bug:other"}, "private": False},
                "marker": "bzr-live:smoke:comment",
            }
        )
        with self.assertRaises(ScenarioValidationError):
            InFlightRecord(
                scenario_digest=self.digest,
                event="comment",
                attempt=1,
                actor=Reference("actor", "ada"),
                action_class="append",
                expected_postcondition=invalid_values,
                reconciliation_marker="bzr-live:smoke:comment",
            )

    def test_read_rejects_tampered_mode_and_unknown_fields(self) -> None:
        with JournalStore(self.state) as store:
            path = store.write_in_flight(self.in_flight())
            path.chmod(0o644)
            with self.assertRaises(ScenarioValidationError):
                store.read("comment")
        path.chmod(0o600)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["unknown"] = True
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o600)
        with JournalStore(self.state) as store:
            with self.assertRaises(ScenarioValidationError):
                store.read("comment")

    def test_completed_record_round_trips_finite_time_tracking_numbers(self) -> None:
        output = {"estimated_time": 8.0, "remaining_time": 0.0, "id": 42}
        with JournalStore(self.state) as store:
            store.write_in_flight(self.in_flight())
            path = store.replace_completed(self.completed(handler_output=output))
            record = store.read("comment")
        self.assertIsInstance(record, CompletedRecord)
        self.assertEqual(dict(record.handler_output), output)  # type: ignore[union-attr,arg-type]
        for key in ("estimated_time", "remaining_time"):
            self.assertIs(type(record.handler_output[key]), float)  # type: ignore[union-attr,index]
        self.assertIs(type(record.handler_output["id"]), int)  # type: ignore[union-attr,index]
        self.assertEqual(record.scenario_digest, self.digest)  # type: ignore[union-attr]
        self.assertIn('"estimated_time":8.0', path.read_text(encoding="utf-8"))

    def test_completed_record_rejects_non_finite_numbers_before_writing(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ScenarioValidationError) as caught:
                    self.completed(handler_output={"estimated_time": value})
                self.assertIn("$.handler_output.estimated_time", str(caught.exception))
                self.assertIn("must be a finite number", str(caught.exception))
        # Reach `replace_completed`'s own re-validation. Passing `self.completed(...)` would
        # raise while the argument is evaluated, so the store would never be entered.
        smuggled = self.completed()
        object.__setattr__(smuggled, "handler_output", MappingProxyType({"t": float("nan")}))
        with JournalStore(self.state) as store:
            store.write_in_flight(self.in_flight())
            with self.assertRaises(ScenarioValidationError) as caught:
                store.replace_completed(smuggled)
            self.assertIn("must be a finite number", str(caught.exception))
            self.assertIsInstance(store.read("comment"), InFlightRecord)

    def test_record_file_holding_an_unreadable_number_fails_to_decode(self) -> None:
        # Each case names the mechanism that must refuse it. Asserting the message, not just
        # the exception, is what makes the subtests discriminating: all three would raise
        # `ScenarioValidationError` even if only `math.isfinite` were left.
        cases = (
            ('"estimated_time":8.0', '"estimated_time":NaN',
             "journal:$: non-finite numbers are not supported"),
            ('"estimated_time":8.0', '"estimated_time":1e400',
             "journal:$.handler_output.estimated_time: must be a finite number"),
            # `allow_float=True` is document-scoped, so these four exact `int` guards are the
            # only thing keeping a float out of the rest of the record.
            ('"attempt":1', '"attempt":1.0', "journal:$.attempt: must be a positive integer"),
            ('"exit_status":0', '"exit_status":0.0', "journal:$.exit_status: must be an integer"),
            ('"journal_version":1', '"journal_version":1.0',
             "journal:$.journal_version: unsupported journal version"),
            ('"bug:race":42', '"bug:race":42.0',
             "journal:$.resolved_ids.bug:race: must be a positive integer"),
        )
        for index, (original, replacement, expected) in enumerate(cases):
            with self.subTest(replacement=replacement):
                state = Path(self._temporary.name) / f"state{index}"
                with JournalStore(state) as store:
                    store.write_in_flight(self.in_flight())
                    path = store.replace_completed(
                        self.completed(handler_output={"estimated_time": 8.0})
                    )
                content = path.read_text(encoding="utf-8")
                self.assertIn(original, content)
                path.write_text(content.replace(original, replacement), encoding="utf-8")
                path.chmod(0o600)
                with JournalStore(state) as store:
                    with self.assertRaises(ScenarioValidationError) as caught:
                        store.read("comment")
                self.assertEqual(str(caught.exception), expected)


if __name__ == "__main__":
    unittest.main()
