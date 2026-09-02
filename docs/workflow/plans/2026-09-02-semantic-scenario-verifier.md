# Implementation plan — semantic scenario verifier

**Goal.** Add a `verify <scenario>` command that reads a scenario and its replay journal,
then asserts semantic invariants — field values, history attribution and ordering,
relationship topology, comment visibility, attachment checksums, custom fields — against
live server state through the selected `bzr` binary.

**Architecture.** A new `src/bzr_live/verify/` package with four modules: `expected.py`
folds the scenario into an expected end state with no I/O; `observed.py` reads live state
through the existing `BzrClient`; `checks.py` compares the two and yields findings;
`runner.py` enforces the journal preconditions, orchestrates, and reports. The `verify`
command joins `replay` and `resume` on the existing argv parser and reuses that module's
`ReplayContext`.

**Tech stack.** Python 3.11+, standard library only, run through `uv`. `unittest` for
tests. No new dependency.

Expected implementation size: 1700–2000 changed lines (L) — derived from the file map
below by counting what each task creates: roughly 950 lines across the four `verify/`
modules (the code blocks here are skeletons, and `_freeze`, the eight `_apply` handlers,
the seven comparison groups in `check_fields`, and the five remaining check functions are
described rather than written), roughly 700–900 lines of tests across the three test
modules and their transcribed payloads, plus the `verify-cc-order` fixture and the small
edits to `replay/__main__.py`, `tests/smoke_scenario.sh`, `README.md` and
`docs/bzr-findings.md`. This band disagrees with the issue's `effort:M` label and with the
scope charter's complexity of M; the band is the one to keep, because the file map yields
it directly — six sourced assertion domains each need a fold rule, a check, and both a
passing and a diverging test, and no domain can be dropped without dropping a completion
criterion. The work is still one PR: the tasks are sequential slices of one deliverable
with no cross-task sequencing outside this plan.

## Global Constraints

Transcribed from `docs/workflow/specs/2026-09-02-semantic-scenario-verifier-design.md`
and `AGENTS.md`.

- **Python 3.11+ via `uv`.** `src/bzr_live/` has no runtime dependencies; do not add one.
- **Prove `bzr`, never work around it.** Where `bzr` cannot express or read something,
  report it and cite the `docs/bzr-findings.md` entry. Never substitute a value the
  scenario did not declare.
- **No assertion may depend on a generated numeric ID or an exact timestamp.** Server
  identities come only from `CompletedRecord.resolved_ids`. `when` may be used as a sort
  key and never compared.
- **Disposable fixture.** Owner-only file modes and keeping secrets out of output is the
  whole security bar. No encryption, rotation, or crash-consistency work.
- **Insider group is `admin`**, from `containers/bugzilla/checksetup_answers.txt:31`.
- **The live tier needs an image built with `libxmlrpc-lite-perl`** (Task 0). Without it
  `bzr` cannot read a full comment thread. The verifier **refuses this as a precondition
  naming the missing `XMLRPC::Lite`** rather than reporting a divergence: the journal
  already proved the comment was written, so the read path is what cannot see it, and
  calling that a divergence would file a `bzr` defect that does not exist.
- **Every payload transcribed into a test fixture comes from the rebuilt image** (Task 0),
  because `comment list` and `attachment list` return different shapes on either side of
  that rebuild. See Task 0.
- **`LINKS_MAX_NODES` is 1000**, from `bzr` `src/types/bug/links.rs:13`.
- **Every live observation and the live tier use `bzr 0.8.3-dev (63abb94e)`**, the revision
  `README.md` proves `make smoke` at. Anything below it is untested by this repository's
  own statement, and the homebrew `bzr 0.8.2 (ae39fbd8)` on `PATH` is below it — an
  earlier draft of this design was written from that binary and got finding D3 wrong as a
  result. A release build already exists at
  `/Users/dave/src/bzr/target/release/bzr`; pass it as `BZR_LIVE_BZR`. Confirm with
  `bzr --version` before transcribing any payload — the script prints the revision as its
  first line for exactly this reason.
- **Finding D3 is fixed at that revision.** `a7f6ab70` (`bzr` PR #646, closing `bzr#641`)
  adds `Groups`, `EstimatedTime` and `RemainingTime` to `BugField`
  (`src/types/bug/fields.rs`) and to the `Bug` serializer, so `groups` and
  `estimated_hours` are **asserted**, not waived. `remaining_hours` stays unverifiable on
  PR #23's grounds alone. The replay side (`actions.py`, ADR 0006) is unchanged and out of
  scope.
- **Guardrails:** `make check` (bash -n, shellcheck, compileall, compose config) and
  `make test` (lifecycle shell tests plus
  `uv run --python 3.11 python -m unittest discover -s tests -v`). Both must be green
  before every commit. `make smoke` is the live tier and needs a running fixture plus
  `BZR_LIVE_BZR`.
- **Line width 100, functions ≤100 lines, cyclomatic complexity ≤8.**
- Branch: `feat/semantic-verifier-20`, base `main`.

### Existing names this plan borrows

Each confirmed in the target repository at `fe06a2c`, the branch's base. Issue #24 landed
between the first drafting of this plan and that base and added `tests/test_fault_injection.py`
only, changing nothing under `src/`, so every name below is unchanged:

- `bzr_live.scenario.load_scenario(path) -> ValidatedScenario`
  (`src/bzr_live/scenario/loader.py`)
- `ValidatedScenario(name, description, format_version, resources, resource_plan, events,
  assets, digest)` (`src/bzr_live/scenario/model.py:57`)
- `PlannedEvent(name, actor, action, action_class, payload, dependencies,
  reconciliation_marker, expected_postcondition, creates)`
  (`src/bzr_live/scenario/model.py:44`)
- `PlannedResource(kind, name, data, dependencies)` (`src/bzr_live/scenario/model.py:36`)
- `Reference(kind, name)` (`src/bzr_live/scenario/model.py:16`)
- `CompletedRecord(..., resolved_ids, next_safe_action)`
  (`src/bzr_live/scenario/journal.py:385`)
- `JournalStore(state_dir)` with `read(event, attempt=None)`, used as a context manager
  (`src/bzr_live/scenario/journal.py:696`)
- `ReplayContext(scenario, keys, *, bzr_path, base_url, workspace, run=..., opener=...)`
  with `resource(kind, name)`, `actor_email(ref)`, `actor_key(name)`, `client(ref)`,
  `resolve(ref)`, `adopt(mapping)` (`src/bzr_live/replay/context.py:28`)
- `ReplayError` (`src/bzr_live/replay/context.py:24`)
- `BzrClient.read(args, positionals=None, *, absent_codes=...)`
  (`src/bzr_live/provision/adapters.py:109`)
- `ProvisionError` (`src/bzr_live/provision/adapters.py:11`)
- `KeyStore(state_root)` with `actor_key(name) -> str | None`
  (`src/bzr_live/provision/keys.py`)
- `custom_field_name(slug) -> str` (`src/bzr_live/provision/executor.py:31`)
- `render_attachment_summary(description, marker, sha256) -> str`
  (`src/bzr_live/replay/actions.py:81`)
- `render_marker(text, marker) -> str` (`src/bzr_live/replay/actions.py:77`)

## File map

| File | Responsibility |
|---|---|
| `src/bzr_live/verify/__init__.py` (new) | `Finding`, `VerifyError`, package exports |
| `src/bzr_live/verify/expected.py` (new) | the pure fold: scenario → `ExpectedScenario` |
| `src/bzr_live/verify/observed.py` (new) | `ServerReader`: the five `bzr` read paths |
| `src/bzr_live/verify/checks.py` (new) | the six check families |
| `src/bzr_live/verify/runner.py` (new) | preconditions, orchestration, report, exit code |
| `src/bzr_live/replay/__main__.py` | add `verify` to `choices` and dispatch it |
| `tests/fixtures/verify-cc-order/` (new) | a scenario declaring `cc` after a requestee flag |
| `tests/test_verify_expected.py` (new) | the fold |
| `tests/test_verify_checks.py` (new) | every check against recorded payloads |
| `tests/test_verify_journal.py` (new) | preconditions and the report |
| `tests/smoke_scenario.sh` | run `verify` after the replay, in the same state root |
| `containers/bugzilla/Dockerfile` | add `libxmlrpc-lite-perl`, so `bzr` can read a full comment thread |
| `README.md` | re-measure the published smoke duration with the verify stage in place |

`containers/bugzilla/Dockerfile` is in the charter's surface by the operator's explicit
authorization of 2026-09-02, scoped to that one package and the `make up` rebuild it
needs. The wider audit of the apt list against `Bugzilla/Install/Requirements.pm` for
other silently-missing optional features was explicitly **not** authorized and is not
performed here.

`README.md` is outside the charter's listed surface and is included deliberately: it
publishes `47 events replayed in 78.14s` as a maintained measurement (the commit before
this branch is `docs: re-measure the smoke duration after the fixture changed`), and this
change adds a stage that measurement does not cover. Leaving a figure this change
falsifies would be a doc that fails when followed. The edit is one measured number, adds no contract and no
behaviour, and is reported as a surface note rather than treated as a silent expansion.

## Task 0 — enable XML-RPC in the fixture image

Edits `containers/bugzilla/Dockerfile`.

Add `libxmlrpc-lite-perl` to the apt list, in its alphabetical place after
`libxml-twig-perl`. One package, no other edit to the image, the entrypoint, or
`checksetup_answers.txt`.

Why it comes first: Task 6's visibility check and Task 7's live tier cannot pass without
it. `bzr` reads a comment thread through XML-RPC `Bug.comments` and falls back to REST
only on a transport error; a REST read of bug 7 returns the public comment alone, so the
declared private comment is invisible to the insider read. The image answered
`xmlrpc.cgi` with "The XML-RPC Interface feature is not available in this Bugzilla"
because it installed `libsoap-lite-perl` and no `XMLRPC::Lite`, which Bugzilla's
`Bugzilla/Install/Requirements.pm:303-310` requires separately since SOAP::Lite 1.0.

Proof is live, not unit: `make up` rebuilds the image and recreates the container, and the
`mariadb-data` and `bugzilla-data` volumes survive, so no reset and no data loss.
**Performed 2026-09-02** — the `bugzilla` container was recreated while the `db` container
and both volumes were left in place. Two checks, in order of what they establish:

1. `docker exec <bugzilla> perl -MXMLRPC::Lite -e 1` succeeds. Before the rebuild it
   failed with `Can't locate XMLRPC/Lite.pm in @INC`.
2. `xmlrpc.cgi` answers a `Bugzilla.version` call with `5.2+`. Before the rebuild the same
   endpoint returned the "The XML-RPC Interface feature is not available in this Bugzilla"
   HTML error page with a 200 status — which is why `bzr`'s probe could not tell.

That a full thread then reads back `is_private=true` for a named insider is Task 7.4's to
prove, and is not claimed here. **The first rebuild attempt failed** after 33 minutes on
`curl: (18) Transferred a partial file` fetching `Template-Toolkit-3.106.tar.gz` from
`cpan.metacpan.org` — a transient network failure in a pre-existing layer, downstream of
`Setting up libxmlrpc-lite-perl (0.717-5)`, which had already succeeded. Retrying was
enough. Run `make up` **bare**: piping it through `tail` hides the build's exit status,
and on zsh `${PIPESTATUS[0]}` is empty because the array is `pipestatus`.

Run `make check`; expect green. `make check` validates `compose config` and does not build
the image, so it neither proves nor is affected by this change — Task 7's live tier is
where it is proven.

**This task's `make up` gates every payload transcription in Tasks 4 and 6.** Two of the
five read paths change shape when `xmlrpc.cgi` starts answering, because they prefer
XML-RPC and fall back to REST only on a transport error: `get_comments_since`
(`bzr` `src/client/resources/comment.rs:62`) and `get_attachments`
(`attachment.rs:151`), with `get_attachment` (`attachment.rs:180`) the same for
`attachment download`. `bug view`, `bug history` and `bug links` are REST either way and
are unaffected. A payload transcribed before the rebuild is the REST shape, which the
rebuilt fixture no longer returns — so Step 6.1 in particular must run after this task,
not before.

The consequence with teeth is `attachment list`. Its REST arm requests
`exclude_fields=data` (`attachment.rs:163`); its XML-RPC arm names `data` in
`ATTACHMENT_LIST_FIELDS` (`src/xmlrpc/resources/attachment.rs:12-27`). Before the rebuild
the reply carries no `data` key at all, so every attachment checksum reports
`unverifiable` — an honest report, but a vacuous one, and the whole point of check 5 is
the checksum. A fixture transcribed from the REST reply would bake that vacuum into the
unit tests, where nothing would ever expose it. After the rebuild, confirm with
`bzr --json attachment list 1` that the entry carries `data` before transcribing it.

Commit: `fix(containers): install XMLRPC::Lite so bzr can read comment threads`.

## Task 1 — the expected-state fold

Creates `src/bzr_live/verify/__init__.py`, `src/bzr_live/verify/expected.py`.
Tests `tests/test_verify_expected.py`.

**Interfaces this task publishes.** Later tasks rely on exactly these:

```python
Finding(kind: str, subject: str, check: str, detail: str)   # frozen dataclass
class VerifyError(Exception): ...
INSIDER_GROUP: str
CHAIN_FIELDS: frozenset[str]
ExpectedFlag(name: str, status: str, requestee: str | None)
ExpectedComment(marker: str | None, author: str, private: bool, text: str | None)
ExpectedAttachment(alias: str, bug: str, marker: str, author: str, summary: str,
                   content_type: str, private: bool, obsolete: bool, sha256: str)
ExpectedChange(actor: str, field: str, value: str | frozenset[str] | None, chain: bool)
ExpectedBug(alias, creator, description, scalars, names, edges, duplicate_of,
            custom_fields, flags, comments, attachments, history, unverifiable,
            unasserted)
ExpectedScenario(bugs, insider, outsider, custom_field_keys, actor_emails)
fold(scenario: ValidatedScenario) -> ExpectedScenario
link_edges(bugs) -> dict[str, frozenset[tuple[str, str, str]]]
reachable(edges, root: str) -> dict[str, int]
```

### Step 1.1 — write the failing test module

Create `tests/test_verify_expected.py`:

```python
from __future__ import annotations

import unittest
from pathlib import Path

from bzr_live.scenario import load_scenario
from bzr_live.verify.expected import INSIDER_GROUP, fold, link_edges, reachable

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "scenarios" / "smoke"


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

    def test_only_remaining_hours_and_worktime_are_unverifiable(self) -> None:
        # estimated_hours is asserted, not waived: bzr a7f6ab70 exposes estimated_time in
        # bug view, and README pins make smoke at 63abb94e, which contains it.
        bug = self.expected.bugs["cart-double-charge"]
        fields = {name for name, _ in bug.unverifiable}
        self.assertEqual(fields, {"remaining_hours", "worktime"})
        self.assertEqual(bug.scalars["estimated_time"], "8")

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
    only non-empty declared `groups` in the repository, for the same reason.
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
        # bug.update can never supply this case: actions._UPDATE_UNSUPPORTED refuses a
        # groups update, so bug.create is the only path groups can arrive by. It is
        # asserted rather than waived -- bzr a7f6ab70 exposes it in bug view.
        bug = self.expected.bugs["ordered"]
        self.assertEqual(bug.names["groups"], frozenset({"restricted"}))
        self.assertNotIn("groups", {name for name, _ in bug.unverifiable})


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
```

Run `uv run --python 3.11 python -m unittest tests.test_verify_expected -v`. Expect
`ModuleNotFoundError: No module named 'bzr_live.verify'` — the failure that proves the
tests reach the code under test.

### Step 1.1a — the cc-ordering fixture

Create `tests/fixtures/verify-cc-order/` in the shape of the existing fixture scenarios
(`scenario.json`, `resources.json`, `events.jsonl`; copy the structure from
`tests/fixtures/minimal-scenario/`). It declares three actors (`developer`, `triager`, `releaser`), one `flag-type`, one
`group` resource (`restricted`) and enough
product/component/version resources for one `bug.create` aliased `ordered`, then three
events in this order:

1. `bug.create` for `ordered`, declaring `cc: [developer]` and `groups: [restricted]`;
2. `bug.flag` on `ordered` with `status: "?"` and `requestee: triager`;
3. `bug.update` on `ordered` declaring `cc: [triager, releaser]`.

The `groups` declaration is here rather than in a fixture of its own because
`scenarios/smoke/` declares none and `bug.update` cannot supply the case at all
(`actions._UPDATE_UNSUPPORTED` refuses a `groups` update), so `bug.create` is the only
path that rule fires on and this is the only `bug.create` outside the smoke scenario the
plan writes. It folds to an `unverifiable` row and touches nothing else, so it does not
interact with the two CC rules below.

This fixture pins two rules `scenarios/smoke/` cannot. **Ordering:** after event 2 the
running CC set is `{developer, triager}`; event 3 replaces it with `{triager, releaser}`,
which is what the replay's `--cc-remove=developer@example.test` would leave. A
union-at-the-end model would wrongly keep `developer`, and a model that never added the
requestee would wrongly drop `triager` from the history expectation. **Multi-member
addition:** event 3 adds exactly one member (`releaser`) on top of the running set, so
extend it — or add a fourth event declaring `cc: [triager, releaser, developer]` — until
one update adds two members at once, and assert the fold emits a **single**
`ExpectedChange` whose value is the two-member `frozenset`. No smoke event adds two CCs
or two keywords in one update, so this is the only place the `_NAME_SETS` half of the
one-record-per-event rule is exercised at all.

Three events are the floor, not the count: the third adds only `releaser` on top of the
running set, so a two-member addition needs the extra events this step authorises. Five is
what that works out to — narrow to `[developer]`, then widen to `[triager, releaser]` —
and the earlier assertions are unchanged by them. Verify the fixture loads before writing
the fold:
`uv run --python 3.11 python -c "from bzr_live.scenario import load_scenario;
print(len(load_scenario('tests/fixtures/verify-cc-order').events))"` — expect the number of
events you wrote.

### Step 1.2 — the package root

Create `src/bzr_live/verify/__init__.py`:

```python
"""Semantic verification of a replayed scenario against live server state."""

from dataclasses import dataclass


class VerifyError(Exception):
    """Actionable verification failure; str(exc) is the operator-facing message."""


@dataclass(frozen=True, slots=True)
class Finding:
    """One divergence or unverifiable claim, as the report prints it.

    `kind` is "divergence" (the fixture disagrees with the scenario) or "unverifiable"
    (bzr cannot read the declared value back). `subject` is a symbolic alias, never a
    server id, so a report is readable without the journal beside it.
    """

    kind: str
    subject: str
    check: str
    detail: str


__all__ = ["Finding", "VerifyError"]
```

### Step 1.3 — the fold

Create `src/bzr_live/verify/expected.py`:

```python
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..provision.executor import custom_field_name
from ..replay.actions import render_attachment_summary
from ..scenario import PlannedEvent, Reference, ValidatedScenario

# containers/bugzilla/checksetup_answers.txt:31 sets $answer{'insidergroup'} = 'admin',
# so only a member of that group reads or writes a private comment. `editbugs` is not a
# proxy for it: stock Bugzilla grants editbugs by userregexp '.*'
# (Bugzilla/Install.pm:134-138), so every account on this image holds one regardless of
# what the scenario declares -- the premise commit 3f3f035 withdrew.
INSIDER_GROUP = "admin"

# bzr history fields whose old_value/new_value chain head to tail, so an ordering can be
# reconstructed from the values instead of from the reply's order.
CHAIN_FIELDS = frozenset(
    {"status", "resolution", "assigned_to", "target_milestone", "summary"})

# Declared field -> the reason it cannot be asserted. Printed verbatim by the report.
# 'groups' and 'estimated_hours' are NOT here: bzr a7f6ab70 (PR #646, closing bzr#641)
# exposes both in bug view, and README pins make smoke at 63abb94e, which contains it.
# Only remaining_hours survives, and on Bugzilla's behaviour rather than on any bzr gap.
UNVERIFIABLE_FIELDS: Mapping[str, str] = {
    "remaining_hours":
        "Bugzilla decrements remaining_time by logged work, so the declared value is not "
        "the fixture's final state; PR #23 excludes asserting against it",
}
WORKTIME_UNVERIFIABLE = (
    "Bugzilla gates time-tracking fields on timetrackinggroup (issue #22), so logged "
    "hours are not reliably readable; the work-time comment is asserted instead")

# Declared update field -> the bzr bug view key and bzr history field name it lands in.
_SCALARS: Mapping[str, tuple[str, str]] = {
    "summary": ("summary", "summary"),
    "status": ("status", "status"),
    "resolution": ("resolution", "resolution"),
    "assignee": ("assigned_to", "assigned_to"),
    "milestone": ("target_milestone", "target_milestone"),
}
# Set-valued declared fields that compare by name or email.
_NAME_SETS = ("cc", "keywords")
# Set-valued declared fields whose members are bug aliases.
_EDGE_SETS = ("depends_on", "blocks")
# The inverse Bugzilla materializes on the other endpoint. dupe_of has no stock inverse:
# `bzr bug view 1` returns no `duplicates` field for the target of a duplicate.
_INVERSE = {"depends_on": "blocks", "blocks": "depends_on"}
_DIRECTION = {"depends_on": "out", "blocks": "in", "dupe_of": "out"}


@dataclass(frozen=True, slots=True)
class ExpectedFlag:
    name: str                       # flag-type alias
    status: str
    requestee: str | None           # actor alias


@dataclass(frozen=True, slots=True)
class ExpectedComment:
    marker: str | None              # None for comment 0, the create description
    author: str                     # actor alias
    private: bool
    text: str | None                # the exact body, for comment 0 only


@dataclass(frozen=True, slots=True)
class ExpectedAttachment:
    alias: str
    bug: str
    marker: str
    author: str                     # actor alias
    summary: str                    # as it should stand after every attachment.update
    content_type: str
    private: bool
    obsolete: bool
    sha256: str


@dataclass(frozen=True, slots=True)
class ExpectedChange:
    actor: str                      # actor alias
    field: str                      # bzr history field name
    # A frozenset for a _NAME_SETS field (the added members, compared as a set against the
    # observed value split on ", "), a string for a scalar, None for a generated id: skip
    # it. The representation follows the field, never the rule that produced the change.
    value: str | frozenset[str] | None
    chain: bool


@dataclass(frozen=True, slots=True)
class ExpectedBug:
    alias: str
    creator: str
    description: str
    scalars: Mapping[str, str]
    names: Mapping[str, frozenset[str]]
    edges: Mapping[str, frozenset[str]]
    duplicate_of: str | None
    custom_fields: Mapping[str, object]
    flags: tuple[ExpectedFlag, ...]
    comments: tuple[ExpectedComment, ...]
    attachments: tuple[ExpectedAttachment, ...]
    history: tuple[ExpectedChange, ...]
    unverifiable: tuple[tuple[str, str], ...]
    unasserted: frozenset[str]      # bzr bug view keys the server owns, not the scenario


@dataclass(frozen=True, slots=True)
class ExpectedScenario:
    bugs: Mapping[str, ExpectedBug]
    insider: str | None             # actor alias
    outsider: str | None            # actor alias
    custom_field_keys: tuple[str, ...]
    actor_emails: Mapping[str, str]


@dataclass
class _Bug:
    """Mutable fold state for one bug. Frozen into an ExpectedBug at the end."""

    alias: str
    creator: str
    description: str
    scalars: dict[str, str] = field(default_factory=dict)
    names: dict[str, set[str]] = field(default_factory=dict)
    edges: dict[str, set[str]] = field(default_factory=dict)
    duplicate_of: str | None = None
    custom_fields: dict[str, object] = field(default_factory=dict)
    flags: list[ExpectedFlag] = field(default_factory=list)
    comments: list[ExpectedComment] = field(default_factory=list)
    attachments: list[ExpectedAttachment] = field(default_factory=list)
    history: list[ExpectedChange] = field(default_factory=list)
    unverifiable: list[tuple[str, str]] = field(default_factory=list)
    unasserted: set[str] = field(default_factory=set)


def fold(scenario: ValidatedScenario) -> ExpectedScenario:
    """Fold a scenario's events, in declaration order, into its expected end state."""
    emails = {
        resource.name: resource.data["email"]
        for resource in scenario.resources if resource.kind == "actor"}
    bugs: dict[str, _Bug] = {}
    attachments: dict[str, ExpectedAttachment] = {}
    for event in scenario.events:
        _apply(event, bugs, attachments, emails)
    return ExpectedScenario(
        bugs={alias: _freeze(bug) for alias, bug in bugs.items()},
        insider=_reader(scenario, inside=True),
        outsider=_reader(scenario, inside=False),
        custom_field_keys=tuple(sorted(
            custom_field_name(resource.name) for resource in scenario.resources
            if resource.kind == "custom-field")),
        actor_emails=emails)
```

`_freeze` converts a `_Bug` into its immutable `ExpectedBug`, turning each `set` into a
`frozenset` and each `list` into a `tuple`; it adds nothing and drops nothing.

`_apply` dispatches on `event.action`. Every handler takes the same five arguments so the
table has one shape. Each reads `event.expected_postcondition["values"]`, which the loader
has already validated, so no key check is needed:

```python
_HANDLERS = {
    "bug.create": _create, "bug.update": _update, "bug.comment": _comment,
    "bug.attach": _attach, "bug.worktime": _worktime,
    "bug.custom-field-set": _custom, "bug.flag": _flag,
    "attachment.update": _attachment_update,
}


def _apply(event: PlannedEvent, bugs: dict[str, _Bug],
           attachments: dict[str, ExpectedAttachment],
           emails: Mapping[str, str]) -> None:
    _HANDLERS[event.action](
        event, event.expected_postcondition["values"], bugs, attachments, emails)
```

Every handler but `_create` and `_attachment_update` reaches its bug through

```python
def _bug_of(event: PlannedEvent, values: Mapping[str, object],
            bugs: dict[str, _Bug],
            attachments: Mapping[str, ExpectedAttachment]) -> _Bug:
    """The bug an event's postcondition targets, by alias.

    `expected_postcondition["target"]` is a bug reference for every action except
    bug.attach and attachment.update, where it is the attachment
    (`src/bzr_live/scenario/journal.py:330`). bug.attach declares its bug in `values`;
    attachment.update reaches it through the attachment registered by the attach event.
    """
    target = event.expected_postcondition["target"]
    if target.kind == "bug":
        return bugs[target.name]
    if event.action == "bug.attach":
        return bugs[values["bug"].name]
    return bugs[attachments[target.name].bug]
```

The per-action rules, each with its ground:

Every declared value in a postcondition arrives as a `Reference`. It projects to
`emails[ref.name]` for an actor-kinded field — `assignee`, each `cc` member, a flag
`requestee` — and to `ref.name` otherwise — `product`, `component`, `version`,
`milestone`, each `keywords` member, a custom-field name. The projection happens **before**
any set arithmetic, so both sides of a delta are already strings. `ExpectedFlag.requestee`
is the one exception, and its type signature already shows it: it holds the actor
**alias**, and `check_fields` projects it through `emails` when it compares against the
observed flag.

- **`bug.create`** seeds `scalars` with `summary`, `product`, `component`, `version`, and
  `target_milestone` and `assigned_to` when declared; seeds `names["cc"]` and
  `names["keywords"]`; seeds `edges` from declared `depends_on` / `blocks` **and their
  inverses on the other bug**; appends comment 0 with the declared description and
  `private=False`; records the create actor as `creator`. A non-empty declared `groups`
  seeds `names["groups"]`, which `check_fields` compares as a set against the `groups`
  key `bug view` returns at `a7f6ab70` or later; a declared `estimated_hours` seeds
  `scalars["estimated_time"]`, and is the one of the two that `scenarios/smoke/`
  exercises.
  Create is the **only** path `groups` can arrive by:
  `_UPDATE_UNSUPPORTED` refuses a `groups`
  update outright (`src/bzr_live/replay/actions.py:32-36`), so such an event never reaches a
  completed journal record and never reaches verify at all, while `_CREATE_UNSUPPORTED`
  (`actions.py:23-31`) does not refuse it and `BugCreateHandler.build` writes the declared
  group names into the create document (`actions.py:312-314`).
  It appends **no** history: Bugzilla writes no `bugs_activity` row for a creation.
- **`bug.update`** for each declared key: a `_SCALARS` key sets `scalars[view_key]` and
  appends `ExpectedChange(actor, history_field, value, chain=True)`; `assignee` projects
  through `emails`. A `_NAME_SETS` key computes `added = declared - current` over the
  **projected** strings, replaces the running set with `declared`, and appends exactly
  **one** `ExpectedChange(actor, key, frozenset(added), chain=False)` when `added` is
  non-empty. One per event, not one per member: Bugzilla writes one `bugs_activity` row
  per field per change with the members comma-joined, and `bug history 18` returns a
  single `depends_on '' -> '8, 14'` for an event declaring two. `'' -> 'perf'` is the
  single-addition case of that same rule. The value is a `frozenset` so `check_history`
  can split the observed `new_value` on `", "` and compare sets, never asserting the join
  order — nothing establishes what order Bugzilla joins in. The running set is a model of the server, which is why it is the
  right basis to subtract from: `BugUpdateHandler.build` computes its own add/remove delta
  against the value it reads back from the server
  (`src/bzr_live/replay/actions.py:397-409`), not against the previous declaration, so the
  two agree only while the fold models every server-side addition. They are not the same
  computation; do not claim they are. An `_EDGE_SETS` key does the same, appending one
  `ExpectedChange(actor, key, None, chain=False)` per event with a non-empty `added` —
  value dropped, because the history value is a comma-joined list of generated bug ids —
  and maintains the inverse set on the other bug. Only additions reach the history
  expectation at all: attribution is containment, and a removal's `new_value` is the
  residue rather than the removed member. `duplicate_of` sets
  `duplicate_of` and adds `status` and `resolution` to `unasserted` unless the same event
  declares a status — Bugzilla drives both itself (finding G5). It appends **no**
  `ExpectedChange`: Bugzilla writes no `dupe_of` row, and `bug history 5` returns only
  `triager | resolution | '' -> 'DUPLICATE'` and
  `triager | status | 'CONFIRMED' -> 'RESOLVED'`. The duplicate edge is proven by
  `check_links` and by the `dupe_of` field in `check_fields`. `estimated_hours` sets
  `scalars["estimated_time"]` and appends **no** `ExpectedChange`: the field value is what
  the criterion asks for and is now readable, while nothing here has observed how
  `bug history` renders an `estimated_time` row, and inventing a rendering is how a
  correctly replayed fixture gets reported as divergent. Containment tolerates the record
  if Bugzilla writes one. `remaining_hours`
  appends to `unverifiable` from `UNVERIFIABLE_FIELDS` and touches nothing else. A declared
  `status` with no declared `resolution` discards `scalars["resolution"]`, because
  Bugzilla clears it on a transition to an open status.
- **`bug.comment`** appends
  `ExpectedComment(event.reconciliation_marker, actor, values["private"], None)`.
- **`bug.worktime`** appends the same kind of comment with `private=False`, and appends
  `(("worktime", WORKTIME_UNVERIFIABLE))` to `unverifiable` once per bug.
- **`bug.attach`** builds an `ExpectedAttachment` whose `summary` is
  `render_attachment_summary(values["description"], event.reconciliation_marker,
  values["asset_sha256"])`, registers it under `event.creates.name` in `attachments`, and
  appends it to the bug.
- **`attachment.update`** replaces the registered attachment's `obsolete` — and its
  `summary` when the event declares a `description` — then rewrites the bug's list entry.
  It appends `ExpectedChange(actor, "attachments.isobsolete", "1" if obsolete else "0",
  chain=False)` to the owning bug, matching the observed record `'0' -> '1'`.
- **`bug.custom-field-set`** sets `custom_fields[custom_field_name(field)]` to the value —
  a `frozenset` for a multi-select — and appends
  `ExpectedChange(actor, cf_name, rendered, chain=False)` where `rendered` is the value
  for a scalar and `", ".join(sorted(...))` for a multi-select, matching the observed
  record `'' -> 'cart, payment'`.
- **`bug.flag`** appends an `ExpectedFlag`, replacing any entry with the same flag-type
  name; a status of `X` removes the entry instead. It appends
  `ExpectedChange(actor, "flagtypes.name", spec, chain=False)` where `spec` is
  `f"{name}{status}"` plus `f"({requestee_email})"` when a requestee is declared,
  matching the observed record `'' -> 'review?(releaser@example.test)'`. When a requestee
  is declared it also adds that requestee's email to the **running** `names["cc"]` set at
  this point in the fold, because Bugzilla puts a requestee on the CC list (observed:
  `bug view 8` returns `cc: ['releaser@example.test']` where the scenario declared none).
  Ordered folding is what makes this correct in both directions: a later `bug.update`
  declaring `cc` replaces the set and drops the requestee again, which is what the replay
  does, since `BugUpdateHandler.build` would compute `--cc-remove=<requestee>` against the
  server value. A union taken at the end would assert a member the replay had removed.
  It appends **no** `cc` `ExpectedChange`, and `check_fields` compares `cc` by containment
  rather than equality (Task 3). PR #23 named the requestee landing on CC as one of three
  postconditions that are not the server's final state, and the charter excludes asserting
  against all three — so the running set exists **only** to make a later `cc` declaration's
  delta match the one `BugUpdateHandler.build` computed against live server state. Model,
  not assertion. Dropping the model instead would be the wrong cut: on any scenario that
  declares `cc` after a flag, the fold's `added` set would then disagree with the server's
  record and the *history* check would fail — which is precisely what
  `tests/fixtures/verify-cc-order/` pins.

  One representation per field, chosen by the field, remains the rule for the changes that
  do get emitted: a `_NAME_SETS` change carries a `frozenset` because `check_history`
  splits the observed value on `", "` and compares sets, which is why
  `ExpectedChange.value` is `str | frozenset[str] | None`.

`_reader` picks a role:

```python
def _reader(scenario: ValidatedScenario, *, inside: bool) -> str | None:
    for resource in scenario.resources:
        if resource.kind != "actor":
            continue
        groups = {ref.name for ref in resource.data.get("groups") or ()}
        if (INSIDER_GROUP in groups) is inside:
            return resource.name
    return None
```

`link_edges` and `reachable` derive the graph the topology check compares against:

```python
def link_edges(bugs: Mapping[str, ExpectedBug]) -> dict[str, frozenset[tuple[str, str, str]]]:
    """alias -> {(other alias, bzr relation, bzr direction)} for the declared graph."""
    out: dict[str, set[tuple[str, str, str]]] = {alias: set() for alias in bugs}
    for alias, bug in bugs.items():
        for relation in _EDGE_SETS:
            for other in bug.edges.get(relation, frozenset()):
                out[alias].add((other, relation, _DIRECTION[relation]))
        if bug.duplicate_of is not None:
            out[alias].add((bug.duplicate_of, "dupe_of", _DIRECTION["dupe_of"]))
    return {alias: frozenset(edges) for alias, edges in out.items()}


def reachable(edges: Mapping[str, frozenset[tuple[str, str, str]]],
              root: str) -> dict[str, int]:
    """Hop distance from `root` to every bug reachable through the declared edges."""
    hops: dict[str, int] = {}
    frontier = [root]
    seen = {root}
    depth = 0
    while frontier:
        depth += 1
        following: list[str] = []
        for alias in frontier:
            for other, _relation, _direction in sorted(edges.get(alias, frozenset())):
                if other in seen:
                    continue
                seen.add(other)
                hops[other] = depth
                following.append(other)
        frontier = following
    return hops
```

Run `uv run --python 3.11 python -m unittest tests.test_verify_expected -v`. Expect all
tests to pass. Then run `make check` and `make test`; expect both green.

**Acceptance.** The fold is pure — `expected.py` imports nothing that performs I/O — every
test above passes, `test_only_remaining_hours_and_worktime_are_unverifiable` proves
`estimated_time` reached `scalars` while `remaining_hours` did not, and
`test_flag_requestee_is_modelled_in_cc_but_never_asserted` proves the excluded
postcondition is modelled without being asserted.

Commit: `feat(verify): fold a scenario into its expected end state`.

## Task 2 — journal preconditions and the live readers

Creates `src/bzr_live/verify/observed.py` and the precondition half of
`src/bzr_live/verify/runner.py`. Tests `tests/test_verify_journal.py`.

**Interfaces this task consumes.** `fold`, `ExpectedScenario`, `ExpectedBug`,
`VerifyError`, `Finding` from Task 1.

**Interfaces this task publishes.**

```python
class ServerReader:
    def __init__(self, context: ReplayContext, actor: Reference) -> None: ...
    def bug(self, bug_id: int, fields: Sequence[str]) -> dict: ...
    def history(self, bug_id: int) -> list: ...
    def links(self, bug_id: int, depth: int | None = None) -> list: ...
    def comments(self, bug_id: int) -> list: ...
    def attachments(self, bug_id: int) -> list: ...

VIEW_FIELDS: tuple[str, ...]
LINKS_MAX_NODES: int
check_reader_keys(expected: ExpectedScenario, keys: KeyStore) -> None
resolve_ids(scenario: ValidatedScenario, store: JournalStore) -> dict[str, int]
check_link_bound(expected: ExpectedScenario) -> None
check_comment_transport(alias: str, bug: ExpectedBug, comments: list) -> None
```

`check_reader_keys` runs first, before `ServerReader` is constructed for either role.
`resolve_ids` returns the union of every `CompletedRecord.resolved_ids`, so its keys are
`"bug:<alias>"` and `"attachment:<alias>"`. The runner inverts the `bug:` half into the
`alias_of: dict[int, str]` the field and link checks take.

### Step 2.1 — the failing precondition tests

Create `tests/test_verify_journal.py` with a fake `JournalStore` (a dict of
`CompletedRecord`s built the way `tests/test_replay.py` builds them) and cases for:

- every event complete → `resolve_ids` returns one entry per created bug and attachment;
- one event with no record → `VerifyError` naming that event and telling the operator to
  replay under this state root;
- a record whose `scenario_digest` differs → `VerifyError` naming both digests;
- a record whose `next_safe_action` is `retry` → `VerifyError` naming the event and the
  recorded action;
- a scenario declaring no insider actor → the roles check yields an `unverifiable`
  finding rather than raising, and `run()` still returns 0;
- a reader actor the scenario **does** declare whose API key is absent from the key store
  → `VerifyError` naming that actor, printed by the CLI as a single
  `verify failed: <reason>` line;
- an expected graph whose reachable set from one root exceeds `LINKS_MAX_NODES` →
  `VerifyError` citing the constant;
- `check_comment_transport` on an insider reply carrying every declared marker → returns
  without raising;
- the same on a reply missing the declared **private** marker → `VerifyError` naming the
  bug alias, the marker, and `XMLRPC::Lite`, and telling the operator to add
  `libxmlrpc-lite-perl` and rerun `make up`. Assert the message text names the package:
  the whole value of this precondition is that the operator is not sent to debug the
  replay;
- the same on a bug whose fold declares no private comment → returns without raising even
  when a public marker is missing, because that case is check 4's divergence to report and
  not a transport gap;
- every precondition refusal reaching the CLI names the scenario path: drive `Verifier.run`
  with an absent journal record and assert the message contains the scenario directory,
  which issue #20's *Expected* section requires of every failure.

Run the module; expect `ImportError` on `bzr_live.verify.observed`.

### Step 2.2 — `observed.py`

```python
from __future__ import annotations

from collections.abc import Sequence

from ..provision.adapters import BUG_ABSENT_CODES
from ..replay.context import ReplayContext
from ..scenario import Reference
from . import VerifyError

# bzr's own ceiling on a recursive link walk (src/types/bug/links.rs:13). Above it the
# walk truncates and warns on stderr, which BzrClient.read discards on exit 0, so the
# bound is checked against the declared graph rather than trusted at read time.
LINKS_MAX_NODES = 1000

# The explicit field list every bug read requests. Without it `bzr bug view` omits every
# cf_* field; with it, `bug view 1 --fields id,summary,cf_risk` returns them. Requesting
# a field bzr does not know costs only a stderr warning, so the list stays declarative.
# groups and estimated_time need bzr a7f6ab70 or later (PR #646, closing bzr#641); below
# that they warn and are dropped, which is finding D3 and the reason README pins 63abb94e.
VIEW_FIELDS = (
    "id", "summary", "status", "resolution", "dupe_of", "product", "component",
    "version", "assigned_to", "keywords", "blocks", "depends_on", "cc",
    "target_milestone", "flags", "groups", "estimated_time",
)


class ServerReader:
    """The five bzr read paths a verification needs, as one actor."""

    def __init__(self, context: ReplayContext, actor: Reference) -> None:
        self._client = context.client(actor)
        self._actor = actor.name

    def _object(self, args, positionals, absent_codes) -> object:
        payload = self._client.read(args, positionals=positionals,
                                    absent_codes=absent_codes)
        if payload is None:
            raise VerifyError(
                f"{' '.join(args)} {' '.join(positionals)} as {self._actor} reported "
                "not-found; the journal names a bug the fixture does not hold")
        return payload

    def bug(self, bug_id: int, fields: Sequence[str]) -> dict:
        payload = self._object(
            ["bug", "view", f"--fields={','.join(fields)}"], [str(bug_id)],
            BUG_ABSENT_CODES)
        if not isinstance(payload, dict) or "id" not in payload:
            raise VerifyError(f"bzr bug view {bug_id} returned an unrecognised shape")
        return payload

    def _list(self, args: list[str], bug_id: int) -> list:
        payload = self._object(args, [str(bug_id)], frozenset())
        if not isinstance(payload, list):
            raise VerifyError(
                f"bzr {' '.join(args)} {bug_id} returned an unrecognised shape")
        return payload

    def history(self, bug_id: int) -> list:
        return self._list(["bug", "history"], bug_id)

    def links(self, bug_id: int, depth: int | None = None) -> list:
        args = ["bug", "links"]
        if depth is not None:
            args += ["--recursive", f"--depth={depth}"]
        return self._list(args, bug_id)

    def comments(self, bug_id: int) -> list:
        return self._list(["comment", "list"], bug_id)

    def attachments(self, bug_id: int) -> list:
        return self._list(["attachment", "list"], bug_id)
```

An empty `absent_codes` for the list reads is deliberate and matches
`_NO_ABSENT_CODES` in `src/bzr_live/replay/actions.py:109`: an unreadable bug must raise,
not answer "empty".

### Step 2.3 — the preconditions in `runner.py`

Both live in `runner.py`, which imports `CompletedRecord` from `..scenario`, and
`LINKS_MAX_NODES` from `.observed`, and `link_edges` / `reachable` from `.expected`.

```python
def check_reader_keys(expected: ExpectedScenario, keys: KeyStore) -> None:
    """Every reader role the scenario declares must have a key, before any read.

    Left to `ReplayContext.client`, this surfaces as a `ReplayError` naming provisioning,
    raised from inside the first read -- after the insider's reads have already gone out
    for the outsider's case. The spec promises a refusal before any read, so the check is
    explicit and its message names the verify precondition.
    """
    for role, alias in (("insider", expected.insider), ("outsider", expected.outsider)):
        if alias is not None and keys.actor_key(alias) is None:
            raise VerifyError(
                f"the {role} reader {alias!r} has no API key under this state root; "
                "provision the scenario here before verifying it")


def resolve_ids(scenario: ValidatedScenario, store: JournalStore) -> dict[str, int]:
    """Every server identity, taken only from completed journal records."""
    resolved: dict[str, int] = {}
    for event in scenario.events:
        record = store.read(event.name)
        if record is None or not isinstance(record, CompletedRecord):
            raise VerifyError(
                f"event {event.name!r} has no completed journal record under this state "
                "root; replay the scenario here before verifying it")
        if record.scenario_digest != scenario.digest:
            raise VerifyError(
                f"event {event.name!r} was journalled under scenario digest "
                f"{record.scenario_digest} but this scenario hashes to "
                f"{scenario.digest}; verify the scenario the journal was written for")
        if record.next_safe_action != "advance":
            raise VerifyError(
                f"event {event.name!r} is journalled with next safe action "
                f"{record.next_safe_action!r}, so the replay did not complete; resume "
                "it before verifying")
        resolved.update(record.resolved_ids)
    return resolved


def check_link_bound(expected) -> None:
    edges = link_edges(expected.bugs)
    for alias in expected.bugs:
        count = len(reachable(edges, alias))
        if count > LINKS_MAX_NODES:
            raise VerifyError(
                f"bug {alias!r} reaches {count} bugs, above bzr's LINKS_MAX_NODES of "
                f"{LINKS_MAX_NODES}; a recursive walk would truncate and the "
                "verification would be incomplete")


def check_comment_transport(alias: str, bug: ExpectedBug, comments: list) -> None:
    """A declared private comment the insider cannot see is a fixture gap, not a divergence.

    `resolve_ids` has already established that the bug.comment event holds a completed
    record, so the comment is on the server. An insider read that does not carry its
    marker therefore says the read path cannot reach it. bzr reads a thread over XML-RPC
    Bug.comments (src/client/resources/comment.rs:62) and falls back to REST, which
    returns the public comments alone -- so the cause is the image, and reporting it as a
    divergence would claim a bzr defect against a correctly replayed fixture.
    """
    text = "".join(str(entry.get("text", "")) for entry in comments)
    for comment in bug.comments:
        if comment.private and comment.marker and f"[{comment.marker}]" not in text:
            raise VerifyError(
                f"bug {alias!r}: the fixture cannot serve a full comment thread: the "
                f"private comment [{comment.marker}] is journalled as written but absent "
                "from the insider read. bzr reads a thread over XML-RPC Bug.comments and "
                "falls back to REST, which returns the public comments alone; this image "
                "has libsoap-lite-perl without XMLRPC::Lite "
                "(Bugzilla/Install/Requirements.pm:303-310 requires them separately). "
                "Add libxmlrpc-lite-perl to containers/bugzilla/Dockerfile and rerun "
                "make up")
```

It takes the reply rather than reading one, so it is a pure function the precondition
tests drive directly and Step 7.2 wires to the `comment list` read check 4 already makes.
It is the one precondition that follows a read: everything it discriminates on is in that
reply, and the discrimination is only sound because `resolve_ids` ran first.

**Every refusal names the scenario path.** Issue #20 requires failures to name the scenario
path, the symbolic alias and the observed divergence, and the divergence report line
already carries all three — but none of the `VerifyError` messages above does, and
`__main__.py` prints only `f"{options.command} failed: {exc}"`. An operator reading a CI
log would get `verify failed: event 'link-chain-cart' has no completed journal record
under this state root` with no indication of which scenario. Rather than thread the path
through four message strings, `Verifier.run` wraps the precondition block once:

```python
        try:
            check_reader_keys(self._expected, self._context.keys)
            resolved = resolve_ids(self._scenario, self._store)
            check_link_bound(self._expected)
        except VerifyError as exc:
            raise VerifyError(f"{self._scenario_dir}: {exc}") from exc
```

with `check_comment_transport`'s call site inside the same handler. One place to get right,
and `tests/test_verify_journal.py` asserts a refusal message contains the scenario path.

Run `uv run --python 3.11 python -m unittest tests.test_verify_journal -v`; expect all
tests to pass. Run `make check` and `make test`; expect green.

**Acceptance.** No read path constructs an id from anything but `resolved_ids`, and each
precondition failure names the scenario path, the event, and what the operator must do.

Commit: `feat(verify): add the journal preconditions and the bzr read paths`.

## Task 3 — field, custom-field and flag checks

Creates the first family in `src/bzr_live/verify/checks.py`. Tests the first part of
`tests/test_verify_checks.py`.

**Interfaces this task consumes.** `ExpectedBug`, `Finding` (Task 1); `VIEW_FIELDS`
(Task 2).

**Interfaces this task publishes.**

```python
def check_fields(bug: ExpectedBug, observed: dict, alias_of: Mapping[int, str],
                 emails: Mapping[str, str]) -> list[Finding]: ...
```

The caller reads the bug with `VIEW_FIELDS + expected.custom_field_keys`, so every
declared `cf_*` name is requested by name; `bzr` warns on stderr for a field it does not
know and returns the rest, which keeps the list declarative. It passes
`expected.actor_emails` as `emails`: rule 7 below compares a declared flag requestee by
email and `ExpectedFlag.requestee` holds the actor **alias**, which this plan already
states at the fold rules above — the earlier signature here omitted the map that
projection needs.

### Step 3.1 — the failing tests

Create `tests/test_verify_checks.py` with a payload transcribed from the live reply for
bug 1 quoted in the spec, and cases:

- a matching payload yields no findings;
- a differing `status` yields one `divergence` whose detail names the declared and the
  observed value;
- a `cc` set missing a declared member yields a divergence, while a `cc` set carrying an
  **extra** member the scenario never declared yields none — `cc` is containment, and
  `pay-retry-loop`'s server-added requestee is the case that must not fail;
- a declared `groups` set missing a member yields a divergence, and a declared
  `estimated_hours` differing from the observed `estimated_time` yields one;
- an `assigned_to` differing from the declared actor's email yields a divergence;
- `depends_on` containing an id whose alias is not the declared one yields a divergence
  naming aliases, never ids;
- a declared `cf_risk` absent from the reply yields a divergence naming the field;
- a multi-select `cf_subsystem` compares as a set, so `["payment", "cart"]` passes;
- a declared flag whose `requestee` differs yields a divergence;
- a bug with `status` in `unasserted` yields no finding for a differing status;
- each entry in `bug.unverifiable` yields one `unverifiable` finding carrying its reason.

Run the module; expect `ImportError` on `bzr_live.verify.checks`.

### Step 3.2 — `check_fields`

Compare in this order, appending at most one finding per key:

1. every `bug.unverifiable` entry → `Finding("unverifiable", alias, field, reason)`;
2. every `bug.scalars` key not in `bug.unasserted` → equality against `observed.get(key)`;
3. every `bug.names` key → set equality against `set(observed.get(key) or ())`, **except
   `cc`, which is containment**: `declared <= observed`. The charter excludes asserting
   against the flag-requestee-joins-CC postcondition PR #23 named, and `pay-retry-loop`'s
   whole CC set is server-derived, so equality there would assert exactly the excluded
   thing. Containment reports a declared member the replay failed to write and stays
   silent about a member the server added — the same trade check 2 and check 4 already
   make;
4. every `bug.edges` key → the observed ids mapped through `alias_of`, compared as a set
   of aliases; an id absent from `alias_of` renders as `bug id <n> (not named by this
   scenario)`;
5. `bug.duplicate_of` → `alias_of.get(observed.get("dupe_of"))`;
6. every `bug.custom_fields` key → scalar equality, or set equality for a `frozenset`; a
   key absent from the reply is a divergence, not an unverifiable claim, because the
   `--fields` request named it;
7. every `bug.flags` entry → an observed entry with the same `name` and `status` and, when
   a requestee is declared, the same `requestee` email; a declared status of `X` requires
   no observed entry with that name.

Every detail string reads `declared <value>, observed <value>`.

Run the tests; expect them to pass. `make check` and `make test`; expect green.

**Acceptance.** No finding detail contains a bare numeric bug id; every one names an
alias.

Commit: `feat(verify): assert declared field, custom-field and flag values`.

## Task 4 — history attribution and ordering

Adds `check_history` to `src/bzr_live/verify/checks.py`. Extends
`tests/test_verify_checks.py`.

**Interfaces this task publishes.**

```python
def check_history(bug: ExpectedBug, records: list,
                  observed_fields: Mapping[str, object],
                  emails: Mapping[str, str]) -> list[Finding]: ...
def chain_order(records: list[dict],
                final_value: str | None) -> tuple[list[dict] | None, str | None]: ...
```

`chain_order` returns `(ordered, reason)`. On success `reason` is `None`; on failure
`ordered` is `None` and `reason` is exactly one of `"unlinked"`, `"ambiguous"`, or
`"oversized"`. The caller must branch on the reason, because `unlinked` is a divergence
that fails the run while the other two are `unverifiable` claims that do not — the same
distinction `None` alone could not carry. `final_value` is the field's current value from
the `bug view` reply, which is what anchors the chain's tail; `observed_fields` is that
reply, from which `check_history` takes it.

### Step 4.1 — the failing tests

Transcribe the bug 9 `status` payload from a live reply at implementation time rather
than from this plan — how many records share a `bug_when` depends on how fast the replay
ran, so a payload frozen here would stop matching the fixture. At the time of writing,
`bzr --json --server-url http://127.0.0.1:8080/ bug history 9` returns two status records
at `2026-09-02T14:19:48Z` (`triager RESOLVED->CONFIRMED`, then
`developer CONFIRMED->RESOLVED`) and one at `:49Z` (`developer CONFIRMED->RESOLVED`).
Write **both** shapes as fixtures: the two-bucket one above, and the one-bucket variant
where all three share a timestamp. Both must recover the same order.

- attribution passes when every declared `(who, field, value)` appears;
- attribution fails, once, when a declared change is absent, and the detail names the
  actor and the field;
- attribution passes when the reply carries records the scenario did not declare — the
  `cc` record beside a flag, and the `resolution` record beside a `dupe_of`;
- a declared change appearing twice requires two records: one observed record yields a
  divergence;
- `chain_order` on the two-bucket bug 9 `status` payload, with `final_value="RESOLVED"`,
  returns `(ordering, None)` whose `who` sequence is exactly
  `developer, triager, developer` — **not** the reply's own order, which puts triager
  first. Assert the recovered order itself, not merely that it differs from the reply's;
- `chain_order` on the one-bucket variant returns the same `developer, triager, developer`
  ordering, proving the value-identical records are deduplicated rather than counted as
  two answers;
- `chain_order` returns `(None, "unlinked")` when no ordering links, and the check reports
  a divergence;
- `chain_order` returns `(None, "ambiguous")` on a payload admitting two genuinely
  different orderings — one record with a distinct `who` and the same values, and a
  `final_value` reachable either way — and the check reports an `unverifiable` finding for
  that field, not a guess;
- `chain_order` returns `(None, "oversized")` for a bucket of eight records — one above
  `_MAX_BUCKET` — and a second case pins the budget path;
- `chain_order` returns `(None, "oversized")`, never a solution, on a payload whose first
  linking ordering is found before the budget runs out: several buckets large enough that
  the global budget is exhausted while orderings remain unenumerated. This is the case that
  pins the budget check ahead of the solution-count branches; with the two swapped it
  returns the first solution as though the search had proved it unique;
- `chain_order([], "CONFIRMED")` returns `([], None)`, not `(None, "unlinked")`;
- `check_history` runs no chain check for a bug whose `summary` was seeded at create and
  never changed, so a bug with an empty history yields no finding at all — reproduce with
  the live `bug history 2` reply, which is empty;
- a folded `duplicate_of` produces no `ExpectedChange`, and `check_history` on the live
  `bug history 5` reply (`resolution '' -> 'DUPLICATE'`, `status 'CONFIRMED' ->
  'RESOLVED'`, and no `dupe_of` record) yields no finding for `mark-duplicate`;
- a two-member edge addition expects one record, not two: `check_history` on the live
  `bug history 18` reply (`developer | depends_on | '' -> '8, 14'`) against the fold of
  `link-diamond-sink` yields no finding;
- a two-member `cc` addition matches a single comma-joined record, compared as a set:
  `'' -> 'b@example.test, a@example.test'` satisfies an expectation of
  `frozenset({"a@example.test", "b@example.test"})`;
- ordering passes when the server injects an undeclared status change into the chain, as
  a duplicate marking does.

### Step 4.2 — `chain_order` and `check_history`

`checks.py` imports `base64`, `hashlib`, `binascii`, `Counter` from `collections`,
`permutations` from `itertools`, `Mapping` from `collections.abc`, and `Finding` plus
`CHAIN_FIELDS` and the `Expected*` types from the package.

```python
# Two independent bounds on a factorial enumeration. _MAX_BUCKET caps one bucket, so the
# largest single enumeration is 7! = 5,040. _SEARCH_BUDGET caps the whole search, across
# every bucket and every branch, so it is reached first on any input with more than one
# non-trivial bucket -- two buckets of 7 would cost up to 5,040 x 5,040. The bounds do not
# agree and are not meant to: the bucket cap rejects an input up front, the budget stops a
# search already under way, and an exhausted budget always answers "oversized".
_MAX_BUCKET = 7
_SEARCH_BUDGET = 10_000


def chain_order(records: list[dict],
                final_value: str | None) -> tuple[list[dict] | None, str | None]:
    """Order one field's history records by linking old_value to new_value.

    Records are bucketed by `when` and the buckets ordered by `when`, but the search is
    global: it permutes each bucket internally and requires the whole ordering to link
    head to tail and to end at `final_value`, the field's current value from `bug view`.
    Committing bucket by bucket does not work -- on the live reply for bug 9 the
    14:19:48Z bucket admits both orderings on its own, and only the :49Z record rules one
    out. `when` is a sort key and a search bound; it is never compared to anything,
    because Bugzilla's `ORDER BY bug_when` leaves ties unordered and the reply's own order
    therefore cannot be trusted.

    Candidate orderings are deduplicated by their (who, old_value, new_value) sequence, so
    two records identical in all three are interchangeable rather than two answers.

    Returns (ordering, None) on success, else (None, reason) where reason is "unlinked"
    (no ordering links -- a divergence), "ambiguous" (more than one does), or "oversized"
    (the search exceeded its bounds). The caller fails the run only on "unlinked".
    """
    if not records:
        # No records is not a broken chain. The caller already skips a field the fold
        # never declares a change to, and this is the second guard on the same case:
        # every bug's summary is seeded at create, which writes no history at all.
        return [], None
    buckets = [bucket for _when, bucket in sorted(_by_when(records).items())]
    if any(len(bucket) > _MAX_BUCKET for bucket in buckets):
        return None, "oversized"
    solutions: list[list[dict]] = []
    budget = _SEARCH_BUDGET

    def walk(index: int, carried: str | None, acc: list[dict]) -> None:
        nonlocal budget
        if len(solutions) > 1 or budget <= 0:
            return
        if index == len(buckets):
            if final_value is None or carried == final_value:
                solutions.append(acc)
            return
        seen: set[tuple] = set()
        for permutation in permutations(buckets[index]):
            budget -= 1
            if budget <= 0:
                return
            key = tuple(
                (r["who"], r["old_value"], r["new_value"]) for r in permutation)
            if key in seen:
                continue
            seen.add(key)
            if _links(permutation, carried):
                walk(index + 1, permutation[-1]["new_value"], acc + list(permutation))

    walk(0, None, [])
    # Order matters, and it is the budget that comes first. An abort does not distinguish a
    # dead branch from an unvisited one, so a search that recorded one solution and then ran
    # out of budget has not shown that solution is unique -- a second linking ordering may
    # sit in the part never reached. Returning it as unique would feed a wrong actor
    # sequence to the subsequence assertion and report an ordering proof that was never
    # obtained. ADR 0008 promises "a search too large to settle (oversized) ... reported
    # unverifiable rather than guessed", and this is the line that keeps that promise.
    if budget <= 0:
        return None, "oversized"
    if len(solutions) == 1:
        return solutions[0], None
    if len(solutions) > 1:
        return None, "ambiguous"
    return None, "unlinked"


def _by_when(records: list[dict]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {}
    for record in records:
        buckets.setdefault(record["when"], []).append(record)
    return buckets


def _links(permutation, carried: str | None) -> bool:
    previous = carried
    for record in permutation:
        if previous is not None and record["old_value"] != previous:
            return False
        previous = record["new_value"]
    return True
```

The head of the chain is deliberately unconstrained -- `carried` starts at `None` and
`_links` skips the check while it is -- because the fold does not know the value Bugzilla
gave the field at creation. The tail constraint is what settles the ordering instead:
`final_value` comes from the same `bug view` reply the field check already read, so it
costs no extra call.

`check_history` then:

1. builds the observed multiset of `(who, field, new_value)` for value-carrying fields and
   `(who, field)` for the rest, and reports one divergence per declared change short of
   its declared count;
2. for each field in `CHAIN_FIELDS` with **at least one** `ExpectedChange(chain=True)` in
   the fold **and** at least one observed record, calls
   `chain_order(records_for_field, observed_fields.get(view_key))`. Both halves of that
   guard matter: `bug.create` seeds `summary` (and `target_milestone`, `assigned_to`)
   into `scalars` while writing no history at all, so a guard keyed on "the bug declares
   the field" would run the chain check over an empty record list. The runner's own skip
   (Step 7.2) already keeps the 14 smoke bugs with no `ExpectedChange` out of this function
   entirely; this guard is the per-field case inside a bug that does declare a change, and
   `chain_order`'s empty-records return is the third. A `"unlinked"` reason yields a divergence; `"ambiguous"` and
   `"oversized"` each yield an `unverifiable` finding naming the field and the reason; and
   on success the reconstructed `who` sequence must contain the declared actor sequence
   for that field as a subsequence.

Run the tests; expect them to pass. `make check` and `make test`; expect green.

**Acceptance.** Both bug 9 fixtures recover `developer, triager, developer`. Prove the
test bites: replace the global search with the per-bucket variant that commits the unique
permutation of each bucket in turn, observe the two-bucket test go red with
`reason == "ambiguous"`, and revert. Asserting only that two implementations differ would
pass for the wrong reason, since both return a non-answer.

Commit: `feat(verify): assert history attribution and reconstructed ordering`.

## Task 5 — relationship topology

Adds `check_links` to `src/bzr_live/verify/checks.py`. Extends
`tests/test_verify_checks.py`.

**Interfaces this task publishes.**

```python
def check_links(alias: str, declared_direct: frozenset[tuple[str, str, str]],
                declared_hops: Mapping[str, int], direct: list, walk: list,
                alias_of: Mapping[int, str]) -> list[Finding]: ...
```

### Step 5.1 — the failing tests

Using the `bug links 1` and `bug links 1 --recursive --depth 3` payloads, transcribed from
live replies:

- the direct set matches `{("inv-tax-mismatch", "depends_on", "out")}` and yields no
  finding;
- a missing direct edge yields a divergence naming both aliases and the relation;
- an observed edge to a bug the scenario does not name yields a divergence rendering it as
  `bug id <n> (not named by this scenario)`;
- the depth-3 walk matches `{"inv-tax-mismatch": 1, "dun-retry-storm": 2,
  "inv-duplicate-line": 2}` and yields no finding;
- a node observed at the wrong depth yields a divergence naming declared and observed
  depth;
- the walk's `relation` is not compared: changing `"depends_on"` to `"blocks"` on a
  depth-2 record yields no finding.

### Step 5.2 — `check_links`

Direct edges compare as a set of `(alias, relation, direction)`. Reachability compares
`{alias: depth}` from the walk against `declared_hops`, ignoring `relation` and
`direction` above depth 1 — `bzr`'s frontier is sorted by bug id
(`src/commands/bug/links.rs:42`), so the credited relation for a node reachable two ways
depends on generated identifiers.

The caller passes `depth = max(declared_hops.values())` capped at 10, `bzr`'s documented
maximum, and skips the recursive read entirely when `declared_hops` is empty.

Run the tests; expect them to pass. `make check` and `make test`; expect green.

Commit: `feat(verify): assert declared relationship topology`.

## Task 6 — comments, visibility and attachments

Adds `check_comments`, `check_visibility` and `check_attachments` to
`src/bzr_live/verify/checks.py`. Extends `tests/test_verify_checks.py`.

**Interfaces this task publishes.**

```python
def check_comments(bug: ExpectedBug, comments: list,
                   emails: Mapping[str, str]) -> list[Finding]: ...
def check_visibility(bug: ExpectedBug, comments: list) -> list[Finding]: ...
def check_attachments(bug: ExpectedBug, attachments: list,
                      emails: Mapping[str, str]) -> list[Finding]: ...
```

### Step 6.1 — the failing tests

**Transcribe these payloads only from an image rebuilt by Task 0.** Both commands here are
the two of the five read paths that prefer XML-RPC (`comment.rs:62`, `attachment.rs:151`)
and fall back to REST when `xmlrpc.cgi` is absent, so a reply taken before the rebuild is
a shape the shipped fixture no longer returns. The `attachment list` difference is not
cosmetic: the REST arm sets `exclude_fields=data` (`attachment.rs:163`) and the XML-RPC arm
requests `data` (`src/xmlrpc/resources/attachment.rs:12-27`), so a pre-rebuild transcript
would carry no `data` key and every checksum case below would silently become the
`unverifiable` case. Confirm `data` is present in the transcribed entry before writing the
fixture. This is the ordering constraint Task 0 states; it binds here.

Transcribe the payloads from live replies at implementation time, as Task 4 does:
`bzr --json --server-url <base> comment list 1`, `... comment list 12`, and
`... attachment list 1`. Transcribe the keys `bzr` actually emits: an entry carries `id`,
`bug_id`, `text`, `creator`, `creation_time`, `count`, `is_private` and `attachment_id`,
with `creation_time` populated (`"2026-09-02T14:19:01Z"` for comment 0 of bug 1) and **no**
`time` key at all — the unauthenticated stock-REST reply carries both, `bzr`'s does not, so
a fixture written from the REST shape would not match the command under test. No comment
assertion may use either timestamp; `count` is the ordering key. Then:

- comment 0's creator and text match the declaration, and a differing text yields a
  divergence;
- each declared marker is found exactly once; zero occurrences and two occurrences each
  yield a divergence naming the marker;
- ordering passes although Bugzilla injected `*** Bug 5 has been marked as a duplicate
  ***` at count 1 and `Created attachment 1 ...` at count 3;
- a declared comment whose observed `is_private` is `False` where the scenario says
  `True` yields a divergence;
- `check_visibility` on an outsider reply that still carries a private marker yields a
  divergence; on one that carries every public marker and no private one, none;
- `check_visibility` on an outsider reply missing a **public** marker yields a divergence,
  so an over-restrictive fixture is caught too;
- an attachment whose `summary`, `creator`, `content_type`, `is_private` or `is_obsolete`
  differs each yields one divergence;
- `sha256(base64decode(data))` matching the declared checksum yields no finding, and a
  corrupted `data` yields a divergence naming both digests;
- a reply carrying no `data` key yields one `unverifiable` finding, and the other
  attachment assertions still run.

### Step 6.2 — the three functions

Located by marker token `f"[{marker}]"` in `text` (comments) and `summary`
(attachments), never by index or id. `check_comments` asserts comment 0's `creator` and
`text`, then for each declared marker: exactly one match, `creator` equal to the declaring
actor's email, `is_private` equal to the declaration, and the matched `count` values
non-decreasing in declaration order. `check_visibility` asserts each private marker absent
and each public marker present. `check_attachments` uses
`hashlib.sha256(base64.b64decode(entry["data"])).hexdigest()`, guarded by
`binascii.Error` → one divergence naming the attachment alias.

Run the tests; expect them to pass. `make check` and `make test`; expect green.

Commit: `feat(verify): assert comment visibility and attachment checksums`.

## Task 7 — the runner, the command, and the live tier

Completes `src/bzr_live/verify/runner.py`, edits `src/bzr_live/replay/__main__.py` and
`tests/smoke_scenario.sh`. Extends `tests/test_verify_journal.py`.

**Interfaces this task publishes.**

```python
class Verifier:
    def __init__(self, scenario: ValidatedScenario, context: ReplayContext,
                 store: JournalStore, scenario_dir: str, keys: KeyStore, *,
                 out: Callable[[str], None] = print) -> None: ...
    def run(self) -> int: ...
```

`keys` is passed in rather than reached through `ReplayContext`. The charter lists
`src/bzr_live/replay/context.py` as a dependency this issue **reads and does not modify**,
and `__main__.py` already builds the `KeyStore` at line 31 before it constructs the
context — so the accessor a `self._context.keys` read would need is surface this change
does not have to take, for a value the one caller already holds.

### Step 7.1 — the failing runner tests

Add to `tests/test_verify_journal.py`, driving `Verifier` with a fake `run` that returns
canned `bzr` replies the way `tests/test_replay.py` does:

- a fixture agreeing with the scenario prints
  `verify: <n> checks, 0 divergences, <k> unverifiable` and `run()` returns 0, where
  `<n>` is the number of `(bug, check family)` pairs executed. Assert the **exact**
  number for the fixture under test, never just the shape: a pattern match passes for an
  implementation that skipped nineteen bugs, which is the failure this summary exists to
  make visible. Derive that number with the three conditional reads applied — a bug whose
  fold carries no `ExpectedChange` contributes no history family — so the assertion pins
  the skips as well as the count;
- one divergence prints a line matching
  `verify: <scenario_dir>: <alias>: <check>: <detail>` and `run()` returns 1;
- an unverifiable claim alone still returns 0 and is counted in the summary;
- a scenario declaring no outsider reports the visibility check `unverifiable` and returns
  0;
- no `bzr` invocation carries an API key in its argv.

### Step 7.2 — `Verifier.run`

`run()` calls `check_reader_keys`, `resolve_ids`, `check_link_bound`, adopts the ids into
the context, builds a
`ServerReader` for the insider and one for the outsider when a private comment exists,
then for each bug in declaration order issues `bug`, `history`, `links` and `comments`
reads and collects findings from the six check families. It passes each insider
`comments` reply to `check_comment_transport` **before** handing it to `check_comments`,
so a fixture missing `XMLRPC::Lite` refuses with that precondition's message instead of
reporting a divergence per private comment.

**Four check families take `expected.actor_emails` as their last argument** —
`check_fields`, `check_history`, `check_comments` and `check_attachments`. Every one of
them compares a declaring actor's **alias**, which is what `ExpectedChange.actor`,
`ExpectedComment.author`, `ExpectedAttachment.author` and `ExpectedFlag.requestee` hold,
against a **login** that `bzr` returned. Nothing else in their arguments can bridge the
two. The signature blocks above originally omitted the parameter while the prose beside
them required the projection; Tasks 3 and 4 each hit that and added it.

Three reads are conditional, and every condition comes from the fold rather than from a
reply, so nothing is skipped on the strength of what the server happened to return:

- `attachments` runs only for a bug the fold gives at least one attachment — two of the
  smoke scenario's twenty;
- the recursive `links` read runs only when the root's eccentricity in the declared graph
  is 2 or more; at 1 or 0 the direct read already covers the whole neighbourhood. **When it
  is skipped, pass an empty `declared_hops` together with the empty `walk`** — a populated
  `declared_hops` beside an empty `walk` correctly reports every node absent, so the two
  arguments have to be skipped as a pair. Task 5 pinned this with a test;
- `history` runs, and `check_history` with it, only for a bug whose fold carries at least
  one `ExpectedChange` — **six** of the smoke scenario's twenty: `cart-double-charge`,
  `pay-decline-copy`, `pay-retry-loop`, `pay-token-leak`, `inv-tax-mismatch` and
  `dun-wrong-locale`. Derive that set from the fold rules above, not from the event list:
  only `bug.update`, `bug.flag`, `bug.custom-field-set` and `attachment.update` append an
  `ExpectedChange`, and two rules then subtract. `cart-dupe-report`'s sole such event is
  `mark-duplicate`, which appends none, so it is not a seventh; the materialised inverse
  edges append none either, so `inv-currency-drift` is not an eighth. For the other
  fourteen the attribution multiset is empty, so
  containment holds vacuously and the ordering guard's second clause never fires: the
  family executes, costs a spawn, and evaluates no expectation. Skipping it is what keeps
  `<n>` honest, since the summary's whole claim is that an executed family asserted
  something. This is the second-largest of the three skips, not the largest — the
  attachment skip is, at eighteen bugs.

The `links` read stays unconditional and is not a fourth candidate: its direct-edge check
reports an observed edge the scenario never declared, so it bites on a bug whose fold
declares no edges at all. Each read is a process spawn plus an HTTP round trip, so all
three skips are worth having on a twenty-bug scenario. No latency figure is published here: Task 7.4 measures the stage on
the run that adds it, and that measurement is the one `README.md` carries. It prints each
finding, then the summary, and returns 1 when any finding is a `divergence`.

### Step 7.3 — the command

In `src/bzr_live/replay/__main__.py`, change `choices=("replay", "resume")` to
`choices=("replay", "resume", "verify")`, extend the description, and inside the existing
`with JournalStore(journal) as store:` block:

```python
                if options.command == "verify":
                    return Verifier(
                        scenario, context, store, options.scenario_dir, keys).run()
                engine = ReplayEngine(scenario, context, store, journal)
                getattr(engine, options.command)()
```

with `VerifyError` added to the caught exception tuple and the failure prefix changed to
name the command:

```python
    except (ReplayError, ProvisionError, ScenarioValidationError, VerifyError) as exc:
        print(f"{options.command} failed: {exc}", file=sys.stderr)
        return 1
```

The existing `replay failed:` prefix becomes `replay failed:` / `resume failed:` /
`verify failed:`. One test asserts the prefix — `tests/test_replay.py:1245`,
`self.assertTrue(errors.getvalue().startswith("replay failed:"))` — and it invokes
`cli.main(["replay", ...])`, so `options.command` is `"replay"` and it still passes.
Confirm with `rg -n 'replay failed' tests/` before the edit and update any hit that does
not invoke `replay`.

### Step 7.4 — the live tier

In `tests/smoke_scenario.sh`, after the replay block and before the closing `OK`, inside
the same `$STATE` root the `EXIT` trap removes. No pipe and no `|| true`: a non-zero exit
must fail the script under `set -e`, which is the whole point of the stage.

```bash
echo "smoke scenario: verifying the replayed scenario"
uv run --python 3.11 python -m bzr_live.replay verify "$SCENARIO" \
  --state-root "$STATE/state" --bzr "$BZR" --base-url "$BASE_URL"
```

Update the script's header comment: it no longer "asserts nothing about semantic
invariants, which is issue #20's job".

Run `make check` (shellcheck covers this script), `make test`, then the live tier:

```
BZR_LIVE_BZR=$(command -v bzr) make smoke
```

Expect the replay summary, then
`verify: <n> checks, 0 divergences, <k> unverifiable` and `smoke scenario: OK`. Record the wall time of the verify stage from this run; it is the first measurement of
it, so there is no prior band to compare against and none is asserted here. A
divergence here is a real finding: record it rather than adjusting the assertion to match,
unless the assertion's premise is what is wrong.

Then update `README.md`'s published smoke measurement from that run: replace the
`47 events replayed in 78.14s` figure with the observed replay **and** verify durations,
naming both stages, so the number a reader is told to expect is the number the command
now produces.

**Acceptance.** `make check`, `make test` and `make smoke` are all green; `make smoke`
fails if a bug's summary is edited in the fixture database before the verify stage; and
`README.md`'s figure comes from this run, not from the previous one.

Commit: `feat(verify): add the verify command and its live smoke stage`.

## Task 8 — record the two `bzr` findings

Edits `docs/bzr-findings.md`. Adds two entries; neither is filed upstream, because the
operator declined that on 2026-09-02 for D7 and authorized recording only for both.

Add entry **D7** and its index row, in the shape of the existing entries: `bug history`'s
`comment_id` correlation attributes a comment to a change that did not carry one.
`flatten_history` (`~/src/bzr/src/commands/bug/history.rs:68-71`) documents that it "can
miss (→ null) but never produces a wrong id", correlating on exact `who` plus a canonical
timestamp key. Observed on `scenarios/smoke/` bug 12 with `bzr 0.8.2 (ae39fbd8)`: the
`cf_risk` and `cf_subsystem` records at `2026-09-02T14:20:03Z` by `triager@example.test`
both carry `comment_id: 31`, and comment 31 is the `worktime-inv-tax` comment posted by
the same actor in the same second through a different call; the custom-field write is a
stock-REST `PUT` that posts no comment. Class: **defect**. Upstream column: `hold: ask
operator` — `AGENTS.md` requires the operator's word before filing on
`randomparity/bzr`.

Note in the entry that the verifier never asserts `comment_id`, which is why this is a
recorded finding rather than a blocker.

Add entry **D8** and its index row: `bzr`'s auth probe concludes header auth works when it
does not. `bzr` logs "header auth works on API endpoints despite valid_login rejecting it;
preferring header" and prefers `X-BUGZILLA-API-KEY`, but this Bugzilla rejects that header
for REST — `rest/valid_login` returns `{"result":false}` with the header and
`{"result":true}` with `Bugzilla_api_key` as a query parameter. The probe cannot tell,
because the 200 it reads from `rest/bug` is also what an anonymous caller gets. Observed
consequence: `bzr`'s REST reads on this fixture run effectively unauthenticated; writes are
unaffected, because the 401 they draw triggers `bzr`'s alternate-auth retry, which is why
the replay works at all. Class: **defect** — the probe's success signal does not
discriminate. Upstream column: `hold: ask operator`.

Note in the entry what it costs the verifier: it refutes the premise that every positive
read observes the state the issuing actor would see, so the spec states that boundary
rather than assuming it. Nothing in `scenarios/smoke/` is group-restricted, and the one
check that does depend on insider identity — `comment list` — travels over XML-RPC, which
does authenticate, so no check today reads less than it should.

Run `make check`; expect green.

Commit: `docs: record the comment_id mis-correlation and the auth-probe defect`.

## Deferrals carried into implementation

None recorded by the design review at authoring time. Any deferral the review disposes of
is appended here with its owning record path or tracker issue before implementation
starts.
