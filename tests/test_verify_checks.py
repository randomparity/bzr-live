from __future__ import annotations

import re
import unittest
from dataclasses import replace
from pathlib import Path

from bzr_live.scenario import load_scenario
from bzr_live.verify.checks import chain_order, check_fields, check_history
from bzr_live.verify.expected import ExpectedChange, ExpectedFlag, fold

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


DEVELOPER = "developer@example.test"
TRIAGER = "triager@example.test"
RELEASER = "releaser@example.test"
ADMIN_OPS = "admin-ops@example.test"


def _record(when: str, who: str, field: str, old: str, new: str,
            comment: int | None = None) -> dict:
    """One flattened `bug history` record, in the shape bzr's `--json` data list holds.

    `comment_id` is carried for fidelity with the live reply; no check reads it.
    """
    return {"when": when, "who": f"{who}@example.test", "field": field,
            "old_value": old, "new_value": new, "comment_id": comment}


# `/Users/dave/src/bzr/target/release/bzr --json --server-url http://127.0.0.1:8080/
# bug history 9` for `pay-decline-copy` on a replayed scenarios/smoke/, read live at
# implementation time rather than copied from the plan: how many records share a
# `bug_when` is a property of how fast the replay ran. The reply puts the triager's
# reopen ahead of the developer's resolve -- the order they did not happen in -- because
# Bugzilla's ORDER BY bug_when leaves same-second ties unordered.
HISTORY_9 = [
    _record("2026-09-02T14:19:46Z", "triager", "assigned_to", DEVELOPER, RELEASER),
    _record("2026-09-02T14:19:48Z", "triager", "status", "RESOLVED", "CONFIRMED"),
    _record("2026-09-02T14:19:48Z", "developer", "resolution", "", "FIXED"),
    _record("2026-09-02T14:19:48Z", "developer", "status", "CONFIRMED", "RESOLVED"),
    _record("2026-09-02T14:19:48Z", "triager", "resolution", "FIXED", ""),
    _record("2026-09-02T14:19:49Z", "developer", "status", "CONFIRMED", "RESOLVED"),
    _record("2026-09-02T14:19:49Z", "developer", "resolution", "", "FIXED"),
    _record("2026-09-02T14:20:02Z", "releaser", "flagtypes.name", "", "signoff+"),
]
# `bzr bug view 9 --fields ...` for the same bug: the chain's tail anchor.
VIEW_9 = {
    "summary": "Decline message tells the customer to contact the wrong party",
    "status": "RESOLVED",
    "resolution": "FIXED",
    "assigned_to": RELEASER,
    "target_milestone": "---",
}
STATUS_9 = [record for record in HISTORY_9 if record["field"] == "status"]
# The same three records collapsed into one `when` bucket -- the shape the reply takes
# when the replay runs faster. Both shapes must recover the same order, which is what
# proves the two value-identical developer records are deduplicated rather than counted
# as two answers.
ONE_BUCKET_9 = [dict(record, when="2026-09-02T14:19:48Z") for record in STATUS_9]
# The chain with the triager's reopen dropped: neither ordering of what is left links.
UNLINKED_STATUS = [record for record in STATUS_9 if record["who"] == DEVELOPER]
# The one-bucket chain with the trailing record's `who` changed. The dedup cannot
# collapse it any more, and RESOLVED is reachable with either developer at the tail, so
# two genuinely different orderings link.
AMBIGUOUS_STATUS = [*ONE_BUCKET_9[:2], dict(ONE_BUCKET_9[2], who=RELEASER)]
# Eight records in one bucket -- one above _MAX_BUCKET, rejected before any search.
OVERSIZED_BUCKET = [
    _record("2026-09-02T14:19:48Z", "developer", "summary", f"s{i}", f"s{i + 1}")
    for i in range(8)]
# Two buckets of seven chained records. The identity permutation of each links and ends
# at s14, so the search records a solution on the first branch it walks and then runs out
# of budget with orderings still unenumerated: 2 x 5,040 permutations against 10,000.
BUDGET_BOUND = [
    _record("2026-09-02T14:19:48Z", "developer", "summary", f"s{i}", f"s{i + 1}")
    for i in range(7)
] + [
    _record("2026-09-02T14:19:49Z", "developer", "summary", f"s{i}", f"s{i + 1}")
    for i in range(7, 14)
]
# The chain with a status record the scenario never declared spliced into it, as a
# duplicate marking splices one. The declared actors must still read as a subsequence.
INJECTED_STATUS = [
    *STATUS_9[:2],
    _record("2026-09-02T14:19:49Z", "triager", "status", "RESOLVED", "CONFIRMED"),
    _record("2026-09-02T14:19:50Z", "developer", "status", "CONFIRMED", "RESOLVED"),
]

# `bug history 8` for `pay-retry-loop`: four records, of which the scenario declares one.
# The other three are the server's -- two materialised inverse edges and the CC entry
# Bugzilla writes beside a flag requestee.
HISTORY_8 = [
    _record("2026-09-02T14:19:39Z", "admin-ops", "depends_on", "", "7"),
    _record("2026-09-02T14:19:40Z", "developer", "blocks", "", "18"),
    _record("2026-09-02T14:20:01Z", "developer", "cc", "", RELEASER),
    _record("2026-09-02T14:20:01Z", "developer", "flagtypes.name", "",
            f"review?({RELEASER})"),
]
# `bug history 5` for `cart-dupe-report`: the status and resolution the duplicate marking
# drove, and no `dupe_of` row at all.
HISTORY_5 = [
    _record("2026-09-02T14:19:41Z", "triager", "resolution", "", "DUPLICATE", 21),
    _record("2026-09-02T14:19:41Z", "triager", "status", "CONFIRMED", "RESOLVED", 21),
]
# `bug history 18` for `dun-wrong-locale`: one record for the two members
# `link-diamond-sink` declares, with the generated ids comma-joined.
HISTORY_18 = [
    _record("2026-09-02T14:19:40Z", "developer", "depends_on", "", "8, 14"),
]
# `bug history 2` for `cart-empty-crash`, whose summary was seeded at create and never
# changed: Bugzilla writes no bugs_activity row for a creation.
HISTORY_2: list[dict] = []


class ChainOrderTest(unittest.TestCase):
    """chain_order over the transcribed bug 9 payloads and its three non-answers."""

    def test_the_two_bucket_reply_recovers_an_order_it_did_not_return(self) -> None:
        self.assertEqual([record["who"] for record in STATUS_9],
                         [TRIAGER, DEVELOPER, DEVELOPER])
        ordered, reason = chain_order(STATUS_9, "RESOLVED")
        self.assertIsNone(reason)
        self.assertEqual([record["who"] for record in ordered],
                         [DEVELOPER, TRIAGER, DEVELOPER])

    def test_the_one_bucket_variant_recovers_the_same_order(self) -> None:
        ordered, reason = chain_order(ONE_BUCKET_9, "RESOLVED")
        self.assertIsNone(reason)
        self.assertEqual([record["who"] for record in ordered],
                         [DEVELOPER, TRIAGER, DEVELOPER])

    def test_no_records_is_not_a_broken_chain(self) -> None:
        self.assertEqual(chain_order([], "CONFIRMED"), ([], None))

    def test_a_chain_missing_a_record_is_unlinked(self) -> None:
        self.assertEqual(chain_order(UNLINKED_STATUS, "RESOLVED"), (None, "unlinked"))

    def test_two_genuinely_different_orderings_are_ambiguous(self) -> None:
        self.assertEqual(chain_order(AMBIGUOUS_STATUS, "RESOLVED"), (None, "ambiguous"))

    def test_a_bucket_above_the_cap_is_rejected_before_the_search(self) -> None:
        self.assertEqual(chain_order(OVERSIZED_BUCKET, "s8"), (None, "oversized"))

    def test_an_exhausted_budget_answers_oversized_not_the_first_solution(self) -> None:
        # The premise: the first branch the search walks does link and does end at s14.
        first_bucket, reason = chain_order(BUDGET_BOUND[:7], "s7")
        self.assertIsNone(reason)
        self.assertEqual(len(first_bucket), 7)
        # So a search that checked its solution count before its budget would hand that
        # ordering back as unique. It is not: 10,000 permutations do not cover 5,040 x
        # 5,040, and the orderings never reached could hold a second answer.
        self.assertEqual(chain_order(BUDGET_BOUND, "s14"), (None, "oversized"))


class HistoryCheckTest(unittest.TestCase):
    """check_history against the transcribed replies for bugs 2, 5, 8, 9 and 18."""

    @classmethod
    def setUpClass(cls) -> None:
        expected = fold(load_scenario(str(SMOKE)))
        cls.bugs = expected.bugs
        cls.emails = expected.actor_emails

    def _history(self, bug, records, observed=None):
        return check_history(bug, records, observed or {}, self.emails)

    def _only(self, bug, records, observed=None):
        findings = self._history(bug, records, observed)
        self.assertEqual(len(findings), 1, findings)
        return findings[0]

    def _declaring(self, alias, *changes):
        return replace(self.bugs[alias], history=changes)

    def test_every_declared_change_present_yields_no_finding(self) -> None:
        self.assertEqual(
            self._history(self.bugs["pay-decline-copy"], HISTORY_9, VIEW_9), [])

    def test_an_absent_declared_change_names_the_actor_and_the_field(self) -> None:
        records = [record for record in HISTORY_9
                   if record["field"] != "flagtypes.name"]
        finding = self._only(self.bugs["pay-decline-copy"], records, VIEW_9)
        self.assertEqual(finding.kind, "divergence")
        self.assertEqual(finding.check, "flagtypes.name")
        self.assertIn(RELEASER, finding.detail)
        self.assertIn("flagtypes.name", finding.detail)

    def test_records_the_scenario_never_declared_are_tolerated(self) -> None:
        # The CC entry beside a flag requestee and the two materialised inverse edges on
        # bug 8; the status and resolution a duplicate marking drove on bug 5.
        self.assertEqual(self._history(self.bugs["pay-retry-loop"], HISTORY_8), [])
        self.assertEqual(self._history(self.bugs["cart-dupe-report"], HISTORY_5), [])

    def test_a_change_declared_twice_needs_two_records(self) -> None:
        bug = self._declaring(
            "cart-dupe-report",
            ExpectedChange("admin-ops", "cf_tracker", "OPS-1042", False),
            ExpectedChange("admin-ops", "cf_tracker", "OPS-1042", False))
        record = _record("2026-09-02T14:20:03Z", "admin-ops", "cf_tracker", "",
                         "OPS-1042")
        self.assertEqual(self._history(bug, [record, dict(record)]), [])
        finding = self._only(bug, [record])
        self.assertEqual(finding.kind, "divergence")
        self.assertEqual(finding.check, "cf_tracker")
        self.assertIn(ADMIN_OPS, finding.detail)

    def test_a_chain_that_links_no_ordering_diverges(self) -> None:
        bug = self._declaring(
            "pay-decline-copy",
            ExpectedChange("developer", "status", "RESOLVED", True))
        finding = self._only(bug, UNLINKED_STATUS, {"status": "RESOLVED"})
        self.assertEqual(finding.kind, "divergence")
        self.assertEqual(finding.check, "status")
        self.assertIn("unlinked", finding.detail)

    def test_an_ambiguous_chain_is_unverifiable_rather_than_guessed(self) -> None:
        bug = self._declaring(
            "pay-decline-copy",
            ExpectedChange("developer", "status", "RESOLVED", True))
        finding = self._only(bug, AMBIGUOUS_STATUS, {"status": "RESOLVED"})
        self.assertEqual(finding.kind, "unverifiable")
        self.assertEqual(finding.check, "status")
        self.assertIn("ambiguous", finding.detail)

    def test_an_oversized_chain_is_unverifiable_rather_than_guessed(self) -> None:
        bug = self._declaring(
            "cart-double-charge",
            ExpectedChange("developer", "summary", "s14", True))
        finding = self._only(bug, BUDGET_BOUND, {"summary": "s14"})
        self.assertEqual(finding.kind, "unverifiable")
        self.assertEqual(finding.check, "summary")
        self.assertIn("oversized", finding.detail)

    def test_an_injected_status_change_still_orders(self) -> None:
        declared = self.bugs["pay-decline-copy"].history
        bug = self._declaring(
            "pay-decline-copy",
            *(change for change in declared if change.field == "status"))
        self.assertEqual(self._history(bug, INJECTED_STATUS, VIEW_9), [])

    def test_a_bug_whose_summary_was_only_seeded_yields_no_finding(self) -> None:
        self.assertEqual(
            self._history(self.bugs["cart-empty-crash"], HISTORY_2,
                          {"summary": "Empty cart page returns a server error"}), [])

    def test_a_folded_duplicate_expects_no_dupe_of_record(self) -> None:
        self.assertEqual(self.bugs["cart-dupe-report"].history, ())
        self.assertEqual(self._history(self.bugs["cart-dupe-report"], HISTORY_5), [])

    def test_a_two_member_edge_addition_expects_one_record(self) -> None:
        self.assertEqual(self._history(self.bugs["dun-wrong-locale"], HISTORY_18), [])

    def test_a_two_member_cc_addition_compares_as_a_set(self) -> None:
        bug = self._declaring(
            "cart-dupe-report",
            ExpectedChange("developer", "cc",
                           frozenset({"a@example.test", "b@example.test"}), False))
        record = _record("2026-09-02T14:20:04Z", "developer", "cc", "",
                         "b@example.test, a@example.test")
        self.assertEqual(self._history(bug, [record]), [])


if __name__ == "__main__":
    unittest.main()
