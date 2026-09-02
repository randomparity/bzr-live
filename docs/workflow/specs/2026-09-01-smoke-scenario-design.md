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
| 1 | A created identity becomes resolvable only after its event validates, so no event may cite a bug a later event creates | `src/bzr_live/scenario/loader.py:810`; verified — a `bug.create` with `blocks` naming a later bug is refused with `events.jsonl:1:$.payload.blocks[0]: reference does not resolve` | Topology is declared in two phases: backward edges on create, everything else by later `bug.update` |
| 2 | A flag type name containing `+ - ? X` is unaddressable | `src/bzr_live/replay/actions.py:588-598` (finding D1) | Flag types are single words: `review`, `signoff` |
| 3 | `custom_fields` is refused on `bug.create` | `src/bzr_live/replay/actions.py:24` (finding G4) | Custom-field values are separate `bug.custom-field-set` events |
| 4 | `estimated_hours` / `remaining_hours` are refused on `bug.create` | `src/bzr_live/replay/actions.py:25-28` (finding G1) | Time estimates arrive by `bug.update` |
| 5 | `bug.update` refuses `groups` and `version` | `src/bzr_live/replay/actions.py:31-36` (findings D3, G2) | Neither changes after create |
| 6 | `duplicate_of` conflicts with `status` and `resolution` | `src/bzr_live/replay/actions.py:50` (finding G5) | The duplicate event sets `duplicate_of` alone |
| 7 | `resolution`, `milestone` and `duplicate_of` cannot be cleared | `src/bzr_live/replay/actions.py:37-47` | Reopening is declared as a status change |
| 8 | `bug.create` refuses an omitted `version` | `src/bzr_live/replay/actions.py:281-285` (finding G9) | Every create declares a version |
| 9 | `insidergroup = admin` | `containers/bugzilla/checksetup_answers.txt:31` | The private-comment author is declared in `group:admin` |
| 10 | The rendered attachment summary is `<description> [<marker>] sha256=<64 hex>`, capped at 255 bytes | `src/bzr_live/replay/actions.py:13,81-82` | Attachment descriptions stay under 140 bytes |
| 11 | Resource identity is `<kind>:<name>`, unique across the scenario | `src/bzr_live/scenario/loader.py:287-292` | Versions and milestones carry per-product names |
| 12 | Declared text may not contain `[bzr-live:` | `src/bzr_live/replay/actions.py:169-178` | No summary, description, or comment body quotes a marker |

## Privilege model

Bugzilla grants a new user no groups. Two consequences bind the fixture, and both are
stated here because a missing membership surfaces as an opaque mid-replay failure rather
than a validation error:

- **`editbugs`** — without it a user may edit only bugs they reported or are assigned. Every
  actor that updates a bug it did not report needs it.
- **`canconfirm`** — required to move a bug out of `UNCONFIRMED`.

Both are Bugzilla system groups, pre-created by `checksetup` and reconciled as existing
fixture furniture (`src/bzr_live/provision/executor.py:10-13,162-172`), so the scenario
declares membership without creating the groups. `reporter` is deliberately left
unprivileged: it creates and comments only, which keeps one honest non-privileged path in
the scenario.

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
| 1 | `cart-double-charge` | checkout/cart | reporter | checkout-v1 | milestone `checkout-m1`, assignee `triager`, cc `reporter`, keyword `regression` |
| 2 | `cart-empty-crash` | checkout/cart | reporter | checkout-v1 | |
| 3 | `cart-slow-render` | checkout/cart | developer | checkout-v2 | keyword `perf` |
| 4 | `cart-stale-total` | checkout/cart | triager | checkout-v1 | |
| 5 | `cart-dupe-report` | checkout/cart | reporter | checkout-v1 | becomes a duplicate of #1 |
| 6 | `cart-quantity-reset` | checkout/cart | reporter | checkout-v2 | |
| 7 | `pay-token-leak` | checkout/payment | admin-ops | checkout-v1 | keyword `security`, assignee `developer` |
| 8 | `pay-retry-loop` | checkout/payment | developer | checkout-v1 | |
| 9 | `pay-decline-copy` | checkout/payment | triager | checkout-v2 | carries the reopening cycle |
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

**Reopening.** `pay-decline-copy` goes `CONFIRMED` → `RESOLVED/FIXED` → `CONFIRMED` →
`RESOLVED/FIXED`. The reopen is a status change, never a resolution clear (constraint 7).

## Event stream

Forty-seven events in eight phases. Every event name is unique and every marker derives from
it (`src/bzr_live/scenario/loader.py:791`).

| Phase | Events | Action | Actors |
|---|---:|---|---|
| 1 Creates | 20 | `bug.create`, one per bug above | reporters from the table |
| 2 Topology | 5 | `bug.update` — chain ×2, diamond ×2, duplicate ×1 | triager, admin-ops, developer |
| 3 Lifecycle | 7 | `bug.update` — confirm ×2 (one also reassigns and extends cc), resolve, reopen, re-resolve, estimate, retarget | triager, developer, releaser |
| 4 Comments | 5 | `bug.comment` — four public, one private | admin-ops posts the private one |
| 5 Attachments | 3 | `bug.attach` ×2, `attachment.update` ×1 (obsolete) | triager, developer |
| 6 Flags | 2 | `bug.flag` — one `?` with requestee, one `+` | developer, releaser |
| 7 Work time | 2 | `bug.worktime` | developer, triager |
| 8 Custom fields | 3 | `bug.custom-field-set` — all three field types covered | admin-ops, triager |

Phase 3's estimate event declares `estimated_hours` and `remaining_hours`, which
`bug.update` accepts but cannot read back, so its reconciliation always returns `retry`
(`src/bzr_live/replay/actions.py:55,420-423`). That is correct for an idempotent set and is
noted here because it is the one event in the scenario whose resume behaviour differs from
its neighbours'. Proving what resume does with it belongs to #21.

## Proof

**Offline tier — `tests/test_smoke_scenario.py`.** Loads `scenarios/smoke/` through
`load_scenario` with no server and asserts the invariants that make the fixture worth
replaying, so a fixture defect fails in the existing scenario-contract CI job:

- it loads without error, and the digest is stable across two loads;
- exactly 20 `bug.create` events, spanning at least two products;
- the dependency chain is three deep and crosses products;
- the diamond's apex reaches the sink by two distinct paths;
- exactly one `duplicate_of` assignment;
- the status sequence on `pay-decline-copy` contains a resolved-to-open transition;
- every action in `HANDLERS` appears at least once;
- all three custom-field types are assigned;
- at least one private comment, and its author is in `group:admin`;
- every rendered attachment summary is within `ATTACHMENT_SUMMARY_BYTE_LIMIT`.

The last two are guard tests: each encodes a constraint that would otherwise fail only
against a live server, late.

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

The change adds fixture data plus one shell script that reads actor API keys, so the
security-relevant trigger that applies is secret handling. Everything else about the
deployment is unchanged.

**Boundaries.** The script adds no boundary. It widens no existing one. It reads actor API
keys from the run's state root and passes them to `bzr` through `BZR_LIVE_API_KEY`, which is
the mechanism ADR 0004 already established and `tests/replay_smoke.sh:67-68,82-86` already
uses.

**Actors.** A local operator running `make smoke` on their own machine. Per `AGENTS.md`, the
fixture binds to loopback, every credential in it is fabricated and disposable, and neither a
remote attacker nor a hostile local user is in the threat model.

**Controls.** The state root is a `mktemp -d` directory set to mode 0700 and removed by an
`EXIT` trap; keys reach `bzr` in the environment, never on a command line or in script
output; the scenario's committed files contain no key, no server ID, and no runtime state.

**Out of scope.** Encryption at rest, key rotation, and credential-store integration, all
excluded by `AGENTS.md`. Fault injection and the response-loss proofs are #21's.

## Out of scope

The `verify` command and semantic assertions (#20); response-loss injection and CI wiring
(#21); named replay boundaries (`--through`, #17); the canonical large dataset (#8); any
change to the loader, replay engine, journal, or provisioning executor.
