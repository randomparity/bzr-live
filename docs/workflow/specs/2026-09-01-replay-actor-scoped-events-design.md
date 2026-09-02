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
`scripts/checkpoint restore pristine`); `replay --through EVENT` (deferred, issue #17);
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
| `tests/replay_smoke.sh` | operator-run live proof: create succeeds, its alias round-trips, and a duplicate alias is rejected |
| `src/bzr_live/provision/adapters.py` | *changed*: `BzrClient.read` gains keyword-only `absent_codes` (see **Reading absence**) |
| `docs/bzr-findings.md` | already committed on this branch: the register of bzr limitations this work surfaced — the fixture's own deliverable |
| `containers/bugzilla/checksetup_answers.txt` | *changed*: `defaultplatform` and `defaultopsys`, so an honest create succeeds without runner-side substitution, plus `defaultpriority` corrected from `'--'` to Bugzilla's own `'---'` (the live run found the original rejects every create) |

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
   this journal was written; run `make reset` and replay, or restore the scenario". Records
   are read *through* current event names, so `resume` first lists the journal directory and
   refuses any record whose event the scenario no longer names — otherwise a renamed event
   makes its record unreachable and the binding silently vacuous for it.
2. **Payload support.** Every event is checked against the per-action supported-payload
   table below. Applies to both commands, so a scenario that cannot be replayed says so
   before it half-runs.
3. **Any first execution — pristine alias check.** Before executing an event that has no
   journal record, under *either* subcommand, that event's server alias is read and its
   presence refuses. Adoption of a server-side result requires a journal record proving this
   run attempted the event; an unjournalled event has no such proof whichever command reached
   it. Without this, `resume` against an empty journal performs a first-time replay with no
   baseline proof — and if the alias already exists, the create fails on the duplicate, the
   failure sends the event into reconciliation, and reconciliation adopts the pre-existing bug
   as this event's result. Every later event in the scenario would then mutate a bug the run
   did not create: a silent wrong-target write, not a refusal.
4. **`replay` only — empty journal.** The journal directory is listed once and must hold no
   file matching `<event>.<attempt>.json`, whatever the event name. Scanning only the current
   scenario's event names would let a journal whose events were since renamed read as empty,
   and the digest check cannot fire on a record no event name reaches. The message names the
   stray record: "this scenario has already been replayed under <state-root> (found
   <name>); use `resume`, or reset the fixture and remove the journal directory".
5. **`replay` only — pristine sweep.** The fail-fast form of check 3: every `bug.create`
   event's alias is read up front, before any mutation, rather than one event at a time as
   the run reaches it. A present alias refuses: "<alias> already exists in the fixture; replay
   requires the pristine baseline (`scripts/checkpoint restore pristine`)".

Preconditions 1, 2 and 4 are local and run first; 3 and 5 touch the network, and 3 is the
only one that runs during the loop rather than ahead of it.

### Reading absence

Both the sweep and the `unique-create` reconciliation need "is this alias absent?", and the
boundary does not answer it today. Observed behaviour, from bzr's own functional suite run
against live Bugzilla containers: `bzr --json bug view 999999999` exits **4** with
`api_code` 101, and a group-restricted bug exits 4 with `api_code` 102
(`tests/functional/phases/08e-bugs-restricted-access.sh:289-292` and `:169,180,262`
respectively, in the bzr checkout; the file's header comment at `:24-25` states the same
directions in prose, verified against Bugzilla 5.0.6 and run across 5.0/5.2/5.3). The alias form of the same
condition is `api_code` 100. `BzrClient.read` reports absent only for `api_code` 51, 105 or
106 — the product and component codes issue #4 needed — so every one of those raises.

So `BzrClient.read` takes a keyword-only `absent_codes`, defaulting to today's
`{51, 105, 106}`; replay passes `{100, 101}` for bug lookups. Code 102 is deliberately left
out: a bug the actor cannot see is not a bug that is not there, so the read raises and the
run refuses rather than sweeping past an invisible bug and creating a duplicate. This is the
one change this issue makes to `bzr_live.provision`, authorized by the operator during design
review; every existing call site keeps the default and is unaffected.

Asset integrity is not a precondition here because it cannot fail here: `load_scenario`
already verifies every asset's bytes against its declared SHA-256, and it copies that same
digest into each `bug.attach` postcondition's `asset_sha256`. The engine re-asserts the
equality where it materializes the file for upload, so a future change that decouples the two
fails loudly rather than uploading unverified bytes.

## Supported-payload table

The loader accepts payload shapes that the authorized boundary cannot express in one
observable mutation. Those are refused at precondition 2 (ADR 0006). Everything not listed
as refused is supported.

| Action | Sent as | Refused, and whose limitation the message names |
|---|---|---|
| `bug.create` | `bzr bug create --from-json <tmpfile>`, the file holding exactly what the scenario declared: `alias` (the server alias), `product`, `component`, `summary`, `description`, `version`, `target_milestone`, `assignee`, `cc`, `keywords`, `groups`, `blocks`, `depends_on` | `estimated_hours` / `remaining_hours` → bzr's create JSON has no such field, though Bugzilla accepts both ([G1]); `custom_fields` → bzr excludes `cf_*` from create by design ([G4]); `duplicate_of` → **Bugzilla's** own `Bug.create` has no `dupe_of`, so this one is not bzr's; null `version` → bzr silently substitutes `"unspecified"`, a version this fixture's products do not declare ([G9]) |
| `bug.update` | `bzr bug update <id>` with `--summary`, `--status`, `--resolution`, `--assignee` or `--reset-assigned-to`, `--dupe-of`, `--target-milestone`, `--estimated-time`, `--remaining-time`, and `--cc-add/-remove`, `--keywords-add/-remove`, `--blocks-add/-remove`, `--depends-on-add/-remove` computed as deltas against `bzr bug view` | `groups` → `bzr bug view` does not return `groups`, so no delta can be computed and no result confirmed ([D3]); `version` → bzr's `bug update` has no version flag, though Bugzilla accepts one ([G2]); null `milestone` → bzr offers `--reset-assigned-to` but no milestone reset ([G3]); null `resolution` → **Bugzilla** clears it on transition to an open status, so declare the status change instead; null `duplicate_of` → **Bugzilla** clears a duplicate through a status transition (`clear_resolution` calls `_clear_dup_id` and throws unless the bug is already open), and `Bug.update` types `dupe_of` as `int` with no null form, so declare that status change instead; `duplicate_of` with `status` **or** `resolution` → both flags carry `conflicts_with = "dupe_of"`, deliberately ([G5]) |
| `bug.comment` | `bzr comment add <id> --body-file=<tmpfile> [--private]` | — |
| `bug.attach` | `bzr attachment upload <id> <file> --summary=<description + marker + checksum> --content-type=<type> [--private]` | rendered summary longer than 255 **bytes** when UTF-8 encoded → **Bugzilla's** `attachments.description` is `TINYTEXT` (`Bugzilla/DB/Schema.pm:505`), not a bzr gap → "shorten the attachment description" |
| `bug.worktime` | `bzr bug update <id> --work-time=<hours> --comment-file=<tmpfile>` | — |
| `bug.custom-field-set` | `assign_bug_custom_fields(base_url, actor_key, id, {cf_<slug>: value})` | — |
| `bug.flag` | `bzr bug update <id> --flag=<name><status>[(<requestee email>)]` | flag-type name containing `+`, `-`, `?` or `X` → bzr's flag parser takes the first of those characters as the status, so the name is unaddressable ([D1]) |
| `attachment.update` | `bzr attachment update <id> --obsolete` / `--no-obsolete`, plus `--summary=<description>` when declared | — |

Every refusal on a **bzr** limitation names it and links its entry in
[`docs/bzr-findings.md`](../../bzr-findings.md), which carries the source citation, the class
— defect or deliberate design choice — and the upstream issue. The messages do not tell an
author how to route around bzr; the point of this fixture is that the gap stays visible.
Four rows above are not bzr's limitation at all — `duplicate_of` on create, a null
`resolution`, a null `duplicate_of` on update, and the attachment ceiling. Each says whose
constraint it is instead of linking a register entry it has no business having, and each may
name what satisfies the Bugzilla constraint, because there is no bzr gap whose evidence a
workaround instruction would destroy. That permission keys off the *ground*, not off any
privileged row: a bzr-grounded refusal names the limitation, cites its register entry, and
stops there. Charging bzr for a constraint it did not impose corrupts the register as surely
as hiding a real gap does — an earlier revision made exactly that error on the two
`duplicate_of`/`resolution` clears, and the entry it produced (G8) was withdrawn once the
fixture's own Bugzilla image settled the question. The
table is expected to shrink as bzr closes gaps, and the scenario contract stays deliberately
wider than what bzr can execute — narrowing it would erase the evidence.

The create document carries **only** what the scenario declared. bzr documents `op_sys` and
`rep_platform` as "required by some Bugzilla installations", and this fixture declared no
`defaultplatform`/`defaultopsys`, so the honest fix is in the fixture:
`containers/bugzilla/checksetup_answers.txt` sets both, and the runner substitutes nothing.
Injecting a fixed pair into the create document would have been a value the scenario never
declared, sent so the run would appear to succeed — the shape `AGENTS.md` forbids.

The `bug.flag` refusal is a **bzr defect**, not a scenario error. Resource names are slugs
(`[a-z][a-z0-9-]{0,62}`), `Provisioner._create_flag_type` registers the slug verbatim, and
Bugzilla permits hyphenated flag type names — bzr simply cannot address them. Replay refuses
before mutating, because the alternative is a mid-run failure whose only fix changes the
scenario digest and forces a full reset; the refusal cites [D1] so the defect is recorded
rather than absorbed.

[D1]: ../../bzr-findings.md#d1
[D3]: ../../bzr-findings.md#d3
[G1]: ../../bzr-findings.md#g1
[G2]: ../../bzr-findings.md#g2
[G3]: ../../bzr-findings.md#g3
[G4]: ../../bzr-findings.md#g4
[G5]: ../../bzr-findings.md#g5
[G9]: ../../bzr-findings.md#g9

The attachment ceiling is `attachments.description`, declared `TINYTEXT` in Bugzilla 5.2's
`Bugzilla/DB/Schema.pm` — a MySQL 255-**byte** column. The check therefore measures
`len(rendered.encode("utf-8"))`; counting code points would let a non-ASCII description
through and fail at the server. The marker and checksum already spend about 90 bytes, so a
description's real budget is around 165.

"Fail at the server" was the expectation; the live smoke's first run showed otherwise, and
the correction makes the check more important rather than less. A 256-byte summary is
**accepted** (exit 0) and stored at exactly 255 bytes — Bugzilla truncates above the
database, which reports success, with the column `tinytext` and `sql_mode`
`STRICT_TRANS_TABLES`. Since the reconciliation marker lives in that summary, an
over-length summary would silently lose the handle append-class reconciliation matches on,
and the run would look like it worked. The refusal is what prevents that.

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
| `idempotent-set` | `bzr bug view <bug id>`, or `bzr attachment view <attachment id>` for `attachment.update` | every readable declared value matches | any readable declared value differs, or the target is unreadable | — |

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
(and requestee where declared) — except for status `X`, which clears the flag, so the
declared postcondition is the *absence* of any `flags` entry with that type name and absence
is what advances it; for `attachment.update` it reads `bzr attachment view
<attachment id>` and compares `is_obsolete` and, where declared, `summary`.

`attachment.update` reads by attachment ID rather than by listing a bug's attachments,
because the event carries no bug reference: the loader normalizes its payload to
`{attachment, obsolete, description?}` with the attachment as the postcondition target, and
`bzr attachment list` takes only a bug ID. `bzr attachment view <attachment id>` returns
`id`, `summary` and `is_obsolete` — every field the comparison needs — and
`context.resolve(values["attachment"])` already yields the ID it wants.

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
beyond the journal's existing atomic write and rename. Bugzilla's own authorization — an
actor lacking, say, `timetrackinggroup` gets a boundary error, which is correct.

A pre-existing bug the sweeping actor cannot see is **not** out of scope and does not
reconcile: `api_code` 102 is outside `absent_codes`, so the read raises and the run refuses
with the boundary's own message before mutating anything. The refusal comes from the read
failing, not from any reconciliation outcome — `unique-create` has no ambiguous arm.

## Testing

Unit tests mock `subprocess.run` and the URL opener exactly as `tests/test_provision.py` does.
Three facts are out of their reach because they are properties of the fixture rather than of
this code — that a create omitting `op_sys`/`rep_platform` would be rejected, that the
`alias` key round-trips rather than silently no-opping, and that Bugzilla enforces alias
uniqueness — so `tests/replay_smoke.sh` carries them: an operator-run live proof beside
`tests/provision_smoke.sh`, the same split ADR 0004 chose. It replays the fixture scenario
against a healthy `make up`, asserts that a create declaring no `op_sys`/`rep_platform`
succeeds on the reconfigured fixture, asserts that `bzr bug view <server_alias>` resolves to
the id that create returned, and then issues a second create declaring the same alias and
asserts it **fails** — one more step in a script that already has the fixture up and one bug
created, and the proof that a duplicate create lands in reconciliation rather than producing
two bugs under one alias. It also probes each
`docs/bzr-findings.md` entry marked *read from source* and **prints** the observed behaviour;
the operator transcribes the promotion from *read* to *observed* into the register as an
ordinary edit. The script never writes to the register — its entries carry classifications a
script cannot judge. CI runs the unit suite;
the smoke is operator-run by decision.

Each of these is a case:

- full `replay` of the fixture scenario, all eight actions, asserting the argv of every
  invocation and the resulting journal records. Argv is the whole of it: nothing writes to a
  subprocess's stdin, which is why the create document and comment bodies go to temp files;
- crash before completion, then `resume`: create found by alias → adopted, not repeated;
- crash before completion, then `resume`: alias absent → `retry` recorded, attempt 2 executed;
- append reconciliation: one marker → adopted; zero → retried; two → `stop` recorded and
  refused, and a second `resume` refuses from the record without re-querying;
- idempotent-set reconciliation: postcondition matches → adopted; differs → retried;
- resolution table rebuilt across a resume, so a later event's `depends_on` resolves;
- digest mismatch on `resume` → refused;
- `replay` with a non-empty journal → refused;
- pristine sweep finds an existing alias → refused before any mutation;
- every refused row of the supported-payload table — every `bug.create` row, every
  `bug.update` row including both `duplicate_of` conflicts and the null `duplicate_of`, and
  the `bug.flag` hyphenated-name row;
- a create document carries no field the scenario did not declare — in particular no
  `op_sys` or `rep_platform`;
- a `bug.flag` clear (`X`) reconciles to `advance` when no entry with that type name is
  present;
- attachment description whose rendered summary exceeds 255 bytes → refused, with a
  non-ASCII case so the byte-versus-character unit is pinned by a test;
- `BzrClient.read` with `absent_codes={100, 101}`: exit 4 with `api_code` 100 or 101 returns
  `None`; exit 4 with `api_code` 102 raises; the default set is unchanged for provisioning;
- pristine sweep against a group-restricted bug (`api_code` 102) → refuses rather than
  treating the bug as absent;
- a journal holding a record for an event the scenario no longer names → **both** `replay`
  and `resume` refuse;
- asset checksum mismatch → refused;
- a reconciliation read that itself fails writes no completed record and leaves the in-flight
  record readable for a later `resume`;
- `main()` maps a missing actor key to exit 1 with a `replay failed:` message on stderr, and
  puts the journal at `<state-root>/journal/<scenario name>/`;
- an actor key never appears in any journal record or in any argv.

## Guardrails

`make check` and `make test` must pass. `tests/test_replay.py` is picked up by
`unittest discover -s tests`, which both the `scenario-contract` workflow and `make test`
run. The new ADR, spec and plan paths are added to the `scenario-contract` workflow's
`paths` lists so a change to them gates.
