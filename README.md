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
