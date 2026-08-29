# Bugzilla container lifecycle design

## Scope

Issue #2 requires a reproducible local Bugzilla 5.2/MariaDB substrate. This
design is governed by [ADR 0001](../../adr/0001-pinned-source-compose-lifecycle.md).
It owns Compose and container definitions, lifecycle scripts and tests, root
operator configuration, and the README. Python packages, scenario schemas,
scenario tests, and Python dependency files belong to issue #3 and are excluded.

## Requirements

1. Build Bugzilla 5.2 on Linux amd64 and arm64 from commit
   `644c66f45ce0b1b2746a31a061fbd96886278225`; verify source archive SHA-256
   `bf4f79d9e6b230ad01d9b3c4d12a56af7eebd96cc76d5c9231a696a0750e1660`.
2. Pin Ubuntu 24.04 and MariaDB 10.6 to the multi-platform OCI indexes recorded
   in ADR 0001. Both indexes must advertise Linux amd64 and arm64 manifests.
3. Publish the web service as `127.0.0.1:${BZ_PORT:-8080}:80` only. The
   generated URL base must use the same configured port.
4. Persist `/var/lib/mysql` and `/var/www/html/data` in distinct named volumes.
   The web entrypoint must make the Bugzilla data directory writable by
   `www-data`; MariaDB's first-party entrypoint owns database-volume setup.
5. Generate `.env` atomically with mode 0600 when missing and no project data
   volume exists. If either project data volume exists without `.env`, refuse
   initialization and direct the operator to restore the credential file or run
   confirmed cleanup; never generate credentials that cannot unlock persisted
   MariaDB state. Confirmed cleanup must remain operable without `.env` by
   supplying non-secret, non-persisted placeholder values only while Compose
   renders its teardown command. Generate independent 48-hex-character admin,
   application-database, and MariaDB-root passwords using `openssl rand`. Never
   print their values. Keep `.env` ignored and commit only non-secret
   `.env.example` placeholders.
6. `make up` must initialize secrets, build and start the stack, and wait for
   Compose health checks. MariaDB health must use its first-party
   `healthcheck.sh`; Bugzilla health must make an HTTP request from inside the
   container. Bugzilla starts only after MariaDB is healthy and only becomes
   healthy after `checksetup.pl` succeeds and Apache serves requests.
7. `make down` must stop containers without deleting volumes or `.env`.
8. `make doctor` must validate prerequisites and Compose configuration, report
   container/health state, test the loopback HTTP endpoint, and print bounded
   service diagnostics on failure without printing `.env` or secret values.
9. `make reset` must refuse unless `CONFIRM_RESET=1`; once confirmed, stop the
   project and remove only its containers, networks, and named volumes. It must
   preserve `.env` and locally built images.
10. `make clean` must refuse unless `CONFIRM_CLEAN=1`; once confirmed, remove
    the same project state, locally built Compose images, and `.env`. Removing
    unrelated Docker resources is forbidden.
11. Lifecycle failures must name the failed operation and a next action. Shell
    code must run with strict error handling, quote expansions, and avoid
    secret-bearing command traces.

## Architecture

`compose.yaml` defines `db` and `bugzilla`. MariaDB consumes generated Compose
variables through its official initialization contract and owns the database
volume. The Bugzilla image downloads and verifies the pinned upstream source,
installs the upstream runtime dependencies, and copies only repository-owned
Apache, checksetup, and entrypoint files. The entrypoint waits on MariaDB,
materializes a checksetup answer file from fixed-shape generated values, runs
Bugzilla setup, fixes data ownership, and then executes Apache in the
foreground.

`scripts/lifecycle` is the only state-changing operator implementation. It
normalizes the repository root, fixes the Compose project name, generates
secrets, checks Docker/Compose/OpenSSL/curl prerequisites, and probes Compose's
non-mutating `up --help` output for both `--wait` and `--wait-timeout` before
dispatching the six commands. `Makefile` targets are thin entry points.
`tests/lifecycle_test.sh` uses command stubs and temporary directories to
exercise configuration and
safety behavior without deleting real Docker state. Image and live-stack smoke
checks remain explicit operator/CI commands because they require Docker and
network access.

## Error and recovery behavior

- Missing Docker, Compose, OpenSSL, curl, or either required Compose wait flag
  fails before creating or removing state and names the missing capability.
- Every state-changing command holds an exclusive per-project directory lock in
  the host temporary directory, acquired atomically with `mkdir`. The owner PID
  is diagnostic metadata only. A contender always fails without mutation. When
  metadata is readable and valid, it reports the owner and whether that PID is
  live; missing or malformed metadata is reported as unknown ownership. Every
  contention error reports the exact lock path and directs the operator to
  verify no lifecycle process is running before removing that directory
  manually. The script never reclaims a lock automatically, because doing so
  can unlink a replacement lock. The current owner removes its lock through a
  trap.
- An existing `.env` is never overwritten by initialization. Initialization
  refuses when `.env` is absent but either project data volume exists.
- Interrupted secret generation leaves only a temporary file, removed by a
  trap; installation is a same-directory rename.
- Compose readiness timeout leaves diagnostics available through `make doctor`;
  `make down` remains safe and non-destructive.
- Reset and cleanup fail closed when their exact confirmation variable is absent.
  Their teardown path supplies in-process placeholder interpolation values when
  `.env` is absent; it never writes replacement credentials. Cleanup removes
  `.env` only after Compose has successfully removed project resources and
  locally built images.
- Checksetup failure exits the web container rather than starting a partially
  configured server. Unsafe UTF-8 conversion remains disabled unless the
  operator explicitly sets `BZ_ALLOW_UNSAFE_UTF8_CONVERSION=1` in `.env` after
  making a database backup.

## Threat model

### Boundary inventory

Added boundaries are local environment values entering Compose, the Docker
socket reached by the lifecycle CLI, network downloads during image build, and
HTTP requests entering the published web port. Widened boundaries are none.
Persistent named volumes cross from containers into Docker-managed host state.

### Actor model

The local operator and Docker daemon are trusted. Repository content and pinned
upstream artifacts are trusted only after review and checksum/digest validation.
Other LAN users and unrelated local containers are untrusted. Bugzilla remains
a development service, not an internet-facing production deployment.

### Controls

- Compose rejects missing secret variables; initialization generates them with
  a cryptographic RNG, mode 0600, and no log output.
- The host port binds only to IPv4 loopback. The database publishes no host port.
- The Bugzilla archive checksum and OCI image digests bind supply-chain inputs.
- Fixed Compose arguments are passed as argv, not evaluated shell text.
- Destructive operations use a Compose project name derived from the SHA-256 of
  the canonical checkout root, so sibling checkouts have disjoint resources.
  Exact confirmation variables and the per-project lock gate deletion; cleanup
  uses Compose project resources rather than global Docker prune commands.
- Diagnostics permit Compose to load `.env` only through quiet configuration
  validation. The lifecycle shell never sources or prints the file, command
  tracing stays disabled, the endpoint comes from `docker compose port`, and
  bounded status/log output must not contain the generated secret values.
- Named volumes provide persistence, not host confidentiality; their contents
  inherit Docker daemon access controls.

### Out of scope

TLS, production hardening, internet exposure, multi-user host isolation, backup
or restore automation, and credential rotation are outside this local test
substrate. Operators must back up any data they choose to retain before reset.

## Verification

The lifecycle test must first fail before implementation exists, then cover:
secret-file creation and mode; idempotent initialization; refusal when volumes
outlive `.env`; cleanup without `.env`; no secret output; disjoint project
identities for two checkout roots; live, stale, and ownerless-lock refusal with
the exact manual recovery path and no automatic reclamation; loopback-only
Compose binding; separate volumes; digest and source
pins; health dependencies; non-destructive down; refusal and exact Compose argv
for reset and clean; cleanup's delete-after-Compose ordering; bounded doctor
diagnostics; and
URL-port consistency. `docker compose config --quiet` validates the rendered
stack with generated secrets. Build verification uses Buildx for
`linux/amd64,linux/arm64`. A live arm64 run must exercise `up`, HTTP readiness,
`doctor`, `down`, and confirmed reset. A lifecycle-specific GitHub Actions job
must assert its native x86_64 Ubuntu host architecture and exercise the same
live `up`, HTTP readiness, `doctor`, `down`, and confirmed-reset path. The CI run
therefore supplies the Linux amd64 image build and runtime proof; arm64 Buildx
verification remains a local release guardrail.

## Durable workflow context

- Branch: `feat/bugzilla-container-lifecycle-2`
- Base branch: `main`
- Host architecture: arm64; target architectures: arm64 and x86_64; relationship:
  included.
- Guardrails at design time: none existed. This change introduces
  `make test`, `make check`, and `make build-multiarch`.
