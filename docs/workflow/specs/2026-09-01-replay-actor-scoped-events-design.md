# Replay actor-scoped bzr events with safe resume

Design for issue [#6](https://github.com/randomparity/bzr-live/issues/6).
Decision record: [ADR 0006](../../adr/0006-actor-scoped-event-replay.md).
Depends on the merged contracts in [ADR 0002](../../adr/0002-versioned-scenario-contract.md)
(scenario and journal), [ADR 0004](../../adr/0004-bugzilla-resource-provisioning.md)
(mutation boundaries, actor key store) and [ADR 0005](../../adr/0005-cold-fixture-checkpoints.md)
(the reserved `pristine` checkpoint name).

## Goal

Execute a `ValidatedScenario`'s ordered, actor-scoped events against the local Bugzilla
fixture through `bzr` and the one authorized stock-REST route, journalling intent before
each mutation and completion after it, so an interrupted run resumes to exactly one
semantic result per event or refuses with an actionable message.

## Scope

In scope: the `replay` and `resume` commands and the execution and reconciliation of all
eight actions the merged loader defines.

The pristine precondition is checked per scenario, not globally: replay proves that *this*
scenario's bugs are absent, not that the fixture holds nothing at all. Residue from a
different scenario replayed under the same fixture is the operator's to clear with
`scripts/checkpoint restore pristine`, which is what they are told to run.

Out of scope, with owners: restoring the pristine checkpoint (operator, via
`scripts/checkpoint restore pristine`); `replay --through EVENT` (deferred, epic #1);
`load` and `verify` subcommands (epic #1); checkpoint internals (issue #5); scenario
validation, digest computation and journal record validation (issue #3); resource
provisioning (issue #4, reused unchanged).

## Architecture

A new `bzr_live.replay` package, sibling to `bzr_live.provision` and following its shape
(a thin `__main__` over an executor over stateless boundary clients).

```
scenario dir ──load_scenario──▶ ValidatedScenario ──┐
                                                    │
<state-root>/journal ──JournalStore─────────────────┼──▶ ReplayEngine ──▶ per-event
<state-root>/{admin.key,actor-keys/} ──KeyStore─────┘         │            ActionHandler
                                                              │                 │
                                                              ▼                 ▼
                                                     journal records     BzrClient (per actor)
                                                                         assign_bug_custom_fields
```

| File | Responsible for |
|---|---|
| `src/bzr_live/replay/__init__.py` | package exports |
| `src/bzr_live/replay/context.py` | `ReplayContext`: per-actor `BzrClient` cache, actor emails, the symbolic-reference → server-ID resolution table, asset materialization, and the pre-mutation payload-support check |
| `src/bzr_live/replay/actions.py` | one handler per action: build the invocation, parse the reply into resolved IDs, and reconcile the event against the server |
| `src/bzr_live/replay/engine.py` | `ReplayEngine`: preconditions, the ordered execution loop, journal reads and writes, and the resume decision table |
| `src/bzr_live/replay/__main__.py` | argument parsing and error-to-exit-code mapping |
| `tests/test_replay.py` | unit suite over mocked `subprocess.run` and URL opener |
| `tests/fixtures/replay-scenario/` | a scenario exercising all eight actions |

Every unit is testable in isolation: `ReplayContext` needs only a `ValidatedScenario` and a
`KeyStore`; a handler needs only a context and a `PlannedEvent`; `ReplayEngine` needs a
context, a `JournalStore` and a handler table.

## Commands

```
python -m bzr_live.replay replay <scenario_dir> [--state-root ./state]
                                 [--base-url http://127.0.0.1:8080/] [--bzr bzr]
python -m bzr_live.replay resume <scenario_dir> [same options]
```

Exit 0 on success, 1 on any `ReplayError` (message on stderr, prefixed `replay failed:`),
matching `bzr_live.provision.__main__`.

The journal lives in `<state-root>/journal/<scenario name>/`, a sibling of `admin.key` and
`actor-keys/`. Two reasons for that shape: `JournalStore` rejects any directory entry that
is not `.lock`, `.tmp-*`, or `<event>.<attempt>.json`, so it cannot share the key root; and
scoping by scenario name keeps two scenarios replayed under one state root from colliding on
a shared event name. The scenario name is a loader-validated slug, so it is a safe path
component.

## Preconditions

Checked in this order, before any mutation. Each failure is a `ReplayError` naming the
offending item and the fix.

1. **Digest binding.** Every journal record already present must carry
   `scenario_digest == scenario.digest`. A mismatch refuses: "the scenario changed since
   this journal was written; run `make reset` and replay, or restore the scenario".
2. **Payload support.** Every event is checked against the per-action supported-payload
   table below. Applies to both commands, so a scenario that cannot be replayed says so
   before it half-runs.
3. **`replay` only — empty journal.** The journal directory must hold no attempt file.
   Otherwise: "this scenario has already been replayed under <state-root>; use `resume`, or
   reset the fixture and remove the journal".
4. **`replay` only — pristine sweep.** For each `bug.create` event, `bzr bug view
   <server_alias>` under that event's own actor credential must report absent. A present
   alias refuses: "<alias> already exists in the fixture; replay requires the pristine
   baseline (`scripts/checkpoint restore pristine`)".

Preconditions 1–3 are local and run first; 4 is the only one that touches the network.

Asset integrity is not a precondition here because it cannot fail here: `load_scenario`
already verifies every asset's bytes against its declared SHA-256, and it copies that same
digest into each `bug.attach` postcondition's `asset_sha256`. The engine re-asserts the
equality where it materializes the file for upload, so a future change that decouples the two
fails loudly rather than uploading unverified bytes.

## Supported-payload table

The loader accepts payload shapes that the authorized boundary cannot express in one
observable mutation. Those are refused at precondition 2 (ADR 0006). Everything not listed
as refused is supported.

| Action | Sent as | Refused, with the message's suggested fix |
|---|---|---|
| `bug.create` | `bzr bug create --from-json <tmpfile>`, the file holding `alias` (the server alias), `product`, `component`, `summary`, `description`, `version`, `target_milestone`, `assignee`, `cc`, `keywords`, `groups`, `blocks`, `depends_on` | non-empty `custom_fields` → "move to a follow-up `bug.custom-field-set` event"; non-null `estimated_hours` / `remaining_hours` / `duplicate_of` → "move to a follow-up `bug.update` event"; null `version` → "Bugzilla requires a version on create" |
| `bug.update` | `bzr bug update <id>` with `--summary`, `--status`, `--resolution`, `--assignee` or `--reset-assigned-to`, `--dupe-of`, `--target-milestone`, `--estimated-time`, `--remaining-time`, and `--cc-add/-remove`, `--keywords-add/-remove`, `--blocks-add/-remove`, `--depends-on-add/-remove` computed as deltas against `bzr bug view` | `groups` → "set groups in the `bug.create` event"; `version` → "bzr `bug update` has no version field"; null `resolution` → "set `status` to an open status; Bugzilla clears the resolution"; null `milestone` → "bzr `bug update` cannot clear a milestone"; `status` together with `duplicate_of` → "bzr rejects `--status` with `--dupe-of`; use separate events" |
| `bug.comment` | `bzr comment add <id> --body-file=<tmpfile> [--private]` | — |
| `bug.attach` | `bzr attachment upload <id> <file> --summary=<description + marker + checksum> --content-type=<type> [--private]` | rendered summary longer than 255 characters → "shorten the attachment description" |
| `bug.worktime` | `bzr bug update <id> --work-time=<hours> --comment-file=<tmpfile>` | — |
| `bug.custom-field-set` | `assign_bug_custom_fields(base_url, actor_key, id, {cf_<slug>: value})` | — |
| `bug.flag` | `bzr bug update <id> --flag=<name><status>[(<requestee email>)]` | — |
| `attachment.update` | `bzr attachment update <id> --obsolete` / `--no-obsolete`, plus `--summary=<description>` when declared | — |

Deltas are computed against a fresh `bzr bug view` read taken immediately before the update,
because Bugzilla's list fields are edited by add/remove and not by assignment. The engine
assumes it is the only mutator, which `AGENTS.md` establishes for this fixture.

Two payloads are written to a mode-0600 temporary file rather than passed as an argument:
the `bug create` JSON, because `BzrClient` does not write to a subprocess's stdin, and any
comment body, because a realistic issue history can carry a body large enough to approach
`ARG_MAX`. Both live in the same `TemporaryDirectory` as materialized assets and are removed
with it.

## Marker rendering

The loader supplies `reconciliation_marker = "bzr-live:<scenario>:<event>"`. Append-class
events render it into server-visible text; set-class events do not, because they reconcile
by comparing the declared value.

- `bug.comment` body: `<declared body>` + `"\n\n[" + marker + "]"`.
- `bug.worktime` comment: `<declared comment>` + `"\n\n[" + marker + "]"`.
- `bug.attach` summary: `<declared description> [<marker>] sha256=<asset_sha256>`.

The marker is the loader's, so it is already namespaced by scenario and event name and
cannot collide with another event's marker or with anything a pristine fixture holds.

Because the rendered attachment summary is not the declared description, `bug.attach`
reconciles by marker containment, not equality.

## Execution

Events are walked in `scenario.events` order. For each, the journal's latest record decides:

| Latest record for the event | Action |
|---|---|
| none | write in-flight attempt 1, execute |
| completed, `next_safe_action == "advance"` | merge its `resolved_ids` into the table, skip |
| completed, `next_safe_action == "retry"` | write in-flight attempt *n+1*, execute |
| completed, `next_safe_action == "stop"` | refuse: the event was recorded as ambiguous |
| in-flight | reconcile (below); `advance` merges its IDs and moves on, `retry` writes in-flight attempt *n+1* and executes, `stop` refuses |

**One execution per event per run.** An event that this run has already invoked a boundary
for is never invoked again in the same run, whichever way its reconciliation resolved.
Continuation belongs to the next `resume`, so retry policy stays with the operator rather
than becoming a loop inside the engine. An in-flight or `retry` record inherited from an
*earlier* run has not been executed by this one, so resuming it is that event's first
execution here.

Executing an event:

1. Resolve the actor's API key from `KeyStore.actor_key(<actor name>)`. Absent → refuse:
   "no API key for actor <name>; run `python -m bzr_live.provision <scenario_dir>` first".
2. Write the `InFlightRecord` with `known_secrets={every key the run has loaded}`.
3. Invoke the boundary.
4. Exit 0 with a parseable reply → write the `CompletedRecord` with the observed
   `exit_status`, the reply as `handler_output`, the extracted `resolved_ids`, and
   `next_safe_action="advance"`.
5. Anything else → reconcile and write the `CompletedRecord` the reconciliation determines.
   `advance` means the mutation committed despite the reply, so the run merges its
   `resolved_ids` and moves to the next event. `retry` and `stop` abort the run with the
   boundary's message and, for `stop`, the ambiguity message.

If the reconciliation read in step 5 itself fails, no completed record is written and the
run aborts with that failure. The in-flight record survives for a later `resume`.

## Reconciliation

A reconciliation-derived `CompletedRecord` records `exit_status = -1`, meaning the
invocation's status was not observed, and carries the reconciliation read as
`handler_output`.

| Class | Read | `advance` | `retry` | `stop` |
|---|---|---|---|---|
| `unique-create` | `bzr bug view <server_alias>` | alias present; adopt its `id` | alias absent | reply present but carries no positive integer `id` |
| `append` | `bzr comment list <bug id>` (comment, worktime) or `bzr attachment list <bug id>` (attach) | exactly one entry contains the marker | no entry contains it | two or more do |
| `idempotent-set` | `bzr bug view <bug id>`, or `bzr attachment list <bug id>` for `attachment.update` | every readable declared value matches | any readable declared value differs, or the target is unreadable | — |

`idempotent-set` never yields `stop`: re-applying the whole declared set converges, so a
non-match is always safe to retry.

For `bug.update`, the comparable declared fields and the `bzr bug view` keys they are read
from are `summary`→`summary`, `status`→`status`, `resolution`→`resolution`,
`assignee`→`assigned_to`, `duplicate_of`→`dupe_of`, `milestone`→`target_milestone`,
`cc`→`cc`, `keywords`→`keywords`, `depends_on`→`depends_on`, `blocks`→`blocks`. Reference
values compare after resolution — an actor to its email, a bug to its numeric ID, a
milestone to its name. Three declared shapes have nothing to compare against and therefore
always resolve to `retry`: `estimated_hours` and `remaining_hours`, which `bzr bug view`
does not serialize, and a null `assignee`, whose `--reset-assigned-to` outcome is the
component default and so is not derivable from the declared value. Re-application is
idempotent, so the cost is one extra invocation.

For `bug.custom-field-set` the comparison reads `cf_<slug>` from the same `bzr bug view`
payload; for `bug.flag` it reads the `flags` array, matching on flag type name and status
(and requestee where declared); for `attachment.update` it reads the target attachment's
`is_obsolete` and, where declared, its `summary`.

## Identity resolution

`resolved_ids` keys are `"<kind>:<name>"`. A `bug.create` records `{"bug:<alias>": <id>}`; a
`bug.attach` records `{"attachment:<alias>": <id>}`; every other action records `{}`. The
table is rebuilt at start-up by walking `scenario.events` in order and merging the
`resolved_ids` of each completed-`advance` record, so a resumed run resolves the same
symbolic references the interrupted run did. `depends_on`, `blocks` and `duplicate_of`
resolve through it to numeric IDs; the loader already guarantees the referenced create
precedes the reference.

## Error handling

`ReplayError(Exception)` — one class, `str(exc)` is the operator-facing message, mirroring
`ProvisionError`. Every message names the operation, the offending input, and the fix.
`ScenarioValidationError` from the loader and `ProvisionError` from the boundary clients
propagate and are caught in `__main__` alongside `ReplayError`.

Distinct failure classes and what each says:

- missing actor key → run provisioning first;
- digest mismatch → reset and replay, or restore the scenario;
- non-empty journal under `replay` → use `resume`;
- pristine sweep hit → restore the pristine checkpoint;
- unsupported payload → the field and the scenario edit that resolves it;
- ambiguous append → "found <n> results matching <marker>; the fixture cannot be reconciled
  automatically — reset it (`CONFIRM_RESET=1 make reset`) and replay";
- recorded `stop` on resume → the same message, plus the event name.

## Threat model

Security-relevant because the change moves API keys across a subprocess boundary, builds
command arguments from scenario values, and parses replies it did not produce.

**Actor model.** One local operator on a loopback-bound disposable fixture. There is no
remote attacker and no hostile local user (`AGENTS.md`). The scenario files are authored by
that same operator and are already strictly validated by issue #3's loader before this code
sees them. The trust placed here is: the operator's own scenario files, the operator's `bzr`
binary, and the local fixture's replies.

**Boundaries.**

| Boundary | Direction | Control |
|---|---|---|
| Scenario files → engine (existing, not widened) | in | issue #3's loader: strict typing, unknown-field rejection, reference resolution, asset checksums |
| Key store → `bzr` subprocess (existing, reused) | out | `BzrClient` passes the key in `BZR_LIVE_API_KEY`, never in argv; `shell=False` |
| Key store → Bugzilla REST (existing, reused) | out | `assign_bug_custom_fields` puts the key in the JSON body, never the query string (ADR 0004) |
| Scenario values → `bzr` argv (**widened**: free text now reaches argv) | out | every option is built as a single `--name=value` token, so a value starting with `-` cannot be read as a separate flag; positionals go after `--`; `shell=False` |
| Boundary replies → journal and resolution table (**new**) | in | `json.loads` only; an adopted ID must be a positive integer, enforced by `CompletedRecord.__post_init__` |
| Engine → journal files (existing, reused) | out | `JournalStore` 0700 directory, 0600 records, `O_NOFOLLOW`, exclusive lock; `known_secrets` redaction on every write |
| Assets → temp file for upload (**new**) | out | materialized inside a `TemporaryDirectory` chmod 0700, file mode 0600, SHA-256 verified against the postcondition before upload, removed on exit |

**Out of scope.** Concurrent mutators of the same fixture (single local operator, and the
journal's exclusive lock already prevents two runs sharing a state root). Crash consistency
beyond the journal's existing atomic write and rename. A pre-existing bug hidden from the
sweeping actor by a group: the create then fails on the duplicate alias and reconciles to
ambiguous, so the outcome is a refusal, not a duplicate. Bugzilla's own authorization —
an actor lacking, say, `timetrackinggroup` gets a boundary error, which is correct.

## Testing

Unit tests only, mocking `subprocess.run` and the URL opener exactly as `tests/test_provision.py`
does. The live proof is the operator-run Docker path, consistent with ADR 0004.

Each of these is a case:

- full `replay` of the fixture scenario, all eight actions, asserting the argv and stdin of
  every invocation and the resulting journal records;
- crash before completion, then `resume`: create found by alias → adopted, not repeated;
- crash before completion, then `resume`: alias absent → `retry` recorded, attempt 2 executed;
- append reconciliation: one marker → adopted; zero → retried; two → `stop` recorded and
  refused, and a second `resume` refuses from the record without re-querying;
- idempotent-set reconciliation: postcondition matches → adopted; differs → retried;
- resolution table rebuilt across a resume, so a later event's `depends_on` resolves;
- digest mismatch on `resume` → refused;
- `replay` with a non-empty journal → refused;
- pristine sweep finds an existing alias → refused before any mutation;
- every refused row of the supported-payload table;
- attachment description exceeding 255 characters after marker rendering → refused;
- asset checksum mismatch → refused;
- an actor key never appears in any journal record or in any argv.

## Guardrails

`make check` and `make test` must pass. `tests/test_replay.py` is picked up by
`unittest discover -s tests`, which both the `scenario-contract` workflow and `make test`
run. The new ADR, spec and plan paths are added to the `scenario-contract` workflow's
`paths` lists so a change to them gates.
