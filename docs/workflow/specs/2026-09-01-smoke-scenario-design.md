# Smoke scenario design (issue #19)

Decision record: [ADR 0007](../../adr/0007-committed-smoke-scenario.md).

## Goal

Commit `scenarios/smoke/` — roughly 20 bugs across two products — and prove it validates,
provisions, and replays end to end against the live pinned fixture through the
operator-selected `bzr` binary. The verifier that will assert semantic invariants against
this scenario is issue #20; response-loss injection and CI wiring are issue #21. This spec
covers the fixture and its two-tier proof only.

## Contract constraints that shape the fixture

Every one of these is a property of the existing contract, established by reading it and,
where noted, by running it. They are collected here because the fixture's shape is mostly
determined by them, and none is stated in the existing docs.

| # | Constraint | Source | Effect on the fixture |
|---|---|---|---|
| 1 | A created identity becomes resolvable only after its event validates, so no event may cite a bug a later event creates | `src/bzr_live/scenario/loader.py:810`; verified — a `bug.create` with `blocks` naming a later bug is refused with `events.jsonl:1:$.payload.blocks[0]: reference does not resolve` | An edge must be spelled as the *later* bug's `depends_on` if declared on a create. This constrains the spelling, not the topology — only a cycle is inexpressible. Why most edges are still declared by `bug.update` is a decision, not a constraint: see ADR 0007 |
| 2 | A flag type name containing `+ - ? X` is unaddressable | `src/bzr_live/replay/actions.py:588-598` (finding D1) | Flag types are single words: `review`, `signoff` |
| 3 | `custom_fields` is refused on `bug.create` | `src/bzr_live/replay/actions.py:24` (finding G4) | Custom-field values are separate `bug.custom-field-set` events |
| 4 | `estimated_hours` / `remaining_hours` are refused on `bug.create` | `src/bzr_live/replay/actions.py:25-28` (finding G1) | Time estimates arrive by `bug.update` |
| 5 | `bug.update` refuses `groups` and `version` | `src/bzr_live/replay/actions.py:31-36` (findings D3, G2) | Neither changes after create |
| 6 | `duplicate_of` conflicts with `status` and `resolution` | `src/bzr_live/replay/actions.py:50` (finding G5) | The duplicate event sets `duplicate_of` alone |
| 7 | `resolution`, `milestone` and `duplicate_of` cannot be cleared | `src/bzr_live/replay/actions.py:37-47` | Reopening is declared as a status change |
| 8 | `bug.create` refuses an omitted `version` | `src/bzr_live/replay/actions.py:281-285` (finding G9) | Every create declares a version |
| 9 | `insidergroup = admin` | `containers/bugzilla/checksetup_answers.txt:31` | The private-comment author is declared in `group:admin` |
| 9b | `editbugs` is granted to every account by `userregexp => '.*'` | `Bugzilla/Install.pm:134-138`; verified on the live fixture, where `reporter@example.test` holds it by `GRANT_REGEXP` | No actor is effectively unprivileged, and no bug is ever `UNCONFIRMED` |
| 10 | The rendered attachment summary is `<description> [<marker>] sha256=<64 hex>`, capped at 255 bytes | `src/bzr_live/replay/actions.py:13,81-82` | Attachment descriptions stay under 140 bytes |
| 11 | Resource identity is `<kind>:<name>`, unique across the scenario | `src/bzr_live/scenario/loader.py:287-292` | Versions and milestones carry per-product names |
| 12 | Declared text may not contain `[bzr-live:` | `src/bzr_live/replay/actions.py:169-178` | No summary, description, or comment body quotes a marker |

## Privilege model

**Start here: on this image every account holds `editbugs`, whatever the scenario declares.**
Stock Bugzilla defines `editbugs` with `userregexp => '.*'`
(`Bugzilla/Install.pm:134-138`), and `containers/bugzilla/checksetup_answers.txt` does not
override it, so `checksetup` grants it to every account at creation. Verified on the live
fixture: `reporter@example.test` carries an `editbugs` row of grant type `GRANT_REGEXP`
despite declaring no groups at all. `canconfirm` carries no such regexp and is granted only
where declared.

That single fact governs the rest of this section, and it is stated first because an earlier
draft of this design got it wrong in the other direction — it described `reporter` as an
unprivileged actor and built two confirm events and part of ADR 0007's rationale on that.
There is no unprivileged actor here. Restoring one means clearing that regexp, a
fixture-configuration change outside this issue's surface.

What follows is therefore about what the scenario *declares*, and about what would happen on
an image where the regexp were cleared. Three memberships bind the fixture, and the reason to
state them is the opposite of the obvious one: **a missing membership is silent on the create
path, not loud.** Bugzilla substitutes or drops and reports success.

- **`editbugs`** — without it a user may edit only bugs they reported or are assigned. It
  also gates two things on *create*, both silently:
  `_check_assigned_to` replaces a declared assignee with the component's default assignee
  (`Bugzilla/Bug.pm:1449-1454`), and `_check_dependencies` returns an empty pair, discarding
  every create-time `depends_on` and `blocks` (`:1707-1709`). Both verified by reading the
  pinned fixture image. `_check_keywords` and `_check_target_milestone` carry no such gate,
  so those two survive an unprivileged filer.
- **`canconfirm`** — required to move a bug out of `UNCONFIRMED`. Not load-bearing here:
  `editbugs` alone already makes Bugzilla file a bug as `CONFIRMED`
  (`Bugzilla/Bug.pm:1508-1522`), and every account has `editbugs`, so no bug in this
  scenario is ever `UNCONFIRMED`.
- **`timetrackinggroup`** — a Bugzilla *parameter*, not a membership, defaulting to
  `editbugs` (`Bugzilla/Config/GroupSecurity.pm:42-47` on the pinned image). It gates
  `work_time`, `estimated_time`, and `remaining_time`, so the estimate event and both
  work-time events depend on it. `containers/bugzilla/checksetup_answers.txt` does not set
  it, so the fixture inherits the default and the events work because their actors hold
  `editbugs`. On the create path a non-timetracker is silently given 0
  (`Bug.pm:2153-2159`, "we're forgiving"); on the update path the value is refused instead.
  Pinning it beside `insidergroup` would be the stronger fix and matches this repository's
  own precedent, but it is a `containers/` edit that makes no currently-failing payload
  succeed, so it falls outside this issue's permitted surface. It is recorded here and
  raised in the pull request rather than taken.

`editbugs` and `canconfirm` are Bugzilla system groups, pre-created by `checksetup` and
reconciled as existing fixture furniture
(`src/bzr_live/provision/executor.py:10-13,162-172`), so the scenario declares membership
without creating them. Verified against the live fixture: provisioning an actor declaring
`admin`, `editbugs`, and `canconfirm` succeeds, and a second run reports the actor
`unchanged`, which is `_classify_actor` confirming every declared membership read back.

**The invariant this forces.** Any create that declares an `assignee` or a
`depends_on`/`blocks` edge is filed by an actor that *declares* `editbugs`, and the offline
tier asserts exactly that. Be precise about what that buys on this image: the regexp grant
already makes the substitution unreachable, so the assertion is not currently preventing a
live defect. It pins the scenario's own intent, and it is what would catch the defect if the
regexp were cleared. `reporter` declares no groups and therefore declares no assignee and no
edges on anything it files — a self-consistency property of the fixture, not a claim about
its effective server-side privilege.

If `bzr group add-user` cannot grant a system group, that is a finding for
`docs/bzr-findings.md`, not a reason to promote every actor to admin.

## Resources

**Products and components**

| Product | Components | Versions | Milestones |
|---|---|---|---|
| `checkout` | `cart`, `payment` | `checkout-v1`, `checkout-v2` | `checkout-m1`, `checkout-m2` |
| `billing` | `invoicing`, `dunning` | `billing-v1` | `billing-m1` |

Component default assignees: `cart` → `triager`, `payment` → `developer`,
`invoicing` → `triager`, `dunning` → `developer`.

**Actors**

| Name | Email | Display name | Groups |
|---|---|---|---|
| `admin-ops` | `admin-ops@example.test` | Avery Ops | `admin`, `editbugs`, `canconfirm` |
| `triager` | `triager@example.test` | Tessa Triager | `editbugs`, `canconfirm` |
| `developer` | `developer@example.test` | Devi Developer | `editbugs`, `canconfirm` |
| `releaser` | `releaser@example.test` | Rex Releaser | `editbugs`, `canconfirm` |
| `reporter` | `reporter@example.test` | Robin Reporter | — |

**Keywords:** `regression`, `security`, `perf`.

**Custom fields:** `risk` (single-select: `low`, `medium`, `high`), `subsystem`
(multi-select: `cart`, `payment`, `invoicing`, `dunning`), `tracker` (text). These become
`cf_risk`, `cf_subsystem`, `cf_tracker` (`src/bzr_live/provision/executor.py:30-31`).

**Flag types:** `review` and `signoff`, both targeting bugs, both unscoped — an empty
product and component list yields the `(None, None)` inclusion pair
(`src/bzr_live/provision/executor.py:367-376`).

**Assets:** `notes` → `assets/triage-notes.txt`, `patch` → `assets/retry-fix.patch`.

## Bug topology

Twenty bugs, created in this order. The order matters only where a create declares an edge.

| # | Alias | Product/component | Reporter | Version | Notes |
|---:|---|---|---|---|---|
| 1 | `cart-double-charge` | checkout/cart | reporter | checkout-v1 | milestone `checkout-m1`, cc `reporter`, keyword `regression`. **No assignee**: `reporter` declares no groups, and the scenario keeps declared assignees on actors that declare `editbugs` (see Privilege model). `triage-double-charge` sets it later |
| 2 | `cart-empty-crash` | checkout/cart | reporter | checkout-v1 | |
| 3 | `cart-slow-render` | checkout/cart | developer | checkout-v2 | keyword `perf` |
| 4 | `cart-stale-total` | checkout/cart | triager | checkout-v1 | |
| 5 | `cart-dupe-report` | checkout/cart | reporter | checkout-v1 | becomes a duplicate of #1 |
| 6 | `cart-quantity-reset` | checkout/cart | reporter | checkout-v2 | |
| 7 | `pay-token-leak` | checkout/payment | admin-ops | checkout-v1 | keyword `security`, assignee `developer` |
| 8 | `pay-retry-loop` | checkout/payment | developer | checkout-v1 | |
| 9 | `pay-decline-copy` | checkout/payment | reporter | checkout-v2 | carries the reopening cycle. Lands `CONFIRMED` at create like every other bug here, because `editbugs` is regexp-granted to all accounts (see Privilege model), so no separate confirm event is declared for it |
| 10 | `pay-timeout-3ds` | checkout/payment | reporter | checkout-v1 | |
| 11 | `pay-refund-rounding` | checkout/payment | developer | checkout-v2 | |
| 12 | `inv-tax-mismatch` | billing/invoicing | triager | billing-v1 | milestone `billing-m1` |
| 13 | `inv-pdf-blank` | billing/invoicing | reporter | billing-v1 | |
| 14 | `inv-currency-drift` | billing/invoicing | developer | billing-v1 | |
| 15 | `inv-duplicate-line` | billing/invoicing | triager | billing-v1 | **create-time edge**: `depends_on` #12 |
| 16 | `inv-late-post` | billing/invoicing | releaser | billing-v1 | |
| 17 | `dun-retry-storm` | billing/dunning | developer | billing-v1 | keyword `perf` |
| 18 | `dun-wrong-locale` | billing/dunning | reporter | billing-v1 | diamond sink |
| 19 | `dun-silent-fail` | billing/dunning | triager | billing-v1 | |
| 20 | `dun-grace-window` | billing/dunning | releaser | billing-v1 | **create-time edge**: `blocks` #19 |

**Cross-product dependency chain.** `cart-double-charge` → `inv-tax-mismatch` →
`dun-retry-storm`, declared by two `bug.update` events. It spans `checkout` and `billing`
and is three deep, so a bounded traversal at depth 1 and at depth 2 return different sets —
which is what makes it useful to #20's verifier.

**Diamond.** `pay-token-leak` blocks both `pay-retry-loop` (checkout) and
`inv-currency-drift` (billing); both block `dun-wrong-locale`. Declared by one update on the
apex and one on the sink, so each edge is declared exactly once.

**Duplicate pair.** `cart-dupe-report` is marked `duplicate_of` `cart-double-charge` by a
`bug.update` carrying that field alone (constraint 6).

**Reopening.** `pay-decline-copy` goes `CONFIRMED` (as filed) → `RESOLVED/FIXED` →
`CONFIRMED` → `RESOLVED/FIXED`. The reopen is a status change, never a resolution clear
(constraint 7), and it is the scenario's only genuine status transition into an open state.

No bug in this scenario is ever `UNCONFIRMED`, and none can be. Bugzilla lands a bug filed by
an `editbugs` **or** `canconfirm` holder directly in `CONFIRMED`
(`Bugzilla/Bug.pm:1508-1522`; the fixture's `bug_status` sortkeys are `UNCONFIRMED 100,
CONFIRMED 200`), and `editbugs` is granted to every account by regexp — so an
`UNCONFIRMED → CONFIRMED` event would be accepted (`:1541-1543` skips a same-status change)
and reconcile green having written nothing. An earlier draft declared two such confirm
events; both were verified inert against the live fixture's `bugs_activity` and removed.
Restoring a genuine `UNCONFIRMED` path means clearing `editbugs`' `userregexp`, which is a
fixture-configuration change outside this issue's surface.

**Assignee breadth.** Two distinct actors are declared as assignee, which criterion 4 requires
on the assignee axis and not only on the reporter axis: `developer` on `create-pay-token-leak`
and on `triage-double-charge`, and `releaser` on `assign-decline-copy` in phase 3. Component
default assignees would put bugs on `triager` and `developer` regardless, but a default is
what Bugzilla substitutes, not what the scenario declares — the same distinction the privilege
model above turns on.

## Event stream

Forty-seven events in eight phases — 20 creates, 5 topology updates, 7 lifecycle updates,
5 comments, 3 attachment events, 2 flags, 2 work-time entries, and 3 custom-field
assignments. Every event name is unique and every marker derives from it
(`src/bzr_live/scenario/loader.py:791`). The implementation plan carries the per-event
table: actor, target, and payload fields for all forty-seven. It is not repeated here,
because a second copy is a second thing to keep in agreement by hand.

Phase 3's estimate event declares `estimated_hours` and `remaining_hours`, which
`bug.update` accepts but cannot read back, so its reconciliation always returns `retry`
(`src/bzr_live/replay/actions.py:55,420-423`). That is correct for an idempotent set and is
noted here because it is the one event in the scenario whose resume behaviour differs from
its neighbours'. Proving what resume does with it belongs to #21.

## Proof

**Offline tier — `tests/test_smoke_scenario.py`.** Loads `scenarios/smoke/` through
`load_scenario` with no server and asserts the invariants that make the fixture worth
replaying, so a fixture defect fails under `make test` without a container. It is not yet a
CI gate for fixture-only edits — `scenarios/**` is in neither path filter of
`.github/workflows/scenario-contract.yml`, and adding it belongs to #21 (ADR 0007,
Consequences). Eleven assertions cover loading and digest stability, the bug and product
counts, the dependency chain, the diamond, the duplicate, the reopening, full `HANDLERS`
coverage, and all three custom-field types. The implementation plan carries the method list
with the exact predicate for each; it is not repeated here.

Three of the eleven are **guard tests**. What each actually buys differs, and stating it
honestly matters more than the label:

- **the private-comment author holds `group:admin`** — moves a *loud* failure earlier.
  Bugzilla's `Comment.pm::_check_isprivate` raises `user_not_insider`, so a live replay
  would abort; this catches it at `make test` with no container.
- **every rendered attachment summary fits `ATTACHMENT_SUMMARY_BYTE_LIMIT`** — duplicates a
  precondition the engine already enforces. `BugAttachHandler.check_supported` refuses an
  over-length summary and `ReplayEngine._check_local_preconditions` runs `check_supported`
  over every event before any mutation, so this too is a shift offline rather than added
  coverage. It covers `bug.attach` only.
- **every create declaring an assignee or an edge is filed by an actor declaring
  `editbugs`** — the only one aimed at a genuinely *silent* server-side substitution
  (constraint 8's `Bug.pm:1449-1454` and `:1707-1709`). Per constraint 9b that substitution
  is unreachable on this image; the assertion pins the scenario's intent and would bite if
  the regexp were cleared.

The offline tier's real value is therefore that a fixture defect fails in seconds at a desk
rather than minutes into a container replay — not that it sees things the live run cannot.
Each guard still gets its own deliberate fault injection during implementation: a guard that
cannot be made to fail is not a guard.

**Live tier — `tests/smoke_scenario.sh`, `make smoke`.** Provisions the scenario's resources
and replays its events against a running fixture, following the conventions
`tests/replay_smoke.sh` already established: `BZR_LIVE_BZR` names the binary, `BZ_PORT`
falls back to the checkout's `.env`, state goes in a `mktemp -d` directory removed on exit,
and the script never edits `docs/bzr-findings.md`. It reports the observed wall-clock
duration of the replay, which is the number epic #1 asks for. It asserts nothing about
semantic invariants — that is #20 — so its pass condition is that provisioning and replay
both complete without error.

## Error handling

The fixture is data; its error paths are the loader's and the engine's, unchanged. Two
failure modes are this change's own:

- **A scenario that does not load.** The loader already reports
  `<file>:<line>:<json-path>: <message>`. The offline test asserts loading succeeds, so this
  surfaces in CI with the offending path named.
- **An honest payload the boundary refuses at replay.** The engine raises before mutating and
  names the limitation and its finding (`src/bzr_live/replay/actions.py:243-250`). The
  response is to record it in `docs/bzr-findings.md` and, where the cause is fixture
  configuration rather than `bzr`, to fix `containers/` — never to alter the declared payload
  into something the scenario did not mean.

Editing `containers/` changes the checkpoint stack fingerprint and invalidates saved
checkpoints (`README.md:127-130`). Any such fix says so in its commit message.

## Threat model

The change adds fixture data plus one shell script that creates the state root actor API keys
are written into, so the security-relevant trigger that applies is secret handling.
Everything else about the deployment is unchanged.

**Boundaries.** The script adds no boundary and widens no existing one. It handles no key
material itself: its only invocations are `python -m bzr_live.provision` and
`python -m bzr_live.replay`, which mint and read the keys internally, so no key ever enters
the shell process — a stronger boundary than `tests/replay_smoke.sh`, which does read
`actor-keys/*.key` at its lines 67-68 in order to call `bzr` directly.

**Actors.** A local operator running `make smoke` on their own machine. Per `AGENTS.md`, the
fixture binds to loopback, every credential in it is fabricated and disposable, and neither a
remote attacker nor a hostile local user is in the threat model.

**Controls.** The state root is a `mktemp -d` directory set to mode 0700 and removed by an
`EXIT` trap; keys reach `bzr` in the environment, never on a command line or in script
output; the scenario's committed files contain no key, no server ID, and no runtime state —
the last enforced by the loader, which types every reference position and refuses anything
that is not a `{"ref": ...}` object (`src/bzr_live/scenario/loader.py:162-172`).

**Out of scope.** Encryption at rest, key rotation, and credential-store integration, all
excluded by `AGENTS.md`. Fault injection and the response-loss proofs are #21's.

## Out of scope

The `verify` command and semantic assertions (#20); response-loss injection and CI wiring
(#21); named replay boundaries (`--through`, #17); the canonical large dataset (#8); any
change to the loader, replay engine, journal, or provisioning executor.
