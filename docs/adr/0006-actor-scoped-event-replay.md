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

**Reconciliation is keyed on the recovery class.** A `unique-create` reconciles by reading
its server alias: present adopts the returned ID, absent proves no commit and records
`retry`. An `append` reconciles by searching the target's comments or attachments for its
namespaced marker: exactly one match adopts, zero proves no commit, two or more is
ambiguous and records `stop`. An `idempotent-set` reconciles by reading the target and
comparing the declared postcondition: a match adopts, anything else records `retry`,
because re-applying the whole declared set converges. Fields the read surface does not
expose cannot confirm a match, so they resolve to `retry` and are re-applied; that is one
extra idempotent invocation, never a duplicate.

**Adopting a server-side result requires a journal record proving this run attempted it.**
`resume` has that proof. `replay` does not, so before its first mutation it reads every
`bug.create` event's server alias with that event's own actor credential and refuses if any
is already present, and it refuses outright if the journal directory holds any record. That
sweep is what "replay begins from the keyed pristine baseline" means operationally.
`replay` verifies the precondition; restoring the baseline stays the operator's
`scripts/checkpoint restore pristine`.

**Markers are rendered only for append-class events.** A comment body gains a trailing
`[<marker>]` line; a work-time update carries the same marker in the comment posted in the
same `bug update` call; an attachment's description gains `[<marker>] sha256=<hex>`. A set
reconciles by comparison, so appending a marker to it would make the observed value differ
from the declared one.

**A payload the boundary cannot express in one mutation is refused before any mutation.** A
per-action supported-payload table runs with the pristine sweep and refuses `bug.create`
carrying `estimated_hours`, `remaining_hours`, `duplicate_of`, `custom_fields`, or a null
`version`; `bug.update` carrying `groups`, `version`, a null `resolution`, a null
`milestone`, or both `status` and `duplicate_of`; and any attachment description that would
exceed Bugzilla's 255-character summary column once its marker is appended. Each refusal
names the field and the scenario edit that resolves it.

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
  actor by a group would pass the sweep; the create then fails on the duplicate alias and
  reconciles to ambiguous, so the outcome degrades to a refusal, never a silent duplicate.

## Considered & rejected

- **Persist `next_safe_action: reconcile` and let `resume` resolve it.** verified: ADR 0002's
  retry transition, implemented at `src/bzr_live/scenario/journal.py:436-437`, admits attempt
  *n+1* only when attempt *n* recorded `retry`, and `replace_completed` requires a matching
  in-flight record — so an event completed as `reconcile` can neither be retried nor
  rewritten.
- **Reconcile creates by searching for the declared summary instead of a server alias.**
  verified: summaries are not unique and Bugzilla does not enforce them; the alias column is
  unique and `bzr bug view` resolves it, per `src/cli/bug/view.rs:59-62` ("Bug ID(s) or
  alias(es)") at bzr `b80303b7`.
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
