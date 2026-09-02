from __future__ import annotations

import re
import unittest
from dataclasses import replace
from pathlib import Path

from bzr_live.scenario import load_scenario
from bzr_live.verify.checks import check_fields
from bzr_live.verify.expected import ExpectedFlag, fold

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "scenarios" / "smoke"

# `bzr bug view 1 --fields <VIEW_FIELDS + the scenario's cf_* names>` for
# `cart-double-charge` on a replayed scenarios/smoke/. The keys are exactly the requested
# ones: bug_to_json serializes the whole Bug and then retains the selection
# (bzr src/output/resources/bug.rs:73-88).
#
# Three shapes are not what reading the scenario would suggest, and each is taken from
# bzr's serializer at the revision README pins (63abb94e) rather than guessed:
# `component` and `version` deserialize through deserialize_optional_string_list and come
# back as arrays (src/types/bug.rs:50-51, 225-226); `estimated_time` is an f64
# (src/types/bug.rs:68), so the scenario's declared "8" arrives as 8.0.
BUG_VIEW_1 = {
    "id": 1,
    "summary": "Checkout charges twice when two tabs submit one cart",
    "status": "CONFIRMED",
    "resolution": "",
    "dupe_of": None,
    "product": "checkout",
    "component": ["cart"],
    "version": ["checkout-v1"],
    "assigned_to": "developer@example.test",
    "keywords": ["regression", "perf"],
    "blocks": [],
    "depends_on": [12],
    "cc": ["reporter@example.test", "triager@example.test"],
    "target_milestone": "checkout-m2",
    "flags": [],
    "groups": [],
    "estimated_time": 8.0,
    "cf_risk": "high",
    "cf_subsystem": ["payment", "cart"],
    "cf_tracker": "OPS-1042",
}

# The journal's resolved_ids inverted: the only route from a server id back to an alias.
ALIAS_OF = {1: "cart-double-charge", 5: "cart-dupe-report", 8: "pay-retry-loop",
            12: "inv-tax-mismatch"}

# The one rendering allowed to carry a number, for an id the scenario never named.
# Stripping it must leave a detail with no digits at all.
UNNAMED = re.compile(r"bug id \d+ \(not named by this scenario\)")


class FieldCheckTest(unittest.TestCase):
    """check_fields against the transcribed reply for bug 1."""

    @classmethod
    def setUpClass(cls) -> None:
        expected = fold(load_scenario(str(SMOKE)))
        cls.bug = expected.bugs["cart-double-charge"]
        cls.emails = expected.actor_emails

    def _check(self, *, bug=None, drop=(), **overrides):
        observed = {key: value
                    for key, value in {**BUG_VIEW_1, **overrides}.items()
                    if key not in drop}
        return check_fields(self.bug if bug is None else bug, observed, ALIAS_OF,
                            self.emails)

    def _divergences(self, **kwargs):
        return [f for f in self._check(**kwargs) if f.kind == "divergence"]

    def _only(self, **kwargs):
        findings = self._divergences(**kwargs)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].subject, "cart-double-charge")
        return findings[0]

    def test_a_matching_payload_yields_no_findings(self) -> None:
        # The bug's two unverifiable claims are exercised on their own below; clearing
        # them leaves the seven comparison groups as the only thing this case reports.
        self.assertEqual(self._check(bug=replace(self.bug, unverifiable=())), [])

    def test_a_differing_status_names_the_declared_and_observed_value(self) -> None:
        bug = replace(self.bug, scalars={**self.bug.scalars, "status": "RESOLVED"})
        finding = self._only(bug=bug, status="CONFIRMED")
        self.assertEqual(finding.check, "status")
        self.assertEqual(finding.detail, "declared RESOLVED, observed CONFIRMED")

    def test_a_differing_component_diverges_through_the_array_shape(self) -> None:
        # The single-element array bzr returns unwraps for comparison, so a real
        # difference still bites rather than every component reading as a mismatch.
        finding = self._only(component=["payment"])
        self.assertEqual(finding.check, "component")
        self.assertEqual(finding.detail, "declared cart, observed payment")

    def test_a_missing_cc_member_diverges(self) -> None:
        finding = self._only(cc=["reporter@example.test"])
        self.assertEqual(finding.check, "cc")
        self.assertIn("triager@example.test", finding.detail)

    def test_an_extra_cc_member_the_scenario_never_declared_yields_none(self) -> None:
        # cc is containment, not equality. PR #23 named the flag requestee landing on the
        # CC list as a postcondition that is not the server's final state and excluded
        # asserting against it; pay-retry-loop declares no cc at all, so equality would
        # assert exactly that exclusion.
        self.assertEqual(
            self._divergences(cc=[*BUG_VIEW_1["cc"], "releaser@example.test"]), [])

    def test_a_missing_declared_group_diverges(self) -> None:
        bug = replace(self.bug,
                      names={**self.bug.names, "groups": frozenset({"restricted"})})
        finding = self._only(bug=bug)
        self.assertEqual(finding.check, "groups")
        self.assertEqual(finding.detail, "declared restricted, observed (none)")

    def test_a_differing_estimated_time_diverges(self) -> None:
        finding = self._only(estimated_time=6.0)
        self.assertEqual(finding.check, "estimated_time")
        self.assertEqual(finding.detail, "declared 8, observed 6.0")

    def test_a_differing_assignee_diverges(self) -> None:
        finding = self._only(assigned_to="triager@example.test")
        self.assertEqual(finding.check, "assigned_to")
        self.assertEqual(
            finding.detail,
            "declared developer@example.test, observed triager@example.test")

    def test_a_depends_on_edge_to_another_bug_names_aliases(self) -> None:
        finding = self._only(depends_on=[8])
        self.assertEqual(finding.check, "depends_on")
        self.assertEqual(finding.detail,
                         "declared inv-tax-mismatch, observed pay-retry-loop")

    def test_an_id_the_scenario_never_named_renders_without_a_bare_number(self) -> None:
        finding = self._only(depends_on=[99])
        self.assertEqual(
            finding.detail,
            "declared inv-tax-mismatch, observed bug id 99 (not named by this scenario)")

    def test_a_declared_duplicate_matching_the_reply_yields_no_finding(self) -> None:
        bug = replace(self.bug, duplicate_of="inv-tax-mismatch")
        self.assertEqual(self._divergences(bug=bug, dupe_of=12), [])

    def test_an_absent_dupe_of_diverges_naming_the_alias(self) -> None:
        bug = replace(self.bug, duplicate_of="inv-tax-mismatch")
        finding = self._only(bug=bug, dupe_of=None)
        self.assertEqual(finding.check, "dupe_of")
        self.assertEqual(finding.detail, "declared inv-tax-mismatch, observed (absent)")

    def test_a_declared_custom_field_absent_from_the_reply_diverges(self) -> None:
        # The --fields request named cf_risk, so its absence is the fixture disagreeing,
        # not a value bzr cannot read back.
        finding = self._only(drop=("cf_risk",))
        self.assertEqual(finding.check, "cf_risk")
        self.assertEqual(finding.detail, "declared high, observed (absent)")

    def test_a_multi_select_custom_field_compares_as_a_set(self) -> None:
        self.assertEqual(self._divergences(cf_subsystem=["payment", "cart"]), [])
        finding = self._only(cf_subsystem=["payment"])
        self.assertEqual(finding.check, "cf_subsystem")
        self.assertEqual(finding.detail, "declared cart, payment, observed payment")

    def test_a_flag_whose_requestee_differs_diverges(self) -> None:
        bug = replace(self.bug, flags=(ExpectedFlag("review", "?", "releaser"),))
        finding = self._only(bug=bug, flags=[{
            "name": "review", "status": "?", "setter": "developer@example.test",
            "requestee": "developer@example.test"}])
        self.assertEqual(finding.check, "flags")
        self.assertEqual(
            finding.detail,
            "declared review?(releaser@example.test), "
            "observed review?(developer@example.test)")

    def test_a_flag_matching_name_status_and_requestee_yields_no_finding(self) -> None:
        bug = replace(self.bug, flags=(ExpectedFlag("review", "?", "releaser"),))
        self.assertEqual(self._divergences(bug=bug, flags=[{
            "name": "review", "status": "?", "setter": "developer@example.test",
            "requestee": "releaser@example.test"}]), [])

    def test_a_declared_clear_requires_no_entry_with_that_name(self) -> None:
        # fold drops a cleared flag rather than folding an "X" ExpectedFlag, so this
        # pins the published rule for a caller that builds one, mirroring the replay
        # reconciler (src/bzr_live/replay/actions.py:618-621).
        bug = replace(self.bug, flags=(ExpectedFlag("review", "X", None),))
        self.assertEqual(self._divergences(bug=bug), [])
        finding = self._only(bug=bug, flags=[{"name": "review", "status": "?"}])
        self.assertEqual(finding.detail, "declared reviewX, observed review?")

    def test_a_status_the_fold_left_unasserted_yields_no_finding(self) -> None:
        # Bugzilla drives status and resolution itself on a duplicate marking (finding
        # G5), so neither is the scenario's to claim.
        bug = replace(
            self.bug,
            scalars={**self.bug.scalars, "status": "RESOLVED", "resolution": "DUPLICATE"},
            unasserted=frozenset({"status", "resolution"}),
            unverifiable=())
        self.assertEqual(self._check(bug=bug, status="CONFIRMED", resolution=""), [])

    def test_each_unverifiable_entry_is_reported_with_its_reason(self) -> None:
        findings = self._check()
        self.assertTrue(all(f.kind == "unverifiable" for f in findings), findings)
        self.assertEqual([f.check for f in findings], ["remaining_hours", "worktime"])
        self.assertEqual({f.subject for f in findings}, {"cart-double-charge"})
        self.assertEqual([f.detail for f in findings],
                         [reason for _field, reason in self.bug.unverifiable])

    def test_no_finding_detail_carries_a_bare_bug_id(self) -> None:
        duplicate = replace(self.bug, duplicate_of="inv-tax-mismatch")
        findings = [
            *self._divergences(depends_on=[8]),
            *self._divergences(depends_on=[99]),
            *self._divergences(depends_on=[]),
            *self._divergences(bug=duplicate, dupe_of=99),
            *self._divergences(bug=duplicate, dupe_of=None),
        ]
        self.assertEqual(len(findings), 5)
        for finding in findings:
            residue = UNNAMED.sub("", finding.detail)
            self.assertIsNone(re.search(r"\d", residue), finding.detail)


if __name__ == "__main__":
    unittest.main()
