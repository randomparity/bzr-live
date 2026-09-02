# 0010. Gate the scenarios tree and prove the live path in the x86_64 job

## Status

Accepted (2026-09-02)

## Context

ADR 0007 shipped `scenarios/smoke/` and its two proof tiers, and recorded that neither is
a CI gate: `.github/workflows/scenario-contract.yml:5-10,21-26` and
`.github/workflows/container-lifecycle.yml:5-20,23-42` name `src/**`, `tests/**`,
containers, compose and named design documents, and neither names `scenarios/**`. A pull
request editing only `scenarios/smoke/events.jsonl` therefore runs no job at all. ADR 0008
then added `verify`, and `tests/smoke_scenario.sh` now provisions, replays and verifies the
committed scenario against a running fixture — but only when an operator invokes it.

Issue #25 asks for both halves: `scenarios/**` in the path filters of both workflows on
both triggers, and a live x86_64 Linux path that runs provision, replay, verify, checkpoint
save, mutate, restore, re-verify and resume inside the existing `x86_64-linux` job's
45-minute budget. Issue #7's guarantee — that the fixture composes on a second architecture
and that a restored checkpoint is the state the scenario declares — has no evidence in this
repository beyond one operator's arm64 macOS run.

Three facts shape the answer. `tests/smoke_scenario.sh:20-21` mktemps its state root and
removes it on an EXIT trap, and `verify` reads the journal and actor keys that root holds,
so every stage that must see the replayed state has to run inside one invocation of that
script. No published `bzr` release reaches the revision floor `README.md:193-208` states —
the newest is `v0.8.2`, which carries the D6 defect — so CI cannot install a release.
`libdbus-1-dev` is not preinstalled on GitHub's Ubuntu images: `bzr`'s own CI installs it
in every job that compiles (`randomparity/bzr` `.github/workflows/ci.yml:21-22,55-56`).

## Decision

1. **Add `scenarios/**` to both workflows on both triggers**, and add this change's own
   inputs — `tests/smoke_scenario.sh`, ADR 0010, its spec and its plan — to
   `container-lifecycle.yml`, matching how that workflow already names ADR 0005 and its
   spec. Do not add `src/**` to the live workflow.

2. **Extend the existing `x86_64-linux` job.** Three new steps: install `libdbus-1-dev` and
   compile `bzr` at a pinned revision, `make up`, and `BZR_LIVE_BZR=… make smoke`. The
   smoke step runs before `make checkpoint-smoke`, so `make smoke` meets its documented
   fresh-fixture precondition without depending on what another test leaves behind.

3. **Obtain `bzr` with `cargo +1.89.0 install --git https://github.com/randomparity/bzr
   --rev 63abb94e7e14a2db79efe0ddf0011a1f32ed8640 --locked bzr`**, into a prefix under
   `$RUNNER_TEMP`, after `rustup toolchain install 1.89.0` and an apt install of
   `libdbus-1-dev pkg-config`. Three things are pinned: the source revision `README.md`
   states the scenario is proven at, the dependency set (`--locked`), and the compiler —
   `1.89.0` is what the pinned tree's own `rust-toolchain.toml` and its `Cargo.toml`
   `rust-version` name. Default features stay on, matching upstream's native x86_64 Linux
   build. The runner image supplies `cargo` and `rustup`; the job installs neither.

4. **Put the whole sequence in `tests/smoke_scenario.sh`**, unconditionally, so `make smoke`
   is one path that CI and operators both run. The stages after `verify` are: save a
   checkpoint over the verified fixture, mutate it, restore, re-verify, resume. That script's
   state root is canonicalized at its `mktemp`, because `src/bzr_live/checkpoint.py:155-167`
   requires every path argument to equal its own `resolve()` and macOS `TMPDIR` sits under
   the `/var` → `/private/var` symlink — `tests/checkpoint_smoke.sh:5-6` already does this.

5. **Mutate by rewriting one bug's `summary`** — the first `bug.create` the scenario
   declares, addressed by its declared alias and filed as its own actor. Every declared
   summary is in `bug.scalars`, which `src/bzr_live/verify/checks.py:70-73` compares
   against `bug view`, so a restore that silently did nothing leaves a divergence the
   re-verify must report.

6. **Name GitHub-hosted macOS runners as unavailable** for the arm64 live proof, and keep
   that proof operator-run and recorded in `README.md`.

## Consequences

- A pull request editing only a fixture file under `scenarios/` now runs the offline tier
  in `Scenario contract` and the live tier in `Container lifecycle`. ADR 0007's second
  consequence — "It is not yet a CI gate for fixture-only edits" — stops holding from this
  change forward.
- The live job now compiles a second repository's source, uncached, on every triggering
  pull request: 293 packages in release mode at the pinned revision. What it can reach is
  bounded by the full SHA, `--locked`, `permissions: contents: read` and no secret in the
  job; what it costs is not bounded at all, and the first CI run is the measurement. If
  that run puts the job over 45 minutes, the pre-agreed remedy is to cache the built
  prefix on the pinned SHA — see the rejected bullet, which states why it is not paid for
  up front.
- Two pins now need raising by hand together when `README.md`'s proven revision moves: the
  `--rev` SHA and, if the new tree's `rust-toolchain.toml` moves, the `1.89.0` toolchain.
- **A change under `src/bzr_live/replay/` or `src/bzr_live/verify/` still does not run the
  live job.** Adding `src/**` to `container-lifecycle.yml` would put a ~20-minute live job
  on nearly every pull request, which issue #25 does not ask for. The residual is real and
  left to the operator.
- `make smoke` now stops and restarts the Compose stack twice, because a cold checkpoint
  does. Operators pay that on every local run, and a failed restore leaves the fixture in
  whatever state the restore reached — recoverable by `CONFIRM_RESET=1 make reset`, which
  is the recovery this repository already documents for every other fixture failure.
- The scenario is mutated in place during a run that has already verified it. The mutation
  is a probe, not a substitution: it is applied after the scenario's own proof has passed,
  it is reverted by the restore, and the re-verify would fail if it were not.
- `resume` over the restored journal is a cheap final assertion — every event reads back as
  already complete — so it costs one pass over the journal and no server mutation.

## Considered & rejected

- **A third workflow, or a second live workflow, for the scenario tree.** judgment: issue
  #25 names the existing `x86_64-linux` job as where a live smoke step belongs, and a
  second live workflow would build the same image twice per pull request.
- **Install a published `bzr` release instead of compiling one.** verified: `gh release
  list --repo randomparity/bzr` returns `v0.8.2` as the newest, and `README.md:196-198`
  records that every revision before `5fb99362` reports a component's `default_assignee` as
  null (finding D6) so provisioning refuses. No release reaches the floor.
- **Build `bzr` from its default branch instead of a pinned revision.** judgment: an
  upstream commit would then turn this repository's merge gate red, and the run could no
  longer say which revision it proved — the property `tests/smoke_scenario.sh:45-49` exists
  to preserve.
- **Compile with `--no-default-features` to avoid the libdbus dependency.** verified:
  `randomparity/bzr` `.github/workflows/ci.yml:21-22` and `release.yml:115-117` install
  `libdbus-1-dev pkg-config` and build the native x86_64 Linux binary with default features,
  so the apt step reproduces the upstream configuration while the feature flag would prove a
  differently-built binary.
- **Check `randomparity/bzr` out into the workspace with `actions/checkout`.** verified:
  `compose.yaml:20-21` builds from context `.` and the repository has no `.dockerignore`, so
  a source tree inside the workspace would be sent to the Docker daemon as build context.
- **Keep `tests/smoke_scenario.sh` as it is and add a CI-only script, or gate the new
  stages behind an environment variable.** judgment: two paths where the state root is the
  only thing binding them, and the path CI runs would be the one no operator exercises.
- **Mutate by appending a comment.** verified: `src/bzr_live/verify/checks.py:509-534`
  locates each *declared* comment by marker and orders only those, because Bugzilla posts
  its own notices into the thread; an undeclared extra comment is invisible to `verify`, so
  the probe would prove nothing.
- **Run `verify` a second time between the mutation and the restore, to show it fails.**
  judgment: it buys a property the source already settles (rejected bullet above) at the
  cost of a second full verify pass in a job with a 45-minute ceiling; a cheap `bug view`
  read-back proves the mutation reached the server.
- **Cache the compiled prefix with `actions/cache`, keyed on the pinned SHA.** judgment:
  it would make the compile a first-run cost, and the pin makes the key exact — but the
  build's cost is unmeasured, and paying for a cache before knowing whether the job needs
  one adds a third-party action and a cross-run artifact to a fixture whose `AGENTS.md`
  asks for the simple implementation. Held as the named remedy if the first CI run
  overruns, rather than adopted blind. `randomparity/bzr`'s own CI uses
  `Swatinem/rust-cache` (`.github/workflows/ci.yml:24-26`) for its far more frequent jobs.
- **Land the path filters alone and leave the live sequence operator-run.** judgment: it
  closes the gap this record opens with — a fixture-only pull request running no job — at
  none of the cost the live half carries. Rejected because issue #25's second Expected
  paragraph asks for the live x86_64 path in the same breath as the filters, and issue
  #7's guarantee is the half that has no evidence.
- **Run the live tier on a GitHub-hosted arm64 macOS runner.** verified: GitHub's
  runners reference states nested virtualization is unsupported on arm64 macOS runners
  (Apple Virtualization Framework), and container operations are Linux-only
  (`actions/runner#1866`), so no GitHub-hosted macOS runner can start this fixture.
