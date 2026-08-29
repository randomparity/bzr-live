from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from bzr_live.scenario import Reference, ScenarioValidationError, load_scenario


class ScenarioEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "scenario"
        (self.root / "assets").mkdir(parents=True)
        self.asset_content = b"proof\n"
        (self.root / "assets" / "trace.log").write_bytes(self.asset_content)
        self.scenario = {
            "format_version": 1,
            "name": "smoke",
            "description": "Replay proof",
            "assets": [
                {
                    "name": "trace-log",
                    "path": "assets/trace.log",
                    "sha256": hashlib.sha256(self.asset_content).hexdigest(),
                }
            ],
        }
        self.resources = {"format_version": 1, "resources": self.resource_catalog()}
        self.events = self.event_catalog()
        self.write()

    def resource_catalog(self) -> list[dict[str, object]]:
        return [
            {"kind": "group", "name": "triage", "description": "Triage"},
            {"kind": "actor", "name": "ada", "email": "ada@example.test", "display_name": "Ada", "groups": [{"ref": "group:triage"}]},
            {"kind": "actor", "name": "bob", "email": "bob@example.test", "display_name": "Bob"},
            {"kind": "product", "name": "checkout", "description": "Checkout"},
            {"kind": "component", "name": "payments", "product": {"ref": "product:checkout"}, "description": "Payments", "default_assignee": {"ref": "actor:ada"}},
            {"kind": "version", "name": "one", "product": {"ref": "product:checkout"}},
            {"kind": "milestone", "name": "next", "product": {"ref": "product:checkout"}},
            {"kind": "custom-field", "name": "severity", "field_type": "single-select", "values": ["low", "high"]},
            {"kind": "keyword", "name": "regression", "description": "Regression"},
            {"kind": "flag-type", "name": "review", "description": "Review", "target": "bug", "products": [{"ref": "product:checkout"}], "components": [{"ref": "component:payments"}]},
        ]

    def event_catalog(self) -> list[dict[str, object]]:
        actor = {"ref": "actor:ada"}
        bug = {"ref": "bug:race"}
        return [
            {
                "format_version": 1,
                "name": "create-race",
                "actor": actor,
                "action": "bug.create",
                "payload": {
                    "alias": "race",
                    "product": {"ref": "product:checkout"},
                    "component": {"ref": "component:payments"},
                    "summary": "Checkout race",
                    "description": "Observed race",
                    "version": {"ref": "version:one"},
                    "milestone": {"ref": "milestone:next"},
                    "assignee": {"ref": "actor:ada"},
                    "cc": [{"ref": "actor:bob"}],
                    "groups": [{"ref": "group:triage"}],
                    "depends_on": [],
                    "blocks": [],
                    "keywords": [{"ref": "keyword:regression"}],
                    "estimated_hours": "1.25",
                    "remaining_hours": "1",
                    "custom_fields": [{"field": {"ref": "custom-field:severity"}, "value": "high"}],
                },
            },
            {"format_version": 1, "name": "update-race", "actor": actor, "action": "bug.update", "payload": {"bug": bug, "set": {"summary": "Updated", "assignee": None, "version": None, "keywords": [{"ref": "keyword:regression"}]}}},
            {"format_version": 1, "name": "comment-race", "actor": actor, "action": "bug.comment", "payload": {"bug": bug, "body": "Investigating"}},
            {"format_version": 1, "name": "attach-proof", "actor": actor, "action": "bug.attach", "payload": {"alias": "evidence", "bug": bug, "asset": {"ref": "asset:trace-log"}, "description": "Trace", "content_type": "text/plain"}},
            {"format_version": 1, "name": "log-time", "actor": actor, "action": "bug.worktime", "payload": {"bug": bug, "hours": "0.50", "comment": "Investigation"}},
            {"format_version": 1, "name": "set-severity", "actor": actor, "action": "bug.custom-field-set", "payload": {"bug": bug, "values": [{"field": {"ref": "custom-field:severity"}, "value": "low"}]}},
            {"format_version": 1, "name": "request-review", "actor": actor, "action": "bug.flag", "payload": {"bug": bug, "flag_type": {"ref": "flag-type:review"}, "status": "?", "requestee": {"ref": "actor:bob"}}},
            {"format_version": 1, "name": "obsolete-proof", "actor": actor, "action": "attachment.update", "payload": {"attachment": {"ref": "attachment:evidence"}, "obsolete": True, "description": "Superseded"}},
        ]

    def write(self) -> None:
        (self.root / "scenario.json").write_text(json.dumps(self.scenario), encoding="utf-8")
        (self.root / "resources.json").write_text(json.dumps(self.resources), encoding="utf-8")
        (self.root / "events.jsonl").write_text(
            "\n".join(json.dumps(event) for event in self.events) + "\n", encoding="utf-8"
        )

    def assert_invalid(self, field: str) -> None:
        self.write()
        with self.assertRaises(ScenarioValidationError) as caught:
            load_scenario(self.root)
        self.assertIn(field, str(caught.exception))

    def test_plans_all_actions_with_exact_recovery_contract(self) -> None:
        scenario = load_scenario(self.root)
        self.assertEqual(
            [event.action_class for event in scenario.events],
            ["unique-create", "idempotent-set", "append", "append", "append", "idempotent-set", "idempotent-set", "idempotent-set"],
        )
        self.assertEqual(scenario.events[0].creates, Reference("bug", "race"))
        self.assertEqual(scenario.events[3].creates, Reference("attachment", "evidence"))
        self.assertIsNone(scenario.events[1].creates)
        expected_alias = "bzr-live-" + hashlib.sha256(b"v1\0smoke\0race").hexdigest()[:31]
        self.assertEqual(scenario.events[0].expected_postcondition["values"]["server_alias"], expected_alias)
        self.assertEqual(len(expected_alias), 40)
        for event in scenario.events:
            self.assertEqual(set(event.expected_postcondition), {"action", "target", "values", "marker"})
            self.assertEqual(event.expected_postcondition["action"], event.action)
            self.assertEqual(event.expected_postcondition["marker"], event.reconciliation_marker)
        self.assertEqual(
            scenario.events[0].dependencies,
            (
                Reference("actor", "ada"),
                Reference("product", "checkout"),
                Reference("component", "payments"),
                Reference("version", "one"),
                Reference("milestone", "next"),
                Reference("actor", "bob"),
                Reference("group", "triage"),
                Reference("keyword", "regression"),
                Reference("custom-field", "severity"),
            ),
        )
        with self.assertRaises(TypeError):
            scenario.events[0].payload["summary"] = "changed"  # type: ignore[index]

    def test_asset_bytes_are_verified_and_snapshotted(self) -> None:
        scenario = load_scenario(self.root)
        (self.root / "assets" / "trace.log").write_bytes(b"changed")
        self.assertEqual(scenario.assets["trace-log"].content, self.asset_content)
        self.scenario["assets"][0]["sha256"] = "0" * 64  # type: ignore[index]
        self.assert_invalid("$.assets[0].sha256")

    def test_rejects_unsafe_and_symlinked_assets(self) -> None:
        self.scenario["assets"][0]["path"] = "assets/../trace.log"  # type: ignore[index]
        self.assert_invalid("$.assets[0].path")
        self.scenario["assets"][0]["path"] = "assets/trace-link"  # type: ignore[index]
        (self.root / "assets" / "trace-link").symlink_to("trace.log")
        self.assert_invalid("$.assets[0].path")

    def test_rejects_forward_output_and_unsupported_action(self) -> None:
        self.events[0]["payload"] = {"bug": {"ref": "bug:later"}, "body": "too soon"}
        self.events[0]["action"] = "bug.comment"
        self.assert_invalid("events.jsonl:1")
        self.events = self.event_catalog()
        self.events[0]["action"] = "bug.delete"
        self.assert_invalid("$.action")

    def test_rejects_wrong_ownership_custom_values_and_flag_scope(self) -> None:
        self.resources["resources"].append({"kind": "product", "name": "other", "description": "Other"})  # type: ignore[union-attr]
        self.resources["resources"].append({"kind": "version", "name": "other-v", "product": {"ref": "product:other"}})  # type: ignore[union-attr]
        self.events[0]["payload"]["version"] = {"ref": "version:other-v"}  # type: ignore[index]
        self.assert_invalid("$.payload.version")
        self.resources = {"format_version": 1, "resources": self.resource_catalog()}
        self.events = self.event_catalog()
        self.events[5]["payload"]["values"][0]["value"] = "urgent"  # type: ignore[index]
        self.assert_invalid("$.payload.values[0].value")

    def test_rejects_invalid_media_type_and_boolean(self) -> None:
        self.events[3]["payload"]["content_type"] = "text /plain"  # type: ignore[index]
        self.assert_invalid("$.payload.content_type")
        self.events = self.event_catalog()
        self.events[7]["payload"]["obsolete"] = 1  # type: ignore[index]
        self.assert_invalid("$.payload.obsolete")


if __name__ == "__main__":
    unittest.main()
