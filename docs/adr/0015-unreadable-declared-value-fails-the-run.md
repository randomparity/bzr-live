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

Issue #59 sets the repository's direction on exactly this question. Its Notes say what this
record follows: *"This issue makes the fixture **fail loudly**, not compensate."* That is
`AGENTS.md`'s rule applied to the verifier, and it is the ground this decision stands on.

**What #59 does not do, stated because an earlier draft of this record relied on it and was
wrong.** #59 does not convert waivers generally: its Scope is two items — turn
`UNVERIFIABLE_FIELDS["estimated_hours"]` into a conditional refusal, and retarget
`check_comment_transport`'s diagnostic. `remaining_hours` and the work-time hours stay waived,
as this record's Decision also says. And the alternative weighed here was never a
`UNVERIFIABLE_FIELDS` entry at all — it would have been a `Finding("unverifiable", ...)`
emitted from `Verifier._links`, a different mechanism in a different module. **So #59 would
not have removed the waiver this record declines to add.** #59 is also `status:blocked` on
issue #58.

**What #59 shares with this is the signal, and that is why its approach is the precedent.**
#59's detectable class is *"fields Bugzilla withholds while still answering 200"*, and it
rules out the other by name: *"A `401` is not usable, because `bzr` repairs it."* D12's read
is in the first class, not the second — the search endpoint answers **200** with an empty
list, which is precisely a withheld 200. The 401 in this story belongs to the *direct*
endpoint `bug view` reads, where `bzr`'s retry does repair it and the read succeeds. So #59
and this record answer the same shape of problem the same way; they differ only in which
field, and in which module the answer lives.

One conflict follows and is recorded rather than smoothed: #59's acceptance criteria require
*"a live `make smoke`"* to pass. While this decision stands and `bzr#719` is open, `make smoke`
does not pass, so #59 cannot satisfy that criterion on `main` without either the upstream fix
or an amendment to its own criteria. Whoever picks up #59 meets this record first.

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

- **`make smoke` is red for every run of it, on both CI arms, until upstream moves.**
  `.github/workflows/container-lifecycle.yml` triggers on `pull_request` **and** on
  `push: branches: [main]`, over the same path list — `scenarios/**`, the containers, the
  compose file, `Makefile`, the lifecycle and checkpoint scripts, `tests/smoke_scenario.sh`,
  and `README.md`. So merging this branch turns the **default branch's own** `Container
  lifecycle` run red, and keeps it red for every later push touching those paths. A red check
  on a pull request and a red `main` are different signals: the second is what a maintainer, a
  status badge, and anyone bisecting reads as "the repository is broken". A genuine
  replay-engine regression in a later pull request produces the same red as this one, and a
  reader cannot tell them apart from the job's status alone. This is precisely what ADR 0008
  predicted, and it has been accepted rather than disputed.
- **CI's checkpoint round trip stops running.** In that job, `Exercise checkpoint round trip`
  (`make checkpoint-smoke`) is the step *after* `Exercise the live scenario smoke path`, and
  only `Clean project resources` carries `if: always()`. Once `make smoke` fails, the
  checkpoint step never executes, so this decision costs the save/restore coverage as well as
  the verify coverage. `make checkpoint-smoke` still runs locally.
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
- **The narrowing covers the root of a links read and not a neighbour of one.** `bzr`'s own
  ADR 0006 decides that *"related bugs that cannot be fetched are silently skipped"*, and D12's
  upstream argument turns on that root-versus-related line. The other side of it lands here: if
  a future scenario restricts a bug that carries a dependency or duplicate edge, an
  *unrestricted* neighbour's walk loses it silently and `check_links` reports a missing edge as
  a **divergence** — which in this repository means "the replay wrote the wrong thing", against
  a fixture that is correct. Not reachable today: `scenarios/smoke`'s restricted bug carries no
  edges, and nothing else names it. A scenario that restricts a bug inside the link graph needs
  this resolved first.
- **One residual misdiagnosis remains, narrowed but not closed.** `bzr` exits 2 both for
  not-found and for a clap usage error, and `BzrClient.read` maps that status to absent, so a
  malformed invocation against a restricted bug would still be reported as D12. The one route
  reachable from a declared scenario — a graph deeper than `bzr`'s `--depth` ceiling of 10 —
  is now refused by `check_link_bound` before any argument is built. What is left needs a
  change to `bzr`'s CLI contract, which the repository already refuses rather than absorbs
  elsewhere.

## Considered & rejected

- **Report the links read `unverifiable` and let the run pass, per ADR 0008 unchanged.**
  judgment, and the strongest alternative here: it is the disposition ADR 0008 prescribes, it
  would keep the gate discriminating for every other assertion, it would keep `main` green,
  and it would leave the checkpoint step running. Its cost is that `make smoke` would pass
  over a read `bzr` cannot perform, which is the shape `AGENTS.md` names — *"A fixture that
  quietly compensates reports success while proving nothing"* — and which issue #59 states as
  the repository's direction for the verifier. The operator weighed both and chose the refusal
  on 2026-09-06. This is a judgment call between two defensible dispositions, not a
  correctness result, and this record does not claim otherwise.
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
