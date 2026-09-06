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
create the moment the duplicate alias sent it into reconciliation.

Be exact about what that sweep proves, because issue #6's criterion is worded more strongly
than the check: it proves *this scenario's* bugs are absent, not that the fixture is at the
pristine baseline. It is a per-scenario proxy, and it is the strongest check available —
`pristine` is a reserved checkpoint *name* (ADR 0005), and nothing in `bzr_live.checkpoint`
answers "is the running database at the baseline?"; `stack_fingerprint` keys build inputs, not
live contents. The residual case is residue from a *different* scenario, which the sweep
cannot see and which this scenario's namespaced aliases make harmless to it. Establishing the
baseline itself stays the operator's `scripts/checkpoint restore pristine`, per the exclusion
this issue was scoped under.

A second residue follows from the same absence, and it bites on `resume` rather than `replay`.
Nothing binds a journal to a fixture *identity* — no record, manifest or field carries a
database uuid, checkpoint id or install timestamp, and nothing compares one. So an operator who
resets the fixture mid-run and then resumes gets every completed event skipped and its stale
ids adopted into the resolution table, and Bugzilla restarts ids at 1 after a reset, which
makes collision likely rather than exotic. Adding fixture-identity persistence was considered
and rejected: it is the crash-consistency machinery `AGENTS.md` scopes out, and the charter does
not authorize it. The honest mitigation is documentary — a reset invalidates the journal, so the
journal directory goes with it — and `README.md` now says so rather than claiming `resume`
detects it.

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

**Amended after the `groups` readback was measured (issue #27): `bug.update` carrying
`groups` is no longer refused, and the rejected alternative below is withdrawn with it.**
The refusal read "`bzr bug view` does not return groups, so no delta can be computed and no
result confirmed (finding D3)". Every clause of that is dead **by measurement**, not by
inference from an upstream commit: at `bzr 0.8.3-dev (63abb94e)`, this repository's floor,
a default-transport `bug view --fields=id,groups` returns `[]` for an unrestricted bug and
`['restricted']` for a bug restricted to the group issue #34 made settable. `a7f6ab70`
(`bzr` PR #646, adding `Groups` to the `Bug` serializer, an ancestor of the floor) explains
the reading; it does not substitute for having taken it. A declared `groups` set is
therefore executed as an add/remove delta against observed state, like `cc` and `keywords`,
and confirmed from `bug view` by set comparison.

That second reading survives finding **D8** for a reason worth stating, because it is what
separates `groups` from the two time fields this amendment leaves alone. `bzr`'s header
auth is still not real auth, so the first read of a group-restricted bug draws an **HTTP
401** — Bugzilla maps `bug_access_denied` to `STATUS_NOT_AUTHORIZED`
(`Bugzilla/WebService/Constants.pm:270`) — and `bzr`'s transport retries with its alternate
auth method, whose query-parameter credential this fixture does parse as real auth, and
gets a 200 carrying the value. Bugzilla instead omits `estimated_time` and `remaining_time`
from an otherwise-successful **200** for a caller who has not cleared `timetrackinggroup`,
so on a bug the caller could have read anonymously, nothing fires the retry and the field
stays unread. **Under D8 a read is confirmable when Bugzilla either does not gate the field
against an anonymous caller, or refuses the whole read with an error status that fires the
retry *and* the credential that retry carries is authorized for the bug; it is unconfirmable
when Bugzilla answers 200 and silently omits the field.** That is a property of the request
rather than of the field: the loudness in the `groups` case comes from the bug's visibility,
not from `groups` itself. The second disjunct's added clause is not hypothetical — when the
retry's credential is *not* authorized, the fallback draws 401 in its turn and `bzr` reports
the first attempt's error instead, which is the first of the three residuals recorded below.

**Follow that rule to its conclusion and it reaches the time fields too, which is why the
always-retry entry for `estimated_hours` is a conservative floor and not an absolute.** On a
*group-restricted* bug the 401 is raised for the whole read, so the retry fires and its
query-parameter credential — authenticated, and a `timetrackinggroup` member — brings the
time fields back with everything else. Measured at `63abb94e` against this fixture, reading
the one group-restricted bug as `admin-ops`: `bug view --fields=id,estimated_time,
remaining_time,groups` returns `{"id":1,"groups":["restricted"],"estimated_time":0.0,
"remaining_time":0.0}`, and at the wire the header read is HTTP 401 code 102 while the
query-parameter read is a 200 carrying `estimated_time`. So `estimated_hours` is
unconfirmable on every bug a caller can read anonymously — which is every bug the fixture
holds but that one — and confirmable on a bug restricted away from them. It stays in
`_UPDATE_ALWAYS_RETRY` because the readable case is the general one and a rule that
confirmed a field only on restricted bugs would be a worse contract than never confirming
it; but the entry is a floor over the common case, not a claim the field can never be read.
This amendment is what makes the exception reachable at all, since restricting a bug through
`bug.update` was refused until now.

The grounds were never the same, and the single shared rationale over the always-retry set
is what let D3's staleness cover both time fields at once; each now names its own.

**The fixture gap this amendment first recorded as its own blocker is closed, and that is
what makes the removal honest rather than a trade.** When the amendment was drafted, no bug
group was settable on any product here — `group_control_map` held no rows and every
`checksetup` group carried `isbuggroup = 0` — so an authenticated `groups.add` returned
Bugzilla error 120 for every group name alike, and dropping the refusal would have moved a
precondition failure into the middle of a mutation. Issue #34 fixed it in the fixture, which
is where `AGENTS.md` puts a fixture-configuration gap: the scenario contract now declares a
group's products, the container-local admin bridge writes the `group_control_map` row
through `Bugzilla::Product::set_group_controls`, and `scenarios/smoke` declares `restricted`
as settable on `checkout` and `billing` (finding G11, ADR 0013). So the payload a scenario
declares can now actually execute, and the refusal is removed on the strength of that rather
than in spite of it.

Three residuals this amendment does not close, each recorded rather than guarded against —
a client-side refusal would be this repository's own forbidden substitution, and each failure
is a truthful report of what the server did.

**One is `bzr`'s.** It masks Bugzilla's stated cause on a refused `groups` write: on the
alternate-auth retry, a fallback response also carrying HTTP 401 makes it report the first
attempt's error instead of the fallback's, so error 120 surfaces as 410 "You must log in" —
the operator is sent to fix authentication on a request that was authenticated. Measured on
this fixture at the floor, both halves: `bzr bug update --groups-add=editbugs` reports
`api_code 410` "You must log in", while the same write over raw REST with query-parameter
auth reports `code 120`, "not allowed to restrict bugs to this group in the 'checkout'
product". **Recording this in the findings register is issue #39's work, not this change's**,
so it is described here rather than cited by identifier.

**One is Bugzilla's, and it enforces the boundary rather than leaking past it.** A
`bug.update` reaches groups by a different path from a `bug.create`, and only the create path
skips the membership check. `Bug.update` runs `set_all` → `_add_remove($params, 'groups')`
(`Bugzilla/Bug.pm:2455`) → `add_group` / `remove_group` (`:2554-2563`), and `add_group`
carries two gates: `group_is_settable` at `:3157-3158`, then, for a caller not in the group,
`ThrowUserError('group_restriction_not_allowed')` at `:3162-3167` unless the same update also
changes the product. `remove_group` refuses the same caller at `:3205-3211` under a
*different* error — `group_invalid_removal`, which it also throws at `:3189` for a group the
bug is not in and at `:3199-3201` for a mandatory group. **Different errors, same wire code:**
`Bugzilla/WebService/Constants.pm:144-145` maps both `group_invalid_removal` and
`group_restriction_not_allowed` to **120**, and `:276` maps 120 to `STATUS_NOT_AUTHORIZED`.
So the masking above covers the removal path as well as the add path, and both were measured
rather than inferred. The engine cannot reach `:3189` in ordinary
operation, because `_delta` (`src/bzr_live/replay/actions.py:119-123`) computes removals from
*observed* state and so never names a group the bug is not in.

So a declared `groups` update by an actor outside the group is refused **before any
mutation** — which is the boundary holding, not leaking — and the residual is only that `bzr`
masks the stated cause. The create path differs, and the reason is this fixture's own
configuration rather than Bugzilla's model: `_check_groups` (`:1851-1887`, `VALIDATORS` at
`:122`) carries no `in_group` call and gates solely through `Product::group_is_settable`,
whose `groups_available` arm (`Bugzilla/Product.pm:659-693`) selects member groups behind
`groups_in_sql()` but admits *other* groups on `othercontrol` alone, with no membership
check, whenever that column is `CONTROLMAPSHOWN` or `CONTROLMAPDEFAULT`. #34's rows carry
`CONTROLMAPSHOWN`. Under a stricter `othercontrol` the asymmetry would disappear — it is the
same row this record's equality consequence below rests on.

**The asymmetry was measured, not inferred from that source reading.** At the floor against
this fixture, as `triager` — a smoke actor outside `restricted`, whose only member is
`admin-ops`:

| Path | Probe | Result |
|---|---|---|
| update | `bug update --groups-add=restricted -- 1` | **refused**, `api_code 410` masking `code 120` "not allowed to restrict bugs to this group in the 'checkout' product" |
| create | `bug create --product checkout … --groups restricted` | **allowed**, bug 2 created and its `bug_group_map` row written |

Same actor, same group, same product, opposite outcomes. The create path predates this
amendment and is not opened by it.

**One is this repository's own, and it is the residual this amendment newly opens.** Folding
`groups` into the verifier's asserted state makes the verifier assert a field that decides
whether the verifier's own reader can read the bug at all — and the reader is chosen by
membership of `INSIDER_GROUP`, which is `"admin"` (`src/bzr_live/verify/expected.py:16`,
`:408-415`), never by membership of the group the update restricts to. A scenario that
restricts a bug to a group the insider is outside makes `ServerReader.bug` read it and get
`api_code` 102, which `BUG_ABSENT_CODES = {100, 101}` deliberately excludes by the
"inaccessibility is not absence" rule above — so it raises out of `_read_all` and **aborts the
whole verification** instead of producing one finding about one bug. The outsider reader,
which is built on every run and read only where a scenario declares a private comment
(`runner.py:175-178`), is outside `admin` by construction and carries the same exposure.

**Measured, and the trigger is narrower than "outside the group".** Bugzilla also grants the
bug's *reporter* access regardless of restriction (`reporter_accessible`, set on the bug row).
So on the bug `triager` created and restricted to a group it is not in, `triager` still reads
`groups: ['restricted']` — while `developer`, outside the group *and* not the reporter, gets
`api_code 102` from the same read, both at the floor and over raw REST. The exposure is
therefore a reader that is outside the restricting group **and** neither the reporter nor on
the CC list, which is the case a scenario reaches as soon as its declaring actor and its
insider differ.

**The replay engine carries it too, and less gracefully.** Every later event on a restricted
bug reads it as *its own* actor, not as the insider: `BugUpdateHandler.build`
(`actions.py:371-372`), `BugUpdateHandler.reconcile` (`:415`) and `_require_absent`
(`engine.py:112`) all go through the same `BUG_ABSENT_CODES`. An actor outside the restricting
group gets 102 → `ProvisionError`. The `build` site is the unpleasant one: `engine.py:174`
calls it *before* `_execute`'s `try`, so the failure is never routed into `_settle` and the run
dies on a bare "bzr boundary failure (exit 4) running bug view" that names neither the event
nor the group.

`scenarios/smoke` is safe only by coincidence: `admin-ops` is both its first `admin` member
and its only `restricted` member, and nothing states or enforces that they must be the same
actor. Whoever declares the first live `groups` update owes the check for **every actor that
later touches the bug**, not only for the insider.

One consequence rides on how #34 wrote that mapping. `groups` compares by **equality**, like
`keywords`: `Bugzilla/Bug.pm:1883` unions a product's mandatory groups into the set and
`:1860-1864` adds its default groups when the caller names none, either of which would make
the observed set a strict superset of the declared one. Neither occurs here, and the ground
is now narrower and more durable than "the map is empty": the bridge writes `membercontrol`
and `othercontrol` as `CONTROLMAPSHOWN` and nothing else (`containers/bugzilla/bridge.pl`,
per ADR 0013's least-privilege choice), and `groups_mandatory` selects `CONTROLMAPMANDATORY`
while a default group needs `CONTROLMAPDEFAULT`. A mapping written at either of those values
would call for comparing `groups` by containment instead — a mode this reconciler does not
have and would have to grow. `_UPDATE_COMPARE_SETS` compares every field by equality
(`src/bzr_live/replay/actions.py`, in `reconcile`, `:454`), `cc` included. The containment `cc` carries in
`src/bzr_live/verify/checks.py:84` belongs to the verifier and answers an unrelated question
— a flag requestee landing on the CC list, and a server-derived CC set that equality would
assert the exclusion of (PR #23) — not a product widening a set.

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
  Observed live to be enforced by silent truncation rather than by rejection; see the
  consequence below.

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
- Three of this record's boundary facts were inferred from the fixture's configuration rather
  than observed against it: that a create omitting `op_sys`/`rep_platform` would be rejected,
  that the `alias` key round-trips, and that Bugzilla enforces alias uniqueness — the last
  being what makes a duplicate create *fail* into reconciliation rather than quietly produce a
  second bug under one alias. The Bugzilla-model grounds above are *not* on this list: they
  were settled by reading the fixture's own image, and each carries its file and line. The
  unit suite mocks the subprocess boundary and cannot reach any of the three, so
  `tests/replay_smoke.sh` — an operator-run live proof beside `tests/provision_smoke.sh`, the
  split ADR 0004 already chose — is what discharges them.

  **`tests/replay_smoke.sh` has now run, and all three are observed.** Against a fresh
  fixture on Bugzilla 5.2+ with bzr `b80303b7`: all eight events replayed; the alias
  `bzr-live-2f12319c0845fcbdafe07609b352b81` round-tripped to bug 1 through `bug view`; and a
  second create declaring that same alias failed with exit 4. The `op_sys`/`rep_platform`
  fact is discharged in the form that matters — a create declaring neither now *succeeds*,
  because `defaultplatform`/`defaultopsys` supply them, which is what the whole of Task 0
  exists to arrange.

  That first run also uncovered a second fixture defect, and it is the more valuable half
  of the run's output: `checksetup_answers.txt` answered `defaultpriority = '--'`, which is
  not a legal priority. Bugzilla seeds `["Highest", "High", "Normal", "Low", "Lowest",
  "---"]` (`Bugzilla/DB.pm:89`) and defaults the parameter to the last of them
  (`Bugzilla/Config/BugFields.pm:39-44`), but `Bugzilla/Config.pm:235-236` stores an
  answers-file value *without* running the parameter's checker — so the invalid answer
  installed cleanly and then failed every create that declared no priority, at
  `Bugzilla/Bug.pm:713-714` where `defaultpriority` is substituted and `_check_select_field`
  rejects it. The answer is now `'---'`. It predates this branch and was invisible until
  something first created a bug through the fixture, which is what replay does.

  That first run also falsified one thing this record asserted. The 255-byte attachment
  ceiling is **not** enforced by rejection: a 256-byte summary was accepted (exit 0,
  attachment 2) and stored at exactly 255 bytes. The mechanism is the database's, not
  Bugzilla's: the column is `TINYTEXT`, Bugzilla applies no length validator to
  `attachments.description` (`Bugzilla/Attachment.pm:578-584` trims and rejects only an
  empty value), and it removes `STRICT_TRANS_TABLES` from the session `sql_mode` at
  `Bugzilla/DB/MariaDB.pm:87-100` — "Disable ANSI and strict modes, else Bugzilla will
  crash" — while `containers/mariadb/bugzilla.cnf` sets none of its own. So MariaDB
  truncates silently. "Bugzilla truncates over-long fields" is not a general rule:
  `bugs.short_desc` *is* guarded, by `MAX_FREETEXT_LENGTH` at `Bugzilla/Bug.pm:2046-2049`,
  which throws `freetext_too_long`. It is specifically the columns Bugzilla forgot to
  validate that fall through to a non-strict database — which is why the ceiling is checked
  per *column* here, on both `bug.attach` and `attachment.update`, rather than per marker.
  The client-side refusal is therefore *more* necessary
  than this record argued, not less: the reconciliation marker lives in that summary, so a
  truncated summary destroys the handle append-class reconciliation matches on, and the
  server would report success while doing it.
- Replay imports two private names across package boundaries: `_ATTEMPT_FILE` from
  `bzr_live.scenario.journal` (issue #3) and `_KEY_ENV` from `bzr_live.provision.adapters`
  (issue #4). Both are deliberate. `JournalStore`'s public read tolerates other events'
  record files, so the stray-record scan criterion 4 needs cannot be built on the public API,
  and the alternative — forking the attempt-file pattern into `engine.py` — would drift
  silently, which is worse than an import that fails loudly at startup. Expanding either
  merged module to publish an accessor was the other option and was not taken: it is an
  excluded module's owner's call. If issue #3 later publishes a listing accessor, switch.
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
  silently diverge. **Withdrawn by the amendment above (issue #27):** `bzr` serializes
  `groups` from `a7f6ab70` on (`src/types/bug.rs:242` at `63abb94e`), so the delta is
  computable and the set is confirmable, and the declared set is now applied in full.
- **Keep refusing a declared `groups` update, re-grounded on the fixture gap rather than on
  D3, until `containers/` provisions a settable bug group.** verified: **moot — the gap it
  proposed to wait on is closed.** The gap was real when this alternative was written:
  `group_control_map` held no rows and every `checksetup` group carried `isbuggroup = 0`, so
  an authenticated `groups.add` returned Bugzilla error 120 for every name. Issue #34
  provisioned the mapping through the container-local admin bridge (finding G11, ADR 0013),
  and `PUT /rest/bug/4 {"groups":{"add":["restricted"]}}` now answers
  `"changes":{"groups":{"removed":"","added":"restricted"}}`. There is nothing left to wait
  on. Its second ground stands on its own and is why the wait was never the right instrument
  either: a refusal here would have named Bugzilla's configuration, not a `bzr` limitation,
  and `AGENTS.md` puts a fixture-configuration gap in `containers/` rather than in a
  client-side refusal.
- **Compare the declared `groups` set by containment.** judgment:
  containment would silently accept a bug carrying groups the scenario never declared, and
  the widening it guards against — a product's mandatory or default bug groups — cannot
  occur while every `group_control_map` row this fixture writes carries `CONTROLMAPSHOWN` in
  both control columns, which is neither `CONTROLMAPMANDATORY` nor `CONTROLMAPDEFAULT`.
  Recorded as the consequence above rather than pre-emptively weakened.
- **Record the withdrawal in a new ADR 0011 rather than amending this one in place.**
  judgment: the decision changes this record's own refusal table and its own rejected
  alternative, so a separate record would leave both reading as current; ADR 0008 set the
  in-place precedent for exactly this situation when its first live run corrected it.
- **A separate reconciliation index beside the journal.** judgment: a second source of truth
  for what the journal records already answer, and one more file to keep consistent with it.
- **Auto-retry a failed event inside the same run.** judgment: an unbounded loop against a
  permanently failing event, and the bound that fixes it is a policy the operator should set
  by choosing to run `resume`.
