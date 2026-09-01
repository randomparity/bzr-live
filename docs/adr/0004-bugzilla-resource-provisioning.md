# 0004. Three-boundary two-pass Bugzilla resource provisioning

## Status

Accepted (2026-08-31)

## Context

Issue #4 requires provisioning every `ValidatedScenario.resource_plan` entry into the
disposable local Bugzilla fixture with stable-name reconciliation, clear failure on
divergent declared fields, and owner-readable local actor API-key files. ADR 0002 keeps
`bzr_live.scenario` mutation-free, so provisioning needs its own executor and mutation
boundaries. bzr (the authorized client) supports users, groups, products, and
components but has no create surface for versions, milestones, custom-field
definitions, keywords, flag types, or API keys, and the issue forbids raw SQL and
general REST administration. A prior implementation was rejected as over-engineered for
a throwaway fixture; the revised issue body fixes the boundaries explicitly.

## Decision

A host-side `bzr_live.provision` package routes each resource kind to one of three
fixed boundaries: `bzr` subprocess invocations (stateless `--server-url`, key via
environment) for users, groups, products, and components; one fixed-operation
container-local Perl bridge (`docker compose exec`, JSON over stdin/stdout, closed
operation allowlist, Bugzilla object layer only) for the kinds bzr cannot create and
for API-key minting; and a single stock-REST route for per-bug custom-field
assignment. Reconciliation is two passes over the already-ordered `resource_plan`:
read and compare every declared definition (divergence stops the run before any
resource mutation or actor-key minting; only the admin API-key bootstrap precedes
pass 1), then create what is absent in plan order and read each creation back. Actor
and admin API keys live in `<state-root>/actor-keys/` as 0600 files in a 0700
owner-checked directory; an existing key file is reused, and keys never appear in
ordinary output.

## Consequences

- An identical rerun is a verified no-op, and a partial run converges on rerun; the
  fixture answers existence. The key files are the one piece of durable local state:
  a fixture recreated under a kept state root holds stale keys — the run-start
  `whoami` verification catches a stale admin key; stale actor key files surface only
  when event execution first uses them, or when the operator resets the state root.
- A divergent pre-existing resource is detected before the run performs any resource
  mutation or actor-key minting; concurrent mutators are unhandled by design (single
  local operator).
- The bridge is a second implementation surface inside the container image, bounded
  by its allowlist; extending provisioning to a new kind means touching the routing
  table and possibly the bridge.
- Custom-field slugs gain a deterministic `cf_`/underscore name mapping that later
  event execution must share.
- The unit suite mocks the subprocess/HTTP boundaries; proof that the bridge's
  Bugzilla API calls work belongs to the live Docker smoke, which needs a bzr binary
  carrying the `default_assigned_to` fix (unreleased candidate until then), so CI
  runs only the unit suite for now.

## Considered & rejected

- **Raw SQL into MariaDB for the unsupported kinds.** verified: the issue body's
  "Required implementation boundaries" states "Never use raw SQL"; source: GitHub
  issue `randomparity/bzr-live#4`.
- **General Bugzilla REST administration client.** verified: the issue body restricts
  REST to per-bug custom-field assignment and lists general REST administration as a
  non-goal; source: GitHub issue `randomparity/bzr-live#4`.
- **Create-first, catch "already exists" instead of read-first.** verified: the issue
  body's "Required implementation boundaries" mandates the chosen alternative — "Use a
  straightforward two-pass flow: read and compare all declared definitions, then create
  missing resources in plan order and read them back"; source: GitHub issue
  `randomparity/bzr-live#4`.
- **A durable local reconciliation ledger for idempotency.** verified: the issue
  Non-goals exclude durable registries and transaction protocols, and the fixture
  read-back already answers existence; source: GitHub issue `randomparity/bzr-live#4`.
- **Storing actor keys in `.env` or one aggregate file.** judgment: per-actor files
  under one 0700 directory let later event execution read a single actor's credential
  without parsing an aggregate. (They also align with the runner-state directory shape
  proposed in ADR 0005, which is not yet accepted.)
- **Driving the bridge over HTTP instead of `docker compose exec`.** judgment: an HTTP
  admin endpoint inside the container would widen the fixture's network surface;
  `exec` keeps the bridge reachable only by the local operator who already owns the
  container.
