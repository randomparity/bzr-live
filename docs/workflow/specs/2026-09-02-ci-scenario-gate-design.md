# CI gate for the scenarios tree and the live smoke path — design

Issue #25, sub-issue of #21, part of #7. Decision record: [ADR 0010](../../adr/0010-ci-gated-live-scenario-proof.md).

## Problem

A pull request that edits only `scenarios/smoke/events.jsonl` triggers no CI job, because
neither workflow's `paths` filter names `scenarios/**`. Separately, the live proof that
`tests/smoke_scenario.sh` performs — provision, replay, verify — runs only when an operator
invokes it, so issue #7's x86_64 Linux requirement has no evidence in this repository.

## Requirements

R1. `scenarios/**` appears in the `paths` filter of `.github/workflows/scenario-contract.yml`
and `.github/workflows/container-lifecycle.yml`, on both the `pull_request` and the `push`
trigger.

R2. The `x86_64-linux` job runs, in this order and all inside its existing 45-minute
budget: provision, replay, verify, checkpoint save, mutate, restore, re-verify, resume.
Every stage fails the job when it fails.

R3. The job obtains a `bzr` binary at the revision `README.md` states the scenario is proven
at, and the run prints the revision it used.

R4. The arm64 macOS position is stated in `README.md`: which runner is unavailable and why,
and where the manual arm64 evidence lives.

R5. `make smoke` remains a single command an operator runs, and CI runs that same command.

R6. No stage substitutes a value the scenario did not declare. The one value this change
introduces — the mutation summary — is written to the fixture *after* the scenario's own
verification has passed, and is reverted before the re-verify.

## Design

### Path filters (R1)

`scenario-contract.yml` gains one line, `- scenarios/**`, in each of its two `paths` lists.

`container-lifecycle.yml` gains, in each of its two `paths` lists:

    - scenarios/**
    - tests/smoke_scenario.sh
    - docs/adr/0010-ci-gated-live-scenario-proof.md
    - docs/workflow/specs/2026-09-02-ci-scenario-gate-design.md
    - docs/workflow/plans/2026-09-02-ci-scenario-gate.md

Those last four are this change's own inputs, listed the way that workflow already lists
ADR 0005 and its spec. `src/**` is deliberately not added; ADR 0010 records the residual.

### Job steps (R2, R3)

Three steps go into the existing `x86_64-linux` job, between `Run lifecycle contract checks`
and `Exercise checkpoint round trip`:

1. **Install the pinned bzr** — `sudo apt-get update && sudo apt-get install -y
   libdbus-1-dev pkg-config`, then `cargo install --git https://github.com/randomparity/bzr
   --rev 63abb94e7e14a2db79efe0ddf0011a1f32ed8640 --locked --root "$RUNNER_TEMP/bzr" bzr`.
   The prefix is outside the workspace because `compose.yaml:20-21` builds from context `.`
   and the repository has no `.dockerignore`.
2. **Start the fixture** — `make up`.
3. **Exercise the live scenario smoke path** — `BZR_LIVE_BZR="$RUNNER_TEMP/bzr/bin/bzr"
   make smoke`.

The smoke step precedes `make checkpoint-smoke` so that the fixture `make smoke` sees is the
one `make up` just installed, which is the fresh-fixture precondition `README.md:189-191`
states. `CONFIRM_CLEAN=1 make clean` already runs `if: always()` and removes the volumes.

### Script stages (R2, R5, R6)

`tests/smoke_scenario.sh` gains five stages after the existing verify stage. They run in the
same invocation because the state root the earlier stages wrote dies with the script's EXIT
trap.

- **save** — `scripts/checkpoint save smoke --store "$STATE/store" --runner-state
  "$STATE/state"`. The store is a sibling of the state root: `scripts/checkpoint` refuses
  a store and runner state that overlap.
- **mutate** — read the first `bug.create` event's declared alias and actor out of the
  loaded scenario, and its actor email out of the scenario's resources, then send
  `bug update --summary=<probe text>` as that actor with `--server-api-key-env`.
- **read back** — `bug view` the same alias and require the observed summary to equal the
  probe text. This proves the mutation reached the server, so a restore that reverts
  nothing cannot be mistaken for one that reverted something.
- **restore** — `scripts/checkpoint restore smoke` with the same store and runner state.
- **re-verify and resume** — `verify` again against the same state root, then `resume`.
  `verify` exits non-zero on any divergence, and the declared summary is one of the scalars
  it compares, so a restore that did not revert the mutation fails here. `resume` reads
  every event's journal record as already complete and sends no mutation.

Every stage is bare — no pipe, no `|| true` — so `set -euo pipefail` fails the script and
therefore the job.

### Documentation (R4, R5)

`README.md`'s smoke-scenario section gains: the new stages in the live tier's description,
the note that `make smoke` now stops and restarts the stack, the CI position (which job runs
it, at which pinned `bzr` revision, with the measured duration), and the arm64 statement —
GitHub-hosted macOS runners support neither Docker container operations nor nested
virtualization on arm64, so the arm64 proof stays the operator's, with this branch's own run
as the current record. The sentence that today reads "Fault injection and x86_64 CI wiring
are tracked separately, so `make smoke` is operator-run rather than a merge gate today" is
rewritten to state what actually holds after this change.

## Failure modes

| Mode | Behaviour |
|---|---|
| `cargo install` fails or the revision does not resolve | The step fails; no fixture has been started yet. |
| The pinned `bzr` predates a fixture requirement | `tests/smoke_scenario.sh:49` printed the revision as its first line, so the log names it. |
| Provision, replay or verify diverges on x86_64 | The script exits non-zero with the stage's own message. That is issue #7's evidence, whichever way it lands. |
| Checkpoint save or restore fails | `scripts/checkpoint` exits non-zero; the job fails and `make clean` removes the volumes. |
| Restore silently reverts nothing | The re-verify reports a `summary` divergence and exits 1. |
| The job exceeds 45 minutes | GitHub cancels it. The first run's measured duration is reported on the pull request. |

## Threat model

**Boundaries this design adds.** One: the runner fetches and compiles source from
`randomparity/bzr` and then executes the result. That is external code running on a CI
runner with the repository checked out.

**Boundaries it widens.** The `paths` filters. More pull requests now reach the live job,
including pull requests from forks — `randomparity/bzr-live` is public.

**Actor model.** The untrusted actor is the author of a fork pull request. They control
every file at the pull-request head, including `scenarios/**`, `tests/smoke_scenario.sh` and
the workflow file itself, because `pull_request` runs the workflow as it exists on the head.
That is already true of this repository before this change: `make test` and
`make checkpoint-smoke` already execute head-controlled scripts. The trust this design
places is in GitHub's own boundary — a fork pull request gets a read-only `GITHUB_TOKEN` and
no secrets — and nowhere else. There is no remote attacker against the fixture: it binds
127.0.0.1 and lives only for the job.

**Control per boundary.**

- *External source → runner.* `--rev` is a full 40-character commit SHA and `--locked` uses
  the pinned `Cargo.lock`, so the build is content-addressed and its dependency set fixed.
  Existing controls stand unchanged: `permissions: contents: read`, `persist-credentials:
  false` on the checkout, and the `Verify checked out commit` step.
- *Fork pull request → runner.* GitHub's own control, named above. This change adds no
  secret, no write permission and no `pull_request_target` trigger, so it does not widen it.
- *Fixture credentials.* Actor API keys are minted into the mktemp'd state root, which is
  mode 0700 and removed by the script's EXIT trap; the mutation stage passes its key through
  `BZR_LIVE_API_KEY` and `--server-api-key-env`, never argv, matching
  `tests/replay_smoke.sh:82-86`. No stage echoes a key.
- *Runner disk.* `CONFIRM_CLEAN=1 make clean` runs `if: always()` and removes the volumes,
  the local images and `.env`.

**Explicitly out of scope.** A fork pull request that edits the workflow to install a
different revision: that is the platform's model for public repositories, already reachable
through every existing step, and the read-only token plus absent secrets is the control.
Supply-chain compromise of a dependency already inside `bzr`'s `Cargo.lock`: out of this
repository's reach, and `AGENTS.md` places production-grade supply-chain machinery outside a
disposable fixture's scope. Secrets at rest: this repository has none in CI.
