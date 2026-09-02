from __future__ import annotations

import base64
import hashlib
import re
import unittest
from dataclasses import replace
from pathlib import Path

from bzr_live.scenario import load_scenario
from bzr_live.verify.checks import (
    chain_order,
    check_attachments,
    check_comments,
    check_fields,
    check_history,
    check_links,
    check_visibility,
)
from bzr_live.verify.expected import (
    ExpectedChange, ExpectedFlag, fold, link_edges, reachable)

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
            12: "inv-tax-mismatch", 15: "inv-duplicate-line", 17: "dun-retry-storm",
            18: "dun-wrong-locale"}

# The one rendering allowed to carry a number, for an id the scenario never named.
# Stripping it must leave a detail with no digits at all.
UNNAMED = re.compile(r"bug id \d+ \(not named by this scenario\)")
# A hop distance in the declared graph, the only other number a detail may carry.
DEPTH = re.compile(r"depth \d+")


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


def _link(bug_id: int, relation: str, direction: str, depth: int,
          summary: str) -> dict:
    """One `bug links` record, in the shape bzr's `--json` data list holds.

    `summary` and `status` are carried for fidelity with the live reply; no check reads
    either, and `summary` is the only field that distinguishes two records here.
    """
    return {"id": bug_id, "relation": relation, "direction": direction, "depth": depth,
            "summary": summary, "status": "CONFIRMED"}


TAX = "Invoice tax total disagrees with the order tax total"
DUNNING = "Dunning retries all fire in the same minute"
DUPLICATE_LINE = "Duplicate line item on invoices for split shipments"
CHARGES_TWICE = "Checkout charges twice when two tabs submit one cart"

# `/Users/dave/src/bzr/target/release/bzr --json --server-url http://127.0.0.1:8080/
# bug links 1` for `cart-double-charge` on a replayed scenarios/smoke/, read live at the
# revision README pins (63abb94e) rather than copied from the plan: bzr 9ad5ceb2 ("decode
# object-valued duplicate links") changed src/types/bug/links.rs after the reads the
# design docs quote, so only a read at the pinned binary is evidence for this family.
LINKS_1 = [_link(12, "depends_on", "out", 1, TAX)]
# `bug links 1 --recursive --depth 3`: bug 12 at one hop, and the diamond's other two
# corners at two. Nothing reaches bug 5 -- Bugzilla materialises dupe_of on the source
# alone, so the duplicate pair is readable only from `cart-dupe-report`.
WALK_1 = [
    _link(12, "depends_on", "out", 1, TAX),
    _link(17, "depends_on", "out", 2, DUNNING),
    _link(15, "blocks", "in", 2, DUPLICATE_LINE),
]
# `bug links 5` for `cart-dupe-report`: the duplicate edge, from the source endpoint.
LINKS_5 = [_link(1, "dupe_of", "out", 1, CHARGES_TWICE)]


class LinkCheckTest(unittest.TestCase):
    """check_links against the transcribed link replies for bugs 1 and 5."""

    @classmethod
    def setUpClass(cls) -> None:
        expected = fold(load_scenario(str(SMOKE)))
        cls.edges = link_edges(expected.bugs)
        cls.hops = reachable(cls.edges, "cart-double-charge")

    def _check(self, alias="cart-double-charge", *, declared_hops=None, direct=None,
               walk=None):
        # walk=[] is the shape Task 7 hands a skipped recursive read, and it comes with
        # an empty declared_hops: the two arguments are one decision, never mixed.
        if walk == []:
            declared_hops = {}
        return check_links(
            alias, self.edges[alias],
            self.hops if declared_hops is None else declared_hops,
            LINKS_1 if direct is None else direct,
            WALK_1 if walk is None else walk,
            ALIAS_OF)

    def _only(self, *args, **kwargs):
        findings = self._check(*args, **kwargs)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].kind, "divergence")
        return findings[0]

    def test_the_declared_direct_edge_is_the_one_the_reply_carries(self) -> None:
        self.assertEqual(self.edges["cart-double-charge"],
                         frozenset({("inv-tax-mismatch", "depends_on", "out")}))
        self.assertEqual(self._check(), [])

    def test_a_missing_direct_edge_names_both_aliases_and_the_relation(self) -> None:
        finding = self._only(direct=[], walk=[])
        self.assertEqual(finding.subject, "cart-double-charge")
        self.assertEqual(finding.detail,
                         "declared inv-tax-mismatch depends_on out, observed (absent)")

    def test_an_observed_edge_the_scenario_never_named_diverges(self) -> None:
        findings = self._check(
            direct=[*LINKS_1, _link(99, "blocks", "in", 1, "unknown")], walk=[])
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(
            findings[0].detail,
            "declared (absent), observed bug id 99 (not named by this scenario) "
            "blocks in")

    def test_an_observed_edge_to_a_named_bug_the_scenario_omits_diverges(self) -> None:
        findings = self._check(
            direct=[*LINKS_1, _link(18, "blocks", "in", 1, "locale")], walk=[])
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].detail,
                         "declared (absent), observed dun-wrong-locale blocks in")

    def test_the_duplicate_pair_reads_from_the_source_endpoint(self) -> None:
        # dupe_of has no stock inverse, so bug 1's own reply carries no edge back to 5;
        # both directions of that claim are asserted here.
        self.assertEqual(
            self._check("cart-dupe-report", declared_hops={}, direct=LINKS_5, walk=[]),
            [])
        self.assertEqual(self._check(), [])

    def test_the_depth_three_walk_matches_the_declared_hop_distances(self) -> None:
        self.assertEqual(
            self.hops,
            {"inv-tax-mismatch": 1, "dun-retry-storm": 2, "inv-duplicate-line": 2})
        self.assertEqual(self._check(), [])

    def test_a_node_observed_at_the_wrong_depth_names_both_depths(self) -> None:
        walk = [WALK_1[0], dict(WALK_1[1], depth=3), WALK_1[2]]
        finding = self._only(walk=walk)
        self.assertEqual(finding.detail,
                         "declared dun-retry-storm at depth 2, observed depth 3")

    def test_a_declared_node_the_walk_never_reaches_diverges(self) -> None:
        finding = self._only(walk=WALK_1[:2])
        self.assertEqual(finding.detail,
                         "declared inv-duplicate-line at depth 2, observed (absent)")

    def test_the_walks_relation_is_not_compared(self) -> None:
        # bzr sorts its frontier by bug id (src/commands/bug/links.rs:42), so the relation
        # credited to a node reachable two ways depends on generated identifiers -- which
        # is exactly what no assertion here may rest on. Depth is the graph's property.
        walk = [WALK_1[0], WALK_1[1], dict(WALK_1[2], relation="depends_on",
                                           direction="out")]
        self.assertEqual(self._check(walk=walk), [])

    def test_an_empty_declared_graph_still_reports_an_observed_edge(self) -> None:
        # The one family Task 7 leaves unconditional: a bug whose fold declares no edge
        # at all must still bite when the fixture holds one.
        findings = check_links("pay-decline-copy", frozenset(), {},
                               [_link(12, "blocks", "in", 1, TAX)], [], ALIAS_OF)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].subject, "pay-decline-copy")
        self.assertEqual(findings[0].detail,
                         "declared (absent), observed inv-tax-mismatch blocks in")

    def test_an_isolated_root_with_nothing_to_walk_yields_no_finding(self) -> None:
        self.assertEqual(
            check_links("pay-decline-copy", frozenset(), {}, [], [], ALIAS_OF), [])

    def test_no_finding_detail_carries_a_bare_bug_id(self) -> None:
        findings = [
            *self._check(direct=[], walk=[]),
            *self._check(direct=[*LINKS_1, _link(99, "blocks", "in", 1, "x")], walk=[]),
            *self._check(walk=[*WALK_1, _link(99, "blocks", "in", 2, "x")]),
            *self._check(walk=[WALK_1[0], dict(WALK_1[1], depth=3), WALK_1[2]]),
        ]
        self.assertEqual(len(findings), 4)
        for finding in findings:
            # A hop distance is a property of the declared graph, not a server id, so it
            # is the one other number a detail may carry.
            residue = DEPTH.sub("", UNNAMED.sub("", finding.detail))
            self.assertIsNone(re.search(r"\d", residue), finding.detail)


# `bzr --json --server-url http://127.0.0.1:8080 --api hybrid comment list 1` for
# `cart-double-charge` on a replayed scenarios/smoke/, read live at the revision README
# pins (63abb94e). The keys are the ones bzr emits and no others: an entry carries
# `creation_time` and no `time` key at all, while the unauthenticated stock-REST reply
# carries both -- a fixture written from the REST shape would not match the command under
# test. No assertion here reads either: several of these comments land inside one second,
# so `count` is the only ordering key.
#
# Counts 1 and 3 are Bugzilla's own injections, not the scenario's: marking bug 5 a
# duplicate writes the notice at count 1, and the attachment upload writes one at count 3.
# The declared comments are therefore neither a prefix of this list nor contiguous in it,
# which is why every lookup goes through the marker token.
COMMENTS_1 = [
    {"id": 1, "bug_id": 1,
     "text": "Two concurrent submissions of the same cart both succeed and the customer "
             "is charged twice.",
     "creator": "reporter@example.test", "creation_time": "2026-09-02T14:19:01Z",
     "count": 0, "is_private": False, "attachment_id": None},
    {"id": 22, "bug_id": 1,
     "text": "*** Bug 5 has been marked as a duplicate of this bug. ***",
     "creator": "triager@example.test", "creation_time": "2026-09-02T14:19:42Z",
     "count": 1, "is_private": False, "attachment_id": None},
    {"id": 23, "bug_id": 1,
     "text": "Reproduced with two tabs on v1. The cart lock is taken after "
             "authorisation, so both submissions see an unpaid cart.\n\n"
             "[bzr-live:smoke:comment-triage-double-charge]",
     "creator": "triager@example.test", "creation_time": "2026-09-02T14:19:54Z",
     "count": 2, "is_private": False, "attachment_id": None},
    {"id": 28, "bug_id": 1,
     "text": "Created attachment 1\nTriage notes "
             "[bzr-live:smoke:attach-triage-notes] "
             "sha256=96a330b23f0ebeb73d94721fce926b0b49daacfc448696af2f373afb30b49681",
     "creator": "triager@example.test", "creation_time": "2026-09-02T14:19:57Z",
     "count": 3, "is_private": False, "attachment_id": 1},
    {"id": 30, "bug_id": 1,
     "text": "Traced the lock ordering and drafted the fix.\n\n"
             "[bzr-live:smoke:worktime-double-charge]",
     "creator": "developer@example.test", "creation_time": "2026-09-02T14:20:02Z",
     "count": 4, "is_private": False, "attachment_id": None},
]

# `... --api hybrid comment list 7` for `pay-token-leak`, read with no API key -- the
# shape an actor outside the insider group sees. The declared private comment is absent,
# which is what check_visibility asserts; comment 0 is public and remains readable.
COMMENTS_7_OUTSIDER = [
    {"id": 7, "bug_id": 7,
     "text": "The payment gateway token appears in plaintext in the request log at info "
             "level.",
     "creator": "admin-ops@example.test", "creation_time": "2026-09-02T14:19:11Z",
     "count": 0, "is_private": False, "attachment_id": None},
]

# The private comment as the thread would carry it, appended to the live outsider reply.
# Constructed, not transcribed: the whole point of the two cases below is a fixture that
# should not serve this entry to an outsider, so no live reply exhibits it.
PRIVATE_ENTRY = {
    "id": 24, "bug_id": 7,
    "text": "Log retention for the affected hosts is 30 days; rotating the gateway "
            "credentials before disclosure.\n\n"
            "[bzr-live:smoke:comment-private-token-leak]",
    "creator": "admin-ops@example.test", "creation_time": "2026-09-02T14:19:56Z",
    "count": 1, "is_private": True, "attachment_id": None,
}

# `... --api hybrid attachment list 1`, read live at 63abb94e. `data` is present only
# because of `--api hybrid`: at the default transport bzr detects rest from the server's
# 5.2+ version and the REST arm sets exclude_fields=data, so the same command returns this
# entry with every other key and no `data` at all (finding D9). The checksum cases below
# would all silently become the unverifiable case on a payload transcribed from that
# reply, which is what blocked this task until the transport decision was taken.
ATTACHMENT_DATA = (
    "Q2hlY2tvdXQgZG91YmxlLWNoYXJnZSB0cmlhZ2Ugbm90ZXMKPT09PT09PT09PT09PT09PT09"
    "PT09PT09PT09PT09PT09PT0KClJlcHJvZHVjZWQgb24gdGhlIHYxIGNoZWNrb3V0IHBhdGgg"
    "d2l0aCB0d28gYnJvd3NlciB0YWJzIHN1Ym1pdHRpbmcgdGhlIHNhbWUKY2FydCB3aXRoaW4g"
    "cm91Z2hseSAyMDBtcyBvZiBlYWNoIG90aGVyLiBCb3RoIHN1Ym1pc3Npb25zIHdlcmUgYWNj"
    "ZXB0ZWQgYW5kIHRoZQpjdXN0b21lciB3YXMgY2hhcmdlZCB0d2ljZS4KCldoYXQgd2Uga25v"
    "dzoKCi0gVGhlIGNhcnQgbG9jayBpcyB0YWtlbiBhZnRlciB0aGUgcGF5bWVudCBhdXRob3Jp"
    "c2F0aW9uLCBub3QgYmVmb3JlIGl0LgotIFRoZSBzZWNvbmQgc3VibWlzc2lvbiBzZWVzIGEg"
    "Y2FydCB0aGF0IHN0aWxsIHJlYWRzIGFzIHVucGFpZC4KLSBSZWZ1bmRzIGhhdmUgdG8gYmUg"
    "aXNzdWVkIGJ5IGhhbmQgdG9kYXk7IHRoZXJlIGlzIG5vIGF1dG9tYXRpYyByZXZlcnNhbC4K"
    "Ck5leHQgc3RlcHM6IGNvbmZpcm0gd2hldGhlciB0aGUgYmlsbGluZyBzaWRlIG9ic2VydmVz"
    "IG9uZSBpbnZvaWNlIG9yIHR3bywgYW5kCndoZXRoZXIgdGhlIGR1bm5pbmcgcmV0cnkgcGF0"
    "aCBjYW4gY29tcG91bmQgdGhlIHByb2JsZW0uCg==")

SUMMARY_1 = ("Triage notes [bzr-live:smoke:attach-triage-notes] "
             "sha256=96a330b23f0ebeb73d94721fce926b0b49daacfc448696af2f373afb30b49681")

ATTACHMENTS_1 = [
    {"id": 1, "bug_id": 1, "file_name": "triage-notes.txt", "summary": SUMMARY_1,
     "content_type": "text/plain", "creator": "triager@example.test",
     "creation_time": "2026-09-02T14:19:57Z",
     "last_change_time": "2026-09-02T14:20:00Z", "size": 622, "is_obsolete": True,
     "is_private": False, "is_patch": False, "flags": [], "data": ATTACHMENT_DATA},
]

TRIAGE_MARKER = "bzr-live:smoke:comment-triage-double-charge"
PRIVATE_MARKER = "bzr-live:smoke:comment-private-token-leak"


class CommentCheckTest(unittest.TestCase):
    """check_comments against the transcribed thread for bug 1."""

    @classmethod
    def setUpClass(cls) -> None:
        expected = fold(load_scenario(str(SMOKE)))
        cls.bug = expected.bugs["cart-double-charge"]
        cls.leak = expected.bugs["pay-token-leak"]
        cls.emails = expected.actor_emails

    def _check(self, comments=None, *, bug=None):
        return check_comments(self.bug if bug is None else bug,
                              COMMENTS_1 if comments is None else comments, self.emails)

    def _only(self, comments=None, *, bug=None):
        findings = self._check(comments, bug=bug)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].kind, "divergence")
        return findings[0]

    def _replace_text(self, marker: str, text: str) -> list:
        return [dict(entry, text=text) if f"[{marker}]" in entry["text"] else entry
                for entry in COMMENTS_1]

    def test_the_live_thread_matches_the_declared_one(self) -> None:
        # Bugzilla's duplicate notice at count 1 and attachment notice at count 3 sit
        # between the declared comments; the marker lookup steps over both, and the
        # ordering guard reads the non-contiguous counts 2 and 4 as ordered.
        self.assertEqual(self._check(), [])

    def test_a_differing_comment_zero_text_diverges(self) -> None:
        comments = [dict(COMMENTS_1[0], text="Something else entirely."), *COMMENTS_1[1:]]
        finding = self._only(comments)
        self.assertEqual(finding.subject, "cart-double-charge")
        self.assertEqual(finding.check, "comments")
        self.assertIn("comment 0 text:", finding.detail)
        self.assertIn("Something else entirely.", finding.detail)

    def test_a_comment_zero_written_by_another_actor_diverges(self) -> None:
        comments = [dict(COMMENTS_1[0], creator="triager@example.test"),
                    *COMMENTS_1[1:]]
        finding = self._only(comments)
        self.assertEqual(
            finding.detail,
            "comment 0 creator: declared reporter@example.test, observed "
            "triager@example.test")

    def test_a_thread_with_no_comment_zero_diverges(self) -> None:
        finding = self._only(COMMENTS_1[1:])
        self.assertEqual(finding.detail,
                         "declared a create description, observed no comment 0")

    def test_a_marker_observed_no_times_names_it(self) -> None:
        finding = self._only(
            [entry for entry in COMMENTS_1 if f"[{TRIAGE_MARKER}]" not in entry["text"]])
        self.assertEqual(finding.detail,
                         f"[{TRIAGE_MARKER}] declared once, observed 0 times")

    def test_a_marker_observed_twice_names_it(self) -> None:
        duplicated = [*COMMENTS_1, dict(COMMENTS_1[2], id=99, count=5)]
        finding = self._only(duplicated)
        self.assertEqual(finding.detail,
                         f"[{TRIAGE_MARKER}] declared once, observed 2 times")

    def test_a_comment_attributed_to_another_actor_diverges(self) -> None:
        comments = [dict(entry, creator="releaser@example.test")
                    if f"[{TRIAGE_MARKER}]" in entry["text"] else entry
                    for entry in COMMENTS_1]
        finding = self._only(comments)
        self.assertEqual(
            finding.detail,
            f"[{TRIAGE_MARKER}] creator: declared triager@example.test, observed "
            "releaser@example.test")

    def test_a_public_comment_observed_private_diverges(self) -> None:
        comments = [dict(entry, is_private=True)
                    if f"[{TRIAGE_MARKER}]" in entry["text"] else entry
                    for entry in COMMENTS_1]
        finding = self._only(comments)
        self.assertEqual(
            finding.detail,
            f"[{TRIAGE_MARKER}] is_private: declared False, observed True")

    def test_a_private_comment_observed_public_diverges(self) -> None:
        # pay-token-leak is the scenario's one private comment. Reading it back with
        # is_private False means the fixture stored it as a public comment, which the
        # visibility check alone would never catch: it is present either way.
        comments = [*COMMENTS_7_OUTSIDER, dict(PRIVATE_ENTRY, is_private=False)]
        finding = self._only(comments, bug=self.leak)
        self.assertEqual(finding.subject, "pay-token-leak")
        self.assertEqual(
            finding.detail,
            f"[{PRIVATE_MARKER}] is_private: declared True, observed False")

    def test_comments_observed_out_of_declaration_order_diverge(self) -> None:
        # The triage comment is declared before the work-time comment; swapping their
        # counts leaves both present and correctly attributed, so only the ordering
        # guard can see it.
        swapped = []
        for entry in COMMENTS_1:
            if f"[{TRIAGE_MARKER}]" in entry["text"]:
                swapped.append(dict(entry, count=4))
            elif "[bzr-live:smoke:worktime-double-charge]" in entry["text"]:
                swapped.append(dict(entry, count=2))
            else:
                swapped.append(entry)
        finding = self._only(swapped)
        self.assertIn("is declared after", finding.detail)
        self.assertIn("bzr-live:smoke:worktime-double-charge", finding.detail)


class VisibilityCheckTest(unittest.TestCase):
    """check_visibility against a reply read as an actor outside the insider group."""

    @classmethod
    def setUpClass(cls) -> None:
        expected = fold(load_scenario(str(SMOKE)))
        cls.bug = expected.bugs["cart-double-charge"]
        cls.leak = expected.bugs["pay-token-leak"]

    def test_an_outsider_reply_withholding_the_private_comment_is_clean(self) -> None:
        self.assertEqual(check_visibility(self.leak, COMMENTS_7_OUTSIDER), [])

    def test_an_outsider_reply_carrying_the_private_comment_diverges(self) -> None:
        findings = check_visibility(self.leak,
                                    [*COMMENTS_7_OUTSIDER, PRIVATE_ENTRY])
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].kind, "divergence")
        self.assertEqual(findings[0].subject, "pay-token-leak")
        self.assertEqual(findings[0].check, "visibility")
        self.assertIn(f"[{PRIVATE_MARKER}]", findings[0].detail)
        self.assertIn("declared private", findings[0].detail)

    def test_an_outsider_reply_carrying_every_public_marker_is_clean(self) -> None:
        self.assertEqual(check_visibility(self.bug, COMMENTS_1), [])

    def test_an_outsider_reply_missing_a_public_comment_diverges(self) -> None:
        # The other direction, and the reason this check is not just "no private marker
        # appears": a fixture that withheld the whole thread would otherwise pass.
        withheld = [entry for entry in COMMENTS_1
                    if f"[{TRIAGE_MARKER}]" not in entry["text"]]
        findings = check_visibility(self.bug, withheld)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn(f"[{TRIAGE_MARKER}]", findings[0].detail)
        self.assertIn("declared public", findings[0].detail)


class AttachmentCheckTest(unittest.TestCase):
    """check_attachments against the transcribed reply for bug 1."""

    @classmethod
    def setUpClass(cls) -> None:
        expected = fold(load_scenario(str(SMOKE)))
        cls.bug = expected.bugs["cart-double-charge"]
        cls.emails = expected.actor_emails

    def _check(self, **overrides):
        entry = {**ATTACHMENTS_1[0], **overrides}
        return check_attachments(self.bug, [entry], self.emails)

    def _only(self, **overrides):
        findings = self._check(**overrides)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].subject, "cart-double-charge")
        self.assertEqual(findings[0].check, "attachments")
        return findings[0]

    def test_the_live_reply_matches_the_declared_attachment(self) -> None:
        # obsolete=True is the state after attachment.update, not the upload's own.
        self.assertEqual(self._check(), [])

    def test_a_differing_summary_diverges(self) -> None:
        # The marker still locates the entry, so a summary the attachment.update was
        # meant to leave behind is compared rather than silently unmatched.
        finding = self._only(summary=SUMMARY_1.replace("Triage notes", "Old notes"))
        self.assertIn("summary:", finding.detail)
        self.assertIn("Old notes", finding.detail)

    def test_a_differing_creator_diverges(self) -> None:
        finding = self._only(creator="reporter@example.test")
        self.assertEqual(
            finding.detail,
            "attachment 'triage-notes' creator: declared triager@example.test, "
            "observed reporter@example.test")

    def test_a_differing_content_type_diverges(self) -> None:
        finding = self._only(content_type="application/octet-stream")
        self.assertEqual(
            finding.detail,
            "attachment 'triage-notes' content_type: declared text/plain, "
            "observed application/octet-stream")

    def test_a_differing_is_private_diverges(self) -> None:
        finding = self._only(is_private=True)
        self.assertEqual(finding.detail,
                         "attachment 'triage-notes' is_private: declared False, "
                         "observed True")

    def test_a_differing_is_obsolete_diverges(self) -> None:
        finding = self._only(is_obsolete=False)
        self.assertEqual(finding.detail,
                         "attachment 'triage-notes' is_obsolete: declared True, "
                         "observed False")

    def test_a_marker_observed_no_times_names_it(self) -> None:
        findings = check_attachments(self.bug, [], self.emails)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(
            findings[0].detail,
            "[bzr-live:smoke:attach-triage-notes] declared once, observed 0 times")

    def test_corrupted_content_names_both_digests(self) -> None:
        corrupted = base64.b64encode(b"not the declared bytes").decode("ascii")
        finding = self._only(data=corrupted)
        expected = hashlib.sha256(b"not the declared bytes").hexdigest()
        self.assertEqual(
            finding.detail,
            f"attachment 'triage-notes' sha256: declared {self.bug.attachments[0].sha256}"
            f", observed {expected}")

    def test_data_that_is_not_base64_diverges_rather_than_raising(self) -> None:
        finding = self._only(data="not base64 at all!!")
        self.assertEqual(finding.detail,
                         "attachment 'triage-notes': the reply's data is not valid "
                         "base64")

    def test_a_reply_with_no_data_is_unverifiable_and_the_rest_still_runs(self) -> None:
        # The default-transport shape (finding D9). The checksum is the only claim it
        # costs: the metadata comparisons must still bite, or a fixture reverting to
        # `--api rest` would quietly stop asserting anything about an attachment.
        entry = {key: value for key, value in ATTACHMENTS_1[0].items() if key != "data"}
        findings = check_attachments(self.bug, [dict(entry, is_obsolete=False)],
                                     self.emails)
        self.assertEqual(len(findings), 2, findings)
        kinds = {finding.kind: finding for finding in findings}
        self.assertEqual(set(kinds), {"divergence", "unverifiable"})
        self.assertIn("is_obsolete:", kinds["divergence"].detail)
        self.assertIn("the reply carries no data", kinds["unverifiable"].detail)
        self.assertIn("finding D9", kinds["unverifiable"].detail)


if __name__ == "__main__":
    unittest.main()
