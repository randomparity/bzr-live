bzr Live Test Bugzilla Database
===============================

This repository runs a pinned Bugzilla 5.2 and MariaDB database for live bzr
experiments beyond automated unit tests. The web service binds only to IPv4
loopback by default.

Prerequisites
-------------

- Docker Desktop or Docker Engine
- Docker Compose v2 with `up --wait` and `up --wait-timeout`
- Docker Buildx for the multi-architecture build guardrail
- Bash 3.2+, Make, OpenSSL, and curl

Start and inspect
-----------------

```sh
make up
make doctor
```

The first `make up` generates `.env` with mode 0600. It contains the Bugzilla
administrator, application database, and MariaDB root passwords and is ignored
by Git. The values are never printed by the lifecycle commands. Do not copy
`.env.example` to `.env`; its values are placeholders. Restore the original
`.env` if Docker volumes remain from an earlier run—new credentials cannot open
an existing MariaDB database.

Bugzilla is available at <http://127.0.0.1:8080/>. Change `BZ_PORT` in `.env`
to use another loopback port. `make up` waits for MariaDB initialization,
Bugzilla `checksetup.pl`, Apache startup, and the HTTP health check before it
returns.

Lifecycle commands
------------------

- `make init` creates private credentials without starting containers.
- `make up` builds, starts, and waits for the healthy stack.
- `make doctor` validates Compose and reports service and HTTP readiness. On a
  readiness failure it prints at most 100 log lines per service.
- `make down` stops containers and preserves credentials and both data volumes.
- `CONFIRM_RESET=1 make reset` removes this checkout's containers, network, and
  MariaDB/Bugzilla data volumes but preserves `.env` and the built image.
- `CONFIRM_CLEAN=1 make clean` also removes this checkout's locally built image
  and `.env`, after Compose cleanup succeeds.

Reset and clean are destructive. Back up anything you intend to keep before
running either command. Neither command invokes a global Docker prune. Each
checkout derives a separate Compose project identity, so sibling worktrees do
not share lifecycle resources.

State and recovery
------------------

MariaDB data lives in one Docker volume and Bugzilla's mutable `data` directory
in another. `make down` preserves both. A lifecycle lock serializes start, stop,
and destructive commands. If a process or host crash leaves a stale lock, the
next command reports its exact path; verify no lifecycle process is running
before removing that directory manually.

Unsafe UTF-8 conversion remains disabled. If Bugzilla explicitly requires it,
back up the database first and set `BZ_ALLOW_UNSAFE_UTF8_CONVERSION=1` in `.env`
for the next `make up`.

Verification
------------

```sh
make test
make check
make build-multiarch
```

`make build-multiarch` builds the Bugzilla image for Linux amd64 and arm64.
Pull requests also run the live lifecycle on native x86_64 Ubuntu.

Scenario provisioning
---------------------

Provision a validated scenario's resources into the running fixture:

    uv run python -m bzr_live.provision tests/fixtures/provision-scenario \
      --state-root ./state --bzr /path/to/bzr

Reruns are idempotent: existing resources that match their declaration are
reported `unchanged`, and a resource that differs in a declared field fails with
a conflict naming the field. Actor API keys are written under the state root
(default `./state`, gitignored) as owner-only files — `admin.key` for the
fixture admin and `actor-keys/<actor>.key` per scenario actor. The state root is
disposable local fixture state; deleting it re-mints keys on the next run.

`tests/provision_smoke.sh` runs the live two-run proof against a fresh fixture
(`BZR_LIVE_BZR=<bzr binary> bash tests/provision_smoke.sh`).

Scenario replay
----------------

Replay a validated scenario's events against actors provisioned above, journaling
every attempt for safe resume:

    uv run python -m bzr_live.replay replay tests/fixtures/replay-scenario \
      --state-root ./state --bzr /path/to/bzr

`replay` requires no journal record under this state root and no server-side
collision on any alias the scenario declares — a per-scenario proxy for the pristine
baseline rather than a check of it, since nothing here can ask whether the database
is at that baseline (ADR 0006) — and the scenario's actors already provisioned with
API keys under the same state root; `resume` continues a run from its journal after an
interruption. Each exits 1 with a `<command> failed: ...` message on stderr — `replay
failed:`, `resume failed:`, `verify failed:` — naming the precondition or `bzr` limitation
that stopped it.

`resume` refuses on three things: a scenario whose digest no longer matches the journal, a
journal record for an event the scenario no longer names, and a create whose alias already
exists without a journal record proving this run made it. It does **not** detect a fixture
reset — no journal record carries a fixture identity — and Bugzilla restarts ids at 1, so a
journal outlives the fixture it describes and its stored ids then point at whatever now
occupies them. Treat a reset as invalidating the journal: remove the journal directory with
it and replay from the start.

Replay needs a fixture installed with the current `containers/bugzilla/checksetup_answers.txt`.
Bugzilla reads those answers only at install, so a fixture built before them rejects every
create — "There is no Priority named '--'", or a missing platform — with nothing pointing at
the cause. Recreate it first: `CONFIRM_RESET=1 make reset && make up`, then re-provision.

That edit also changes the checkpoint stack fingerprint — `scripts/checkpoint` hashes every
file under `containers/` — so checkpoints saved before it no longer restore ("bundle: stack
fingerprint is incompatible"). Save a fresh `pristine` after recreating the fixture, or the
engine's own "restore the pristine baseline" instruction has nothing to restore.

`tests/replay_smoke.sh` runs the live proof against a fresh fixture
(`BZR_LIVE_BZR=<bzr binary> bash tests/replay_smoke.sh`).

Scenario verification
---------------------

`verify` reads the replayed state back off the server and checks it against the scenario:

    uv run python -m bzr_live.replay verify scenarios/smoke \
      --state-root ./state --bzr /path/to/bzr

It runs **in the same state root as the replay** — it takes every server id from that
run's journal and reads as the actors provisioned there, so a fresh state root cannot
verify an earlier replay. It refuses before any read if the journal is incomplete, was
written for a different scenario digest, or records an event that did not complete.

Six check families cover each bug: declared fields and custom fields and flags, history
attribution and ordering, relationship topology, the comment thread, private-comment
visibility from an actor outside the insider group, and attachment metadata and content
checksums. Four of the six are skipped on a bug that declares nothing for them — the
summary's check count is the number that actually ran, so a run that skipped a bug cannot
report the same number as one that did not. It prints one line per finding, then a summary
line, and exits 1 if any finding is a **divergence** — the fixture disagreeing with the
scenario. A finding may instead be
**unverifiable**, meaning `bzr` or Bugzilla cannot read the declared value back on this
path; those are counted, printed with their reason, and do not fail the run. Each one is
recorded in [docs/bzr-findings.md](docs/bzr-findings.md) or against a tracking issue.

Smoke scenario
--------------

`scenarios/smoke/` is the committed 20-bug scenario. It spans two products and four
components with five actors, and its 48 events exercise every supported action: a
cross-product dependency chain three deep, a diamond spanning both products, a duplicate
pair, a reopening cycle, public and private comments, attachments with an obsolescence,
flags with and without a requestee, work time, keywords, milestones, a bug restricted to a
group, and text, single-select and multi-select custom fields.

Two tiers prove it. The offline tier needs no server and runs with the rest of the suite:

    uv run --python 3.11 python -m unittest tests.test_smoke_scenario

Fifteen assertions cover the topology and coverage invariants, including the scenario
digest, which is pinned so that editing the fixture is a deliberate change. Six are guards:
the insider group behind private comments, the 255-byte attachment summary ceiling, the rule
that any create declaring an assignee or a dependency edge is filed by an actor declaring
`editbugs`, and three around bug groups — that every actor touching a group-restricted bug
holds that group, that such a bug declares no private comment, and that the actor the
verifier reads back as holds every group any bug is restricted to. All but the third move
failures a live run would raise anyway into a container-free run; only the third targets a
substitution Bugzilla makes silently, and on this image even that is unreachable, because
stock Bugzilla grants `editbugs` to every account by regexp. The offline tier's value is
speed and no Docker, not extra reach.

The live tier runs ten stages against the running fixture:

    CONFIRM_RESET=1 make reset && make up
    BZR_LIVE_BZR=/path/to/bzr make smoke

It provisions, replays and verifies the scenario; then saves a checkpoint over the verified
fixture, reads one bug's numeric id, rewrites that bug's summary to a value the scenario does
not declare, reads it back to prove the change reached the server, restores the checkpoint,
verifies again, and resumes the restored journal. The last four stages are what prove a
checkpoint restores the state the scenario declares rather than merely completing: the
re-verify compares every declared summary against the server, so a restore that reverted
nothing fails it. Because a cold checkpoint stops the stack, `make smoke` now stops and
restarts Compose twice.

Start from a fresh fixture: Bugzilla reads `containers/bugzilla/checksetup_answers.txt` only
at install, and an existing fixture may already hold conflicting definitions of the products
and components the scenario declares. `replay` refuses outright if the fixture already holds
the scenario's bugs, so a second `make smoke` without a reset stops at that check.

Every run removes its mode-0700 state root, so the actor API keys provisioning mints do not
outlive it. Cleanup runs from an `EXIT` trap carrying a completion sentinel, and the sentinel
is what keeps that honest: on bash 3.2 — still macOS's `/bin/bash` — any `EXIT` trap turns a
fatal expansion error into exit 0, and capturing `$?` in the handler does not help, since it
is already 0 by then. So the handler instead reports a run that never reached the end of the
script as a named failure on stderr, whatever status bash handed it.

**`make smoke` has been proven at `bzr` `63abb94e` and nowhere else.** Two separate
requirements bear on the revision, and only one of them is a measurement:

- Before `5fb99362`, `bzr` reports a component's `default_assignee` as null even when Bugzilla
  has stored one, so provisioning cannot confirm what it wrote and refuses. That is finding
  [D6](docs/bzr-findings.md); the scenario keeps declaring the field rather than dropping it.
- `src/bzr_live/replay/actions.py` is written against `b80303b7` and refuses a reply shape it
  does not recognise rather than absorbing it. `b80303b7` declares output schema `2.0.0`;
  `5fb99362` declares `0.6.1`, the same major as the `0.8.2` release D6 was found on. Whether
  the engine accepts `bug view`, `comment list`, and `attachment list` at `0.6.1` is not known
  here, because no run has established it.

So `5fb99362` is where the D6 defect stops, not a floor this repository has evidence for. Use
`b80303b7` or later, and treat anything below `63abb94e` as untested. The script prints the
`bzr` revision as its first line for this reason — the same scenario passes or fails on that
revision alone.

Observed: **48 events replayed in 74.12s**, against a fixture reset immediately beforehand.
Measured on Apple M5 Max, macOS (Darwin 25.6.0, arm64), Docker 29.7.2, with
`bzr 0.8.3-dev (63abb94e)`. Provisioning the 29 resources and `make up` are separate
intervals and are not included.

**The stages after replay have no current figure, and that is finding
[D12](docs/bzr-findings.md#d12).** Since the scenario began declaring a bug group, `verify`
refuses at `bug links` on the restricted bug — `bzr` reads links through Bugzilla's search
endpoint, which hides a bug the caller cannot see instead of faulting — so the run stops
there and the seven stages after it do not execute. The refusal is deliberate: it names the
defect and its upstream issue, and is neither waived nor routed around. The figures taken
before it, describing the 47-event scenario, were 47 events replayed in 72.80s, 69 checks
verified in 56.94s, and 69 checks re-verified in 59.02s after the checkpoint round trip, each
reporting 0 divergences and 4 unverifiable claims. Those are history rather than a current
measurement, and they return when `bzr#719` is fixed.

Every figure here was produced under `/bin/bash` 3.2.57, which `make smoke` resolves from
`PATH` on this machine. Ordinary failures propagate correctly there — issue #20's run was
proven to bite, a server-side summary edit producing exit 1 and restoring it exit 0 — but
before the sentinel above, a fatal expansion error under `set -u` would have been
indistinguishable from success. Nothing indicates one occurred.

The four unverifiable claims the fold declares are `cart-double-charge`'s `estimated_hours`
(finding D8) and `remaining_hours` (PR #23), and the work-time hours on the two bugs that log
any (issue #22). Every other declared value on all 20 bugs is asserted — subject to D12, which
stops the run before those assertions are reached.

The live tier proves both that the parts compose and that the state they leave behind is
the one the scenario declares — up to D12, which currently stops it after replay.

Be precise about what that leaves. The replay stage still proves the whole write path,
including the group restriction: all 48 events execute against a real Bugzilla. The verify
stage proves nothing at present, and not only for the restricted bug. `Verifier.run` prints
its findings only after every read has returned, so the refusal discards the findings already
collected — the restricted bug's `groups` comparison among them — and `pay-refund-rounding` is
the eleventh of twenty bugs in fold order, so the nine after it are never read at all. This is
not a false green: the run exits non-zero either way. It does mean the only live evidence that
`bug view` reads `groups: ["restricted"]` back, and that `bug history` carries the change
attributed to `admin-ops`, is the hand-run table in
[D12](docs/bzr-findings.md#d12) — measured once at `63abb94e`, not asserted by `make smoke`.

**It is now a merge gate.** `Container lifecycle`'s `x86_64-linux` job compiles `bzr` at the
pinned revision above and runs this same `make smoke` on every pull request touching
`scenarios/`, the containers, the compose file, the lifecycle or checkpoint scripts,
`tests/smoke_scenario.sh`, or this file. The offline tier gained the same reach: both
workflows now name `scenarios/**`, so a pull request editing only a fixture file runs the
jobs that prove it. Response-loss fault injection is a separate offline suite
(`tests/test_fault_injection.py`).

The arm64 half stays operator-run, and the runner is named rather than wished for: no
GitHub-hosted macOS runner can start this fixture. Container operations are Linux-only
([actions/runner#1866](https://github.com/actions/runner/issues/1866)), and GitHub's
runner reference records that nested virtualization is unsupported on arm64 macOS runners
because of Apple's Virtualization Framework. The figures above are the current arm64 record;
reproduce them with the two commands at the top of this section.
