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

Non-goals, each owned elsewhere: the `scenarios/**` CI path filter (#21),
`timetrackinggroup` provisioning (#22), fault injection and checkpoint-restore proof
(remaining epic #7 scope).

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

Refuse before issuing any read, naming the scenario path in every message:

1. The scenario loads (`load_scenario`, existing errors pass through).
2. Every event has a `CompletedRecord` under `<state-root>/journal/<name>/` whose
   `scenario_digest` equals the scenario digest and whose `next_safe_action` is
   `advance`. Anything else — absent, in-flight, `retry`, `stop` — refuses with the event
   name and what is missing. A partially replayed scenario has no expected final state.
3. Each reader actor the scenario **does** declare has an API key in the key store. A
   *missing role* is not a precondition failure — it falls through to the
   `unverifiable` path below — because a scenario is free to declare only insiders.
4. The expected reachable node count from every link root is at most `1000`
   (`LINKS_MAX_NODES`, `bzr` `src/types/bug/links.rs:13`). Above that `bzr bug links`
   truncates its walk and warns on stderr, which `BzrClient.read` discards on exit 0, so
   the bound is checked against the declared graph instead of trusted at read time.

`CompletedRecord.resolved_ids` is the only source of server identities
(`src/bzr_live/scenario/journal.py:385`); the verifier never re-derives one and never
looks a bug up by a numeric literal.

## Reader actors

Two roles, both chosen from the scenario's own declared actors:

- **insider** — the first declared actor whose `groups` include the fixture's insider
  group. That group is `admin`, set by
  `containers/bugzilla/checksetup_answers.txt:31` (`$answer{'insidergroup'} = 'admin'`).
  Every positive read is issued as this actor, so the observed state is the complete one.
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
  assert a member the replay had removed. PR #23 lists this as one of three
  postconditions that are not the server's final state.
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
  history — the attribution assertion is containment over `(who, field, new_value)`, and
  a removal's `new_value` is the residue rather than the removed member.

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
| `groups` | unverifiable | `bug view` neither serializes it nor accepts it in `--fields` (finding D3, confirmed live: `warning: ignoring unknown field(s): estimated_time, remaining_time, groups`) |
| `estimated_hours` | unverifiable | same reply and same finding D3 |
| `remaining_hours` | unverifiable | finding D3, and Bugzilla decrements it by logged work, so the declared value is not the final state (PR #23) |
| work-time hours | unverifiable | Bugzilla gates time-tracking fields on `timetrackinggroup`, deferred to #22; the `bug.worktime` comment is still asserted |

Three of the four fire on `scenarios/smoke/`. No event there declares a non-empty
`groups`, so that row is unexercised by the shipped scenario and a green smoke run is not
evidence about it.

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
`resolved_ids`), `keywords` (set), `cc` (set, per the fold above), `depends_on` and `blocks`
(sets, through `resolved_ids`), each declared `cf_*` (scalar equality, multi-select as a
set), and `flags` (a declared flag matches on `name`, `status`, and `requestee`; a declared
`X` status requires no entry with that name, mirroring
`src/bzr_live/replay/actions.py`'s reconciler).

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
`blocks` edge on the other bug, the `cc` record beside a flag, the `resolution` record
beside a `dupe_of`. `who` is the declaring actor's email.

The projection drops the value for fields whose history value is a generated numeric ID
(`depends_on`, `blocks`, `dupe_of`) — those assert `(who, field)` only. It keeps the value
for the symbolic ones: `summary`, `status`, `resolution`, `assigned_to`,
`target_milestone`, `keywords` (each added keyword is its own record: `'' -> 'perf'`),
`cc` (email), `flagtypes.name` (`review?(releaser@example.test)`), `attachments.isobsolete`
(`'0' -> '1'`), and each `cf_*` (a multi-select renders comma-space joined:
`'' -> 'cart, payment'`). Every rendering listed here was read from a live reply, not from
`bzr` source.

**Ordering** applies to the scalar chain fields — `status`, `resolution`, `assigned_to`,
`target_milestone`, `summary` — where each record's `old_value` is the previous record's
`new_value`. Records are grouped by `when` and the buckets ordered by `when`; the search
then looks for an ordering that permutes each bucket internally, links head to tail across
every bucket, and ends at the field's current value from `bug view`. The reconstructed
`who` sequence must contain the declared actor sequence as a subsequence.

The search is global, not bucket by bucket, and that is the load-bearing part.
`bzr bug history 9` returns `triager|status RESOLVED->CONFIRMED` and
`developer|status CONFIRMED->RESOLVED` sharing `2026-09-02T14:19:48Z`, then
`developer|status CONFIRMED->RESOLVED` at `:49Z`. That first bucket admits both orderings
on its own; only the `:49Z` record rules one out, because after
`triager, developer` the chain stands at `RESOLVED` and the next record's `old_value` is
`CONFIRMED`. A reconstruction that commits per bucket answers "ambiguous" here, where a
unique ordering exists. Verified against the live reply: the search returns
`developer → triager → developer` for both `status` and `resolution`, which is the
declared sequence. How many records share a `bug_when` depends on how fast the replay
ran, so the premise the design rests on is the stable one — records sharing a `bug_when`
come back in an order Bugzilla does not define — not any particular collision.

Candidate orderings are deduplicated by their `(who, old_value, new_value)` sequence, so
two records identical in all three are interchangeable rather than two answers; without
that, any bucket holding a repeated change is ambiguous by construction.

No assertion compares a timestamp; `when` is a sort key and a search bound only. The
reconstruction has three non-answers: no ordering links (a divergence), more than one
links, and a search larger than its budget. The last two are reported `unverifiable` for
that field rather than guessed.

`comment_id` is never asserted. It is best-effort correlation by `(who, second)` and is
observably wrong when an actor makes a commenting and a non-commenting change in the same
second — see *New finding* below.

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

### 6. Custom fields

Covered by check 1's explicit `--fields` read and by check 2's `cf_*` history records; no
separate read. The values were written through the stock-REST adapter and are read back
through `bzr`, which is the round trip the criterion asks for.

## Report and exit

One line per finding:

```
verify: <scenario_dir>: <alias>: <check>: <detail>
```

then `verify: <n> checks, <m> divergences, <k> unverifiable`. Exit 1 if `m > 0`, else 0.
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
  `next_safe_action` other than `advance`, missing reader role, and the link-node bound.

**Live**: `make smoke` runs `verify` after the replay, inside the same state root, and
fails the script on a non-zero exit.

**Read budget.** Each read is a `bzr` process spawn plus an HTTP round trip, measured at
roughly 0.34s for a `bug view` and 0.75s each for `history`, `links`, `comment list` and
`attachment list` against this fixture. Twenty bugs at five reads apiece would add on the
order of 70–90s to a smoke run that `README.md` currently records as 78.14s for the
replay. The two skips above — no `attachment list` for a bug with no declared attachment,
no recursive `links` read at eccentricity 1 — remove about a third of the calls. The
stage still lengthens `make smoke` materially, so `README.md`'s published figure is
re-measured with the verify stage in place, the way the fixture change before it was.

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
