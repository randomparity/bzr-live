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

The two modelled behaviours are premises. If a future Bugzilla or `bzr` stops adding the
flag requestee to CC, or stops materialising the `blocks` inverse, `verify` fails loudly
and names the bug and field. That is the intended failure: the premise is recorded here
with the live reply that established it, so the failure is diagnosable rather than
mysterious.

Chain-linking has three defined non-answers — no ordering links (a divergence), more than
one links (ambiguous), and a search too large to settle (oversized) — and the last two are
reported `unverifiable` rather than guessed. Verified on the live fixture: for bug 9's
`status` and `resolution` the search returns the unique ordering
`developer → triager → developer`, which is the declared sequence.

`unverifiable` not failing the run means a gap can be ignored by an operator who does not
read the summary. The alternative is a permanently red gate, which gets suppressed
instead of read. Three claims are unverifiable on `scenarios/smoke/` today, and a fourth
(`groups`) whenever a scenario declares one; all four are already recorded as findings or
deferred issues.

## Considered & rejected

- **Assert each event's declared postcondition against final state, reusing the
  reconcilers in `src/bzr_live/replay/actions.py`.** verified: `scenarios/smoke/` declares
  `resolve-decline-copy` (`RESOLVED`), `reopen-decline-copy` (`CONFIRMED`), and
  `refix-decline-copy` (`RESOLVED`) on the same bug, so the first two postconditions are
  false against the final state `bzr bug view 9` returns.
- **Assert history ordering by the reply's record order.** verified: `bzr bug history 9`
  (bzr 0.8.2 `ae39fbd8`, 2026-09-02) returns two status records sharing
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
  for four gaps already recorded, and a gate that is always red is a gate nobody reads.
- **Fetch attachment bytes with `bzr attachment download`.** verified: `bzr attachment
  list 1` already carries the body base64-encoded in `data` against this fixture, so the
  download adds a subprocess, a temporary directory, and a cleanup path for bytes already
  in hand.
- **A second Bugzilla client, or stock REST, for the reads.** verified: every read the
  checks need exists on `bzr` — `bug view --fields`, `bug history`, `bug links`,
  `comment list`, `attachment list` — and issue #20 asks for the assertions to run through
  the actual selected binary. A second client would prove the server rather than `bzr`.
- **Do nothing; leave `make smoke`'s replay summary as the proof.** verified: the replay
  reports 47 events executed and asserts nothing about the resulting state, so a wrong
  assignee, a dropped edge, or a leaked private comment all report success today.
