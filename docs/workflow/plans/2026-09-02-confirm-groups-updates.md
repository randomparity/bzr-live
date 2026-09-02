# Confirm `groups` updates — implementation plan

**Goal.** Stop refusing a declared `bug.update` `groups` value on finding D3's now-false
premise: execute it as an add/remove delta, confirm it from `bug view`, carry it through
the verifier's expected-state fold, and give each `_UPDATE_ALWAYS_RETRY` time field a
rationale that names its own ground.

**Architecture.** Two runtime modules change. `src/bzr_live/replay/actions.py` owns the
per-action supported-payload table, the `bug update` argument construction, and the
reconciliation comparison; `groups` moves out of the refusal table and into the
delta-emitting loop and the set-comparison table. `src/bzr_live/verify/expected.py` owns
the fold from scenario events to expected end state; `groups` joins the name-set family it
already belongs to on `bug.create`. Nothing else moves: `VIEW_FIELDS` already requests
`groups` and `checks._names` already asserts it by set equality.

**Tech stack.** Python 3.11+ via `uv`, no runtime dependencies, `unittest` for tests.

Spec: [`../specs/2026-09-02-confirm-groups-updates-design.md`](../specs/2026-09-02-confirm-groups-updates-design.md).
Decision record: the "Amended after the `groups` readback was measured" paragraph in
[ADR 0006](../../adr/0006-actor-scoped-event-replay.md).

Expected implementation size: 180–260 changed lines (S) — from the file map below: ~15
lines in `actions.py`, ~18 in `expected.py`, ~135 of tests across two files, ~50 of
findings-register prose.

## Global Constraints

- **`bzr` floor.** `README.md:180` — "Use `b80303b7` or later, and treat anything below
  `63abb94e` as untested." Every behaviour this change relies on was measured at
  `bzr 0.8.3-dev (63abb94e)`. Do not cite `b80303b7` for a behaviour that only exists from
  `a7f6ab70` on.
- **Citation rule, `src/bzr_live/replay/actions.py:18-22`.** A message or comment naming a
  *`bzr`* limitation cites its `docs/bzr-findings.md` entry as "(finding X)". One whose
  ground is Bugzilla's own model names Bugzilla and cites no entry. Do not cite D3 anywhere
  after this change: it is fixed.
- **No silent substitution (`AGENTS.md`).** Never send a payload the scenario did not
  declare, and never drop a declared field from a command.
- **Python style.** Follow the surrounding file: 4-space indent, ≤ 95-char lines to match
  the existing wrap width, no new imports, comments that carry a non-obvious ground rather
  than narrating the code.
- **Guardrails.** `make check` and `make test` must both be green before each commit. Run
  them bare — no pipes, no `|| true`, no redirection to `/dev/null`.

## File map

| File | Change |
|---|---|
| `src/bzr_live/replay/actions.py` | modify — refusal table, `build` delta loop, set-comparison table, always-retry comment |
| `src/bzr_live/verify/expected.py` | modify — `_NAME_SETS`, `_create` loop, `_update_other` docstring, `UNVERIFIABLE_FIELDS` note |
| `tests/test_replay.py` | modify — `groups` refusal, build-delta, and reconcile tests |
| `tests/test_verify_expected.py` | modify — the fold regression test |
| `docs/bzr-findings.md` | modify — D3 status, new D10 entry, register table row |
| `docs/adr/0006-actor-scoped-event-replay.md` | modified in the design phase — amendment paragraph |
| `docs/workflow/specs/2026-09-02-confirm-groups-updates-design.md` | created in the design phase |

## Task 1 — `groups` is executed and confirmed on `bug.update`

All in `src/bzr_live/replay/actions.py`. One task, not three: no reviewer could accept the
refusal removal while rejecting the argument construction that makes it honest — shipping
the first without the second is the silent substitution `AGENTS.md` forbids.

### Interfaces

Consumed from the existing module, all confirmed present at `HEAD`:

- `_UPDATE_UNSUPPORTED: dict[str, str]` — declared field → refusal text, read by
  `BugUpdateHandler.check_supported`.
- `_UPDATE_COMPARE_SETS: dict[str, tuple[str, Callable]]` — declared field →
  `(bug view key, lambda context, ref: <projection>)`, read by `BugUpdateHandler.reconcile`
  and compared as `{project(context, ref) for ref in values[field]} != set(bug.get(key))`.
- `_UPDATE_ALWAYS_RETRY: tuple[str, ...]` — declared fields that never confirm.
- `_delta(declared: list, observed: list) -> tuple[list, list]` — returns `(add, remove)`.
- `BugUpdateHandler.build(self, context, event) -> Invocation`, whose local `observed` is
  the `_bug_object(...)` dict read from `bug view` before the arguments are assembled.

Task 2 relies on nothing from this task; the two are independent.

### Steps

1. **Write four failing tests** in `tests/test_replay.py`, beside the existing `bug.update`
   cases and in their fixture style:

   - a `bug.update` declaring `groups` passes `check_supported` without raising;
   - a `bug.update` declaring `version` still raises `ReplayError`;
   - `build` on declared groups `{a, b}` against an observed `["b", "c"]` produces
     `--groups-add=a` and `--groups-remove=c`, and emits neither flag for a member already
     in agreement;
   - `reconcile` returns `next_action == "advance"` when the observed `groups` list equals
     the declared set, and `"retry"` when it differs.

2. **Run them and confirm they fail.**

   ```
   uv run --python 3.11 python -m unittest tests.test_replay -v
   ```

   Expect four failures, no errors: a raised `ReplayError` on the refusal test, absent
   `--groups-add`/`--groups-remove` on the build test, `retry` where `advance` was expected
   on the reconcile test.

3. **Delete the `"groups"` entry from `_UPDATE_UNSUPPORTED`.** `version` (finding G2)
   stays and becomes the table's only entry.

4. **Add `groups` to the delta loop in `BugUpdateHandler.build`**, as the loop's first
   tuple so the flag order matches the table order elsewhere in the file:

   ```python
   ("groups", "--groups-add", "--groups-remove", lambda r: r.name),
   ```

   Both flags exist at the floor: `src/cli/bug/update.rs:212-220` at `63abb94e` declares
   `groups_add` and `groups_remove`.

5. **Add the projection to `_UPDATE_COMPARE_SETS`**, first, for the same reason:

   ```python
   "groups": ("groups", lambda context, ref: ref.name),
   ```

   The set table, not `_UPDATE_COMPARE`: `groups` is a Bugzilla list field, and the scalar
   table compares a declared value against `bug.get(key)` by equality, which a tuple of
   references can never satisfy against a JSON list.

6. **Replace the `_UPDATE_ALWAYS_RETRY` comment** so each field names its own ground. The
   tuple is unchanged — both fields stay. The replacement states: `estimated_hours` —
   Bugzilla gates the time-tracking fields on `timetrackinggroup` (`editbugs` on this
   image) and omits them from an otherwise-successful 200 for a caller that does not clear
   it, so finding D8's unauthenticated REST read never sees the value and no error status
   fires `bzr`'s alternate-auth retry the way a group-restricted read does;
   `remaining_hours` — Bugzilla decrements `remaining_time` by logged work, so the declared
   value is not the fixture's final state, a ground that is Bugzilla's own and survives any
   `bzr` fix. Keep the existing trailing sentence about `check_supported` having already
   refused the other fields, with `groups` dropped from its list.

   What must leave: the old comment's "bzr bug view never serializes either" is D3's claim
   and is now false for both fields — `bzr` serializes them; Bugzilla withholds them.

7. **Run the tests and confirm they pass.**

   ```
   uv run --python 3.11 python -m unittest tests.test_replay -v
   ```

   Expect `OK`, no failures, no errors.

8. **Run the guardrails, bare, and commit.**

   ```
   make check
   make test
   ```

   Expect both to exit 0; `make test` reports at least 382 tests — the count after #20
   landed — plus the four added here.

### Acceptance criteria

- A `bug.update` declaring `groups` is no longer refused; one declaring `version` still is.
- The built argument list carries `--groups-add=` / `--groups-remove=` derived from the
  observed set, and carries neither when declared and observed agree.
- Reconciliation advances on a matching `groups` set and retries on a differing one.
- Both time fields remain in `_UPDATE_ALWAYS_RETRY`, and nothing in the file cites D3.

## Task 2 — the expected-state fold carries `groups`

All in `src/bzr_live/verify/expected.py`. Independent of Task 1.

### Interfaces

Consumed from the existing module, all confirmed present at `HEAD`:

- `_NAME_SETS: tuple[str, ...]` — currently `("cc", "keywords")`; read by `_create` and by
  `_update`'s dispatch.
- `_update_names(bug: _Bug, actor: str, key: str, raw: object, emails: Mapping[str, str])
  -> None` — replaces `bug.names[key]` with the declared set and appends one
  `ExpectedChange(actor, key, frozenset(added), False)` when the set grew.
- `_update_other(bug: _Bug, key: str, raw: object, declared: Mapping[str, object]) -> None`
  — the arm for declared update keys that are neither scalar, name set, nor edge set.
- `_project(value: Reference, emails: Mapping[str, str]) -> str` — a `group` reference
  projects to its `name`; only an `actor` projects to an email.
- `UNVERIFIABLE_FIELDS: Mapping[str, str]` and its header comment.

The history field name is right without translation: Bugzilla's `Bug.history` maps its
`bug_group` activity field to the API name `groups`
(`Bugzilla/WebService/Bug.pm:692-693`, read from this fixture's own image).

### Steps

1. **Write the failing regression test** in `tests/test_verify_expected.py`. Fold a
   scenario whose `bug.create` declares groups `{a}` and whose later `bug.update` declares
   groups `{a, b}`, then assert both halves:

   - `fold(scenario).bugs[<alias>].names["groups"] == frozenset({"a", "b"})`;
   - `ExpectedChange(<actor alias>, "groups", frozenset({"b"}), False)` is in that bug's
     `history`.

   Assert the *value*, not the key's presence: a fold that dropped the update would leave
   `names["groups"]` holding the created set `{a}`, and a test asserting only that the key
   exists would pass against exactly the defect this test is for.

2. **Run it and confirm it fails.**

   ```
   uv run --python 3.11 python -m unittest tests.test_verify_expected -v
   ```

   Expect one failure: `names["groups"]` is `frozenset({'a'})` where `frozenset({'a',
   'b'})` was expected, because `_update_other` ignores the key.

3. **Add `groups` to `_NAME_SETS`**, making it `("cc", "keywords", "groups")`.

4. **Collapse `_create`'s loop** from `for key in (*_NAME_SETS, "groups")` to
   `for key in _NAME_SETS`; it listed `groups` separately only because it was not in the
   tuple.

5. **Rewrite `_update_other`'s docstring.** It currently names `groups` among the keys the
   refusal table holds back and forward-references issue #27 as pending. The replacement
   says: ignoring anything else is safe only because `_UPDATE_UNSUPPORTED` refuses
   `version` before any mutation; `groups` was in that set until issue #27 and is now
   folded through `_update_names`; any key a future change releases from the refusal table
   needs an arm here, or the verifier will quietly stop asserting it.

6. **Correct the `UNVERIFIABLE_FIELDS` header note.** It currently infers the `groups`
   readback from the upstream commit. Replace the inference with the measurement: at
   `63abb94e` against the running fixture, a default-transport `bug view` returns `[]` for
   an unrestricted bug and the real member list for a bug restricted to a group — finding
   D8's header read draws a 401 there and `bzr`'s alternate-auth retry recovers it, which
   is the loud failure the time fields below do not get. Keep the existing closing
   sentence: no `scenarios/smoke` bug declares one, so the assertion has unit coverage only
   and the live tier has never exercised it.

7. **Run the test and confirm it passes.**

   ```
   uv run --python 3.11 python -m unittest tests.test_verify_expected -v
   ```

   Expect `OK`.

8. **Prove the test bites.** Temporarily make `_update` route `groups` to `_update_other`
   again, re-run the command in step 7, observe the failure from step 2, then revert.

9. **Run the guardrails, bare, and commit.**

   ```
   make check
   make test
   ```

   Expect both to exit 0.

### Acceptance criteria

- A declared `bug.update` `groups` value lands in the folded bug's `names["groups"]` and
  contributes one `ExpectedChange` for the members it added.
- The regression test has been observed red against a fold that drops the update.
- `_update_other` no longer forward-references issue #27, and names only the key the
  refusal table still holds.

## Task 3 — the findings register records what was measured

`docs/bzr-findings.md` only. The register's conventions, read from the file: a
summary-table row per entry (`| [ID](#anchor) | Class | Summary | Upstream |`), a `## <ID>`
section stating whether the behaviour was **observed** against a running fixture or
**read** from source, the `bzr` source that establishes it at a commit, a class verdict, an
**Upstream** line, and a **What the fixture does** paragraph.

### Steps

1. **Update D3.** It is already "defect, **fixed**" in the table and "Read from source" in
   its section. Add, after the existing upstream paragraph: the `groups` half is now
   **observed** at `63abb94e` on the default transport, including for a group-restricted
   bug, by the 401-then-alternate-auth-retry mechanism; and the fix is *not* sufficient for
   the two time fields, which stay unread under D8 because Bugzilla omits them from a
   successful 200 rather than erroring. Keep the existing description of the defect as
   found.

2. **Add D10** — a new section and a summary-table row: *`bzr` reports the first attempt's
   error when the alternate-auth retry also draws 401.*

   Observed at `63abb94e` against the running fixture, request captured through a local
   logging proxy. A `bug update --groups-add=admin` sends the header-authenticated PUT and
   gets HTTP 401 with Bugzilla code 410 ("You must log in"); the retry with
   `Bugzilla_api_key` as a query parameter is genuinely authenticated and Bugzilla answers
   it with code **120** ("you are not allowed to restrict bugs to this group in the
   'checkout' product"). Because the fallback's HTTP status is also 401, `bzr` logs "auth
   fallback also failed, returning original 401" and reports the first attempt's 410. The
   operator is told to log in when the real fault is a product/group configuration gap.

   Class: **defect** — the fallback carried a substantive, authenticated server error and
   it was discarded. Upstream: `hold: recording only` — filing on `randomparity/bzr` is not
   authorised for this campaign. What the fixture does: nothing; the path is unreachable
   today, because no scenario declares a bug `groups` value and no group is settable on any
   product in this fixture.

3. **Run the guardrails, bare, and commit.**

   ```
   make check
   make test
   ```

   Expect both to exit 0. Neither lints Markdown, so this checks that nothing else broke.

### Acceptance criteria

- D3's section distinguishes what the upstream fix settled (`groups`, observed) from what
  it did not (the time fields, still unread under D8).
- D10 exists with a summary-table row, an observed-at revision, the captured evidence, a
  class verdict, and `hold: recording only`.
- No entry in the register cites D3 as a live constraint on `groups`.

## Deferrals carried from the design review

None. This section exists so a later reader can tell an empty list from an omitted one; a
deferral the design review records belongs here with its owning record path or tracker
issue.

## Follow-up discovered, not taken

**No bug group is settable on any product in this fixture.** `group_control_map` is empty
and every group `checksetup` creates has `isbuggroup = 0`, so an authenticated `groups.add`
returns Bugzilla error 120 for any group name, and nothing in `containers/`, `bridge.pl`,
or the provisioning boundary table writes that mapping. Per `AGENTS.md` the gap belongs in
`containers/`, not in a client-side refusal; it is outside this issue's narrowed scope and
unexercised, since no scenario declares a bug `groups` value. Report it to the campaign
rather than taking it here.
