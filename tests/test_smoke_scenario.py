"""Offline invariants over the committed smoke scenario (issue #19).

These assertions run with no server and no Docker. Five are guards, and what each
one buys differs -- the honest accounting, because an overstated rationale is how a
test keeps being trusted for a reason it does not earn:

- `test_private_comment_author_is_an_insider` moves a *loud* failure earlier.
  Bugzilla's `Comment.pm::_check_isprivate` raises `user_not_insider` for a
  non-insider, so a live replay would abort with an error; this catches it at
  `make test` instead, without a container.
- `test_attachment_summaries_fit` duplicates a precondition the engine already
  enforces: `BugAttachHandler.check_supported` refuses an over-length rendered
  summary, and `ReplayEngine._check_local_preconditions` runs `check_supported`
  over every event before any mutation. Its value is the same shift -- offline
  rather than mid-replay -- not extra coverage. It covers `bug.attach` only;
  `AttachmentUpdateHandler` carries the same ceiling for `attachment.update`.
- `test_creates_declaring_assignee_or_edges_are_privileged` is the only one aimed
  at a *silent* server-side substitution (`Bugzilla/Bug.pm:1449-1454` and
  `:1707-1709`). On the pinned image that substitution is currently unreachable:
  stock `editbugs` carries `userregexp => '.*'` (`Bugzilla/Install.pm:134-138`), so
  every account is granted it automatically. The assertion tests the *declared*
  group set, which is the property the scenario controls, and it would bite if that
  regexp were ever cleared.
- `test_group_restrictions_are_settable_by_their_actor` and
  `test_group_restricted_bugs_declare_no_private_comment` are the same *loud*
  shift as the first guard, one for a Bugzilla refusal mid-replay and one for a
  verifier read that cannot be issued at all. Each states its own reason where
  it is written.

See docs/workflow/specs/2026-09-01-smoke-scenario-design.md, "Proof".
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

# Pinned so that editing the fixture is a deliberate two-file change. Every edit
# changes this digest and invalidates any journal written against the old content
# (ADR 0006), which is the consequence ADR 0007 records and this constant enforces.
EXPECTED_DIGEST = "832bc5a1049e116976be79f8d2d9a02fdbe896bc3f3aed68521bbf687af1fec1"

# The chain and the diamond, named once so a topology edit fails here rather than
# in a test body that reads like an incantation.
CHAIN = ("dun-retry-storm", "inv-tax-mismatch", "cart-double-charge")
DIAMOND_APEX = "pay-token-leak"
DIAMOND_SINK = "dun-wrong-locale"


def _blocks_graph(scenario):
    """The graph the scenario leaves behind, over bug aliases, oriented as `blocks`.

    An edge (a, b) means "a blocks b". `depends_on` is the inverse relation, so it
    contributes a reversed edge. Both orientations must land in one graph: the
    diamond's apex edges are declared as `blocks` and its sink edges as
    `depends_on`, so a graph built from either alone contains no path through it.

    Each declaration replaces rather than extends, because that is what replay does:
    `BugUpdateHandler.build` diffs the declared list against the observed one and
    emits `--depends-on-remove` / `--blocks-remove` for the difference
    (`src/bzr_live/replay/actions.py:397-409`), so a later `set` naming fewer refs
    deletes edges on the server. Accumulating instead would make the assertions
    below blind to exactly that: a removal would leave the final graph unchanged
    here while shrinking it on the fixture.
    """
    latest: dict[tuple[str, str], tuple] = {}
    for event in scenario.events:
        if event.action not in {"bug.create", "bug.update"}:
            continue
        postcondition = event.expected_postcondition
        target = postcondition["target"]
        if target.kind != "bug":
            continue
        values = postcondition["values"]
        for field in ("blocks", "depends_on"):
            # A create's postcondition carries every field, an update's only the
            # ones its `set` block names, so presence is what marks a declaration.
            if field in values:
                latest[target.name, field] = tuple(values[field] or ())
    edges: set[tuple[str, str]] = set()
    for (name, field), refs in latest.items():
        for ref in refs:
            edges.add((name, ref.name) if field == "blocks" else (ref.name, name))
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

    def _group_restrictions(self):
        """(event, target bug alias, declared group names) per bug-group declaration.

        Both payload forms land here. A create's postcondition carries every key, so
        an undeclared `groups` is an empty collection; an update's carries only the
        keys its `set` block names. Emptiness is what marks a non-declaration in
        either, so the guards below bind to the create form the day one declares a
        group, without being written twice.
        """
        found = []
        for event in self.scenario.events:
            target = event.expected_postcondition["target"]
            declared = event.expected_postcondition["values"].get("groups")
            if target.kind == "bug" and declared:
                found.append((event, target.name, {ref.name for ref in declared}))
        return found

    def test_digest_matches_the_pinned_value(self):
        self.assertEqual(
            self.scenario.digest, EXPECTED_DIGEST,
            "the fixture changed; update EXPECTED_DIGEST deliberately and note that "
            "any journal written against the old content is now invalid")

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

    def test_a_bug_group_restriction_is_declared(self):
        """Issue #42: the groups write path is only proven live if something declares it.

        The contract accepts `groups` on both bug payloads and the verifier folds it as
        an asserted field, but a path no scenario declares is exercised by the unit
        suite alone -- and `scenarios/smoke/` is the one CI replays against a real
        Bugzilla.
        """
        self.assertTrue(
            self._group_restrictions(),
            "no event declares a bug 'groups' value, so no live run drives the groups "
            "write path")

    def test_every_handler_action_is_exercised(self):
        self.assertEqual({event.action for event in self.scenario.events}, set(HANDLERS))

    def test_all_custom_field_types_assigned(self):
        """Criterion 4 asks for all three *types*, not merely all declared fields."""
        type_of = {
            resource.name: resource.data["field_type"]
            for resource in self.scenario.resources
            if resource.kind == "custom-field"
        }
        assigned = {
            assignment["field"].name
            for event in self.scenario.events
            if event.action == "bug.custom-field-set"
            for assignment in event.expected_postcondition["values"]["values"]
        }
        self.assertEqual(assigned, set(type_of), "not every declared custom field is set")
        self.assertEqual(
            {type_of[name] for name in assigned},
            {"text", "single-select", "multi-select"})

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

    def test_group_restrictions_are_settable_by_their_actor(self):
        """Guard: a non-member's restriction throws `group_restriction_not_allowed`.

        `Bugzilla/Bug.pm::add_group` refuses a group the acting user is not a member
        of, outside a product change, so a replay that has already mutated the
        fixture aborts part-way -- and finding D11 renders that refusal as
        `410 "You must log in"`, which does not name the real cause.

        Membership is the only half of `add_group` left to guard. Its other refusal,
        `group_is_settable` on a group the scenario did not map to the bug's product
        (ADR 0013), is already a load-time failure: `loader._update_set` raises
        "group is outside the target product" before an event exists to inspect.
        """
        for event, alias, groups in self._group_restrictions():
            held = self._groups(event.actor.name)
            for name in sorted(groups):
                self.assertIn(
                    name, held,
                    f"{event.name!r} restricts {alias!r} to group {name!r} as "
                    f"{event.actor.name!r}, who is not a member of it")

    def test_group_restricted_bugs_declare_no_private_comment(self):
        """Guard: the verifier's outsider read cannot open a group-restricted bug.

        `check_visibility` proves a private comment is withheld by re-reading the
        thread as an actor outside the insider group (`verify/runner.py`). That actor
        is outside the restricting group too, so Bugzilla refuses the whole read
        rather than returning a thread with the private comment dropped, and
        `ServerReader._object` raises on the refusal instead of reporting a finding.
        Declaring both on one bug asserts comment privacy through a bug the reader
        may not open, which reads as a fixture fault rather than the scenario's own.
        """
        private = {
            event.expected_postcondition["target"].name
            for event in self.scenario.events
            if event.action == "bug.comment"
            and event.expected_postcondition["values"]["private"]
        }
        for _event, alias, _groups in self._group_restrictions():
            self.assertNotIn(
                alias, private,
                f"{alias!r} is declared group-restricted and also carries a private "
                "comment, so the outsider visibility read cannot reach it")

    def test_attachment_summaries_fit(self):
        """Guard: Bugzilla truncates an over-length description instead of refusing."""
        for event in self.scenario.events:
            if event.action != "bug.attach":
                continue
            values = event.expected_postcondition["values"]
            rendered = render_attachment_summary(
                values["description"], event.reconciliation_marker, values["asset_sha256"])
            size = len(rendered.encode("utf-8"))
            self.assertLessEqual(
                size, ATTACHMENT_SUMMARY_BYTE_LIMIT,
                f"{event.name!r} renders a {size}-byte summary")

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
