from __future__ import annotations

import unittest
from pathlib import Path

from bzr_live.scenario import load_scenario
from bzr_live.verify.expected import INSIDER_GROUP, fold, link_edges, reachable

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "scenarios" / "smoke"
GROUPS_UPDATE = ROOT / "tests" / "fixtures" / "verify-groups-update"


class FoldSmokeScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = fold(load_scenario(str(SMOKE)))

    def test_every_declared_bug_is_folded(self) -> None:
        self.assertEqual(len(self.expected.bugs), 20)

    def test_reopening_cycle_folds_to_the_last_declared_state(self) -> None:
        bug = self.expected.bugs["pay-decline-copy"]
        self.assertEqual(bug.scalars["status"], "RESOLVED")
        self.assertEqual(bug.scalars["resolution"], "FIXED")
        self.assertEqual(bug.scalars["assigned_to"], "releaser@example.test")

    def test_flag_requestee_joins_expected_cc(self) -> None:
        bug = self.expected.bugs["pay-retry-loop"]
        self.assertEqual(bug.names["cc"], frozenset({"releaser@example.test"}))

    def test_set_fields_fold_as_the_union_of_declarations(self) -> None:
        bug = self.expected.bugs["cart-double-charge"]
        self.assertEqual(bug.names["keywords"], frozenset({"regression", "perf"}))
        self.assertEqual(
            bug.names["cc"],
            frozenset({"reporter@example.test", "triager@example.test"}))

    def test_keyword_delta_reaches_history_not_the_whole_set(self) -> None:
        bug = self.expected.bugs["cart-double-charge"]
        # keywords is a _NAME_SETS field, so the change carries the added members as a
        # frozenset even when only one was added -- check_history splits the observed
        # value on ", " and compares sets, so the single-addition case is not special.
        added = [c.value for c in bug.history if c.field == "keywords"]
        self.assertEqual(added, [frozenset({"perf"})])

    def test_create_contributes_no_history(self) -> None:
        bug = self.expected.bugs["cart-empty-crash"]
        self.assertEqual(bug.history, ())

    def test_inverse_blocks_edge_lands_on_both_endpoints(self) -> None:
        self.assertIn("inv-tax-mismatch",
                      self.expected.bugs["cart-double-charge"].edges["depends_on"])
        self.assertIn("cart-double-charge",
                      self.expected.bugs["inv-tax-mismatch"].edges["blocks"])

    def test_dupe_edge_is_recorded_on_the_source_only(self) -> None:
        self.assertEqual(
            self.expected.bugs["cart-dupe-report"].duplicate_of, "cart-double-charge")
        self.assertIsNone(self.expected.bugs["cart-double-charge"].duplicate_of)

    def test_duplicate_unasserts_status_and_resolution(self) -> None:
        bug = self.expected.bugs["cart-dupe-report"]
        self.assertIn("status", bug.unasserted)
        self.assertIn("resolution", bug.unasserted)

    def test_the_two_time_tracking_fields_and_worktime_are_unverifiable(self) -> None:
        # estimated_hours is waived, but NOT on finding D3's grounds: bzr a7f6ab70 does
        # expose estimated_time in bug view, and README pins make smoke at 63abb94e,
        # which contains it. The first live run of the verify stage established that
        # a7f6ab70 is not sufficient -- Bugzilla gates the time-tracking fields on
        # timetrackinggroup and finding D8 leaves bzr's REST reads unauthenticated, so
        # bug view withholds the field on the transport this verifier uses.
        bug = self.expected.bugs["cart-double-charge"]
        fields = {name for name, _ in bug.unverifiable}
        self.assertEqual(fields, {"estimated_hours", "remaining_hours", "worktime"})
        self.assertNotIn("estimated_time", bug.scalars)
        reason = dict(bug.unverifiable)["estimated_hours"]
        self.assertIn("timetrackinggroup", reason)
        self.assertIn("D8", reason)

    def test_one_bug_declares_a_group_so_the_live_tier_asserts_it(self) -> None:
        # UNVERIFIABLE_FIELDS says so; this pins the claim rather than leaving it prose.
        # Exactly one, because the count is what the two comments narrowed on it rest on:
        # the estimated_hours entry states the anonymously-readable case, and it is
        # `pay-refund-rounding` declaring no time field that keeps that true (issue #42).
        restricted = [
            alias for alias, bug in self.expected.bugs.items() if "groups" in bug.names]
        self.assertEqual(restricted, ["pay-refund-rounding"])
        bug = self.expected.bugs["pay-refund-rounding"]
        self.assertEqual(bug.names["groups"], frozenset({"restricted"}))
        self.assertEqual(bug.unverifiable, ())

    def test_custom_fields_carry_their_cf_names(self) -> None:
        bug = self.expected.bugs["cart-double-charge"]
        self.assertEqual(bug.custom_fields["cf_risk"], "high")
        self.assertEqual(bug.custom_fields["cf_subsystem"], frozenset({"cart", "payment"}))
        self.assertEqual(bug.custom_fields["cf_tracker"], "OPS-1042")
        self.assertEqual(
            self.expected.custom_field_keys, ("cf_risk", "cf_subsystem", "cf_tracker"))

    def test_comments_carry_author_privacy_and_marker_order(self) -> None:
        bug = self.expected.bugs["cart-double-charge"]
        self.assertIsNone(bug.comments[0].marker)
        self.assertEqual(bug.comments[0].author, "reporter")
        markers = [c.marker for c in bug.comments[1:]]
        self.assertEqual(markers, [
            "bzr-live:smoke:comment-triage-double-charge",
            "bzr-live:smoke:worktime-double-charge"])

    def test_private_comment_is_marked_private(self) -> None:
        bug = self.expected.bugs["pay-token-leak"]
        private = [c for c in bug.comments if c.private]
        self.assertEqual(len(private), 1)
        self.assertEqual(private[0].author, "admin-ops")

    def test_attachment_folds_its_obsolescence_and_checksum(self) -> None:
        bug = self.expected.bugs["cart-double-charge"]
        self.assertEqual(len(bug.attachments), 1)
        attachment = bug.attachments[0]
        self.assertTrue(attachment.obsolete)
        self.assertEqual(
            attachment.sha256,
            "96a330b23f0ebeb73d94721fce926b0b49daacfc448696af2f373afb30b49681")
        self.assertIn("[bzr-live:smoke:attach-triage-notes]", attachment.summary)

    def test_flag_requestee_is_modelled_in_cc_but_never_asserted(self) -> None:
        # The running set exists to make a later cc delta correct, not to assert the
        # postcondition PR #23 excluded. So it holds the requestee, and no ExpectedChange
        # and no equality assertion rests on it.
        bug = self.expected.bugs["pay-retry-loop"]
        self.assertEqual(bug.names["cc"], frozenset({"releaser@example.test"}))
        self.assertEqual([c for c in bug.history if c.field == "cc"], [])

    def test_flags_fold_with_their_requestee(self) -> None:
        bug = self.expected.bugs["pay-retry-loop"]
        self.assertEqual(len(bug.flags), 1)
        self.assertEqual(bug.flags[0].name, "review")
        self.assertEqual(bug.flags[0].status, "?")
        self.assertEqual(bug.flags[0].requestee, "releaser")

    def test_reader_roles_come_from_declared_groups(self) -> None:
        self.assertEqual(self.expected.insider, "admin-ops")
        self.assertIsNotNone(self.expected.outsider)
        self.assertNotEqual(self.expected.outsider, "admin-ops")
        self.assertEqual(INSIDER_GROUP, "admin")


class CcOrderingTest(unittest.TestCase):
    """The fold rules scenarios/smoke/ does not exercise.

    No smoke bug declares `cc` after a requestee flag, so the ordering rule -- a later
    declaration replaces the running set and drops the requestee, matching the
    `--cc-remove` the replay engine would compute -- is pinned on a fixture written for
    it rather than left until a scenario happens to hit it. The same fixture carries the
    only *create-time* declared `groups` in the repository, for the same reason:
    `scenarios/smoke/` declares its one group restriction on `bug.update` (issue #42).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = fold(load_scenario(
            str(ROOT / "tests" / "fixtures" / "verify-cc-order")))

    def test_a_later_cc_declaration_drops_the_requestee(self) -> None:
        bug = self.expected.bugs["ordered"]
        self.assertNotIn("developer@example.test", bug.names["cc"])
        self.assertEqual(
            bug.names["cc"],
            frozenset({"triager@example.test", "releaser@example.test"}))

    def test_a_multi_member_addition_is_one_expected_change(self) -> None:
        bug = self.expected.bugs["ordered"]
        multi = [c for c in bug.history
                 if c.field == "cc" and isinstance(c.value, frozenset)
                 and len(c.value) == 2]
        self.assertEqual(len(multi), 1)

    def test_created_groups_are_folded_as_an_asserted_field(self) -> None:
        # A create-time groups set that no later event updates, folded as asserted rather
        # than waived -- bzr a7f6ab70 exposes the field in bug view. The update path is
        # covered by tests/fixtures/verify-groups-update, which needs its own fixture: an
        # update here would replace this set and destroy the create-only case.
        bug = self.expected.bugs["ordered"]
        self.assertEqual(bug.names["groups"], frozenset({"restricted"}))
        self.assertNotIn("groups", {name for name, _ in bug.unverifiable})


class GroupsUpdateFoldTest(unittest.TestCase):
    """A declared `groups` set on bug.update reaches expected state (issue #27).

    Regression guard for a silent coupling: `_update_other` ignored every key outside
    {duplicate_of, estimated_hours, remaining_hours}, which was safe only while
    actions._UPDATE_UNSUPPORTED refused `groups` before any mutation. With that refusal
    gone, a fold that drops the update would leave the verifier asserting the *created*
    set against a server holding the updated one -- confirming a bug it never checked.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = fold(load_scenario(str(GROUPS_UPDATE)))

    def test_a_declared_groups_update_replaces_the_folded_set(self) -> None:
        # Asserts the VALUE, not the key's presence: a fold that dropped the update
        # leaves names["groups"] holding {restricted}, which a presence-only test passes.
        bug = self.expected.bugs["guarded"]
        self.assertEqual(
            bug.names["groups"], frozenset({"restricted", "escalated"}))

    def test_the_added_member_reaches_history_as_one_change(self) -> None:
        bug = self.expected.bugs["guarded"]
        added = [c for c in bug.history if c.field == "groups"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].actor, "developer")
        self.assertEqual(added[0].value, frozenset({"escalated"}))
        self.assertFalse(added[0].chain)


class TopologyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = fold(load_scenario(str(SMOKE)))
        cls.edges = link_edges(cls.expected.bugs)

    def test_direct_edges_carry_relation_and_direction(self) -> None:
        self.assertIn(
            ("inv-tax-mismatch", "depends_on", "out"), self.edges["cart-double-charge"])
        self.assertIn(
            ("cart-double-charge", "blocks", "in"), self.edges["inv-tax-mismatch"])
        self.assertIn(
            ("cart-double-charge", "dupe_of", "out"), self.edges["cart-dupe-report"])

    def test_chain_is_three_deep_across_products(self) -> None:
        hops = reachable(self.edges, "cart-double-charge")
        self.assertEqual(hops["inv-tax-mismatch"], 1)
        self.assertEqual(hops["dun-retry-storm"], 2)
        self.assertEqual(hops["inv-duplicate-line"], 2)

    def test_diamond_sink_is_two_hops_from_the_apex(self) -> None:
        hops = reachable(self.edges, "pay-token-leak")
        self.assertEqual(hops["pay-retry-loop"], 1)
        self.assertEqual(hops["inv-currency-drift"], 1)
        self.assertEqual(hops["dun-wrong-locale"], 2)

    def test_dupe_target_does_not_reach_back_to_its_duplicate(self) -> None:
        self.assertNotIn(
            "cart-dupe-report", reachable(self.edges, "cart-double-charge"))
        self.assertEqual(
            reachable(self.edges, "cart-dupe-report")["cart-double-charge"], 1)


if __name__ == "__main__":
    unittest.main()
