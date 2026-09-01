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

## Purpose: prove `bzr`. Highlight its limits, do not work around them

This fixture exists to exercise `bzr`'s real write paths. When `bzr` cannot express
something a scenario legitimately declares, that gap **is the finding** — it is the
most valuable thing this repository produces. Surfacing it is the job; routing
around it silently destroys the evidence.

- **Never silently substitute.** Do not inject a value the scenario did not declare,
  swap one command for another, or drop a field `bzr` will not accept. A fixture that
  quietly compensates reports success while proving nothing.
- **Fail before mutating, and say why.** Where a payload cannot be executed, refuse it
  as a precondition and name the `bzr` limitation with its source citation — not a
  workaround instruction. "bzr's `bug create --from-json` has no `dupe_of` field" is a
  finding; "move it to a follow-up event" is a shrug.
- **Record every gap in `docs/bzr-findings.md`**, with the `bzr` source citation, the
  observed behaviour, and whether it is a defect or a deliberate design choice. File
  the defects as issues on `randomparity/bzr` and cross-link them. Ask the operator
  before filing; never file speculatively.
- **Fix the fixture in the fixture.** A Bugzilla install-configuration gap belongs in
  `containers/`, not in a client-side substitution. Adjust the fixture so the honest
  payload succeeds, rather than teaching the runner to send something else.
- **Authorized workarounds are named ones only.** The epic authorizes exactly two: the
  container-local Perl admin bridge for setup surfaces `bzr` does not offer, and one
  stock-REST route for per-bug custom fields. Anything else needs the same explicit
  authorization; do not invent a third.

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
