"""Offline invariants over the committed smoke scenario (issue #19).

These assertions run with no server and no Docker. Three of them are guards: they
encode constraints whose violation is silent on a live Bugzilla, so no amount of
running the scenario would surface them. See
docs/workflow/specs/2026-09-01-smoke-scenario-design.md, "Proof".
"""

from __future__ import annotations

import unittest
from pathlib import Path

from bzr_live.replay.actions import (
    ATTACHMENT_SUMMARY_BYTE_LIMIT,
    HANDLERS,
    render_attachment_summary,
)
from bzr_live.scenario import load_scenario

SCENARIO = Path(__file__).resolve().parent.parent / "scenarios" / "smoke"

# The chain and the diamond, named once so a topology edit fails here rather than
# in a test body that reads like an incantation.
CHAIN = ("dun-retry-storm", "inv-tax-mismatch", "cart-double-charge")
DIAMOND_APEX = "pay-token-leak"
DIAMOND_SINK = "dun-wrong-locale"


def _blocks_graph(scenario):
    """One directed graph over bug aliases, oriented as `blocks`.

    An edge (a, b) means "a blocks b". `depends_on` is the inverse relation, so it
    contributes a reversed edge. Both orientations must land in one graph: the
    diamond's apex edges are declared as `blocks` and its sink edges as
    `depends_on`, so a graph built from either alone contains no path through it.
    """
    edges: set[tuple[str, str]] = set()
    for event in scenario.events:
        if event.action not in {"bug.create", "bug.update"}:
            continue
        postcondition = event.expected_postcondition
        target = postcondition["target"]
        if target.kind != "bug":
            continue
        values = postcondition["values"]
        for ref in values.get("blocks") or ():
            edges.add((target.name, ref.name))
        for ref in values.get("depends_on") or ():
            edges.add((ref.name, target.name))
    return edges


def _paths(edges, start, end):
    """Every distinct simple path from start to end. The graph is small and acyclic."""
    found = []

    def walk(node, seen):
        if node == end:
            found.append(tuple(seen))
            return
        for source, target in edges:
            if source == node and target not in seen:
                walk(target, (*seen, target))

    walk(start, (start,))
    return found


class SmokeScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenario = load_scenario(SCENARIO)
        cls.creates = [e for e in cls.scenario.events if e.action == "bug.create"]
        cls.product_of = {
            event.creates.name: event.expected_postcondition["values"]["product"].name
            for event in cls.creates
        }
        cls.actors = {
            resource.name: resource
            for resource in cls.scenario.resources
            if resource.kind == "actor"
        }

    def _groups(self, actor_name):
        return {ref.name for ref in self.actors[actor_name].data["groups"]}

    def test_loads_with_stable_digest(self):
        self.assertEqual(load_scenario(SCENARIO).digest, self.scenario.digest)

    def test_twenty_bugs_across_two_products(self):
        self.assertEqual(len(self.creates), 20)
        self.assertGreaterEqual(len(set(self.product_of.values())), 2)

    def test_dependency_chain_is_three_deep_and_crosses_products(self):
        edges = _blocks_graph(self.scenario)
        for source, target in zip(CHAIN, CHAIN[1:]):
            self.assertIn((source, target), edges, f"missing chain edge {source}->{target}")
        self.assertGreater(
            len({self.product_of[alias] for alias in CHAIN}), 1,
            "the dependency chain does not cross a product boundary")

    def test_diamond_has_two_distinct_paths(self):
        paths = _paths(_blocks_graph(self.scenario), DIAMOND_APEX, DIAMOND_SINK)
        self.assertEqual(len(paths), 2, f"expected two paths, found {paths}")
        self.assertNotEqual(paths[0], paths[1])

    def test_exactly_one_duplicate_assignment(self):
        duplicates = [
            event for event in self.scenario.events
            if event.action == "bug.update"
            and event.expected_postcondition["values"].get("duplicate_of") is not None
        ]
        self.assertEqual(len(duplicates), 1)
        values = duplicates[0].expected_postcondition["values"]
        # bzr's --dupe-of carries conflicts_with for both (finding G5).
        self.assertNotIn("status", values)
        self.assertNotIn("resolution", values)

    def test_reopening_present(self):
        statuses = [
            event.expected_postcondition["values"]["status"]
            for event in self.scenario.events
            if event.action == "bug.update"
            and event.expected_postcondition["target"].name == "pay-decline-copy"
            and "status" in event.expected_postcondition["values"]
        ]
        resolved = statuses.index("RESOLVED")
        self.assertIn(
            "CONFIRMED", statuses[resolved + 1:],
            f"no resolved-to-open transition in {statuses}")

    def test_every_handler_action_is_exercised(self):
        self.assertEqual({event.action for event in self.scenario.events}, set(HANDLERS))

    def test_all_custom_field_types_assigned(self):
        declared = {
            resource.name for resource in self.scenario.resources
            if resource.kind == "custom-field"
        }
        assigned = {
            assignment["field"].name
            for event in self.scenario.events
            if event.action == "bug.custom-field-set"
            for assignment in event.expected_postcondition["values"]["values"]
        }
        self.assertEqual(assigned, declared)

    # --- guards ----------------------------------------------------------------

    def test_private_comment_author_is_an_insider(self):
        """Guard: private comments need the insidergroup, pinned to `admin`."""
        private = [
            event for event in self.scenario.events
            if event.action == "bug.comment"
            and event.expected_postcondition["values"]["private"]
        ]
        self.assertTrue(private, "no private comment declared")
        for event in private:
            self.assertIn(
                "admin", self._groups(event.actor.name),
                f"{event.name!r} posts a private comment as {event.actor.name!r}, "
                "who is not in the insider group")

    def test_attachment_summaries_fit(self):
        """Guard: Bugzilla truncates an over-length description instead of refusing."""
        for event in self.scenario.events:
            if event.action != "bug.attach":
                continue
            values = event.expected_postcondition["values"]
            rendered = render_attachment_summary(
                values["description"], event.reconciliation_marker, values["asset_sha256"])
            self.assertLessEqual(
                len(rendered.encode("utf-8")), ATTACHMENT_SUMMARY_BYTE_LIMIT,
                f"{event.name!r} renders a {len(rendered.encode('utf-8'))}-byte summary")

    def test_creates_declaring_assignee_or_edges_are_privileged(self):
        """Guard: Bugzilla silently discards these from a filer without editbugs.

        `_check_assigned_to` substitutes the component default (Bugzilla/Bug.pm:1449-1454)
        and `_check_dependencies` drops the edges entirely (:1707-1709), both without an
        error, so a mis-filed create makes the scenario stop proving what it declares
        while every other gate stays green.
        """
        for event in self.creates:
            values = event.expected_postcondition["values"]
            privileged_fields = [
                name for name in ("assignee", "depends_on", "blocks") if values.get(name)
            ]
            if not privileged_fields:
                continue
            self.assertIn(
                "editbugs", self._groups(event.actor.name),
                f"{event.name!r} declares {privileged_fields} but is filed by "
                f"{event.actor.name!r}, who lacks editbugs")


if __name__ == "__main__":
    unittest.main()
