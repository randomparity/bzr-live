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

Seven facts about the boundary constrain the answer, and they are not all of one kind.
Four are bzr limitations read at `b80303b7`, each recorded with its class in
`docs/bzr-findings.md`. A fifth is read from bzr but is a capability rather than a
limitation, so it earns no register entry. The last two rest on this repository's own
`containers/bugzilla/checksetup_answers.txt` — a fixture-configuration gap and a fixture
parameter — and neither is a bzr limitation either:

- `bug create --from-json` sets `deny_unknown_fields` and defines no `estimated_time` or
  `remaining_time` (finding G1) and no `cf_*` key (G4, a deliberate upstream choice);
  `bug update` defines no `--version` (G2) and no milestone reset (G3). It also defines no
  `dupe_of`, but neither does Bugzilla's own `Bug.create`, so that one is not a bzr gap.
- `bzr bug view` serializes no `groups`, `estimated_time` or `remaining_time`, so those
  fields can be written through bzr but never read back (D3).
- A flag-type name containing `-`, `+`, `?` or `X` cannot be addressed at all: bzr's
  `parse_single_flag` takes the *first* of those characters as the status, so `needs-info?`
  parses as name `needs` (D1). Bugzilla permits such names and this repository's slugs allow
  hyphens.
- `bug view` accepts aliases as well as numeric IDs (`src/cli/bug/view.rs:59-62`, "Bug ID(s)
  or alias(es)"), so a scenario-namespaced server alias is a handle a create can be
  reconciled against. This is a bzr capability, not a limitation, so it earns no register
  entry. That the handle is also *unique* — that Bugzilla rejects a second bug declaring an
  alias already in use, rather than accepting it — is inferred here, not observed; see the
  inferred-facts consequence below.
- bzr documents `op_sys` and `rep_platform` as "required by some Bugzilla installations"
  (`src/cli/bug/create.rs:138,141`) and passes both on every functional create, and this
  fixture's `containers/bugzilla/checksetup_answers.txt` sets no `defaultplatform` or
  `defaultopsys` answer. The scenario contract has no slot for either — the loader's
  `bug.create` postcondition key set (`src/bzr_live/scenario/journal.py:227-234`) does not
  carry them. This is a *fixture-configuration* gap rather than a bzr one: Bugzilla supplies
  the values from its own parameters when they are set.
- Bug aliases are enabled on this fixture: `checksetup_answers.txt` sets
  `$answer{'usebugaliases'} = 1`. That matters because bzr's own functional containers run
  with them off — the header comment of
  `tests/functional/phases/08c-bugs-create-fields.sh` says "bug aliases are disabled on these
  default-config containers, so the field silently no-ops" (D4) — so the parameter, not the
  client, is what makes the alias round-trip available here.
- A `bug view` of a bug that is absent, or that the caller may not see, exits **4**, not 2.
  bzr's functional suite asserts exit 4 with `api_code` 101 for a missing numeric ID and
  exit 4 with `api_code` 102 for a group-restricted bug, both against a live Bugzilla
  (`tests/functional/phases/08e-bugs-restricted-access.sh:289-292` and `:169,180,262`
  respectively, in the bzr checkout); the matching alias code is 100, per
  `src/client/response.rs:549-550`. `BzrClient.read` reports absent only for
  `api_code` 51, 105 or 106 — the product and component codes issue #4 needed — so as it
  stands it raises on all three.

## Decision

**Reconciliation resolves before the completed record is written; `reconcile` is never
persisted.** A run writes the in-flight intent, invokes the boundary, and then writes
exactly one completed record whose `next_safe_action` is `advance`, `retry`, or `stop`. A
bzr exit of 0 whose reply carries the identifier the event needs resolves to `advance`
directly. Anything else — a non-zero exit, an unparseable reply, or an exit-0 reply carrying
no usable identifier — triggers a reconciliation read against Bugzilla, whose answer supplies
the value. Those three are the whole list: every invocation this design issues names exactly
one target, so there is no partial-batch case. If the reconciliation read itself
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

**The fixture is fixed in the fixture; the runner substitutes nothing.** `checksetup_answers.txt`
gains `defaultplatform` and `defaultopsys` so a create declaring neither succeeds on its own
terms. Replay sends exactly what the scenario declares and nothing else. Injecting a fixed
`op_sys`/`rep_platform` pair in the create document was the alternative, and it is the shape
`AGENTS.md` forbids: a value the scenario never declared, sent so the run would appear to
succeed.

**A payload bzr cannot execute is refused before any mutation, and the refusal names the
limitation rather than a workaround.** Each refusal cites its entry in
`docs/bzr-findings.md`, which is where the gap is recorded with its bzr source, its class
— defect or design choice — and its upstream issue. Surfacing these is what the fixture is
for; a message reading "move it to a follow-up event" would tell an author to route around
bzr and leave no trace that bzr could not do it.

**Markers are rendered only for append-class events.** A comment body gains a trailing
`[<marker>]` line; a work-time update carries the same marker in the comment posted in the
same `bug update` call; an attachment's description gains `[<marker>] sha256=<hex>`. A set
reconciles by comparison, so appending a marker to it would make the observed value differ
from the declared one.

**A payload the boundary cannot express in one mutation is refused before any mutation.** A
per-action supported-payload table runs with the pristine sweep and refuses `bug.create`
carrying `estimated_hours` or `remaining_hours` (finding G1), `custom_fields` (G4),
`duplicate_of`, or a null `version` (G9); `bug.update` carrying `groups` (D3), `version` (G2),
a null `milestone` (G3), a null `resolution`, a null `duplicate_of`, or `duplicate_of`
together with `status` or `resolution` (G5); a `bug.flag` whose flag-type name contains
`+ - ? X` (D1); and any attachment description whose rendered summary would exceed 255
**bytes** once its marker is appended.

**Four of those grounds are Bugzilla's, not bzr's, and saying so matters as much as naming
the ones that are.** Charging bzr for a constraint it did not impose corrupts the register
exactly as silently routing around a real gap would, and the register is this repository's
product. All four are verified against the fixture's own Bugzilla image
(`bzr-live-…-bugzilla`, `BUGZILLA_VERSION` "5.2+", read with `docker run --rm --entrypoint
sh … cat`), not inferred:

- `duplicate_of` **on create** is absent from bzr because it is absent from Bugzilla's own
  `Bug.create`.
- A null `resolution` **on update** cannot be sent at all. `Bug.update`'s own documentation
  states that "attempting to set the resolution to *any* value (even an empty or null string)
  on an open bug will cause an error to be thrown", and that "if you change the `status` field
  to an open status, the resolution field will automatically be cleared, so you don't have to
  clear it manually" (`Bugzilla/WebService/Bug.pm:4085-4094`). The mechanism is in
  `Bugzilla::Bug::set_bug_status`, which calls `clear_resolution()` on a transition to an open
  status and then *deletes* `resolution` from the update parameters (`Bugzilla/Bug.pm:2956-2960`).
- A null `duplicate_of` **on update** is the same mechanism. `clear_resolution` is what clears
  the duplicate — it calls `_clear_dup_id` — and it throws `resolution_cant_clear` unless the
  bug is already open (`Bugzilla/Bug.pm:2933-2939`). `Bug.update`'s `dupe_of` is typed `int`
  with no null form, and its documentation says to set `dupe_of` and *not* the status or
  resolution, because Bugzilla derives those (`Bugzilla/WebService/Bug.pm:3935-3942`). So bzr's
  `dupe_of: Option<u64>` mirrors Bugzilla exactly, and the absent `--reset-dupe-of` is not a
  bzr gap: there is nothing on the wire for it to send.
- The attachment ceiling is `attachments.description`, declared `TINYTEXT` at
  `Bugzilla/DB/Schema.pm:505` — 255 bytes, so the check counts encoded bytes, not code points.

An earlier revision of this record charged the last two to bzr, on the reasoning that
`--reset-assigned-to` and `--reset-qa-contact` establish a reset pattern the other fields lack.
That reasoning was wrong in the direction this ADR warns about, and the register entry it
produced (G8) has been withdrawn. The asymmetry in `UpdateArgs` is real, but it mirrors a
Bugzilla asymmetry rather than creating one: `assigned_to` and `qa_contact` have component
defaults to reset *to*, and `resolution` and `dupe_of` are cleared by a status transition.

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
- Under `replay` a `bug.create` alias is read **twice**: once by the up-front sweep and once
  by the per-event check, which runs whether or not the sweep already proved that alias
  absent. That is the price of binding the check to every first execution rather than to the
  `replay` command, and the binding is what closes the `resume`-against-an-empty-journal hole
  above. Deduplicating the two within a run would be a few lines, and is deliberately not
  done: at fixture scale the saving is a handful of local round trips, and a cache of "already
  proved absent" is one more thing that can be wrong about the server. Rebuilding the ID
  resolution table costs one `JournalStore.read` per event. That read lists
  the journal directory each time, so table rebuild is quadratic in event count — acceptable
  at fixture scale (hundreds of events), and the reason this design does not target
  million-event scenarios.
- The sweep reads with each create's own actor credential rather than the admin key, so no
  admin key or `.env` parsing enters the replay CLI. A pre-existing bug hidden from that
  actor by a group answers `api_code` 102, which is not in `absent_codes`, so the sweep
  raises and `replay` refuses before mutating anything. That is the intended outcome, and it
  is reached by the read failing rather than by any reconciliation: the run stops with the
  boundary's own message rather than treating an invisible bug as an absent one.

- The supported-payload table is a census of what bzr cannot express, so it is a live
  document: each row points at `docs/bzr-findings.md`, and a bzr release that closes a gap
  deletes a row here rather than adding one. The scenario contract stays wider than the
  engine on purpose — narrowing the contract to what bzr can do today would erase the
  evidence.
- Three of this record's boundary facts are inferred from the fixture's configuration rather
  than observed against it: that a create omitting `op_sys`/`rep_platform` would be rejected,
  that the `alias` key round-trips, and that Bugzilla enforces alias uniqueness — the last
  being what makes a duplicate create *fail* into reconciliation rather than quietly produce a
  second bug under one alias. The Bugzilla-model grounds above are *not* on this list: they
  were settled by reading the fixture's own image, and each carries its file and line. The
  unit suite mocks the subprocess boundary and
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
  `tests/functional/phases/08e-bugs-restricted-access.sh:169,180,262` in the bzr checkout
  (the file's header comment at `:24-25` states the same three directions in prose); collapsing
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
