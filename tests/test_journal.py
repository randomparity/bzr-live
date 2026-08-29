from __future__ import annotations

import json
import os
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
                "values": {"body": "safe"},
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

    def completed(self, attempt: int = 1, next_action: str = "advance") -> CompletedRecord:
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
            handler_output=freeze_planned({"ok": True}),
            exit_status=0,
            resolved_ids=MappingProxyType({"bug:race": 42}),
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
            "access_token": "token-value",
            "refreshToken": "refresh-value",
            "client-secret": "secret-value",
            "set-cookie": "cookie-value",
            "message": "redacted<actredacted",
            "nested": ["safe-act-value"],
            "prefix-LEAK": "key text",
        }
        completed = self.completed()
        object.__setattr__(completed, "handler_output", freeze_planned(output))
        with JournalStore(self.state) as store:
            store.write_in_flight(self.in_flight(), known_secrets=("redacted", "<", "act", "LEAK"))
            store.replace_completed(completed, known_secrets=("redacted", "<", "act", "LEAK"))
            stored = store.read("comment")
        self.assertIsInstance(stored, CompletedRecord)
        redacted = stored.handler_output  # type: ignore[union-attr]
        for key in output.keys() - {"message", "nested", "prefix-LEAK"}:
            self.assertIsNone(redacted[key])
        self.assertEqual(redacted["message"], "")
        self.assertEqual(redacted["nested"], ("safe--value",))
        self.assertNotIn("prefix-LEAK", redacted)
        self.assertEqual(redacted["prefix-"], "key text")
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


if __name__ == "__main__":
    unittest.main()
