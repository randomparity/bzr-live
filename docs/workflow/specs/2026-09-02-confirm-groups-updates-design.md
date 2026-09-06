# Confirm `groups` updates — design

Issue [#27](https://github.com/randomparity/bzr-live/issues/27). Part of epic #7.
Decision record: the amendment to
[ADR 0006](../../adr/0006-actor-scoped-event-replay.md), "Amended after the `groups`
readback was measured".

## Problem

`src/bzr_live/replay/actions.py` refuses a declared `bug.update` `groups` value as a
precondition, citing finding **D3** — `bzr bug view` serialising no `groups` entry, so no
delta can be computed and no result confirmed. That premise is **empirically dead**, not
merely fixed upstream: at `bzr 0.8.3-dev (63abb94e)`, the revision `README.md:223` names as
this repository's floor, a default-transport `bug view` returns `groups: ['restricted']` for
a restricted bug. `a7f6ab70` (`bzr` PR #646, closing
[bzr#641](https://github.com/randomparity/bzr/issues/641)) adds `Groups` to the `Bug`
serialiser and is an ancestor of the floor, which is *why* the reading works — but the
citation this design rests on is the reading, not the commit.

So the refusal now declines work `bzr` can do, on a premise that stopped being true. That
is the failure `AGENTS.md` warns about from the other direction: asserting a stale limit
destroys the same evidence as hiding a real one.

**The second half of the premise is gone too.** When this design was first written the
fixture could not accept a `groups` write at all — `group_control_map` held no rows — so
removing the refusal would have traded `AGENTS.md`'s fail-before-mutating rule for a
mid-replay failure. Issue #34 closed that in the fixture where `AGENTS.md` says such a gap
belongs: `scenarios/smoke/resources.json` declares `restricted` settable on `checkout` and
`billing`, and the admin bridge writes the `group_control_map` row (finding G11, ADR 0013).

**#34 proved that over raw REST. This design proved the half #27 actually turns on — the
`bzr` client path — and it had not been run by anyone.** Measured at the floor
`bzr 0.8.3-dev (63abb94e)` against a fixture built from this branch, freshly installed and
provisioned from `scenarios/smoke`, as `admin-ops`; the image under test was confirmed to be
the tree under review first (`containers/bugzilla/bridge.pl` and the in-container
`/usr/local/bin/bzr-live-bridge` both `3bd69de6b659…`):

| Probe | Result |
|---|---|
| `bug update --groups-add=restricted -- 1` | exit 0, `"action": "updated"` |
| `bug view --fields=id,groups -- 1` | `{"id":1,"groups":["restricted"]}` |
| `bug_group_map` | one row, bug 1 → `restricted` |
| `bug update --groups-remove=restricted -- 1` | exit 0, `"action": "updated"` |
| `bug view --fields=id,groups -- 1` | `{"id":1,"groups":[]}`; `bug_group_map` back to 0 rows |

Both flags this design adds to `build` therefore work at the floor against a real Bugzilla,
and the read-back the reconciliation rests on returns the real value on the default
transport. The declared payload can execute, so the refusal is removed on that strength
rather than in spite of it.

`docs/adr/0006` carries the same stale premise twice — in its refusal list (line 149) and
in the rejected alternative "Apply a declared `groups` set on update as adds only"
(line 424) — so it needs a matching amendment.

## Goal

A declared `bug.update` `groups` value is executed as an add/remove delta against observed
server state and confirmed from `bug view`, exactly as `cc` and `keywords` already are; the
verifier's expected-state fold carries it so the assertion is real; and the two
`_UPDATE_ALWAYS_RETRY` time fields keep their non-confirming status under rationales that
each name their own ground.

Non-goals, each owned elsewhere: `estimated_hours` becoming confirmable and any change to
`bug view`'s transport (#30, finding D8); provisioning a product-settable bug group in
`containers/` (issue #34, landed — this design consumes it rather than waiting on it);
declaring a bug `groups` value in `scenarios/smoke` so the live tier exercises the path
(follow-up, below — that file is not on this issue's surface and its event count is a figure
`README.md` publishes and issue #35 re-measures).

## Evidence

Every claim below was measured by this design against the running fixture at
`bzr 0.8.3-dev (63abb94e)`, with an admin API key minted through the fixture's own bridge.
The fixture was restored to its prior state afterwards: no probe group, no
`bug_group_map` row, no minted key.

### `groups` is readable on the transport the engine uses

| Probe | `groups` |
|---|---|
| `bug view --fields=id,groups`, default transport, bug with no groups | `[]` |
| `bug view --fields=id,groups`, default transport, bug restricted to a group | `["<group>"]` |
| `--api xmlrpc bug view --fields=id,groups`, same bug | `["<group>"]` |
| `GET /rest/bug/<id>?include_fields=id,groups`, no credential | error 102, not authorised |

The third row is not a contradiction of the fourth, and the mechanism matters enough to
state. `bzr` serialises `groups` unconditionally (`src/types/bug.rs:242` at `63abb94e`;
the wire field is `Vec<String>` with `#[serde(default)]` at `:124`), and `bug_to_hash` returns
it with no permission gate (`Bugzilla/WebService/Bug.pm:1279-1281`, read from this
fixture's image). Finding **D8** still holds — `bzr`'s header auth is not real auth — but
for a group-restricted bug the server answers the header read with **HTTP 401**, and
`bzr`'s transport retries with its alternate auth method and gets a **200** carrying the
real value. Verbatim, at `RUST_LOG=debug`:

```
DEBUG bzr::client::transport: API response url=".../rest/bug/3" status=401 Unauthorized
DEBUG bzr::client::transport: 401 received, retrying with alternate auth method
DEBUG bzr::client::transport: auth fallback response url=".../rest/bug/3" status=200 OK
```

### Why `estimated_time` does not recover on the bugs the fixture is made of

Every row carries the *same* valid admin key; only the way it is presented differs, which
is the whole of the contrast.

| Probe | `estimated_time` |
|---|---|
| `bug view --fields=id,estimated_time`, default transport, key via `--server-api-key-env` | absent |
| `--api xmlrpc bug view --fields=id,estimated_time`, same key | `8.0` |
| `GET /rest/bug/1?...` with the key in an `X-BUGZILLA-API-KEY` header | absent |
| `GET /rest/bug/1?...&Bugzilla_api_key=<key>` | `8` |

Rows 2 and 4 are not a transport that reads more fields; they are the two paths on which
the key is actually *authenticated*. A run carrying no credential at all reproduces rows 1
and 3 exactly, which is what makes the point: on the default transport the presented key
changes nothing.

Bugzilla gates the time-tracking fields on `timetrackinggroup` (`editbugs` on this image)
and, for a caller that does not clear it, omits them from an otherwise-**successful 200**.
There is no error status, so nothing triggers `bzr`'s alternate-auth retry and the field
stays unread.

**All four rows above are reads of an unrestricted bug, and that qualifier is not decorative.**
Restrict the bug and the contrast disappears, because the retry is fired by the bug's
visibility and then carries an authenticated credential for everything in the reply. Measured
at the floor against the fixture's one group-restricted bug, as `admin-ops`:

| Probe | result |
|---|---|
| `bug view --fields=id,estimated_time,remaining_time,groups`, default transport | `{"id":1,"groups":["restricted"],"estimated_time":0.0,"remaining_time":0.0}` |
| `GET /rest/bug/1?...` with the key in an `X-BUGZILLA-API-KEY` header | HTTP 401, code 102 |
| `GET /rest/bug/1?...&Bugzilla_api_key=<key>` | HTTP 200 carrying `estimated_time` |

So `estimated_hours` is unreadable on every bug a caller can read anonymously — every bug
this fixture holds but that one — and readable on a bug restricted away from them. Keeping it
in `_UPDATE_ALWAYS_RETRY` is therefore a **conservative floor over the common case**, not a
claim the field can never be read, and the rationale written into `actions.py` must say so.
`groups` still moves and the time fields still do not, but the reason is the shape of the
request, not a property the fields carry.

**That contrast is the rule this design takes**, and it is a property of the *request*
rather than of the field. Under D8 a read is confirmable when Bugzilla either does not gate
the field against an anonymous caller — `groups` on an unrestricted bug — or refuses the
whole read with an error status that fires `bzr`'s alternate-auth retry **and** the
credential that retry carries is authorized for the bug; this fixture does parse that
query-parameter credential as real auth. It is unconfirmable when Bugzilla answers **200**
and silently omits the field, which is what it does to the time-tracking fields for a
caller that has not cleared `timetrackinggroup`. Where the retry's credential is *not*
authorized, the fallback 401s in its turn and `bzr` reports the first attempt's error
instead — the residual the ADR records.

The loudness in the `groups` case therefore comes from the bug's *visibility*, not from
anything about the field: `bug_access_denied` maps to `STATUS_NOT_AUTHORIZED`
(`Bugzilla/WebService/Constants.pm:270`), and it is that 401 which fires the retry. The two
time fields never shared a ground with `groups` or with each other, and the single shared
rationale over `_UPDATE_ALWAYS_RETRY` is what let D3's staleness cover both.

### `remaining_hours` was never `bzr`'s to answer for

Bugzilla decrements `remaining_time` by logged work, so the declared value is not the
server's final state. PR #23 established this independently of D3 and it survives the fix.
Per `actions.py:18-22` its rationale names Bugzilla and cites no findings entry.

## Design

### `src/bzr_live/replay/actions.py`

1. **Drop `groups` from `_UPDATE_UNSUPPORTED`**, leaving `version` (finding G2) alone.

2. **Emit the delta in `BugUpdateHandler.build`.** `groups` joins the add/remove loop that
   already serves `cc`, `keywords`, `depends_on` and `blocks`, projecting a group reference
   to its `name` and emitting `--groups-add=` / `--groups-remove=` against
   `observed.get("groups")`. Both flags exist at the floor
   (`src/cli/bug/update.rs:212-220` at `63abb94e`).

   This step is load-bearing and the issue does not name it. Removing the refusal without
   it would build a `bug update` that omits the declared field entirely — a silent
   substitution, which `AGENTS.md` forbids ahead of any other consideration here.

3. **Confirm it in `_UPDATE_COMPARE_SETS`**, not `_UPDATE_COMPARE`. `groups` is a Bugzilla
   list field whose delta is order-independent, so it compares as a set beside `cc` and
   `keywords`. The issue says "an `_UPDATE_COMPARE` projection"; the set table is the one
   that matches the field's shape, and putting it in the scalar table would compare a
   `tuple` of references against a JSON list and never match.

   It compares by **equality**, like `keywords`, not by containment like `cc`: Bugzilla can
   widen the set behind the caller (`Bugzilla/Bug.pm:1883` and `:1860-1864`), but only
   through a product's mandatory or default bug groups, which this fixture has none of. That
   ground survived #34 and got narrower: the bridge writes `membercontrol` and `othercontrol`
   as `CONTROLMAPSHOWN` only, while `groups_mandatory` selects `CONTROLMAPMANDATORY`
   (`Bugzilla/Product.pm:713-725`) and a default group needs `CONTROLMAPDEFAULT`
   (`:659-664`) — all read from this fixture's own image. The ADR records it as a rejected
   alternative and as a consequence.

4. **Give each `_UPDATE_ALWAYS_RETRY` field its own rationale.** The tuple stays a tuple —
   the file's own convention (lines 15-17) keeps grounds in comments rather than in
   operator-facing messages, and a dict whose values nothing reads would be ceremony, not
   structure. The comment gains one line per field: `estimated_hours` under D8's silent
   omission, `remaining_hours` under Bugzilla's decrement. Neither cites D3.

### `src/bzr_live/verify/expected.py`

5. **`groups` joins `_NAME_SETS`.** The fold already stores a created bug's groups in
   `bug.names` (`_create`, via `for key in (*_NAME_SETS, "groups")`), `VIEW_FIELDS` already
   requests `groups`, and `checks._names` already asserts `bug.names` by set equality. So
   the whole change is to let an *update* reach the same arm the *create* already uses:
   `_NAME_SETS` becomes `("cc", "keywords", "groups")` and `_create`'s loop collapses to
   `for key in _NAME_SETS`.

   `_update`'s dispatch then routes `groups` to `_update_names`, which replaces
   `bug.names["groups"]` and appends one `ExpectedChange(actor, "groups", frozenset(added),
   False)` when the set grew. The history field name is right: Bugzilla's `Bug.history`
   maps its `bug_group` activity field to the API name `groups`
   (`Bugzilla/WebService/Bug.pm:692-693`, read from this fixture's image).

6. **Rewrite `_update_other`'s docstring.** It currently forward-references this issue as
   pending work and names `groups` as one of the keys the refusal table holds back. After
   this change the fold ignores only `version`, which `_UPDATE_UNSUPPORTED` still refuses.

7. **Correct the `UNVERIFIABLE_FIELDS` header note.** Its claim that `groups` is readable
   was inferred from the upstream commit; it is now measured, and the sentence should say
   which transport and by what mechanism. Its other claim stays true and stays: no scenario
   declares a bug `groups` value, so the assertion has unit coverage only.

### `docs/bzr-findings.md`

8. **D3** is promoted from *read from source* to observed-fixed for `groups`, with the
   401-then-retry mechanism, and states plainly that the fix is sufficient for `groups`
   and not for the time fields — the correction issue #20's live run forced.

9. **No new entry, and no identifier minted.** This run's measurements surfaced a `bzr`
   defect — on the alternate-auth retry, when the fallback response also carries HTTP 401,
   `bzr` discards the fallback's body and reports the *first* attempt's error, so Bugzilla's
   `code 120` reaches the operator as `api_code 410` "You must log in" and they are sent to
   fix authentication on a request that was authenticated. **Recording it belongs to issue
   #39**, which owns it; the evidence this run took is handed over rather than written here.

   So this design *describes* the behaviour wherever it depends on it and cites no identifier
   for it. Earlier drafts called it "D10". That identifier was never written to
   `docs/bzr-findings.md` by anyone — the register runs D1 and D3-D9 plus G1-G11 — so citing
   it pointed at a record that does not exist, and minting it here would collide with #39's
   own numbering.

### `docs/adr/0006-actor-scoped-event-replay.md`

10. An in-place **"Amended after ..."** paragraph, following the precedent ADR 0008 set
    when its own live run corrected it. It withdraws the D3-grounded refusal and the
    matching rejected alternative, and records the loud-versus-silent confirmability rule
    the *Evidence* section establishes.

    No new ADR. The campaign pencilled 0011 for this issue; the decision here amends a
    record that already governs the refusal table, and the repository's own precedent for
    exactly that situation is an in-place paragraph. 0011 stays free.

## Trust boundary

The new argument is an operator-authored slug constrained to `[a-z][a-z0-9-]{0,62}`
(`loader.py:25`) that must resolve to a declared `group` resource, and it reaches `bzr`
through the same `shell=False` argv path `cc` and `keywords` already take — no boundary is
added and none widened. Group *semantics* are Bugzilla's, and nothing here asserts them.

## Testing

**Automated: unit only.** No scenario under `scenarios/` declares a bug `groups` value, so
`make smoke` is unchanged by this and the repository's live tier does not cover the path.
That is now a property of scenario *content*, not of fixture capability — issue #34 removed
the capability objection — and closing it means editing `scenarios/smoke`, which is off this
issue's surface. Reported as a follow-up rather than taken.

**Hand-run live proof, once, recorded not automated.** Unit tests over a fake `bzr` prove the
arguments this change constructs; they cannot prove that `bzr` accepts those arguments or
that Bugzilla answers them. So the built `bug update --groups-add=` is replayed once through
the real engine, the real floor binary and the provisioned fixture, and the transcript is
what the findings register and the PR cite. Without it this change would ship asserting a
`bzr` behaviour on the strength of a mock.

Existing tests and comments assert the behaviour being removed and are corrected in the
same commits rather than left to fail; the fold test needs a new fixture directory, since
`tests/test_verify_expected.py` folds only from those. The plan's file map names each.

| Test | Proves |
|---|---|
| `check_supported` accepts a `bug.update` declaring `groups` | the refusal is gone |
| `check_supported` still refuses `version` — the existing `test_update_rejects_version` covers this and needs no new test | the rest of the table is intact |
| `build` emits `--groups-add` / `--groups-remove` from the observed delta | criterion 2 — nothing declared is dropped |
| `build` emits neither flag when declared and observed already agree | the delta is a delta |
| `reconcile` advances on a matching `groups` set, retries on a differing one | criterion 1 |
| the fold carries a declared `groups` update into `names["groups"]` and one `ExpectedChange` | criterion 3 — the regression test the issue asks for |
| `reconcile` retries on a declared `estimated_hours`, as it already does on `remaining_hours` | criterion 4 — `estimated_hours` has no update-path coverage today |

The fold test is the one the issue names specifically, so it asserts the value and not just
the key's presence: a fold that dropped the update would leave `names["groups"]` holding
the *created* set, which a test asserting only "the key exists" would pass.

## What the live run proved, and what it found

Run at the floor `bzr 0.8.3-dev (63abb94e)` against a fixture built from this branch,
freshly installed and provisioned from `scenarios/smoke`, with the in-container bridge
confirmed byte-identical to the tree (`3bd69de6b659…`). Two replays through the real engine,
each with its own scenario name and therefore its own journal and server alias.

**Positive — the flag is built from the observed delta, sent, and reconciled.** A bug created
restricted, then a `bug.update` declaring `groups: []`, with a `BZR_LIVE_BZR` wrapper that
runs the real binary and then discards the reply. The completed record:

```
invocation.arguments : ["bug", "update", "--groups-remove=restricted", "6"]
exit_status          : -1        (_RECONCILED_EXIT -- written by _settle, so reconcile ran)
next_safe_action     : "advance"
handler_output.groups: []
```

`bug_group_map` confirms the removal landed. `exit_status` is what makes this more than a
happy path: a clean run writes `"advance"` at `engine.py:197` without ever calling
`reconcile`, so only `_RECONCILED_EXIT` proves the comparison executed.

**Negative — and this is the half that discriminates.** The same shape with the wrapper
faulting *before* the binary runs, so nothing reaches the server:

```
event 'restrict-guarded' failed: bzr boundary failure (exit 1) running
bug update --groups-add=restricted: fault: mutation never sent.
event 'restrict-guarded': declared 'groups' differs from the fixture
```

**Proved live that this bites**, by deleting `_UPDATE_COMPARE_SETS["groups"]` and re-running
the identical scenario: it **advanced**, exit 0, silently accepting a bug whose declared
`groups` never reached the server. Restored. That is the discrimination the positive run
cannot supply on its own — `reconcile` skips a field it does not know and advances — and it
is why this task's acceptance criterion was rewritten during design review.

### A pre-existing defect this run uncovered

`src/bzr_live/scenario/journal.py:115-132`'s `_validate_json` accepts `None`, `bool`, `int`,
`str`, list and mapping — **not `float`**. Bugzilla returns `estimated_time` and
`remaining_time` as JSON numbers, so any reconciliation whose `bug view` read is
*authenticated* records a payload the journal refuses, and the run aborts with
`journal:$.handler_output.estimated_time: contains an unsupported JSON value`.

The trigger is the mechanism this design already documents: a group-restricted read 401s,
`bzr`'s alternate-auth retry authenticates, and the time fields come back as floats.

**Not caused by this change, and that was established by control rather than asserted.** A
control replay with a `summary` update on an *unrestricted* bug reconciles cleanly — the read
is unauthenticated, so no float appears. A second control declaring `groups` **at create
time only** — a path `origin/main` fully supports and this change does not touch — reproduces
the abort exactly. `git diff origin/main` over `journal.py`, `engine.py`, `context.py` and
`adapters.py` is empty. This change widens the defect's reachability, because a bug can now
become restricted through `bug.update` as well as through `bug.create`; it does not create
it. Reported as a follow-up, not fixed here: the fix is a `float` arm in a shared journal
validator, which is its own change with its own round-trip and precision questions.

## Follow-up this design does not take

**No scenario under `scenarios/` declares a bug `groups` value**, so the path this change
opens has no coverage in the repository's own live tier. The fixture-side objection is gone —
issue #34 made `restricted` settable on `checkout` and `billing` — so what remains is a
scenario edit: a `bug.update` declaring `groups` on a `checkout` bug, authored by
`admin-ops`. Two reasons it is not taken here. `scenarios/smoke/` is not on this issue's
surface; and its event count is a figure `README.md` publishes and issue #35 is queued to
re-measure, so changing it now would stale a number a queued issue exists to fix.

Whoever takes it must use `admin-ops`, and for two independent reasons — both of which the
ADR records in full.

- **Bugzilla will refuse anyone else.** On the update path `add_group`
  (`Bugzilla/Bug.pm:3162-3167`) throws `group_restriction_not_allowed` for a caller outside
  the group; `remove_group` refuses the same caller at `:3205-3211` under a *different* error,
  `group_invalid_removal`. Both refusals are pre-mutation, so nothing is half-applied — and
  both reach the operator as `bzr`'s masked 410 "You must log in", because
  `WebService/Constants.pm:144-145` gives the two errors the same wire code 120 and `:276`
  maps 120 to `STATUS_NOT_AUTHORIZED`. Measured on both paths. (`_check_groups` at
  `:1851-1887` admits a non-member here, but it is the *create*-time validator only,
  registered at `VALIDATORS` `:122`, and it admits one only because #34's `group_control_map`
  rows carry `othercontrol = CONTROLMAPSHOWN`. The ADR carries the full derivation.)
- **The verifier's reader would abort, and so would the replay engine.** `INSIDER_GROUP` is
  `"admin"` (`src/bzr_live/verify/expected.py:16`, `:408-415`), so the reader is chosen by
  `admin` membership and not by the restricting group. Restrict a bug to a group the insider
  is outside and `ServerReader.bug` gets `api_code` 102, which `BUG_ABSENT_CODES` excludes on
  purpose, so it raises out of `_read_all` and ends the whole run rather than yielding one
  finding. The engine has the same exposure through `build` and `reconcile`, which read as the
  *event's* actor — and `build` is called outside `_execute`'s `try` (`engine.py:174`), so
  there it aborts on an unattributed boundary error. The obligation is therefore on every
  actor that later touches the bug, not only the insider. `admin-ops` is the one smoke actor
  in both `admin` and `restricted`, and nothing enforces that coincidence.

**One more payload class this change newly admits**, named here so it is not discovered at
replay time. Nothing checks that a declared group's `products` list (ADR 0013's field) covers
the target bug's product. A `bug.update` declaring, say, `groups: [group:editbugs]` on a
`checkout` bug is loader-valid, reaches Bugzilla, and is refused by `group_is_settable`
(`Bugzilla/Product.pm:740-748` — a system group carries `isbuggroup = 0`) with error 120,
which `bzr` again presents as an authentication message. A pre-mutation check is **declined
here, not overlooked**: the scenario contract holds everything needed for it, but the ground
would be Bugzilla's configuration rather than a `bzr` limitation, so per `actions.py:18-22` it
does not belong in the refusal table, and the natural home — validating a scenario's declared
groups against the products it declares — is `scenario/loader.py`, which is off this issue's
surface. Reported as a follow-up.

The ADR 0006 amendment is the durable record: it states the measurement, the three residuals
this change leaves standing, and why `groups` compares by equality. Read it there rather than
here.
