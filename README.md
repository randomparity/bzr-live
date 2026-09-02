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
interruption. Both exit 1 with a `replay failed: ...` message on stderr naming the
precondition or `bzr` limitation that stopped them.

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

Smoke scenario
--------------

`scenarios/smoke/` is the committed 20-bug scenario. It spans two products and four
components with five actors, and its 48 events exercise every supported action: a
cross-product dependency chain three deep, a diamond spanning both products, a duplicate
pair, a reopening cycle, public and private comments, attachments with an obsolescence,
flags with and without a requestee, work time, keywords, milestones, and text,
single-select and multi-select custom fields.

Two tiers prove it. The offline tier needs no server and runs with the rest of the suite:

    uv run --python 3.11 python -m unittest tests.test_smoke_scenario

Eleven assertions cover the topology and coverage invariants. Three are guards against
Bugzilla behaviour that is silent server-side — the insider group behind private comments,
the 255-byte attachment summary ceiling Bugzilla truncates rather than refuses, and the rule
that any create declaring an assignee or a dependency edge must be filed by an actor holding
`editbugs`, because Bugzilla otherwise substitutes or discards it without an error.

The live tier provisions and replays the scenario against the running fixture:

    CONFIRM_RESET=1 make reset && make up
    BZR_LIVE_BZR=/path/to/bzr make smoke

Start from a fresh fixture: Bugzilla reads `containers/bugzilla/checksetup_answers.txt` only
at install, and an existing fixture may already hold conflicting definitions of the products
and components the scenario declares.

**`make smoke` needs a `bzr` at `5fb99362` or later.** Earlier revisions report a component's
`default_assignee` as null even when Bugzilla has stored one, so provisioning cannot confirm
what it wrote and refuses. That is finding [D6](docs/bzr-findings.md); the scenario keeps
declaring the field rather than dropping it. The script prints the `bzr` revision as its first
line for this reason — the same scenario passes or fails on that revision alone.

Observed: **48 events replayed in 74.05s**, replay only, excluding provisioning and
`make up`. Measured on Apple M5 Max, macOS (Darwin 25.6.0, arm64), Docker 29.7.2, with
`bzr 0.8.3-dev (63abb94e)`. Provisioning the 28 resources and `make up` are each separate
intervals and are not included.

The live tier proves the parts compose; it asserts no semantic invariants about the
replayed state. That verifier is issue #20, and fault injection plus x86_64 CI wiring is
issue #21, so `make smoke` is operator-run rather than a merge gate today.
