from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from bzr_live.scenario import Reference, ScenarioValidationError, load_scenario


FIXTURE = Path(__file__).parent / "fixtures" / "minimal-scenario"


class ScenarioResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "scenario"
        shutil.copytree(FIXTURE, self.root)

    def write_json(self, name: str, value: object) -> None:
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")

    def valid_resources(self) -> list[dict[str, object]]:
        return [
            {
                "kind": "component",
                "name": "payments",
                "product": {"ref": "product:checkout"},
                "description": "Payments",
                "default_assignee": {"ref": "actor:ada"},
            },
            {
                "kind": "actor",
                "name": "ada",
                "email": "ada@example.test",
                "display_name": "Ada",
                "groups": [{"ref": "group:triage"}],
            },
            {
                "kind": "flag-type",
                "name": "review",
                "description": "Review",
                "target": "bug",
                "products": [{"ref": "product:checkout"}],
                "components": [{"ref": "component:payments"}],
            },
            {"kind": "milestone", "name": "next", "product": {"ref": "product:checkout"}},
            {"kind": "version", "name": "one", "product": {"ref": "product:checkout"}},
            {"kind": "group", "name": "triage", "description": "Triage"},
            {"kind": "product", "name": "checkout", "description": "Checkout"},
            {
                "kind": "custom-field",
                "name": "severity",
                "field_type": "single-select",
                "values": ["low", "high"],
            },
            {"kind": "keyword", "name": "regression", "description": "Regression"},
        ]

    def assert_invalid(self, source: str, field: str) -> str:
        with self.assertRaises(ScenarioValidationError) as caught:
            load_scenario(self.root)
        message = str(caught.exception)
        self.assertIn(source, message)
        self.assertIn(field, message)
        return message

    def test_loads_typed_immutable_resources_in_stable_dependency_order(self) -> None:
        self.write_json("resources.json", {"format_version": 1, "resources": self.valid_resources()})
        scenario = load_scenario(self.root)
        self.assertEqual(
            [resource.name for resource in scenario.resource_plan],
            ["triage", "ada", "checkout", "payments", "review", "next", "one", "severity", "regression"],
        )
        component = scenario.resources[0]
        self.assertEqual(component.data["product"], Reference("product", "checkout"))
        self.assertEqual(
            component.dependencies,
            (Reference("product", "checkout"), Reference("actor", "ada")),
        )
        with self.assertRaises(TypeError):
            component.data["description"] = "changed"  # type: ignore[index]

    def test_rejects_duplicate_json_keys_with_source_and_field(self) -> None:
        (self.root / "resources.json").write_text(
            '{"format_version":1,"resources":[{"kind":"group","name":"x","description":"ok","description":"again"}]}',
            encoding="utf-8",
        )
        self.assert_invalid("resources.json", "$.resources[0].description")

    def test_rejects_unknown_fields_and_boolean_versions(self) -> None:
        self.write_json("scenario.json", {"format_version": True, "name": "minimal", "extra": 1})
        self.assert_invalid("scenario.json", "$.extra")
        self.write_json("scenario.json", {"format_version": True, "name": "minimal"})
        self.assert_invalid("scenario.json", "$.format_version")

    def test_rejects_floats_and_non_finite_numbers(self) -> None:
        (self.root / "resources.json").write_text(
            '{"format_version":1,"resources":[],"bad":1.5}', encoding="utf-8"
        )
        self.assert_invalid("resources.json", "$")
        (self.root / "resources.json").write_text(
            '{"format_version":1,"resources":[],"bad":NaN}', encoding="utf-8"
        )
        self.assert_invalid("resources.json", "$")

    def test_rejects_duplicate_resource_identity(self) -> None:
        resource = {"kind": "group", "name": "triage", "description": "Triage"}
        self.write_json("resources.json", {"format_version": 1, "resources": [resource, resource]})
        self.assert_invalid("resources.json", "$.resources[1]")

    def test_rejects_missing_and_wrong_kind_resource_references(self) -> None:
        resources = self.valid_resources()
        resources[0]["product"] = {"ref": "group:triage"}
        self.write_json("resources.json", {"format_version": 1, "resources": resources})
        self.assert_invalid("resources.json", "$.resources[0].product")

    def test_rejects_invalid_flag_scope_dependency(self) -> None:
        resources = self.valid_resources()
        resources.insert(0, {"kind": "product", "name": "other", "description": "Other"})
        resources[3]["products"] = [{"ref": "product:other"}]
        self.write_json("resources.json", {"format_version": 1, "resources": resources})
        self.assert_invalid("resources.json", "$.resources[3].components")

    def test_rejects_invalid_custom_field_catalog(self) -> None:
        resources = self.valid_resources()
        resources[7]["values"] = ["high", "high"]
        self.write_json("resources.json", {"format_version": 1, "resources": resources})
        self.assert_invalid("resources.json", "$.resources[7].values")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires no-follow opens")
    def test_required_documents_reject_symlinks(self) -> None:
        for name in ("scenario.json", "resources.json", "events.jsonl"):
            with self.subTest(name=name):
                target = self.root / name
                saved = self.root / f"{name}.saved"
                target.rename(saved)
                target.symlink_to(saved.name)
                self.assert_invalid(name, "$")
                target.unlink()
                saved.rename(target)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_required_documents_reject_fifos_without_blocking(self) -> None:
        target = self.root / "events.jsonl"
        target.unlink()
        os.mkfifo(target)
        self.assert_invalid("events.jsonl", "$")


if __name__ == "__main__":
    unittest.main()
