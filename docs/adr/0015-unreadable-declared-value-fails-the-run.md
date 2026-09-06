# 0015. A declared value `bzr` cannot read back fails the run, where a filed defect is the cause

## Status

Accepted (2026-09-06)

## Context

[ADR 0008](0008-semantic-scenario-verifier.md) decided the verifier's disposition for a value
`bzr` cannot read: *"A value `bzr` cannot read back is reported `unverifiable`, with the field,
the alias, and the finding citation — and does not fail the run. The report counts divergences
and unverifiable claims separately; the exit status follows divergences alone."* Its
*Considered & rejected* list weighed the opposite and declined it: *"**Fail the run on an
unverifiable claim.** judgment: turns `make smoke` permanently red for gaps already recorded,
and a gate that is always red is a gate nobody reads."*

That reasoning was a **cost** judgment, not a correctness one, and it was sound. A waiver
prints the field, the alias and the finding citation; it loses no evidence a reader needs. What
it buys is a gate that keeps discriminating for every other assertion, and ADR 0008 valued that
above the sharper signal a refusal gives. Nothing since has shown the judgment wrong.

Two things have changed around it.

Issue #42 declared the first bug `groups` value in `scenarios/smoke`, and its first live run
produced finding [D12](../bzr-findings.md#d12), filed upstream as
[`bzr#719`](https://github.com/randomparity/bzr/issues/719). `bzr` reads a bug's links through
Bugzilla's *search* endpoint, which filters a bug the caller cannot see into an empty `200`
rather than faulting. No error status fires `bzr`'s alternate-auth retry, so the read stays
unauthenticated and `links.rs` reports the root absent — to a caller that `bug view` serves the
same bug to, in the same run. `Verifier._read_all` reads every bug's topology unconditionally,
so **any** scenario declaring a bug group meets this.

Issue #59 exists to convert `UNVERIFIABLE_FIELDS` waivers into refusals. A new waiver added
here would be work that issue is already scoped to undo, and would pull against it while it is
open. That is what makes the waiver the worse direction *now*, and it is a fact about the
backlog rather than a discovery about ADR 0008.

## Decision

**Where a declared value cannot be read because of a `bzr` defect this repository has recorded
and filed, the verifier refuses and the run fails, naming the finding and its upstream issue at
the point of failure.** This amends ADR 0008's disposition for that one class. ADR 0008 stays
Accepted and is not superseded: its rule still governs every other unreadable value, including
the `timetrackinggroup`-gated time fields, which continue to be waived through
`UNVERIFIABLE_FIELDS` and continue not to fail the run.

The class is drawn narrowly, by three conditions together — a **filed** upstream defect, a
value the scenario **declares**, and a read that reports **absence** rather than a shape it
does not recognise. `Verifier._links` implements exactly that: it rewrites only `ReadNotFound`,
only for a bug whose fold carries a declared `groups` set, and its message names D12 and
`bzr#719`.

**The refusal retires itself.** Nothing has to be removed when the defect is fixed: the rewrite
fires only on a read that failed, so a read that succeeds never reaches it. Two independent
upstream fixes each close it —
[`bzr#719`](https://github.com/randomparity/bzr/issues/719) reading the root through the direct
path, or [`bzr#713`](https://github.com/randomparity/bzr/issues/713)'s auth work making the
search request authenticate. D12's measurement table settles the second: the same request
already returns the bug under query-parameter auth, so an authenticated search needs no change
in this repository at all.

## Consequences

- **`make smoke` is red for every pull request that runs it, until upstream moves.** That is
  the whole cost and it is not narrower than it sounds: `Container lifecycle`'s `x86_64-linux`
  job runs `make smoke` on every pull request touching `scenarios/`, the containers, the
  compose file, the lifecycle or checkpoint scripts, `tests/smoke_scenario.sh`, or `README.md`.
  A genuine replay-engine regression introduced by a later pull request produces the same red
  as this one, and a reader cannot tell them apart from the job's status alone. This is
  precisely what ADR 0008 predicted, and it has been accepted rather than disputed.
- **The verify stage asserts nothing while this stands.** `Verifier.run` prints its findings
  only after every read returns, so the refusal unwinds `_read_all` and discards the findings
  already collected — including the restricted bug's own `groups` comparison. The restricted
  bug is the eleventh of twenty in fold order, so the nine after it are never read. Against
  `main`, where verify reported 69 checks over 20 bugs, this is a loss and not merely a pause.
- Nothing silently passes. The exit status is non-zero on either disposition, so no green run
  is produced that would not have been produced before.
- The refusal's message is the operator's whole diagnosis: it names the finding, the upstream
  issue, that the fixture and scenario are both correct, and that it retires on its own. A
  reader who sees it needs neither this record nor the findings file to act.
- `ReadNotFound` is added to `verify/__init__.py` and raised by `ServerReader._object`. Callers
  that catch `VerifyError` are unaffected; only a caller that must distinguish absence from an
  unrecognised reply shape needs the subclass.

## Considered & rejected

- **Report the links read `unverifiable` and let the run pass, per ADR 0008 unchanged.**
  judgment: it is the disposition ADR 0008 prescribes and it would keep the gate
  discriminating — the strongest alternative here. Declined because issue #59 is scoped to
  convert existing waivers into refusals, so this would add work already planned for removal.
  The operator weighed this explicitly on 2026-09-06 and chose the refusal.
- **Route the links read through `--api xmlrpc`.** verified: measured working — the XML-RPC arm
  fetches each node through the direct path, so the alternate-auth retry fires and the read
  succeeds. Rejected because it would leave `make smoke` green over a read `bzr` cannot perform
  on its default transport, which is the compensation `AGENTS.md` forbids: *"A fixture that
  quietly compensates reports success while proving nothing."* It differs from `observed.py`'s
  existing `--api hybrid` selection, which is `bzr`'s own documented setting for the private
  comment and attachment data it is used for; nothing documents `xmlrpc` as the transport for
  links, and it happens to work only as a side effect of that arm's request shape.
- **Narrow the CI trigger so `make smoke` stops gating the affected paths.** judgment: it
  converts a visible cost into an invisible one and removes the gate the repository relies on
  for every other assertion. Explicitly withheld by the operator.
- **Wait for `bzr#719` before declaring a bug group in any scenario.** judgment: the write path
  is what issue #42 exists to prove, it works, and it is proven live — 48 events replayed, the
  restriction applied through `bzr bug update --groups-add`, and the value read back by hand
  through `bug view` and `bug history`. Withholding the scenario would leave that unproven for
  as long as an upstream fix takes, to buy back a gate this record has already accounted for.
- **Amend ADR 0008 in place, or mark it superseded.** judgment: its rule still governs every
  unreadable value outside this class, so a supersession banner would overstate what changed.
  A narrow amending record leaves both readable.
