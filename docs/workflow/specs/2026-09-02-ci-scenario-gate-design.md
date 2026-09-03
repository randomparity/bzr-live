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
Every stage fails the job when it fails. The script adds two reads around the mutation — one
to resolve the target's numeric id, one to confirm the mutation landed — so it runs ten
commands against the fixture, not eight.

R3. The job obtains a `bzr` binary at the revision `README.md` states the scenario is proven
at, and the run prints the revision it used.

R4. The arm64 macOS position is stated in `README.md`: which runner is unavailable and why,
and where the manual arm64 evidence lives.

R5. `make smoke` remains a single command an operator runs, and CI runs that same command.

R6. `scenario-contract.yml` gates every design record on both triggers, so no record can be
omitted from it silently again — issue #20's ADR 0008, spec and plan are the omission that
prompted this, and the enumerate-or-glob question is decided rather than deferred.

R7. `tests/smoke_scenario.sh` cannot report success for a run that failed. Issue #29 owns the
three sibling smoke scripts; this one is assessed and fixed here.

R8. The new CI step is shown to fail when the thing it checks fails, by a controlled fault,
and how that was shown is recorded.

R9. No stage substitutes a value the scenario did not declare. The one value this change
introduces — the mutation summary — is written to the fixture *after* the scenario's own
verification has passed, and is reverted before the re-verify.

## Design

### Path filters (R1)

`scenario-contract.yml` gains `- scenarios/**` in each of its two `paths` lists, and its
seven per-record entries in each list — ADR 0002, 0003 and 0006 with their specs and plans —
collapse to two:

    - docs/adr/**
    - docs/workflow/**

That is R6. The enumeration had already missed issue #20's ADR 0008, spec and plan in both
lists; the omission was masked because `src/**` and `tests/**` are listed too, so PR #28 ran
the job on its code changes and nobody saw the gap. ADR 0010 decision 2 records the choice
and the ADR-0003 clause it amends.

`container-lifecycle.yml` gains, in each of its two `paths` lists:

    - scenarios/**
    - tests/smoke_scenario.sh

Those two are what change what the live job proves. Neither `src/**` nor this change's own
ADR, spec and plan is added; ADR 0010 decision 1 records why, and its consequences record
both residuals.

`make check` reads no YAML — it runs `bash -n`, shellcheck, `compileall` and `docker
compose config` (`Makefile:38-57`) — so a dropped entry or a mis-indented step in either
file passes every local gate, and a workflow GitHub cannot parse or whose filters no longer
match looks from the outside exactly like a gate that passed. `tests/test_ci_workflow_gates.py`
holds what a dependency-free line-oriented reader can hold: the entries are present, no line
carries a tab, every indent is even, the live job's steps appear in the order this design
depends on, and the workflow's pinned `bzr` revision is the one `README.md` claims.

### Job steps (R2, R3)

Three steps go into the existing `x86_64-linux` job, between `Run lifecycle contract checks`
and `Exercise checkpoint round trip`:

1. **Install the pinned bzr** — `sudo apt-get update && sudo apt-get install -y
   libdbus-1-dev pkg-config`, then `rustup toolchain install 1.89.0 --profile minimal`,
   then `cargo +1.89.0 install --git https://github.com/randomparity/bzr --rev
   63abb94e7e14a2db79efe0ddf0011a1f32ed8640 --locked --root "$RUNNER_TEMP/bzr" bzr`. The
   prefix is outside the workspace because `compose.yaml:20-21` builds from context `.` and
   the repository has no `.dockerignore`. `cargo` and `rustup` are runner-image
   prerequisites, not things this job installs: GitHub's `ubuntu-24.04` image ships both,
   and the toolchain it defaults to drifts with the image, which is why `1.89.0` — the
   channel the pinned tree's own `rust-toolchain.toml` names — is requested explicitly.
2. **Start the fixture** — `make up`.
3. **Exercise the live scenario smoke path** — `BZR_LIVE_BZR="$RUNNER_TEMP/bzr/bin/bzr"
   make smoke`.

The smoke step precedes `make checkpoint-smoke` so that the fixture `make smoke` sees is the
one `make up` just installed, which is the fresh-fixture precondition `README.md:189-191`
states. `CONFIRM_CLEAN=1 make clean` already runs `if: always()` and removes the volumes.

### Exit-status hygiene (R7)

`tests/smoke_scenario.sh` carried the same `trap 'rm -rf "$STATE"' EXIT` shape issue #29
tracks. Measured, `set -euo pipefail` throughout: an ordinary command failure — what every
stage here produces — exits 1 through any trap on both bash 3.2.57 and 5.3.15. A *fatal
expansion error* (an unbound variable under `set -u`; this file has no bash-4-only construct)
exits **1 with no trap** and **0 with any trap** on bash 3.2, including
`tests/checkpoint_smoke.sh:13-18`'s status-preserving `cleanup`, because `$?` is already 0 at
handler entry. ADR 0010 decision 8 carries the table.

So the trap stays and gains issue #29's completion sentinel: cleanup still removes the state
root on every path, and a status of 0 that never reached `COMPLETED=1` becomes a named stderr
failure. That is the one remedy which both restores the status and keeps the 0700 state root —
actor API keys included — from outliving a failing run, which is PR #23's intent. Dropping the
trap restores the status too but leaks the root; a status-preserving cleanup alone leaks
nothing but measures the same masked 0. No interpreter guard is needed and none is added,
which matters because `make smoke` resolves `bash` from `PATH` and finds `/bin/bash` 3.2.57
first on this repository's reference host. It also leaves all five smoke scripts carrying one
pattern.

`tests/test_smoke_trap_status.py` is the harness that holds this: `tests/smoke_scenario.sh`
joins its `SMOKE_SCRIPTS` rows, so the same three fault-injection modes run against this
script under every discovered interpreter.

### Proving the gate bites (R8)

A gate is worth what its failure behaviour is worth, and neither a green local run nor a
green first CI run shows that this step can go red. The proof is a controlled fault: comment
out the `scripts/checkpoint restore` call so the probe summary is never reverted, run
`make smoke`, and require it to exit non-zero with the re-verify reporting a `summary`
divergence on the probe bug — then restore the line and re-run to green. That fault targets
the one stage whose failure is otherwise indistinguishable from success, since a restore that
reverts nothing leaves a fixture that still looks replayed.

### Script stages (R2, R5, R9)

`tests/smoke_scenario.sh` gains seven executable stages after the existing verify stage —
R2's five, plus the `bug view` that resolves the mutation target's numeric id and the one
that reads the mutation back — bringing the script to ten stages in all. They run in one
invocation because the state root is `mktemp`'d and `verify` and `resume` read the journal
and the actor keys inside it, so a second invocation could not see the replay.

- **save** — `scripts/checkpoint save smoke --store "$STATE/store" --runner-state
  "$STATE/state"`. Two constraints bind those arguments, both in `checkpoint.py`: store and
  runner state must not overlap (`:334-335`), which siblings satisfy, and each must equal
  its own `resolve()` (`:155-167`), which the script's `mktemp` root does not on macOS
  because `TMPDIR` sits under the `/var` → `/private/var` symlink. The script therefore
  canonicalizes `$STATE` at its `mktemp`, as `tests/checkpoint_smoke.sh:5-6` does.
- **resolve** — read the first `bug.create` event's **server** alias and actor out of the
  loaded scenario — `scenario/loader.py:711-716` drops the declared alias from the
  postcondition and substitutes the synthesized `bzr-live-<hash>`, which is what the fixture
  actually carries — and its actor email out of the scenario's resources, then `bug view` that
  alias and take the numeric `id` from the reply. `bug update` accepts `Vec<u64>` only
  (finding G10), so the alias cannot address the mutation even though it addresses this read.
- **mutate** — send `bug update --summary=<probe text> -- <id>` as that actor with
  `--server-api-key-env`. ADR 0010 decision 6 records why the summary is the field chosen.
- **read back** — `bug view` the same server alias again and require the observed summary to equal
  the probe text, so a restore that reverts nothing cannot be mistaken for one that did.
- **restore** — `scripts/checkpoint restore smoke` with the same store and runner state.
- **re-verify and resume** — `verify` again against the same state root, then `resume`.
  `verify` exits non-zero on any divergence, so an unreverted mutation fails here. `resume`
  reads every event's journal record as already complete and sends no mutation.

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
| The job exceeds 45 minutes | GitHub cancels it, and `scenarios/**` becomes a red gate rather than a passing one. The budget is not asserted here: the job measures 3.5–5.8 minutes today across its last eight runs, leaving ~39 minutes; the addition is one uncached release build of 293 packages plus provisioning, one replay (72.63s on an M5 Max), two verify passes (56.91s each there), two stack stop/start cycles and a journal-only resume — plus the cost `make checkpoint-smoke`
inherits from now running second, since its one save and two restores archive a fixture
holding the replayed scenario rather than an empty one. The M5 Max figures are an
arm64-native lower bound, not a transferable estimate for a hosted runner. That is plausibly
inside the budget and plausibly not, and the first CI run is what settles it. If it overruns, the remedy is to cache the built prefix on the pinned SHA — the ADR's rejected bullet holds it ready — not to raise `timeout-minutes`, which is issue #25's stated constraint. |

## Threat model

**Boundaries this design adds.** One: the runner fetches and compiles source from
`randomparity/bzr` and then executes the result. That is external code running on a CI
runner with the repository checked out.

**Boundaries it widens.** The `paths` filters, on both workflows. More pull requests now
reach the live job, including pull requests from forks — `randomparity/bzr-live` is public.
A docs-only fork pull request now also reaches the **offline** job, which runs `unittest
discover` over head-controlled test code, `uv build`, and `tests/smoke_installed.py` against
the built wheel (`scenario-contract.yml:64-74`); before this change it ran no job at all. The
control is the same *Fork pull request → runner* row below, not a new one.

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
- *Fixture credentials.* Actor API keys are minted into the mktemp'd state root, and the
  checkpoint store beside it holds an archive of the fixture database including its api-key
  rows. Both are mode 0700 under a 0700 parent, and the script removes the whole tree at the
  end of its success path -- a failed run leaves it, which is the trade *Exit-status hygiene*
  records. The mutation stage passes its key through `BZR_LIVE_API_KEY` and
  `--server-api-key-env`, never argv, matching `tests/replay_smoke.sh:82-86`. No stage echoes
  a key.
- *Runner disk.* `CONFIRM_CLEAN=1 make clean` runs `if: always()` and removes the volumes,
  the local images and `.env`.

**Explicitly out of scope.** A fork pull request that edits the workflow to install a
different revision: that is the platform's model for public repositories, already reachable
through every existing step, and the read-only token plus absent secrets is the control.
Supply-chain compromise of a dependency already inside `bzr`'s `Cargo.lock`: out of this
repository's reach, and `AGENTS.md` places production-grade supply-chain machinery outside a
disposable fixture's scope. Secrets at rest: this repository has none in CI.
