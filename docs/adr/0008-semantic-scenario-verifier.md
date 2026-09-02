# ADR 0008: The scenario verifier asserts a folded expected state

## Status

Accepted

## Context

Issue #20 asks for a `verify` command that proves a replayed scenario semantically:
field values, actor attribution and ordering in history, relationship topology, comment
visibility, attachment checksums, and custom-field values — with no assertion depending on
a generated numeric ID or an exact timestamp.

Three things about the fixture make that harder than a field-by-field comparison.

A scenario is a sequence of events, not a state. `scenarios/smoke/` resolves
`pay-decline-copy`, reopens it, and resolves it again, so no single event's declared
postcondition is the server's final state.

The server writes records the scenario never declared. A `depends_on` edge materialises
`blocks` on the other bug; a flag requestee joins the CC list; a `dupe_of` drives a status
and resolution change; Bugzilla posts its own comments for a duplicate marking and an
attachment upload. PR #23 named three of these as postconditions that are not the final
state, and asked this work not to assert against them.

Some declared values cannot be read back at all. `bzr bug view` neither serializes
`groups`, `estimated_time`, or `remaining_time` nor accepts them in `--fields` — finding
D3, confirmed live: `warning: ignoring unknown field(s): estimated_time, remaining_time,
groups`.

## Decision

**Expected state is the fold of the scenario's events, computed with no I/O.** A pure
`verify/expected.py` folds events in declaration order into one record per bug alias. The
verifier compares that against live state; it never asserts a per-event postcondition
against final state.

**Server-generated additions are modelled where they are derivable from the scenario, and
tolerated where they are not.** Modelled: a flag requestee joins the bug's running CC set
at the point the flag is declared — so a later `cc` declaration that omits it removes it,
exactly as the replay engine's own delta does — and the link graph materialises the
`depends_on`/`blocks` inverse on both endpoints. Tolerated by asserting containment rather
than equality: history records the scenario did not declare, and comments Bugzilla posts
itself. Status and resolution are not asserted for a bug the scenario marks duplicate
without declaring a status.

**History ordering is proven by chain-linking `old_value` to `new_value` across the
whole record list, with `when` bounding the search.** Records are bucketed by `when` and
the buckets ordered by `when`; the reconstruction then searches for an ordering that
links head to tail across every bucket and ends at the field's current value, which
`bug view` supplies. The search is global rather than per bucket: on the live reply for
bug 9 the first bucket admits two orderings and only the next bucket rules one of them
out, so a reconstruction that commits bucket by bucket answers "ambiguous" where a
unique ordering exists. Candidate orderings are deduplicated by their
`(who, old_value, new_value)` sequence, so two records identical in all three are
interchangeable rather than two answers. The reconstructed actor sequence must contain
the declared one as a subsequence. No assertion compares a timestamp; `when` is a sort
key and a search bound only.

**A value `bzr` cannot read back is reported `unverifiable`, with the field, the alias,
and the finding citation — and does not fail the run.** The report counts divergences and
unverifiable claims separately; the exit status follows divergences alone.

**Which `bzr` a claim is measured against is part of the claim.** Every live observation
here is taken at `bzr 0.8.3-dev (63abb94e)`, the revision `README.md` proves `make smoke`
at and below which it tells operators to treat the fixture as untested. An earlier draft
measured against the `0.8.2 (ae39fbd8)` build on `PATH` and waived `groups` and
`estimated_hours` under finding D3 — but `a7f6ab70` (`bzr` PR #646, closing `bzr#641`,
the issue D3 was filed as) adds `Groups`, `EstimatedTime` and `RemainingTime` to
`BugField` and to the `Bug` serializer, and it is an ancestor of `63abb94e`. `groups` is
therefore **asserted**. `remaining_hours` stays unverifiable on PR #23's grounds — that
Bugzilla decrements it — which is a statement about Bugzilla and survives the `bzr` fix.

**Amended after the first live run of the verify stage: `estimated_hours` is unverifiable
too, and not on D3's grounds.** `a7f6ab70` is necessary and not sufficient. Bugzilla gates
the time-tracking fields on `timetrackinggroup`, which this image sets to `editbugs`, and
finding **D8** leaves every `bzr` REST read unauthenticated — so `bug view` withholds the
field from a caller who is in fact a member. Measured at `63abb94e` on a freshly replayed
fixture with the insider's own valid key: the default transport and `--api hybrid` both
return a `bug view` carrying no `estimated_time`, `--api xmlrpc` returns `8.0`, and
`GET /rest/bug/1?Bugzilla_api_key=...` returns `8`. The value on the server is the declared
one, so this is not a divergence; it is a claim this verifier's transport cannot read back.
Switching `bug view` to `--api xmlrpc` would read it, and is **not** taken here: the
operator authorized `--api hybrid` on the reads that need it, `xmlrpc` is a different mode,
and moving `bug view` to it would invalidate every reply shape this ADR verified under
REST. Reported, not decided.

Recording a limitation `bzr` has already removed is the same failure as routing around one
it still has: both put something in `docs/bzr-findings.md` that the code does not support,
and both drop a chartered assertion. So a waiver carries the revision it was observed at,
and a revision below the supported floor is not evidence for one.

**Where the fixture cannot answer a `bzr` read, the fixture is fixed — not the
assertion.** The comment-visibility criterion was unsatisfiable because `bzr` reads a
thread through XML-RPC `Bug.comments` first (`src/client/resources/comment.rs:51-56`
documents it as the only path returning the full thread) and this image answered
`xmlrpc.cgi` with "The XML-RPC Interface feature is not available in this Bugzilla".
`containers/bugzilla/Dockerfile` installed `libsoap-lite-perl` but not `XMLRPC::Lite`,
which Bugzilla's `Bugzilla/Install/Requirements.pm:303-310` requires separately since
SOAP::Lite 1.0. The decision is to add `libxmlrpc-lite-perl` to that apt list. Reporting
the criterion `unverifiable` instead was rejected: `bzr` is behaving correctly here, so
that would have recorded a `bzr` gap that does not exist and dropped a chartered check.

## Consequences

The fold is unit-testable with no fixture running, which is where most of the verifier's
logic lives and where its tests are cheapest.

History proves what Bugzilla records, and it records less than the scenario declares.
There is no `dupe_of` row — `bug history 5` returns only the `status` and `resolution`
changes the duplicate marking drove — so a duplicate is proven through `bug links` and the
`dupe_of` field, not through history. And a set field writes one row per event with its
added members comma-joined, not one row per member: `bug history 18` returns
`depends_on '' -> '8, 14'` for an event declaring two. The fold expects one change per
event per set field, and compares the added members as a set so the join order is never
asserted.

Containment assertions cannot catch a spurious extra history record or an extra comment.
That is the price of not modelling Bugzilla's own writes, and the field-value checks —
which are equality — still catch the state such a record would have produced.

The verifier now depends on the fixture image carrying XML-RPC. `make up` rebuilds the
image and recreates the container, and `mariadb-data` and `bugzilla-data` are top-level
volumes, so taking the change costs a rebuild and no fixture data.

An operator running against an image built before this change gets a **precondition
refusal naming `XMLRPC::Lite`**, not a divergence. The distinction is the point: a
divergence claims the replay wrote the wrong thing, and here the replay wrote exactly what
the scenario declared. The journal record for the `bug.comment` event is `advance`, so the
comment exists on the server; a marker absent from the *insider* read therefore says the
read path cannot see it, which is a fixture gap. Reporting that as a replay defect is the
silent-substitution failure inverted — it would file a `bzr` finding that does not exist.

**The package is necessary but not sufficient, and this ADR previously said otherwise.**
Task 6 established the correction against source and live reply. `get_comments_since`
(`src/client/resources/comment.rs:62`), `get_attachments` (`attachment.rs:151`) and
`get_attachment` (`attachment.rs:180`) do call `dispatch_xmlrpc_first` — but that helper
(`src/client/mod.rs:262-280`) branches on the **detected `api_mode`** and never consults
`xmlrpc.cgi`. `version_to_api_mode` (`src/client/version.rs:119-140`) maps `>= 5.1` to
`rest`, this fixture answers `5.2+`, and `ApiMode::Rest` calls the REST closure
unconditionally. So installing `XMLRPC::Lite` makes `xmlrpc.cgi` answer without causing
`bzr` to call it.

Observed: `attachment list 1` returns no `data` key at the default mode and the same entry
plus `data` under `--api hybrid`; `comment list`, `bug view`, `bug history` and `bug links`
are byte-identical across the two. Recorded as finding **D9**.

Two consequences follow. The attachment-checksum criterion and the private-comment
visibility criterion are both unreachable at the auto-detected transport — the checksum
would report `unverifiable` for a missing key, and the design would look correct while
asserting nothing. And the choice of transport is now an explicit decision rather than an
inherited default: `--api hybrid` is a supported `bzr` flag and the narrowest thing that
makes both criteria reachable, but taking it is the operator's call, because it reverses a
premise four design reviews read and it changes what every check family reads through.
Until that decision is recorded, this ADR states the constraint rather than resolving it.

The two modelled behaviours are premises, and they now fail differently from each other. If
a future Bugzilla stops materialising the `blocks` inverse, `verify` fails loudly and names
the bug and field — the intended failure, diagnosable because the premise is recorded here
with the live reply that established it. The flag-requestee-joins-CC premise is deliberately
**not** assertable: the charter excludes it, so `cc` is compared by containment and the
model is used only to compute later deltas. If Bugzilla stops adding the requestee, nothing
on `pay-retry-loop` fails; the first scenario declaring `cc` after a flag is what would
surface it, through the history check. That is the cost of honouring the exclusion, and it
is recorded here rather than left for a reader to discover.

Chain-linking has three defined non-answers — no ordering links (a divergence), more than
one links (ambiguous), and a search too large to settle (oversized) — and the last two are
reported `unverifiable` rather than guessed. Verified on the live fixture: for bug 9's
`status` and `resolution` the search returns the unique ordering
`developer → triager → developer`, which is the declared sequence.

`unverifiable` not failing the run means a gap can be ignored by an operator who does not
read the summary. The alternative is a permanently red gate, which gets suppressed
instead of read. Three claims are unverifiable on `scenarios/smoke/` today —
`estimated_hours`, `remaining_hours` and the work-time hours — and all three are recorded
as findings or deferred issues. That is down from four: retargeting the design at the
supported `bzr` revision turned `groups` into an assertion, and the first live run put
`estimated_hours` back on the other side for a different, evidenced reason.

## Considered & rejected

- **Assert each event's declared postcondition against final state, reusing the
  reconcilers in `src/bzr_live/replay/actions.py`.** verified: `scenarios/smoke/` declares
  `resolve-decline-copy` (`RESOLVED`), `reopen-decline-copy` (`CONFIRMED`), and
  `refix-decline-copy` (`RESOLVED`) on the same bug, so the first two postconditions are
  false against the final state `bzr bug view 9` returns.
- **Assert history ordering by the reply's record order.** verified: `bzr bug history 9`
  (re-verified at bzr 0.8.3-dev `63abb94e`; first read at `0.8.2 ae39fbd8`, below the
  supported floor) returns two status records sharing
  `2026-09-02T14:19:48Z` — `triager RESOLVED->CONFIRMED` then
  `developer CONFIRMED->RESOLVED` — in the opposite order to the one they happened in,
  because Bugzilla's `ORDER BY bug_when` leaves ties unordered. How many records share a
  `bug_when` is a property of how fast the replay ran, so the stable premise is that
  records sharing a `bug_when` come back in an order Bugzilla does not define, not that
  any particular number of them collide.
- **Assert the discovering relation for every node in a recursive link walk.** verified:
  `bzr`'s frontier is sorted by bug id (`src/commands/bug/links.rs:42`), so for a node
  reachable by two paths the credited relation depends on generated identifiers, which
  issue #20 forbids an assertion from depending on. Depth is asserted instead.
- **Model `remaining_hours` as the declared value minus logged work.** judgment: derivable,
  but PR #23 handed this issue the explicit instruction not to assert against that
  postcondition, and inventing an assertion the handoff excluded is scope this work does
  not own.
- **Fail the run on an unverifiable claim.** judgment: turns `make smoke` permanently red
  for gaps already recorded, and a gate that is always red is a gate nobody reads.
- **Keep waiving `groups` and `estimated_hours` under finding D3.** verified: D3's upstream
  issue `bzr#641` is closed by `a7f6ab70`, an ancestor of the `63abb94e` revision
  `README.md` proves `make smoke` at. Waiving them *under D3* would record a `bzr` gap that
  no longer exists. `estimated_hours` is waived under D8 and `timetrackinggroup` instead,
  which is a different claim with its own live evidence; `groups` stays asserted.
- **Assert `cc` by set equality.** verified: `pay-retry-loop` declares no `cc`, so its
  observed set is entirely the flag requestee Bugzilla added — one of the three
  postconditions PR #23 told this issue not to assert against. Containment honours the
  exclusion; the fold keeps modelling the addition, because that model is what makes a
  later `cc` declaration's delta match the replay engine's.
- **Fetch attachment bytes with `bzr attachment download`.** verified: `bzr attachment
  list 1` already carries the body base64-encoded in `data` against this fixture, so the
  download adds a subprocess, a temporary directory, and a cleanup path for bytes already
  in hand. That holds on the XML-RPC arm only, which is why it depends on the package
  above; the checksum falls back to `unverifiable` rather than to a download when a reply
  carries no `data`.
- **A second Bugzilla client, or stock REST, for the reads.** verified: every read the
  checks need exists on `bzr` — `bug view --fields`, `bug history`, `bug links`,
  `comment list`, `attachment list` — and issue #20 asks for the assertions to run through
  the actual selected binary. A second client would prove the server rather than `bzr`.
- **Do nothing; leave `make smoke`'s replay summary as the proof.** verified: the replay
  reports 47 events executed and asserts nothing about the resulting state, so a wrong
  assignee, a dropped edge, or a leaked private comment all report success today.
