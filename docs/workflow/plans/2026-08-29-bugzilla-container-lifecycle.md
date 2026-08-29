# Bugzilla container lifecycle implementation plan

**Goal:** Deliver a pinned, loopback-only, readiness-aware Bugzilla 5.2 and
MariaDB lifecycle for local live testing.

**Architecture:** Compose owns a `db` and `bugzilla` service with distinct named
volumes. A pinned source-build image performs Bugzilla setup before starting
Apache. One strict Bash lifecycle command derives a per-checkout project name,
generates private credentials, serializes mutations, diagnoses readiness, and
confines confirmed deletion to that Compose project; Make exposes the operator
commands.

**Tech stack:** Docker Compose v2 with `up --wait` and `--wait-timeout`, Docker
Buildx, Bash 3.2+, Make, OpenSSL, curl, Bugzilla 5.2, MariaDB 10.6, GitHub
Actions.

## Global constraints

- Bugzilla commit:
  `644c66f45ce0b1b2746a31a061fbd96886278225`.
- Bugzilla archive SHA-256:
  `bf4f79d9e6b230ad01d9b3c4d12a56af7eebd96cc76d5c9231a696a0750e1660`.
- Ubuntu OCI index:
  `sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517`.
- MariaDB OCI index:
  `sha256:92e50059ea0a5965a33ef751970eab37d421b91ebbd01ac909039cffe159e574`.
- Target platforms are Linux amd64 and arm64. The local host is arm64 macOS;
  native x86_64 Ubuntu CI supplies the amd64 live proof.
- Bind only `127.0.0.1:${BZ_PORT:-8080}:80`; publish no database port.
- Never source, echo, trace, or commit `.env`; generated mode is 0600.
- Never run global Docker prune commands. Reset and cleanup act only on the
  SHA-256-derived checkout project identity and require exact confirmation.
- Issue #3's Python packages, dependencies, schemas, tests, and scenario docs
  are excluded.
- Base branch: `main`; branch: `feat/bugzilla-container-lifecycle-2`.
- Guardrails introduced by this plan: `make test`, `make check`, and
  `make build-multiarch`.

## Task 1: Specify lifecycle and Compose behavior with a failing shell test

**Files:** create `tests/lifecycle_test.sh`; create fixture/stub files only under
`tests/fixtures/` if the test cannot keep them in its temporary directory.

**Interfaces:** consumes the operator contract in the specification. Later tasks
must satisfy executable `scripts/lifecycle <init|up|down|doctor|reset|clean>` and
Make targets with the same names. The test invokes the script with
`BZ_LIVE_ROOT=<temporary checkout>` and a stubbed `PATH` so no real Docker state
is touched.

1. Write a Bash test runner that creates one temporary root per case, installs
   argv-recording `docker` and deterministic `openssl` stubs, copies the
   repository configuration under test, and removes the temporary root through
   a trap. Assertions exit nonzero with the case name and expected/actual value.
2. Add cases for: three independent 48-hex secrets and mode 0600; idempotent
   init; no secret stdout/stderr; a stubbed mid-generation failure that leaves
   no secret temporary file; refusal when either derived project volume exists
   without `.env`; cleanup without `.env` using only in-process placeholders;
   different canonical roots yielding different project names;
   live, stale, and ownerless lock refusal with the exact lock path/manual action;
   `down` omitting `--volumes`; reset confirmation and `down --volumes
   --remove-orphans`; clean confirmation and `down --volumes --remove-orphans
   --rmi local`, with `.env` removed only after successful Docker exit; bounded
   doctor `ps`/`logs`; and configured endpoint discovery through `compose port`.
3. Add static assertions against `compose.yaml` for loopback binding, distinct
   volumes, required interpolation, health checks, healthy dependency, immutable
   image pins, and URL-port consistency.
4. Run `bash tests/lifecycle_test.sh`. Expected: nonzero with the first missing
   `scripts/lifecycle` contract, proving the test is red before implementation.
5. Commit only after the implementation tasks make this suite green; the red
   observation remains execution evidence, not a commit.

**Acceptance:** every listed behavior can fail independently; no assertion
matches incidental formatting when a rendered/argv behavior can be observed.

## Task 2: Build and configure the pinned services

**Files:** create `compose.yaml`, `containers/bugzilla/Dockerfile`,
`containers/bugzilla/entrypoint.sh`,
`containers/bugzilla/checksetup_answers.txt`,
`containers/bugzilla/apache.conf`, and `containers/mariadb/bugzilla.cnf`.

**Interfaces:** Compose services are exactly `db` and `bugzilla`; volumes are
`mariadb-data` and `bugzilla-data`. The lifecycle script calls
`docker compose --project-name <derived> --file <root>/compose.yaml`.

1. Define MariaDB from
   `mariadb:10.6@sha256:92e50059ea0a5965a33ef751970eab37d421b91ebbd01ac909039cffe159e574`,
   map `MARIADB_DATABASE=bugs`, `MARIADB_USER=bugs`, and require the generated
   database/root passwords. Mount only `mariadb-data:/var/lib/mysql`. Use
   `healthcheck.sh --connect --innodb_initialized` with bounded interval,
   timeout, retries, and start period. Mount the Bugzilla MariaDB config
   read-only; set packet and full-text values required by upstream Bugzilla.
2. Define the Bugzilla build, required environment, distinct data volume,
   loopback web mapping, `db: condition: service_healthy`, and an in-container
   HTTP health check. `BZ_URLBASE` interpolates `BZ_PORT`. Do not add a restart
   policy or tmpfs mounts.
3. In the Dockerfile, use the pinned Ubuntu index, install upstream-required
   Apache/Perl/MariaDB-client packages, download the canonical commit archive,
   verify the exact SHA-256 before extraction, install the upstream CPAN
   requirements, configure Apache modules, and copy only the owned setup files.
4. In `entrypoint.sh`, use strict mode; require every environment variable; wait
   for MariaDB with a bounded loop; create a private answer file by replacing
   only the fixed-shape values generated by this repository; pass the default-
   disabled `BZ_ALLOW_UNSAFE_UTF8_CONVERSION` answer; run `checksetup.pl`;
   remove the answer file; repair `/var/www/html/data` ownership recursively to
   `www-data` after successful setup; and `exec apachectl -D FOREGROUND`. Never
   print credentials.
5. Run the static portion of `bash tests/lifecycle_test.sh`. Expected: Compose
   assertions pass; lifecycle cases remain red until Task 3.

**Acceptance:** `docker compose config --quiet` succeeds with generated secrets;
`docker buildx imagetools inspect` confirms both pinned indexes contain amd64 and
arm64 manifests.

## Task 3: Implement safe operator lifecycle

**Files:** create `scripts/lifecycle`, `Makefile`, `.env.example`, `.gitignore`;
update `README.md`; finish `tests/lifecycle_test.sh` if a behavioral assertion
needs a corrected test boundary.

**Interfaces:** `scripts/lifecycle` accepts exactly one command from `init`,
`up`, `down`, `doctor`, `reset`, and `clean`. `Makefile` delegates one-for-one.
Environment inputs are `BZ_LIVE_ROOT` for tests, `BZ_WAIT_TIMEOUT`,
`CONFIRM_RESET`, and `CONFIRM_CLEAN`; `.env` holds `BZ_PORT`, admin identity,
the three generated secrets, and `BZ_ALLOW_UNSAFE_UTF8_CONVERSION=0`.

1. Resolve the canonical root without following caller-provided shell text.
   Derive `PROJECT=bzr-live-<first 12 lowercase hex of SHA-256(root)>` with
   OpenSSL. Build Compose argv in a Bash array.
2. Implement an atomic `mkdir` lock at
   `${TMPDIR:-/tmp}/${PROJECT}.lifecycle.lock`. Install the trap immediately;
   write PID metadata best-effort. On contention, never delete: report the exact
   path/manual verification action and conditionally report valid PID/liveness.
3. Implement `init`: before generation, inspect the two derived named volumes;
   refuse an absent `.env` if either exists. Create a mode-0600 sibling temp path
   and register it with the exit trap before writing. Populate fixed non-secret
   defaults and three `openssl rand -hex 24` values, byte-check without printing,
   then rename and clear the active-temp variable. Every failure path removes
   the active temp file. Existing `.env` returns success unchanged.
4. Implement `up` as locked init followed by `compose up --build --detach --wait
   --wait-timeout`; avoid recursive lock acquisition by using private functions.
5. Before any mutation, require Docker, `docker compose`, OpenSSL, and curl, and
   inspect `docker compose up --help` for both `--wait` and `--wait-timeout`.
   Implement locked `down` without volume/image flags. Implement `doctor` as a
   read-only prerequisite check, `compose config --quiet`, bounded `compose ps`,
   endpoint lookup via `compose port bugzilla 80`, curl HTTP check, and bounded
   logs only on failure. Ensure known generated values never appear in output.
6. Implement reset/clean confirmation before mutation. Use temporary exported
   placeholder interpolation values only when `.env` is absent. Reset executes
   project `down --volumes --remove-orphans`. Clean adds `--rmi local` and removes
   `.env` only after exit zero.
7. Route every reachable failure through a command-specific error that names the
   failed operation and a concrete next action. Add independent stub failures
   for Docker, Compose, each wait flag, OpenSSL, curl, init, up, down, doctor,
   reset, and clean and assert both fields without matching raw dependency
   prose.
8. Map Make targets, commit placeholders only in `.env.example`, ignore `.env`,
   and document prerequisites, commands, readiness, loopback URL, persistence,
   confirmation syntax, credential location, diagnostics, and backup warning.
9. Run `bash tests/lifecycle_test.sh`. Expected: all cases pass.
10. Temporarily remove `--volumes` from the lifecycle reset implementation,
    leave the test unchanged, and run the suite. Expected: the reset argv case
    fails. Restore the implementation and rerun green.
11. Run `make test` and `make check`. Expected: exit 0 with zero warnings.
12. Commit with `feat: add pinned Bugzilla container lifecycle`.

**Acceptance:** all lifecycle contracts are green with stubbed boundaries; no
secret enters Git; destructive argv is exact and per-checkout.

## Task 4: Prove both target platforms and automate amd64 validation

**Files:** create `.github/workflows/container-lifecycle.yml`; adjust only
lifecycle-owned files for evidence-backed failures.

**Interfaces:** CI invokes `make test`, `make check`, `make up`, `make doctor`,
`make down`, and `CONFIRM_RESET=1 make reset` on native x86_64 Ubuntu. Local
multi-platform proof invokes `make build-multiarch`.

1. Add a least-privilege workflow triggered for lifecycle-path pull-request and
   main changes. Use `ubuntu-24.04`, assert `uname -m` is `x86_64`, run test/check,
   start the live stack, run doctor/down/up/reset, and always run confirmed clean
   in a final step. Use no repository secrets.
2. Add `make build-multiarch` using `docker buildx build --platform
   linux/amd64,linux/arm64 --output type=cacheonly` for the Bugzilla definition.
3. Run `make build-multiarch` locally. Expected: both platform builds exit 0.
4. Run `make up`; wait for exit 0; request the loopback endpoint; run
   `make doctor`; run `make down`; run `make up` again; run
   `CONFIRM_RESET=1 make reset`. Expected: every command exits 0 and reset removes
   both project volumes. Run `CONFIRM_CLEAN=1 make clean` for local cleanup.
5. Run `make test` and `make check` once more. Expected: exit 0.
6. Commit the workflow or any evidence-backed portability correction separately
   as `ci: verify Bugzilla lifecycle on x86 Linux`.

**Acceptance:** local arm64 build/live proof and native x86_64 CI build/live
proof are green. Cleanup leaves no project containers, networks, images,
volumes, or `.env`; no unrelated Docker resource is touched.
