# Bugzilla resource provisioning design

Issue: [#4](https://github.com/randomparity/bzr-live/issues/4). Decision record:
[ADR 0004](../../adr/0004-bugzilla-resource-provisioning.md).

## Goal

Provision every entry in `ValidatedScenario.resource_plan` into the disposable local
Bugzilla fixture, in plan order, reconciling by stable name so an identical rerun is a
reported no-op, failing clearly when an existing resource differs in a declared field,
and writing each fake actor's API key to an owner-readable file under a configured
local state root. Declared custom-field definitions and legal values must afterwards be
observable through bzr.

This is fixture tooling for a throwaway local test server. Reliability of provisioning
matters; production-grade secret management, crash consistency, and hostile-local-user
protection are explicit non-goals (issue #4 Non-goals).

## Architecture

A new host-side package `bzr_live.provision` (outside `bzr_live.scenario`, which per
ADR 0002 performs no mutation). It consumes a `ValidatedScenario` from
`bzr_live.scenario.load_scenario` and drives three fixed mutation boundaries:

| Boundary | Used for | Mechanism |
|---|---|---|
| `bzr` | actor, group, product, component (writes and reads); custom-field legal-value readback | subprocess: caller-supplied bzr argv prefix + `--json --server-url <url> --server-api-key-env BZR_LIVE_API_KEY <verb ...>` |
| `bridge` | version, milestone, custom-field, keyword, flag-type (create + read where bzr has no read), API-key creation | subprocess: `docker compose ... exec -T --user www-data bugzilla bzr-live-bridge <operation>` with a JSON request on stdin and a JSON reply on stdout |
| `bugzilla-rest-custom-field` | per-bug custom-field assignment only | `urllib.request` `PUT /rest/bug/<id>` with `api_key` header; no other REST route |

The boundary names match the journal contract's `_BOUNDARIES` vocabulary
(`bzr`, `bugzilla-rest-custom-field`) plus the container-local bridge. Routing is a
static table from resource kind to boundary; the routing test asserts the whole table.

### Files

- `src/bzr_live/provision/__init__.py` — public exports.
- `src/bzr_live/provision/adapters.py` — `BzrClient` (bzr subprocess seam),
  `BridgeClient` (compose-exec subprocess seam), `assign_bug_custom_fields` (narrow
  REST call), the kind→boundary routing table, and the compose project-name
  derivation (`bzr-live-` + first 12 hex of SHA-256 of the resolved checkout path,
  identical to `scripts/lifecycle`).
- `src/bzr_live/provision/keys.py` — `ActorKeyStore`: state-root key directory.
- `src/bzr_live/provision/executor.py` — two-pass reconciliation over
  `resource_plan`, comparison semantics, report.
- `src/bzr_live/provision/__main__.py` — CLI entry point.
- `containers/bugzilla/bridge.pl` — the fixed-operation Perl bridge, installed by the
  Dockerfile at `/usr/local/bin/bzr-live-bridge` (mode 0755).
- `tests/fixtures/provision-scenario/` — a scenario declaring at least one resource of
  every supported kind, used by unit tests and the live smoke.
- `tests/test_provision.py` — focused unit tests with fake adapters.
- `tests/provision_smoke.sh` — live two-run Docker proof (local; see CI note).
- `AGENTS.md` (+ `CLAUDE.md` symlink) — operator-directed repository scope note:
  bzr-live is a temporary loopback-bound test fixture; ordinary local file hygiene is
  the whole secret story; production-grade infrastructure is scope overreach.
- `README.md` and `.gitignore` — document the conventional `./state/` state root and
  ignore it.

### CLI

```
uv run python -m bzr_live.provision SCENARIO_DIR \
  [--state-root PATH]      # default ./state
  [--base-url URL]         # default http://127.0.0.1:8080/
  [--bzr PATH]             # bzr executable path, default "bzr"
  [--project-root PATH]    # checkout root for compose project derivation, default CWD
```

Exit 0 on success (all resources created or unchanged), non-zero with a one-line
actionable error on conflict or failure. Ordinary output is a per-resource line
(`created product:q4-checkout` / `unchanged product:q4-checkout`) and a summary count.
API keys never appear in ordinary output, errors, or logs.

`--bzr` is the explicit `bzr_command` seam: the operator points it at the unreleased
bzr candidate binary (issue #4 trajectory authorization) or a released bzr.

## Two-pass flow

**Pass 1 — read and compare.** For every `resource_plan` entry, read the current
fixture state through the boundary that owns the kind and classify it:

- `absent` — no resource with the stable name exists.
- `unchanged` — a resource exists and every declared field matches.
- `divergent` — a resource exists and a declared field differs → raise
  `ProvisionConflictError` naming the resource identity, the field, the declared
  value, and the observed value. The run stops before any mutation in pass 2.

**Pass 2 — create and read back.** For each `absent` entry, in `resource_plan` order
(already topologically sorted by the loader), create the resource through its write
boundary, then read it back through its read boundary and require every declared field
to match. A readback mismatch is a hard failure naming the resource and field.

Pass 1 runs to completion before pass 2 starts, so a divergent pre-existing resource
is reported before the run mutates anything. Within one run that ordering is exact; a
concurrent mutator is out of scope (single local operator, issue Non-goals).

A partially provisioned fixture (earlier run stopped mid-way) needs no special state:
pass 1 simply classifies the already-created prefix `unchanged` and the remainder
`absent`, and pass 2 finishes the job.

### Reads per kind

| Kind | Read | Write |
|---|---|---|
| group | `bzr group view NAME` | `bzr group create --name --description` |
| actor | `bzr user search EMAIL --details` | `bzr user create --email --full-name` then `bzr group add-user` per declared group |
| product | `bzr product view NAME` | `bzr product create --name --description` |
| component | `bzr component view PRODUCT NAME` | `bzr component create --product --name --description --default-assignee` |
| version | from `bzr product view PRODUCT` (`versions`) | bridge `create-version` |
| milestone | from `bzr product view PRODUCT` (`milestones`) | bridge `create-milestone` |
| custom-field | bridge `get-custom-field` (type + values), plus `bzr field list CF_NAME` readback for legal values | bridge `create-custom-field` |
| keyword | bridge `get-keyword` | bridge `create-keyword` |
| flag-type | bridge `get-flag-type` | bridge `create-flag-type` |

One `product view` read is reused for the product's own comparison and for its
versions/milestones, cached per run.

### Comparison semantics

Only declared scenario fields are compared; every other server-side attribute is
fixture noise and ignored.

- Emails (actor identity, component `default_assignee`) compare case-insensitively —
  Bugzilla treats logins case-insensitively.
- `actor.groups`: the declared groups must each be present in the user's group
  memberships; extra server-side groups (e.g. every user's implicit defaults) are
  ignored. A pre-existing user missing a declared group is divergent.
- Custom-field names map deterministically to Bugzilla names:
  `cf_` + slug with `-` replaced by `_` (`risk-level` → `cf_risk_level`). Declared
  `field_type` maps text→`FREETEXT`, single-select→`SINGLE_SELECT`,
  multi-select→`MULTI_SELECT`. Declared `values` must equal the field's legal values
  as a set, ignoring Bugzilla's built-in `---` placeholder that single-select fields
  always carry.
- `flag-type`: declared `target`, `description`, and the product/component inclusion
  set are compared; sort keys and grant flags are not declared and not compared.
- Version and milestone comparison is existence within the declared product (they
  declare no other fields).
- Actor display names, group/product/keyword/component descriptions compare exactly.

## Actor API keys

`ActorKeyStore(state_root)` manages `<state_root>/actor-keys/`:

- creates `state_root` and `actor-keys/` with mode 0700 (and requires 0700 plus
  current-user ownership when they already exist);
- one file per login: `<actor-name>.key` holding the raw key and a trailing newline,
  written with `os.open(..., O_CREAT | O_EXCL, 0o600)` then a same-directory atomic
  rename — ordinary local fixture hygiene, nothing more (issue boundary);
- the admin key is stored as `admin.key` alongside actor keys.

Key acquisition per login: if the key file exists, reuse it; otherwise call bridge
`create-api-key {login}` and write the file. The executor verifies the admin key once
per run with `bzr whoami`; an invalid stored key fails with a message naming the file
and suggesting a fixture/state-root mismatch (`make reset` or removing the state
root). Actor keys are written for the scenario's actors so later event execution
(issue #5/#6 scope) can authenticate independently; this issue only creates and
stores them.

Bootstrap order inside a run: the admin key is acquired at run start (file reuse or
bridge create), because every bzr read in pass 1 authenticates with it. Actor keys
are acquired only in pass 2, after the conflict gate: for every actor entry —
created or unchanged — the executor ensures the key file exists, minting via the
bridge only when it is missing. A conflict in pass 1 therefore stops the run before
any resource mutation and before any actor-key minting; a rerun after state-root
loss re-mints missing actor keys and converges. (Bugzilla permits multiple API keys
per user, so re-minting after file loss is safe.) Actors are created without a
password — the server-generated one is never delivered inside the fixture — and
authenticate via API key only.

## The Perl bridge

One script, fixed operations, container-local. Invoked as
`bzr-live-bridge <operation>`; the request object arrives on stdin as one JSON
document, the reply leaves on stdout as one JSON document
(`{"ok": true, "result": ...}` or `{"ok": false, "error": "..."}`, exit 0/1). The
operation name is the only argv-visible datum; secrets never enter argv.

Allowlisted operations — anything else exits 2 with `unknown operation`:

- `create-version {product, name}` — `Bugzilla::Version->create`
- `create-milestone {product, name}` — `Bugzilla::Milestone->create`
- `create-custom-field {name, field_type, values[]}` — `Bugzilla::Field->create`
  (`custom => 1`), then `Bugzilla::Field::Choice` per legal value for select types
- `create-keyword {name, description}` — `Bugzilla::Keyword->create`
- `create-flag-type {name, description, target, products[], components{}}` —
  `Bugzilla::FlagType->create` with inclusions
- `create-api-key {login}` — `Bugzilla::User::APIKey->create`; the only operation
  whose reply carries a secret
- `get-custom-field {name}` / `get-keyword {name}` / `get-flag-type {name}` — read
  current definition or `{"ok": true, "result": null}` when absent; `get-flag-type`
  errors if more than one flag type carries the name (Bugzilla does not enforce
  uniqueness; the fixture contract does)

The bridge uses the Bugzilla Perl object layer exclusively — never raw SQL (issue
boundary). It runs `docker compose exec` as `www-data` so files it may touch under
`/var/www/html/data` keep Apache-compatible ownership. It loads Bugzilla from
`/var/www/html` and runs with `Bugzilla->set_user` to the admin account resolved from
the container's `BZ_ADMIN_EMAIL` environment (already present in the container per
`compose.yaml`), so object-layer permission checks run as the admin.

The exact Bugzilla module invocations are validated against the pinned Bugzilla source
(`BZ_SOURCE_SHA` in the Dockerfile) during implementation and proven by the live
smoke; the unit suite treats the bridge subprocess as a mocked boundary and asserts
the request/response protocol, allowlist bounds, and error mapping.

## REST custom-field adapter

`assign_bug_custom_fields(base_url, api_key, bug_id, values)` issues one
`PUT /rest/bug/<id>` with JSON body `{"cf_x": ...}` and the key in the
`X-BUGZILLA-API-KEY` request header, via `urllib.request`, raising on non-2xx or an
error body. It exists because the issue
charter routes per-bug custom-field assignment through stock REST; resource
provisioning itself never calls it (bugs are event scope). Its tests cover routing,
request shape, and error mapping with a stubbed opener. No other REST route or verb is
implemented.

## Failure modes

- Conflict (`ProvisionConflictError`): divergent declared field, pass-1 stop before
  mutation; message carries identity, field, declared, observed.
- Boundary failure: non-zero bzr/bridge exit or malformed JSON reply → failure naming
  the boundary, the operation, and the resource; stderr from the child is included
  except for `create-api-key`, whose reply is never echoed.
- Readback mismatch after create: hard failure naming resource and field.
- State-root violations (wrong mode, wrong owner, symlink, non-regular key file):
  refusal with the path and the expected mode/ownership.
- Invalid stored admin key: refusal naming the key file and the recovery choice.

All failures exit non-zero with a single actionable message; partial provisioning is
recoverable by rerunning after the cause is fixed (pass 1 re-classifies).

## Testing

Unit tests (`tests/test_provision.py`, stdlib unittest, fake subprocess/HTTP
boundaries — mock the boundary, not the logic):

1. adapter routing — the full kind→boundary table, and that per-bug custom-field
   assignment routes to REST;
2. plan order — creates happen in `resource_plan` order across boundaries;
3. identical rerun — all-unchanged classification issues no writes;
4. divergent existing definition — conflict raised, no pass-2 mutation;
5. partial-run rerun — a prefix-provisioned fixture converges, only the missing
   suffix is created;
6. actor key files — 0700/0600 creation, reuse without re-creation, refusal on bad
   mode/ownership/symlink;
7. bridge operation bounds — the client only emits allowlisted operations; unknown
   operation and error replies map to failures; `create-api-key` replies never reach
   ordinary output or exception text;
8. custom-field readback — declared values compared against `bzr field list` output,
   `---` placeholder ignored, name mapping applied;
9. REST adapter — request shape and error mapping;
10. compose project-name derivation matches `scripts/lifecycle` (same hash, same
    prefix/truncation);
11. CLI argument handling and report formatting (keys absent from output).

Live proof (`tests/provision_smoke.sh`): from a fresh `make up` fixture, run the CLI
twice against `tests/fixtures/provision-scenario`; the first run must report every
resource `created`, the second must report every resource `unchanged`; then verify
one custom field's legal values through `bzr field list`. Requires a bzr binary supplied via `BZR_LIVE_BZR` (the unreleased
candidate until the fix ships in a release).

**CI note:** the scenario-contract workflow runs the unit tests as-is. The live smoke
is local-only for now: GitHub runners have no bzr candidate binary, and pinning an
unreleased bzr into CI is out of scope. Wiring the smoke into the container-lifecycle
workflow is deferred until a bzr release contains the `default_assigned_to` fix; the
PR records this deferral.

## Threat model

Proportionate to a disposable local fixture with fabricated data (issue Non-goals:
no hostile-local-user protection, no production secret handling).

**Boundaries added.** (1) host→bzr subprocess; (2) host→container bridge subprocess;
(3) host→Bugzilla REST (one route); (4) state-root key files.

**Actors.** The local operator (trusted, owns the fixture and every credential in
it); other local OS users (out of scope beyond ordinary 0700/0600 modes per issue);
the fixture's fabricated accounts (not adversaries). No network exposure beyond the
existing loopback-published port.

**Controls per boundary.**

1. bzr subprocess: argv is built from validated `ValidatedScenario` values (loader
   already constrains slugs/emails) and fixed flags; the API key passes via
   environment (`--server-api-key-env`), never argv; `shell=False`.
2. bridge subprocess: fixed argv (operation name from a closed set); all data rides
   JSON stdin, so no shell or Perl interpolation of scenario values; the container
   already holds admin credentials, so the bridge adds no new secret exposure;
   `shell=False`.
3. REST: single hard-coded route template; the bug id is an `int`; body is
   json-encoded; the key travels in the `X-BUGZILLA-API-KEY` header over loopback
   HTTP (the fixture's existing transport).
4. Key files: 0700 directory, 0600 files, owner check, `O_NOFOLLOW`/`O_EXCL` on
   create; keys excluded from stdout/stderr/exceptions.

**Out of scope.** Encryption at rest, rotation, ACL/inode hardening beyond modes,
TLS for loopback HTTP, concurrent-writer coordination, and PID-reuse recovery — all
excluded by the issue charter; the fixture's data is fabricated and transitory.

## Non-goals

Everything in the issue #4 Non-goals list, plus: no Makefile target (the README
documents the CLI invocation), no scenario schema change, no event execution, no
checkpoint integration (issue #5), no general REST client, no bzr configuration-file
management (stateless `--server-url` invocations only).

## Review deferrals

None carried from prior reviews; the ADR and this spec restart the design record for
the revised issue body.
