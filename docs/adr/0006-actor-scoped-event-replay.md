# 0006. Journal-owned reconciliation for actor-scoped event replay

## Status

Accepted (2026-09-01)

## Context

Issue #6 needs the host runner to execute a validated scenario's ordered, actor-scoped
events against the disposable Bugzilla fixture and to survive an interruption without
duplicating a bug, comment, attachment, or work-time entry. ADR 0002 already fixed the
journal record shape — `InFlightRecord`, `CompletedRecord`, the recovery classes
`unique-create` / `idempotent-set` / `append`, the `next_safe_action` vocabulary
`advance | reconcile | retry | stop`, and the rule that a new attempt requires the prior
attempt to have recorded `retry`. ADR 0004 fixed the mutation boundaries and the actor key
store. What is undecided is how a run *uses* that journal: when reconciliation runs, what
authorizes adopting a result the server already holds, and what happens to a payload the
authorized boundary cannot express in one mutation.

Three facts about the boundary constrain the answer, each read from bzr at `b80303b7`:

- `bug create --from-json` sets `deny_unknown_fields` and defines no `estimated_time`,
  `remaining_time`, `dupe_of`, or `cf_*` key; `bug update` defines no `--version`.
- `bzr bug view` serializes no `groups` field, so a declared group set on an existing bug
  cannot be read back.
- `bug view` accepts aliases as well as numeric IDs, so a scenario-namespaced server alias
  is a server-enforced unique handle a create can be reconciled against.
- bzr documents `op_sys` and `rep_platform` as "required by some Bugzilla installations"
  (`src/cli/bug/create.rs:138,141`) and passes both on every functional create, and this
  fixture's `containers/bugzilla/checksetup_answers.txt` sets no `defaultplatform` or
  `defaultopsys` answer. The scenario contract has no slot for either — the loader's
  `bug.create` postcondition key set (`src/bzr_live/scenario/journal.py:227-234`) does not
  carry them — so a scenario cannot supply them even if it wanted to.
- Bug aliases are enabled on this fixture: `checksetup_answers.txt` sets
  `$answer{'usebugaliases'} = 1`. That matters because bzr's own functional containers run
  with them off — `tests/functional/phases/08c-bugs-create-fields.sh:8-9` says "bug aliases
  are disabled on these default-config containers, so the field silently no-ops" — so the
  parameter, not the client, is what makes the alias round-trip available here.
- A `bug view` of a bug that is absent, or that the caller may not see, exits **4**, not 2.
  bzr's functional suite asserts exit 4 with `api_code` 101 for a missing numeric ID and
  exit 4 with `api_code` 102 for a group-restricted bug, both against a live Bugzilla
  (`tests/functional/phases/08e-bugs-restricted-access.sh:24-25,289-292` in the bzr
  checkout); the matching alias code is 100. `BzrClient.read` reports absent only for
  `api_code` 51, 105 or 106 — the product and component codes issue #4 needed — so as it
  stands it raises on all three.

## Decision

**Reconciliation resolves before the completed record is written; `reconcile` is never
persisted.** A run writes the in-flight intent, invokes the boundary, and then writes
exactly one completed record whose `next_safe_action` is `advance`, `retry`, or `stop`. A
bzr exit of 0 with parseable output resolves to `advance` directly. Anything else — a
non-zero exit, an unparseable reply, a partial batch result — triggers a reconciliation
read against Bugzilla, whose answer supplies the value. If the reconciliation read itself
fails, no completed record is written: the in-flight record survives and a later `resume`
reconciles it. Persisting `reconcile` would deadlock the event, because ADR 0002 admits a
second attempt only after a recorded `retry`.

**Absence is read through a widened boundary client, and inaccessibility is not absence.**
`BzrClient.read` gains a keyword `absent_codes` defaulting to today's `{51, 105, 106}`, so
provisioning is unchanged; replay passes `{100, 101}` for every bug lookup. Code 102 stays
unmatched and raises, because a bug the actor may not see is not a bug that is not there,
and treating the two alike is what would let replay create a duplicate.

**Reconciliation is keyed on the recovery class.** A `unique-create` reconciles by reading
its server alias: present adopts the returned ID, absent proves no commit and records
`retry`. An `append` reconciles by searching the target's comments or attachments for its
namespaced marker: exactly one match adopts, zero proves no commit, two or more is
ambiguous and records `stop`. An `idempotent-set` reconciles by reading the target and
comparing the declared postcondition: a match adopts, anything else records `retry`,
because re-applying the whole declared set converges. Fields the read surface does not
expose cannot confirm a match, so they resolve to `retry` and are re-applied; that is one
extra idempotent invocation, never a duplicate.

**Adopting a server-side result requires a journal record proving this run attempted it, and
the check binds every first execution rather than every `replay`.** An event with no journal
record has no such proof under *either* subcommand, so its server alias is read and its
presence refuses, wherever that event is reached from. `replay` additionally sweeps every
`bug.create` alias up front, before any mutation, and refuses outright if the journal
directory holds any record at all — the same check in its fail-fast form. Binding it to the
command instead of to the event would let `resume` against an empty journal perform a
first-time replay with no baseline proof, and, worse, adopt a pre-existing bug as its own
create the moment the duplicate alias sent it into reconciliation. That sweep is what
"replay begins from the keyed pristine baseline" means operationally; restoring the baseline
stays the operator's `scripts/checkpoint restore pristine`.

**Every create sends a fixed `op_sys` and `rep_platform`.** The contract has no slot for
them and the fixture declares no defaults, so replay sends `Linux` and `PC` — the pair bzr's
own functional fixtures use against stock Bugzilla — rather than omitting fields the
installation may require. Scenarios cannot vary them; nothing in the contract could express
the variation anyway.

**Markers are rendered only for append-class events.** A comment body gains a trailing
`[<marker>]` line; a work-time update carries the same marker in the comment posted in the
same `bug update` call; an attachment's description gains `[<marker>] sha256=<hex>`. A set
reconciles by comparison, so appending a marker to it would make the observed value differ
from the declared one.

**A payload the boundary cannot express in one mutation is refused before any mutation.** A
per-action supported-payload table runs with the pristine sweep and refuses `bug.create`
carrying `estimated_hours`, `remaining_hours`, `duplicate_of`, `custom_fields`, or a null
`version`; `bug.update` carrying `groups`, `version`, a null `resolution`, a null
`milestone`, a null `duplicate_of`, or `duplicate_of` together with either `status` or
`resolution`; and any attachment description whose rendered summary would exceed 255 **bytes**
once its marker is appended. Each refusal names the field and the scenario edit that resolves
it. Two of those grounds are narrower than they look and are stated as they are: a null
`version` is refused not because Bugzilla rejects it but because bzr silently defaults it to
`"unspecified"`, a version the fixture's provisioned product does not declare; and the
attachment ceiling is `attachments.description`, which Bugzilla 5.2 declares `TINYTEXT`
(`Bugzilla/DB/Schema.pm`) — 255 bytes, so the check counts encoded bytes, not code points.

## Consequences

- An interrupted run resumes to exactly one semantic result per event or refuses with a
  reset/replay instruction. An append is never repeated: its marker either proves the
  commit or proves its absence.
- An event that reconciles to `stop` is terminal in the journal. A later `resume` reads the
  recorded `stop` and refuses again without re-querying, so the refusal is durable rather
  than dependent on the server still looking ambiguous.
- Within one run an event invokes a boundary at most once. A reconciliation that resolves to
  `advance` means the mutation committed despite the reply, so the run continues; `retry` and
  `stop` abort it. Continuation is `resume`'s job, which keeps retry policy out of the engine
  and in the operator's hands.
- The scenario contract is now wider than the replay engine: a loader-valid scenario can be
  unreplayable. The refusal is deterministic, pre-mutation, and names its fix, but issue #3's
  validation no longer implies replayability, and a future bzr release that gains the missing
  create fields would shrink the table.
- The pristine sweep costs one read per `bug.create` event before the first mutation, and
  rebuilding the ID resolution table costs one `JournalStore.read` per event. That read lists
  the journal directory each time, so table rebuild is quadratic in event count — acceptable
  at fixture scale (hundreds of events), and the reason this design does not target
  million-event scenarios.
- The sweep reads with each create's own actor credential rather than the admin key, so no
  admin key or `.env` parsing enters the replay CLI. A pre-existing bug hidden from that
  actor by a group answers `api_code` 102, which is not in `absent_codes`, so the sweep
  raises and `replay` refuses before mutating anything. That is the intended outcome, and it
  is reached by the read failing rather than by any reconciliation: the run stops with the
  boundary's own message rather than treating an invisible bug as an absent one.

- Two of this record's boundary facts are inferred from the fixture's configuration rather
  than observed against it: that a create omitting `op_sys`/`rep_platform` would be rejected,
  and that the `alias` key round-trips. The unit suite mocks the subprocess boundary and
  cannot reach either, so `tests/replay_smoke.sh` — an operator-run live proof beside
  `tests/provision_smoke.sh`, the split ADR 0004 already chose — is what discharges them.
  Until it has run, both are stated as inferences here rather than as verified grounds.
- Widening `BzrClient.read` is a change to a boundary client issue #4 owns. The new argument
  is keyword-only with today's set as its default, so no provisioning call site changes, but
  the two issues now share one not-found contract rather than one code set.

## Considered & rejected

- **Refuse every in-flight record on resume and tell the operator to reset.** judgment: the
  cheapest design consistent with `AGENTS.md` ("Crash consistency is out of scope … A broken
  fixture is fixed by rerunning, resetting, or recreating it"), and it satisfies the goal's
  own refusal arm — rejected because issue #6's completion criterion requires an interrupted
  run to *adopt* a committed result rather than force a full reset, which is what keeps a
  multi-hundred-event scenario replayable at all.
- **Treat any failed bug read as absent.** verified: a group-restricted bug answers exit 4
  with `api_code` 102, asserted against a live Bugzilla at
  `tests/functional/phases/08e-bugs-restricted-access.sh:24-25` in the bzr checkout; collapsing
  it into absence would let the pristine sweep pass and the create then duplicate.
- **Persist `next_safe_action: reconcile` and let `resume` resolve it.** verified: ADR 0002's
  retry transition, implemented at `src/bzr_live/scenario/journal.py:436-437`, admits attempt
  *n+1* only when attempt *n* recorded `retry`, and `replace_completed` requires a matching
  in-flight record — so an event completed as `reconcile` can neither be retried nor
  rewritten.
- **Reconcile creates by searching for the declared summary instead of a server alias.**
  verified: `bzr bug view` resolves an alias, per `src/cli/bug/view.rs:59-62` ("Bug ID(s) or
  alias(es)") at bzr `b80303b7`, and this fixture enables aliases
  (`checksetup_answers.txt`, `usebugaliases = 1`). judgment: summaries are neither unique nor
  enforced, so a summary search cannot answer "did *this* create commit". The remaining leg —
  that the `alias` key in `bug create --from-json` lands on this server rather than silently
  no-opping as it does on bzr's alias-disabled containers — is asserted, not observed here;
  `tests/replay_smoke.sh` is what proves it, and until that smoke has run this decision rests
  on the parameter above.
- **Reconcile creates by a namespaced marker in the bug's description or whiteboard.**
  judgment: the one alternative that survives the alias silently no-opping, and it is the
  fallback if the smoke shows that happening — rejected for now because it turns a
  server-enforced unique identity into a text search with the append class's ambiguity
  problem, on the one recovery class that currently has none.
- **Let replay restore the pristine checkpoint itself.** judgment: operator decision on this
  issue, taken to keep the engine free of Docker orchestration; `AGENTS.md` scopes this
  repository to a disposable fixture whose recovery is rerun/reset.
- **Execute an unsupported create field as a follow-up second call inside the same event.**
  verified: epic #1 requires "one observable mutation per JSONL event unless a scenario
  explicitly targets a bzr compound operation", and `CompletedRecord` carries exactly one
  `invocation` and one `expected_postcondition`
  (`src/bzr_live/scenario/journal.py:384-397`), so a half-applied two-call event has no
  representable record.
- **Send only the supported subset and drop the rest.** judgment: the fixture would diverge
  silently from its declared postcondition, which is the failure this issue exists to prevent.
- **Apply a declared `groups` set on update as adds only.** verified: `bzr bug view`
  serializes no `groups` entry (`src/types/bug.rs:200-238` at bzr `b80303b7`), so no delta can
  be computed and no read-back can confirm the declared set; converging on a superset would
  silently diverge.
- **A separate reconciliation index beside the journal.** judgment: a second source of truth
  for what the journal records already answer, and one more file to keep consistent with it.
- **Auto-retry a failed event inside the same run.** judgment: an unbounded loop against a
  permanently failing event, and the bound that fixes it is a policy the operator should set
  by choosing to run `resume`.
