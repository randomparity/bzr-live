# AGENTS.md

Guidance for programming agents working in this repository.

## What is bzr-live?

A disposable, repeatable local Bugzilla test fixture: a pinned Bugzilla + MariaDB
Docker Compose stack plus Python tooling that populates it from versioned scenario
definitions so `bzr` (the Bugzilla CLI) can be exercised against live, realistic data.

## Project scope: temporary test fixture, not a production server

This is the single most important constraint in this repository, and past work has
been rejected for ignoring it. bzr-live is a throwaway fixture:

- Every account, credential, and datum in the fixture is fabricated and transitory.
  Nothing in it is worth protecting beyond ordinary local file hygiene.
- The server binds to 127.0.0.1 only. There is no remote attacker in the threat
  model, and no hostile local user either.
- Secrets (the generated `.env`, actor API keys) need owner-only file modes
  (0700 directories, 0600 files) and omission from ordinary command output —
  and nothing more. No encryption at rest, no rotation, no keychains, no
  credential-store contracts.
- Crash consistency is out of scope: no multi-file transaction protocols, no fsync
  choreography, no PID-reuse recovery, no custom subprocess supervision. A broken
  fixture is fixed by rerunning, resetting, or recreating it (`make reset`,
  `make clean`).
- Reliability that matters: deterministic validation, idempotent reruns, and clear
  actionable failures. Build those; skip the rest.

When in doubt, prefer the simple implementation a disposable fixture deserves.
Production-grade infrastructure here is scope overreach, not diligence.

## Build & verification commands

```bash
make up            # build and start the fixture (generates .env on first run)
make down          # stop it
make test          # shell lifecycle tests + Python unittest suite
make check         # bash -n, shellcheck, compileall, compose config
make checkpoint-smoke  # checkpoint save/restore round trip (Docker)
```

Python is 3.11+ via `uv`; the package under `src/bzr_live/` has no runtime
dependencies. ADRs live in `docs/adr/`; specs and plans in `docs/workflow/`.
