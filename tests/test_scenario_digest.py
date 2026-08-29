from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from bzr_live.scenario import ScenarioValidationError, load_scenario


class ScenarioDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "scenario"
        (self.root / "assets").mkdir(parents=True)
        self.contents = {"a.txt": b"a", "b.txt": b"b"}
        for name, content in self.contents.items():
            (self.root / "assets" / name).write_bytes(content)
        self.scenario = {
            "format_version": 1,
            "name": "digest",
            "assets": [
                {"name": "asset-b", "path": "assets/b.txt", "sha256": hashlib.sha256(b"b").hexdigest()},
                {"name": "asset-a", "path": "assets/a.txt", "sha256": hashlib.sha256(b"a").hexdigest()},
            ],
        }
        self.resources = {
            "format_version": 1,
            "resources": [
                {"kind": "actor", "name": "ada", "email": "ada@example.test", "display_name": "Ada"},
                {"kind": "product", "name": "p", "description": "Product"},
                {"kind": "component", "name": "c", "product": {"ref": "product:p"}, "description": "Component"},
            ],
        }
        self.events = [
            {"format_version": 1, "name": "create", "actor": {"ref": "actor:ada"}, "action": "bug.create", "payload": {"alias": "bug", "product": {"ref": "product:p"}, "component": {"ref": "component:c"}, "summary": "Summary"}}
        ]
        self.write()

    def write(self, *, pretty: bool = False) -> None:
        options = {"indent": 2} if pretty else {"separators": (",", ":")}
        (self.root / "scenario.json").write_text(json.dumps(self.scenario, **options), encoding="utf-8")
        (self.root / "resources.json").write_text(json.dumps(self.resources, **options), encoding="utf-8")
        event_options = {"separators": (", ", ": ")} if pretty else options
        (self.root / "events.jsonl").write_text(json.dumps(self.events[0], **event_options) + "\n", encoding="utf-8")

    def test_formatting_key_order_and_asset_declaration_order_are_non_semantic(self) -> None:
        first = load_scenario(self.root).digest
        self.scenario = dict(reversed(list(self.scenario.items())))
        self.scenario["assets"] = list(reversed(self.scenario["assets"]))  # type: ignore[arg-type]
        self.resources = dict(reversed(list(self.resources.items())))
        self.events[0] = dict(reversed(list(self.events[0].items())))
        self.write(pretty=True)
        self.assertEqual(load_scenario(self.root).digest, first)

    def test_manifest_resource_and_event_changes_are_semantic(self) -> None:
        original = load_scenario(self.root).digest
        self.scenario["description"] = "Changed"
        self.write()
        changed_manifest = load_scenario(self.root).digest
        self.assertNotEqual(changed_manifest, original)
        self.scenario.pop("description")
        self.resources["resources"][1]["description"] = "Changed product"  # type: ignore[index]
        self.write()
        changed_resource = load_scenario(self.root).digest
        self.assertNotEqual(changed_resource, original)
        self.resources["resources"][1]["description"] = "Product"  # type: ignore[index]
        self.events[0]["payload"]["summary"] = "Changed summary"  # type: ignore[index]
        self.write()
        self.assertNotEqual(load_scenario(self.root).digest, original)

    def test_asset_checksum_and_bytes_must_change_together(self) -> None:
        original = load_scenario(self.root).digest
        (self.root / "assets" / "a.txt").write_bytes(b"changed")
        with self.assertRaises(ScenarioValidationError):
            load_scenario(self.root)
        checksum = hashlib.sha256(b"changed").hexdigest()
        self.scenario["assets"][1]["sha256"] = checksum  # type: ignore[index]
        self.write()
        self.assertNotEqual(load_scenario(self.root).digest, original)
        self.scenario["assets"][1]["sha256"] = "f" * 64  # type: ignore[index]
        self.write()
        with self.assertRaises(ScenarioValidationError):
            load_scenario(self.root)

    def test_digest_is_lowercase_sha256_of_semantic_envelope(self) -> None:
        scenario = load_scenario(self.root)
        self.assertRegex(scenario.digest, r"^[0-9a-f]{64}$")
        normalized_scenario = {
            "format_version": 1,
            "name": "digest",
            "description": "",
            "assets": sorted(self.scenario["assets"], key=lambda item: item["path"]),  # type: ignore[index]
        }
        normalized_resources = {
            "format_version": 1,
            "resources": [
                {"kind": "actor", "name": "ada", "email": "ada@example.test", "display_name": "Ada", "groups": []},
                {"kind": "product", "name": "p", "description": "Product"},
                {"kind": "component", "name": "c", "product": {"ref": "product:p"}, "description": "Component", "default_assignee": None},
            ],
        }
        normalized_event = {
            "format_version": 1,
            "name": "create",
            "actor": {"ref": "actor:ada"},
            "action": "bug.create",
            "payload": {
                "alias": "bug",
                "product": {"ref": "product:p"},
                "component": {"ref": "component:c"},
                "summary": "Summary",
                "description": "",
                "version": None,
                "milestone": None,
                "assignee": None,
                "cc": [],
                "groups": [],
                "depends_on": [],
                "blocks": [],
                "duplicate_of": None,
                "keywords": [],
                "estimated_hours": None,
                "remaining_hours": None,
                "custom_fields": [],
            },
        }
        assets = sorted(self.scenario["assets"], key=lambda item: item["path"])  # type: ignore[index]
        envelope = {"format_version": 1, "inputs": {"scenario.json": normalized_scenario, "resources.json": normalized_resources, "events.jsonl": [normalized_event]}, "assets": assets}
        expected = hashlib.sha256(json.dumps(envelope, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(scenario.digest, expected)


if __name__ == "__main__":
    unittest.main()
