# Semantic scenario verifier — design

Issue [#20](https://github.com/randomparity/bzr-live/issues/20). Part of epic #7.
Decision record: [ADR 0008](../../adr/0008-semantic-scenario-verifier.md).

## Problem

Nothing in the repository verifies a replayed scenario. `make smoke` provisions and
replays `scenarios/smoke/` and reports that 47 events executed; it asserts nothing about
what the server ended up holding. A replay that silently wrote the wrong assignee, dropped
a dependency edge, or leaked a private comment would report success.

## Goal

A `verify` command that reads a scenario and its replay journal, then asserts semantic
invariants against live server state through the selected `bzr` binary — field values,
actor attribution and ordering in history, relationship topology, comment visibility,
attachment presence and asset checksums, and custom-field values.

Non-goals, each owned elsewhere: the `scenarios/**` CI path filter (#25),
`timetrackinggroup` provisioning (#22), fault injection and checkpoint-restore proof
(#24, merged as PR #26).

## Command surface

```
python -m bzr_live.replay verify <scenario_dir> \
  --state-root <root> --base-url <url> --bzr <path>
```

`verify` joins `replay` and `resume` in the existing dispatch
(`src/bzr_live/replay/__main__.py:19`). It reuses that module's `ReplayContext` — key
store, per-actor `BzrClient`, and the 0700 workspace — so there is no second Bugzilla
client and no second credential path. Every read goes through `BzrClient.read`, whose
`_payload` already unwraps `bzr --json`'s `data` envelope
(`src/bzr_live/provision/adapters.py:71-78`).

It **must run inside the replay's own state root**: the actor API keys and the journal both
live there, and `tests/smoke_scenario.sh` removes that root on an `EXIT` trap. `make smoke`
therefore gains a verify stage between the replay and its final `OK`; no separate Makefile
target is added, because a standalone target could not supply a state root that a replay
has written.

## Preconditions

Four refuse before any read is issued; the fifth is a capability probe over the first read
and is stated after them. All five name the scenario path in the message:

1. The scenario loads (`load_scenario`, existing errors pass through).
2. Every event has a `CompletedRecord` under `<state-root>/journal/<name>/` whose
   `scenario_digest` equals the scenario digest and whose `next_safe_action` is
   `advance`. Anything else — absent, in-flight, `retry`, `stop` — refuses with the event
   name and what is missing. A partially replayed scenario has no expected final state.
3. Each reader actor the scenario **does** declare has an API key in the key store. This
   is an explicit `KeyStore` lookup in the runner, before any reader is constructed —
   not left to `ReplayContext.client`, which raises a `ReplayError` pointing at
   provisioning from inside the first read. A *missing role* is not a precondition
   failure: it falls through to the `unverifiable` path below, because a scenario is free
   to declare only insiders.
4. The expected reachable node count from every link root is at most `1000`
   (`LINKS_MAX_NODES`, `bzr` `src/types/bug/links.rs:13`). Above that `bzr bug links`
   truncates its walk and warns on stderr, which `BzrClient.read` discards on exit 0, so
   the bound is checked against the declared graph instead of trusted at read time.

A fifth precondition is a **capability probe** and so is the one read that precedes the
checks rather than following them. When the scenario declares a private comment, the first
`comment list` read — as the insider, on that bug, the read check 4 needs anyway — must
show that comment's marker. Its absence refuses the run:

```
verify failed: <scenario>: bug <alias>: the fixture cannot serve a full comment thread.
bzr reads one over XML-RPC Bug.comments (src/client/resources/comment.rs:62) and falls
back to REST, which returns the public comments alone; this image has libsoap-lite-perl
without XMLRPC::Lite (Bugzilla/Install/Requirements.pm:303-310 requires them separately).
Add libxmlrpc-lite-perl to containers/bugzilla/Dockerfile and rerun make up.
```

Refusing rather than reporting a divergence is what precondition 2 buys. That precondition
already established the `bug.comment` event holds a completed record with
`next_safe_action: advance`, so the comment **is** on the server; a marker the insider
cannot see says the read path cannot reach it, not that the replay wrote the wrong thing.
Calling that a divergence would report a `bzr` defect against a correctly replayed
fixture — `AGENTS.md`'s silent substitution inverted — and would leave the operator
debugging the replay for a missing OS package. Where the discrimination is imperfect is
stated rather than hidden: an insider read missing the marker because the marker itself
was rendered differently would also refuse here, and the message names the marker so that
case is one `comment list` away from being told apart.

`CompletedRecord.resolved_ids` is the only source of server identities
(`src/bzr_live/scenario/journal.py:385`); the verifier never re-derives one and never
looks a bug up by a numeric literal.

## Reader actors

Two roles, both chosen from the scenario's own declared actors:

- **insider** — the first declared actor whose `groups` include the fixture's insider
  group. That group is `admin`, set by
  `containers/bugzilla/checksetup_answers.txt:31` (`$answer{'insidergroup'} = 'admin'`).
  Every positive read is issued as this actor.

  Issuing a read as the insider is not the same as the server answering it as the insider,
  and on this fixture the two come apart. `bzr` prefers header auth once its probe sees
  `rest/bug` answer 200 (`header auth works on API endpoints despite valid_login rejecting
  it; preferring header`), but this Bugzilla does not accept `X-BUGZILLA-API-KEY` for REST:
  `rest/valid_login` returns `{"result":false}` for the header and `{"result":true}` for
  `Bugzilla_api_key` as a query parameter. A 200 is what an anonymous caller gets too, so
  the probe cannot tell the difference, and `bzr`'s REST reads run unauthenticated. Writes
  are unaffected — they draw a 401, which triggers `bzr`'s alternate-auth retry — which is
  why the replay works. Recorded as a `bzr` finding in `docs/bzr-findings.md`.

  Nothing in `scenarios/smoke/` is group-restricted, so no read the verifier makes today
  returns less than the insider would see. The premise is recorded here rather than assumed,
  so a scenario that does restrict a bug fails against a stated boundary instead of a
  silent one.

  The boundary is REST-only. `comment list` — the one check that actually depends on being
  answered as the insider — goes over XML-RPC, which does authenticate: with `XMLRPC::Lite`
  present, `bzr comment list 7` returns the declared private comment as `is_private=true`,
  which an unauthenticated caller does not get.
- **outsider** — the first declared actor whose `groups` do not include it. Used only for
  the private-comment invisibility check.

`editbugs` is not a proxy for either role: stock Bugzilla grants it by `userregexp '.*'`
(`Bugzilla/Install.pm:134-138`), so every account on this image holds it regardless of what
the scenario declares — the premise commit `3f3f035` withdrew. Insider membership is not
regexp-granted, so it is a real distinction.

A scenario declaring no actor in one of the two roles does not fail: the checks needing
that role are reported `unverifiable` with that reason.

## Expected state — the fold

`verify/expected.py` folds `scenario.events` in declaration order into one
`ExpectedBug` per bug alias. It is pure: no I/O, no server identities, unit-testable on its
own. The fold is what makes the assertions *semantic* — asserting each event's own
postcondition against final state would fail on `scenarios/smoke/`'s reopening cycle, where
`resolve-decline-copy` sets `RESOLVED` and `reopen-decline-copy` later sets `CONFIRMED`.

Per bug the fold carries: `product`, `component`, `summary`, `version`, `milestone`,
`assignee`, `cc`, `keywords`, `depends_on`, `blocks`, `duplicate_of`, `status`,
`resolution`, `custom_fields`, `flags`, an ordered `comments` list, an `attachments` list,
and an ordered `history` list of declared changes.

Three server behaviours are **modelled in the fold**, not tolerated at comparison time,
each observed live against `scenarios/smoke/` on 2026-09-02 with `bzr 0.8.2 (ae39fbd8)`:

- **A flag requestee joins the CC list.** `flag-review-request` declares no CC on
  `pay-retry-loop`; `bzr bug view 8` returns `cc: ['releaser@example.test']` and
  `bug history 8` carries `developer@example.test | cc | '' -> 'releaser@example.test'`
  beside the `flagtypes.name` record. The fold therefore adds the requestee's email to
  that bug's **running** CC set at the point the flag event appears, so a later `cc`
  declaration that omits the requestee removes it again — which is what the replay
  engine does, since `BugUpdateHandler.build` computes its delta against the live server
  value and would emit `--cc-remove=<requestee>`. A union taken at the end would instead
  assert a member the replay had removed.

  PR #23 lists this as one of three postconditions that are not the server's final state,
  and the charter excludes asserting against it — so the running set is used **only** to
  compute later deltas correctly, never as an assertion. It contributes no history
  expectation, and check 1 compares `cc` by containment. On `pay-retry-loop`, whose CC is
  entirely server-derived, the verifier therefore asserts nothing about CC at all.
- **A `depends_on` edge materialises `blocks` on the other bug.** `bzr bug view 12` returns
  `blocks: [1, 15]` from edges declared on bugs 1 and 15. Both endpoints get the inverse.
- **`dupe_of` has no stock inverse.** `bug view 1` returns no `duplicates` field; the edge
  is reachable from `cart-dupe-report` only. The link model materialises `depends_on`/
  `blocks` on both endpoints and `dupe_of` on the source alone.

Two further fold rules follow from how the events are written rather than from server
behaviour:

- **Set fields fold as running sets, and only additions reach the history expectation.**
  The fold tracks each bug's `cc`, `keywords`, `depends_on` and `blocks` as sets, so an
  event declaring `[regression, perf]` over a prior `[regression]` contributes
  `added={perf}` to the history expectation and `{regression, perf}` to the field
  expectation, matching the observed record `'' -> 'perf'`. The running set is a model of
  the server, which is what makes it the right basis: `BugUpdateHandler.build` computes
  its own add/remove delta against the value it reads back from the server
  (`src/bzr_live/replay/actions.py:397-409`), not against the previous declaration, so
  the two agree only while the fold models every server-side addition. The requestee rule
  above is the one such addition this scenario exercises. Removals are not asserted in
  history — the attribution assertion is containment, and a removal's `new_value` is the
  residue rather than the removed member.

  An event contributes **one** history expectation per set field, not one per added
  member. Bugzilla writes one `bugs_activity` row per field per change with the added
  members comma-joined: `bug history 18` returns a single `depends_on '' -> '8, 14'` for
  `link-diamond-sink`, which declares two. The expectation carries the added members as a
  **set**, and the check splits the observed `new_value` on `", "` before comparing, so
  the join order is never asserted — which matters, because nothing establishes what
  order Bugzilla joins in.

  Every declared value arrives as a `Reference`. It projects to `emails[ref.name]` for an
  actor-kinded field — `assignee`, `cc`, a flag `requestee` — and to `ref.name` otherwise
  — `product`, `component`, `version`, `milestone`, `keywords`, a custom-field name. The
  projection happens before any set arithmetic, so both sides of a delta are strings.
- **A declared `status` with no declared `resolution` unasserts the resolution.** Bugzilla
  clears the resolution on a transition to an open status, which is why the engine refuses
  a declared `resolution: null` and tells the author to declare the status change instead
  (`_UPDATE_NO_CLEAR`). The fold therefore drops `resolution` from the compared set at
  such an event and reinstates it only if a later event declares one. On
  `pay-decline-copy` the later `refix-decline-copy` declares `FIXED`, so the final value
  is still asserted.

## Unverifiable claims

A declared value `bzr` cannot read back is reported as `unverifiable`, naming the field,
the alias, and the reason with its `docs/bzr-findings.md` citation. It is printed in the
report and counted in the summary; it does not fail the run, because a recorded gap that
turns every run red stops being read. Silently skipping it is what the report exists to
prevent.

| Declared field | Status | Reason |
|---|---|---|
| `remaining_hours` | unverifiable | Bugzilla decrements it by logged work, so the declared value is not the final state; PR #23 handed this issue the explicit instruction not to assert against it |
| work-time hours | unverifiable | Bugzilla gates time-tracking fields on `timetrackinggroup`, deferred to #22; the `bug.worktime` comment is still asserted |

**`groups` and `estimated_hours` are asserted, not waived.** An earlier draft of this
design waived both under finding D3 — `bug view` omitting `groups`, `estimated_time` and
`remaining_time` — on the strength of a live reply from `bzr 0.8.2 (ae39fbd8)`. That
revision is below the floor this repository supports: `README.md` states `make smoke` is
proven at `63abb94e` and to treat anything below it as untested, and `ae39fbd8` is an
ancestor of `63abb94e`. In the range between them, `a7f6ab70` (*fix(bug): expose group and
time fields in bug views*, `bzr` PR #646 on branch `feat/bug-view-read-fields-641`) closes
`bzr#641`, the upstream issue D3 was filed as: it adds `Groups`, `EstimatedTime` and
`RemainingTime` to `BugField` (`src/types/bug/fields.rs`) and to the `Bug` serializer.

So D3 is fixed at the revision this fixture actually supports, and waiving the two fields
would drop a chartered criterion — declared field values on every bug the scenario names —
while recording a `bzr` gap that no longer exists. Recording a limit `bzr` has already
removed is the inverse of `AGENTS.md`'s rule and every bit as misleading as routing around
a real one. `estimated_hours` is declared by `estimate-double-charge` on
`cart-double-charge`, so this is a live assertion on the shipped scenario, not a
hypothetical one. `remaining_hours` stays unverifiable, but on PR #23's grounds alone —
that reason is about Bugzilla, not `bzr`, and survives the fix untouched.

**Every live observation in this design is taken at `bzr 0.8.3-dev (63abb94e)`**, and the
live tier runs against that binary. This matters beyond D3: `5a6421b9` gave the REST
attachment arm its `exclude_fields=data`, and `9ad5ceb2` changed duplicate-link decoding,
both inside the same range — so a payload transcribed from `ae39fbd8` is evidence about a
revision nothing here supports.

`groups` remains unexercised by `scenarios/smoke/`, which declares none, so a green smoke
run is not evidence about it either way; the fold and the field check handle it, and the
first scenario that declares one is what proves it. Only `bug.create` can raise it: `bzr
bug update` refuses a `groups` change outright
(`src/bzr_live/replay/actions.py:32-36`), so no such event ever reaches a completed
journal record.

The fixture's **replay-side** handling of these three fields is unchanged and out of this
issue's scope. `src/bzr_live/replay/actions.py` still refuses a `groups` update and still
treats the two time fields as never-confirming, per ADR 0006 and D3's "What the fixture
does". Narrowing that now would be a change to a dependency this issue reads rather than
modifies; it is reported as follow-up work, not taken here.

A second consequence of a group-restricted bug is recorded rather than handled. Such a bug
is unreadable to the outsider role, and the reader passes `absent_codes=frozenset()`
deliberately, so an access-denied reply raises out of the run instead of yielding a finding
— the verifier stops rather than reporting why it could not read. Building an access-denied
path for a case the shipped scenario cannot produce would be machinery ahead of its
trigger; the boundary is stated here so the failure is diagnosable, and the first scenario
that restricts a bug is what should buy the handling.

Everything else in the completion criteria is asserted.

## Checks

Six families, in `verify/checks.py`. Each yields `Finding(kind, subject, check, detail)`;
`kind` is `divergence` or `unverifiable`, `subject` is the symbolic alias.

### 1. Field values — `bug view`

Read once per bug with an explicit `--fields` list: the built-ins the checks need plus the
`cf_*` names the scenario declares. The explicit list is load-bearing — `bug view` without
it omits every `cf_*`, while `bug view 1 --fields id,summary,cf_risk,cf_tracker,cf_subsystem`
returns all three (observed live).

Compared: `summary`, `status`, `resolution`, `product`, `component`, `version`,
`target_milestone`, `assigned_to` (declared actor's email), `dupe_of` (through
`resolved_ids`), `keywords` (set), `cc` (set, **by containment** — see below),
`depends_on` and `blocks`
(sets, through `resolved_ids`), `groups` (set) and `estimated_time` where declared,
each declared `cf_*` (scalar equality, multi-select as a
set), and `flags` (a declared flag matches on `name`, `status`, and `requestee`; a declared
`X` status requires no entry with that name, mirroring
`src/bzr_live/replay/actions.py`'s reconciler).

**`cc` is the one set compared by containment rather than equality**, and the reason is a
charter exclusion rather than a technical one. PR #23 named three declared postconditions
that are not the server's final state, and the third is a flag requestee landing on the CC
list; the charter excludes asserting against all three. `pay-retry-loop` declares no `cc`
at all, so its entire observed CC set is server-derived — asserting it by equality would
turn that excluded postcondition into this design's own premise. Containment asserts what
the scenario declared and stays silent about what the server added. The cost is that a
spurious extra CC member goes unreported, which is the same trade this design already
takes deliberately for history records and for injected comments.

The fold still **models** the requestee joining CC (below). Modelling and asserting are
different acts: the model is what makes a later `cc` declaration's delta match the one the
replay engine computed against live server state, so dropping it would make the *history*
expectation wrong on any scenario that declares `cc` after a flag. What the exclusion
forbids is asserting the postcondition, and no assertion now rests on it.

`status` and `resolution` are skipped for a bug carrying a declared `duplicate_of` that the
scenario never gives an explicit status: Bugzilla sets `RESOLVED`/`DUPLICATE` itself
(finding G5; `bug view 5` returns exactly that), so there is nothing declared to compare.

### 2. History attribution and ordering — `bug history`

Two assertions over the flattened records
(`{when, who, field, old_value, new_value, comment_id}`).

`bug.create` contributes no history expectation: Bugzilla writes no `bugs_activity` rows
for a creation, so a bug's declared CC, keywords and edges at create time appear only in
its field values. History expectations come from `bug.update`, `bug.flag`,
`bug.custom-field-set` and `attachment.update`.

**Attribution** is multiset containment: every declared change must appear at least as many
times as it was declared, as `(who, field, projected new_value)`. Containment rather than
equality, because the server adds records the scenario did not declare — the inverse
`blocks` edge on the other bug, the `cc` record beside a flag, and the `status` and
`resolution` records a duplicate marking drives. `who` is the declaring actor's email.

The projection drops the value for `depends_on` and `blocks`, whose history values are
generated numeric IDs — those assert `(who, field)` only. It keeps the value for the
symbolic ones: `summary`, `status`, `resolution`, `assigned_to`, `target_milestone`,
`keywords` and `cc` (the added members as a set, per the rule above),
`flagtypes.name` (`review?(releaser@example.test)`), `attachments.isobsolete`
(`'0' -> '1'`), and each `cf_*` (a multi-select renders comma-space joined:
`'' -> 'cart, payment'`). Every rendering listed here was read from a live reply, not from
`bzr` source.

A declared `duplicate_of` contributes **no** history expectation. Bugzilla writes no
`dupe_of` row: `bug history 5` returns exactly `triager | resolution | '' -> 'DUPLICATE'`
and `triager | status | 'CONFIRMED' -> 'RESOLVED'` — the changes the marking drove — and
nothing else. The duplicate edge is proven by check 3 and by the `dupe_of` field in check
1, which is where the completion criterion asks for it.

**Ordering** applies to the scalar chain fields — `status`, `resolution`, `assigned_to`,
`target_milestone`, `summary` — where each record's `old_value` is the previous record's
`new_value`. It runs for a field only when the fold declares at least one change to it
**and** the reply carries at least one record for it; a field with neither has no ordering
to prove, and reporting one would fail every bug whose summary was set at create and never
changed.

The contract: records are ordered by linking `old_value` to `new_value` across the whole
list, anchored at the tail on the field's current value from `bug view`; buckets of equal
`when` are permuted internally; `when` is a sort key and a search bound and is never
compared. The reconstructed `who` sequence must contain the declared actor sequence as a
subsequence. The reconstruction has three named non-answers — no ordering links (a
divergence), more than one links, and a search beyond its bounds — and the last two are
reported `unverifiable` for that field rather than guessed. Why the search is global
rather than per bucket, and why candidates are deduplicated, is ADR 0008's ground; it is
not restated here.

### 3. Topology — `bug links`

Per bug, two reads:

- **Direct edges**: `bug links <id>` → the set of `(alias, relation, direction)` after
  mapping each observed `id` back through `resolved_ids`. Compared against the declared
  edge set with inverses materialised as above. An observed edge to a bug the scenario does
  not name is a divergence.
- **Bounded reachability**: `bug links <id> --recursive --depth <d>` → the set of
  `(alias, depth)`, where `d` is the eccentricity of that root in the declared graph,
  capped at `bzr`'s maximum of 10. Skipped when `d` is 1 or less: an isolated bug has
  nothing to walk, and a root whose whole neighbourhood is one hop away is already
  covered by the direct read.

Reachability asserts hop distance, not the discovering relation, at depth above 1.
`bzr`'s walk sorts its frontier by bug id (`src/commands/bug/links.rs:42`), so which
relation is credited for a node reachable by two paths depends on generated identifiers —
exactly what the issue forbids an assertion from depending on. Depth is a property of the
graph. Together with the depth-1 relation assertion on every bug, this proves the chain,
the diamond, and the duplicate pair: `bug links 1 --recursive --depth 3` returns bug 12 at
depth 1 and bugs 15 and 17 at depth 2.

### 4. Comments and visibility — `comment list`

Read as the insider. Comment 0 is the create description: assert its `creator` is the
create event's actor, its `text` equals the declared description, and `is_private` is
false.

Every other declared comment — from `bug.comment` and from `bug.worktime`, which posts one
— is located by its `[<reconciliation_marker>]` token in `text`, never by index or id, and
must occur exactly once. Assert `creator` and `is_private` against the declaration.
Ordering is asserted as a subsequence over the server's `count`, because Bugzilla injects
comments the scenario did not declare: `bug 1` carries
`*** Bug 5 has been marked as a duplicate of this bug. ***` at count 1 and
`Created attachment 1 ...` at count 3.

**Visibility** is the second read: for each bug carrying a declared private comment,
re-read `comment list` as the outsider and assert that private comment's marker is absent
while every public marker on that bug is present. An unauthenticated read of bug 7 already
shows the private comment withheld; the check proves it for a named non-insider actor.

**This check needs XML-RPC in the fixture image.** `bzr` reads a comment thread through
XML-RPC `Bug.comments` first and falls back to REST only on a transport error, because
`src/client/resources/comment.rs:51-56` documents XML-RPC as the only path returning the
full thread — a REST read of bug 7 returns the public comment alone, so the insider check
above cannot see the declared private comment at all. The fixture answered `xmlrpc.cgi`
with "The XML-RPC Interface feature is not available in this Bugzilla" because
`containers/bugzilla/Dockerfile` installed `libsoap-lite-perl` and no `XMLRPC::Lite`;
Bugzilla's `Bugzilla/Install/Requirements.pm:303-310` requires the two separately ("Since
SOAP::Lite 1.0, XMLRPC::Lite is no longer included and so it must be checked separately").
Adding `libxmlrpc-lite-perl` to that apt list is the fix, and it is a fixture fix rather
than a client-side substitution, which is what `AGENTS.md` requires. Verified live: with
the package present, `bzr comment list 7` returns `count=1 is_private=true`. The image
rebuild `make up` performs is enough — `mariadb-data` and `bugzilla-data` are top-level
volumes, so no reset and no data loss.

### 5. Attachments — `attachment list`

Read only for a bug the fold gives at least one attachment: two of the smoke scenario's
twenty. Skipping the other eighteen costs nothing, because an attachment the scenario
never declared is not something the verifier asserts about either way.

Located by `[<marker>]` in `summary`, exactly once. Assert:

- `summary` equals `render_attachment_summary(description, marker, sha256)` with the
  description the fold ends on — `attachment.update` may have replaced it;
- `creator`, `content_type`, `is_private` against the declaration, and `is_obsolete`
  against the fold;
- **checksum**: `sha256(base64decode(data))` equals the declared `asset_sha256`.
  `attachment list` carries the attachment body in `data` on this fixture, so the check
  costs no extra call and no temporary file. When a reply carries no `data` the checksum is
  reported `unverifiable` naming that; nothing else is weakened.

**This check depends on the same fixture package check 4 does.** `attachment list` is the
second of the five read paths to prefer XML-RPC: `get_attachments` calls
`dispatch_xmlrpc_first` (`bzr` `src/client/resources/attachment.rs:151`), as does
`get_attachment` (`attachment.rs:180`). The two arms disagree about `data` specifically —
the REST arm requests `exclude_fields=data` (`attachment.rs:163`), the XML-RPC arm asks
for it in `ATTACHMENT_LIST_FIELDS` (`src/xmlrpc/resources/attachment.rs:12-27`). Against
an image without `XMLRPC::Lite` every checksum therefore reports `unverifiable` for a
missing key, which is a true report of a fixture gap and not a divergence — but it means
the strongest assertion in this family is silently vacuous until the rebuild. It is not a
precondition: unlike the private comment, a missing `data` key is already reported
honestly, so a refusal would buy nothing check 4's refusal does not already buy.

### 6. Custom fields

Covered by check 1's explicit `--fields` read and by check 2's `cf_*` history records; no
separate read. The values were written through the stock-REST adapter and are read back
through `bzr`, which is the round trip the criterion asks for.

## Report and exit

One line per finding:

```
verify: <scenario_dir>: <alias>: <check>: <detail>
```

then `verify: <n> checks, <m> divergences, <k> unverifiable`, where `<n>` is the number of
`(bug, check family)` pairs actually executed. That definition is what makes the summary
readable: a bug the verifier skipped moves the number, so a green run cannot look
identical to one that asserted nothing — the failure the replay's own `47 events executed`
summary has. The definition only holds while an executed family evaluates something, which
is why check 2 skips a bug whose fold declares no change (below): counting fourteen history
families that assert nothing would inflate `<n>` in exactly the way this definition exists
to prevent. Exit 1 if `m > 0`, else 0.
A precondition refusal exits 1 with a single `verify failed: <reason>` line, matching how
`replay` reports one today.

## Testing

**Offline**, under `make test`:

- `tests/test_verify_expected.py` — the fold against `tests/fixtures/replay-scenario/` and
  the committed `scenarios/smoke/`: the reopening cycle folds to `RESOLVED`/`FIXED`, the
  flag requestee lands in expected CC, inverse `blocks` edges appear on both endpoints,
  `dupe_of` appears on the source only, and the four unverifiable fields are classified.
- `tests/test_verify_checks.py` — every check against recorded payloads, transcribed from
  the live replies quoted in this document. Each check gets a passing case and a diverging
  case, including: the same-second history bucket that only chain-linking orders correctly,
  a private comment visible to the outsider, a corrupted attachment `data`, and a missing
  `cf_*` key.
- `tests/test_verify_journal.py` — the preconditions: absent record, digest mismatch,
  `next_safe_action` other than `advance`, missing reader role, the link-node bound, and
  the comment-transport probe, whose refusal must name `XMLRPC::Lite`.

**Live**: `make smoke` runs `verify` after the replay, inside the same state root, and
fails the script on a non-zero exit.

**Read budget.** Each check read is a `bzr` process spawn plus an HTTP round trip, and the
verify stage issues several per bug across twenty bugs, so it adds enough wall time that
`README.md`'s published smoke figure stops being true. Three skips keep it down — no
`attachment list` for a bug with no declared attachment, no recursive `links` read at
eccentricity 1, and no `bug history` for a bug whose fold carries no `ExpectedChange` — and
the figure is re-measured from the run that adds the stage, the way the fixture change
before it was. No estimate is published here: the measurement belongs to that run.

The attachment skip is the largest of the three on `scenarios/smoke/`, at eighteen bugs of
twenty; the history skip is the next, at fourteen. Folding the scenario gives a declared
history change on **six** bugs — `cart-double-charge`, `pay-decline-copy`,
`pay-retry-loop`, `pay-token-leak`, `inv-tax-mismatch` and `dun-wrong-locale`. Six is
what the fold rules produce, not what the event list looks like: history expectations come
from `bug.update`, `bug.flag`, `bug.custom-field-set` and `attachment.update` alone, and
two rules subtract from that list. `mark-duplicate` is `cart-dupe-report`'s only such
event and contributes no `ExpectedChange`, which is why that bug is not a seventh. The
materialised inverse edges contribute none either — they are server writes the scenario
did not declare, and containment tolerates them — which is why `inv-currency-drift`, on
the receiving end of both diamond edges, is not an eighth.

For the other fourteen the attribution multiset is empty, so containment is vacuous and
the ordering guard's second clause never fires. Each of those fourteen still cost a `bzr`
spawn and an HTTP round trip and bought no assertion. The `links` read is deliberately
**not** in this class and stays unconditional: its direct-edge check reports an observed
edge the scenario never declared, so it bites on a bug with no declared edges.

## New finding for `docs/bzr-findings.md`

`bug history`'s `comment_id` correlation produces a wrong id. `flatten_history`
(`~/src/bzr/src/commands/bug/history.rs:68-71`) documents that it "can miss (→ null) but
never produces a wrong id", correlating on exact `who` plus a canonical timestamp key.
Observed on bug 12 of `scenarios/smoke/`: the `cf_risk` and `cf_subsystem` records at
`14:20:03Z` by `triager@example.test` both carry `comment_id: 31`, and comment 31 is the
`worktime-inv-tax` comment posted by the same actor in the same second through a different
call. The custom-field write is a stock-REST `PUT` that posts no comment at all. Recorded
with the observed evidence; filing upstream needs the operator's word first
(`AGENTS.md`).

## Threat model

**Boundaries.** The command adds one entry point (`verify` on an existing argv parser) and
widens no existing one. It performs reads only — no `bzr` write path, no REST `PUT`, no
bridge call. It parses two kinds of input it did not produce: `bzr --json` replies, through
the existing `_payload`, and journal records, through the existing `JournalStore`
validators.

**Actors.** A local operator on a loopback-bound disposable fixture. There is no remote
actor and no hostile local user (`AGENTS.md`). The one asymmetry that matters inside the
fixture is insider versus non-insider, which check 4 exercises deliberately rather than
defends against.

**Controls.** Argument values reach `bzr` through `subprocess.run(..., shell=False)` in
`BzrClient._invoke`, as they do for replay; no string is interpolated into a shell.
Server identities come from `resolved_ids`, which `CompletedRecord.__post_init__` has
already validated as positive integers, so no reply value becomes a positional argument
unchecked. The link walk is bounded by the declared-graph node check above. API keys ride
the environment, never argv, through the unchanged `BzrClient`. The report prints observed
field values, so it must never print a key: the verifier reads no field that holds one, and
the existing `known_secrets` redaction path is not reached because the verifier writes no
journal record.

**Out of scope.** Encryption at rest, key rotation, and crash consistency — a disposable
fixture, per `AGENTS.md`. Bugzilla's own authorization correctness: check 4 asserts the
fixture behaves as configured, not that Bugzilla's group model is sound.
