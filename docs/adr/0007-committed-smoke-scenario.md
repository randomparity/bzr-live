# ADR 0007: Committed smoke scenario proven by a dedicated live script

## Status

Accepted

## Context

Issues #2 through #6 built the container lifecycle, the versioned scenario contract,
resource provisioning, checkpoints, and actor-scoped event replay. Each is proven by unit
tests plus a narrow operator-run live script. What none of them proves is that the parts
compose: the largest committed fixture,
`tests/fixtures/replay-scenario/`, declares one bug touched by every supported action, so
no committed scenario exercises cross-bug topology, a second product, more than two actors,
or more than a single single-select custom-field value.

Epic #1 requires a smoke scenario of roughly 20 bugs stored under `scenarios/<name>/`, and
issue #19 scopes the authoring of it. That raises three questions this record settles: where
committed scenarios live relative to the existing unit-test fixtures, whether the fixture is
hand-authored or generated, and how a fixture that can only be fully exercised against a
running Bugzilla gets a proof that CI can also run.

A fourth question is forced by the contract rather than chosen. The loader adds a created
identity to the resolvable set only after the creating event has validated
(`src/bzr_live/scenario/loader.py:810`), so an event cannot cite a bug that a later event
creates. Any topology richer than a chain in creation order has to be expressed some other
way.

## Decision

Committed scenarios live under `scenarios/<name>/`. That tree is operator-facing fixture
data — scenarios an operator loads to get a realistic database. `tests/fixtures/` keeps its
current role: minimal fixtures that exist to exercise one code path in the unit suite. The
smoke scenario is `scenarios/smoke/`.

The fixture is hand-authored JSON and JSONL, validated by the existing loader. No change to
the loader, the replay engine, the journal, or the provisioning executor is part of this
work. Where an honest payload cannot be expressed, that is recorded as a finding in
`docs/bzr-findings.md` rather than accommodated by widening the contract.

Bug topology is declared in two phases: `depends_on` and `blocks` on a `bug.create` cite
only bugs created by earlier events, and every remaining edge — including every edge that
would otherwise be a forward reference — is declared by a later `bug.update`. The scenario
therefore reads as a narrative in which relationships accumulate, which is also how the
relationships it models arise in a real tracker.

The proof is two-tier. `tests/test_smoke_scenario.py` loads and plans the committed scenario
with no server, asserting the topology and coverage invariants; it needs no Docker and runs
under `make test`. `tests/smoke_scenario.sh`, invoked by `make smoke`, provisions and replays
the scenario against a running fixture with a real `bzr` binary, and is the arm that proves
the composition. Wiring either tier into CI is issue #21's, not this record's — including the
`scenarios/**` path filter the offline tier needs, which is why the consequence below is
stated as narrowly as it is.

## Consequences

- A malformed or internally inconsistent smoke fixture fails locally under `make test`,
  without a container. That is what makes the fixture safe to edit at a desk.
- **It is not yet a CI gate for fixture-only edits.** `.github/workflows/scenario-contract.yml`
  filters on `src/**`, `tests/**`, package metadata, and named design documents; `scenarios/**`
  appears in neither its `pull_request` nor its `push` list. So the pull request that adds
  `tests/test_smoke_scenario.py` runs the job — that file is under `tests/` — while a later
  pull request editing only `scenarios/smoke/events.jsonl` runs nothing. The 20-bug fixture
  this record expects people to edit is exactly the artifact left ungated, and the gap closes
  when #21 adds the path filter. Until then the offline tier is a developer guardrail, not a
  merge precondition, and this record does not claim otherwise.
- The live tier stays operator-invoked until #21 wires it into CI, so between this change and
  that one the composition proof is a command someone runs, recorded in the pull request,
  rather than a gate.
- Every edit to the fixture changes the canonical scenario digest, which invalidates any
  journal written against the old content. That is already the contract's behaviour
  (ADR 0006); this record makes a 20-bug fixture the thing people edit, so it will be met
  more often. `README.md` states the recovery.
- The two-phase topology declaration means a reader cannot see a bug's complete relationship
  set at its create event. The offline test asserts the final topology, so the invariant is
  checked in one place even though it is declared across many.
- `scenarios/` becomes a third fixture location alongside `tests/fixtures/` and the
  checkpoint store. The distinction is by audience, not by format, and it is stated here
  because nothing in the file layout makes it evident.

## Considered & rejected

- **Do nothing; extend `tests/fixtures/replay-scenario/`.** verified: its `scenario.json:2`
  declares "Replay fixture: one bug touched by every supported action" and its
  `events.jsonl` holds eight events, seven targeting `bug:checkout-race` and one its
  attachment, so growing it to 20 bugs would redefine the fixture that
  `tests/test_replay.py` and `tests/replay_smoke.sh` were written against, at the same time
  as it stops being minimal.
- **Store the smoke scenario under `tests/fixtures/` too.** verified: epic #1's scenario
  contract requires "Store each scenario under `scenarios/<name>/`", and the runner's
  `load`/`replay`/`verify` commands take a scenario path an operator types.
- **Generate the fixture from a Python script at build time.** judgment: a generator makes
  the committed artifact a program rather than data, so reviewing what the fixture asserts
  means running it; the epic's requirement that generated server IDs never appear in
  committed fixture files is easier to audit on literal JSON than on the output of a
  generator.
- **Extend `tests/replay_smoke.sh` rather than adding a script.** verified: that script's
  header (`tests/replay_smoke.sh:1-9`) scopes it to issue #6's three specific assertions —
  an honest create with no `op_sys`/`rep_platform`, the alias round-trip, and server-side
  alias uniqueness — and it probes finding D1; folding a 20-bug composition proof into it
  would make a failure ambiguous between two issues' contracts.
- **Declare the full topology on each `bug.create` and order creates to suit.** verified:
  a `bug.create` citing a later-created bug is refused with
  `events.jsonl:1:$.payload.blocks[0]: reference does not resolve` (run against
  `load_scenario` at 27d37a4), and a diamond cannot be linearized so that every edge points
  backwards.
- **Add a loader affordance for forward references.** judgment: it would trade a validation
  guarantee that currently catches typos for the convenience of declaring an edge in one
  place instead of two, on a contract this issue is explicitly not chartered to change.
