# Confirm `groups` updates — implementation plan

**Goal.** Stop refusing a declared `bug.update` `groups` value on finding D3's now-false
premise: execute it as an add/remove delta, confirm it from `bug view`, carry it through
the verifier's expected-state fold, and give each `_UPDATE_ALWAYS_RETRY` time field a
rationale that names its own ground.

**Architecture.** `src/bzr_live/replay/actions.py` owns the refusal table, the `bug update`
argument construction, and the reconciliation comparison; `groups` moves out of the first
and into the other two. `src/bzr_live/verify/expected.py` owns the fold to expected state;
`groups` joins the name-set family it already belongs to on `bug.create`. Nothing else
moves: `VIEW_FIELDS` already requests `groups` and `checks._names` already asserts it.

**Tech stack.** Python 3.11+ via `uv`, no runtime dependencies, `unittest`.

Spec: [`../specs/2026-09-02-confirm-groups-updates-design.md`](../specs/2026-09-02-confirm-groups-updates-design.md),
which carries the measured evidence and the grounds for every choice below.
Decision record: the "Amended after the `groups` readback was measured" paragraphs in
[ADR 0006](../../adr/0006-actor-scoped-event-replay.md).

Expected implementation size: 200–255 changed lines (S) — ~15 in `actions.py`, ~18 in
`expected.py`, ~85 in `tests/test_replay.py`, ~35 in `tests/test_verify_expected.py`, ~20
for the fixture's three files (`verify-cc-order`, its model, is 19 lines), ~55 of
findings-register prose; summing to 228.

**Task order is load-bearing.** Task 1 is the fold, Task 2 the refusal removal. They are
independent as modules and not in behaviour: `expected.py:243-251` says the fold may ignore
a key only because `_UPDATE_UNSUPPORTED` refuses it first. Removing the refusal first opens
a one-commit window where the engine writes a declared `groups` update the verifier does
not assert — the silent gap issue #27 exists to close.

## Global Constraints

- **`bzr` floor.** `README.md:206` — "Use `b80303b7` or later, and treat anything below
  `63abb94e` as untested." Everything relied on here was measured at
  `bzr 0.8.3-dev (63abb94e)`; do not cite `b80303b7` for behaviour that starts at
  `a7f6ab70`.
- **Citation rule, `actions.py:18-22`.** A comment or message naming a *`bzr`* limitation
  cites its `docs/bzr-findings.md` entry as "(finding X)"; one whose ground is Bugzilla's
  own model names Bugzilla and cites no entry. **Cite D3 nowhere after this change.**
- **No silent substitution (`AGENTS.md`).** Never drop a declared field from a command.
- **Python style.** Match the surrounding file: 4-space indent, ≤ 95-char lines, no new
  imports, comments that carry a non-obvious ground rather than narrating the code.
- **Guardrails.** `make check` and `make test` green before each commit, run **bare** — no
  pipes, no `|| true`, no redirection.

## File map

| File | Change |
|---|---|
| `src/bzr_live/verify/expected.py` | modify — `_NAME_SETS`, `_create` loop, `_update_other` docstring, `UNVERIFIABLE_FIELDS` note |
| `src/bzr_live/replay/actions.py` | modify — refusal table, `build` delta loop, set-comparison table, always-retry comment |
| `tests/fixtures/verify-groups-update/{scenario.json,resources.json,events.jsonl}` | create — the scenario the fold test folds |
| `tests/test_verify_expected.py` | modify — fold regression test; correct the stale D3 comment at `:172-174` |
| `tests/test_replay.py` | modify — **delete** `test_update_rejects_groups`, add four tests, correct the stale D3 comment at `:640-642` |
| `docs/bzr-findings.md` | modify — D3's two now-false paragraphs, new D10 entry and table row |
| `docs/adr/0006-actor-scoped-event-replay.md` | modified in the design phase |
| `docs/workflow/specs/2026-09-02-confirm-groups-updates-design.md` | created in the design phase |

## Task 1 — the expected-state fold carries `groups`

All in `src/bzr_live/verify/expected.py`. Lands first: inert until Task 2 removes the
refusal, so it can never be the half that opens the gap.

### Interfaces

Consumed from the existing module, all confirmed present at `HEAD`:

- `_NAME_SETS: tuple[str, ...]` — `("cc", "keywords")`; read by `_create` and `_update`.
- `_update_names(bug: _Bug, actor: str, key: str, raw: object, emails: Mapping[str, str])
  -> None` — replaces `bug.names[key]` and appends one
  `ExpectedChange(actor, key, frozenset(added), False)` when the set grew.
- `_update_other(bug: _Bug, key: str, raw: object, declared: Mapping[str, object]) -> None`
  — the arm for keys that are neither scalar, name set, nor edge set.
- `_project(value: Reference, emails: Mapping[str, str]) -> str` — a `group` reference
  projects to its `name`; only an `actor` projects to an email.
- `UNVERIFIABLE_FIELDS: Mapping[str, str]` and its header comment.

No history-field translation is needed: Bugzilla's `Bug.history` maps its `bug_group`
activity field to the API name `groups` (`Bugzilla/WebService/Bug.pm:692-693`, read from
this fixture's image).

### Steps

1. **Create `tests/fixtures/verify-groups-update/`.** `tests/test_verify_expected.py`
   builds no scenario in memory — every fold site calls `fold(load_scenario(<dir>))` — so
   the test needs a directory. Copy the shape of `tests/fixtures/verify-cc-order/`:

   - `resources.json` — two `group` resources (`restricted`, `escalated`), one `actor`,
     one `product`, one `component`, one `version`;
   - `events.jsonl` — a `bug.create` for alias `guarded` declaring
     `"groups":[{"ref":"group:restricted"}]`, then a `bug.update` on it declaring
     `"groups":[{"ref":"group:restricted"},{"ref":"group:escalated"}]`;
   - `scenario.json` — the sibling's field set.

   Not `verify-cc-order`: its `test_created_groups_are_folded_as_an_asserted_field`
   (`:171-177`) asserts a create-only groups set that an update there would destroy.

2. **Write the failing regression test** in `tests/test_verify_expected.py`, folding the
   new fixture:

   - `bugs["guarded"].names["groups"] == frozenset({"restricted", "escalated"})`;
   - `ExpectedChange("<actor alias>", "groups", frozenset({"escalated"}), False)` is in
     that bug's `history`.

   Assert the *value*, not the key's presence: a fold that dropped the update leaves
   `names["groups"]` holding `{restricted}`, which a presence-only test would pass.

3. **Run it and confirm it fails.**

   ```
   uv run --python 3.11 python -m unittest tests.test_verify_expected -v
   ```

   Expect one failure: `frozenset({'restricted'})` where
   `frozenset({'restricted', 'escalated'})` was expected, because `_update_other` ignores
   the key.

4. **Add `groups` to `_NAME_SETS`**, making it `("cc", "keywords", "groups")`.

5. **Collapse `_create`'s loop** from `for key in (*_NAME_SETS, "groups")` to
   `for key in _NAME_SETS`.

6. **Correct the stale comment at `tests/test_verify_expected.py:172-174`**, which says
   "bug.update can never supply this case: actions._UPDATE_UNSUPPORTED refuses a groups
   update, so bug.create is the only path groups can arrive by". Both clauses stop being
   true here. Say instead what the test still covers — a create-time groups set never
   updated, folded as asserted rather than waived — and point at the new fixture for the
   update path. Assertions unchanged.

7. **Rewrite `_update_other`'s docstring**, which names `groups` among the held-back keys
   and forward-references issue #27 as pending. Replacement: ignoring anything else is safe
   only because `_UPDATE_UNSUPPORTED` refuses `version` before any mutation; `groups` was
   in that set until issue #27 and is now folded through `_update_names`; any key a future
   change releases from the refusal table needs an arm here, or the verifier will quietly
   stop asserting it.

8. **Correct the `UNVERIFIABLE_FIELDS` header note**, which infers the `groups` readback
   from the upstream commit. Replace the inference with the measurement: at `63abb94e`
   against the running fixture, a default-transport `bug view` returns `[]` for an
   unrestricted bug and the real member list for a bug restricted to a group — D8's header
   read draws a 401 there and `bzr`'s alternate-auth retry recovers it, the loud failure
   the time fields do not get. Keep the closing sentence: no `scenarios/smoke` bug declares
   one, so the assertion has unit coverage only.

9. **Run the test and confirm it passes.**

   ```
   uv run --python 3.11 python -m unittest tests.test_verify_expected -v
   ```

   Expect `OK`.

10. **Prove the test bites**, with a mutation isolating the *update* path: temporarily make
    `_update`'s dispatch skip `groups` so it falls through to `_update_other`, leaving
    `_NAME_SETS` and `_create` intact. Re-run step 9, observe step 3's mismatch in the new
    test alone, then revert.

    Do **not** mutate by dropping `groups` from `_NAME_SETS`: step 5 made `_create` read
    that tuple, so the create fold would stop too, the new test would fail on `KeyError`
    rather than the stated mismatch, and `test_created_groups_are_folded_as_an_asserted_
    field` would fail beside it — proving the create path bites, which was never in doubt.

11. **Run the guardrails, bare, and commit.**

    ```
    make check
    make test
    ```

    Expect both to exit 0, count at **383** — 382 at `HEAD` plus this task's one test.

### Acceptance criteria

- A declared `bug.update` `groups` value lands in `names["groups"]` and contributes one
  `ExpectedChange` for the members it added.
- The regression test has been observed red against a fold that drops the update.
- `_update_other` no longer forward-references issue #27, and names only `version`.
- No comment in `tests/test_verify_expected.py` still claims a `groups` update is refused.

## Task 2 — `groups` is executed and confirmed on `bug.update`

All in `src/bzr_live/replay/actions.py`. One task, not three: no reviewer could accept the
refusal removal while rejecting the argument construction that makes it honest — shipping
the first without the second is the silent substitution `AGENTS.md` forbids.

### Interfaces

Consumed from the existing module, all confirmed present at `HEAD`:

- `_UPDATE_UNSUPPORTED: dict[str, str]` — declared field → refusal text, read by
  `BugUpdateHandler.check_supported`.
- `_UPDATE_COMPARE_SETS: dict[str, tuple[str, Callable]]` — declared field →
  `(bug view key, lambda context, ref: <projection>)`, compared by `reconcile` as
  `{project(context, ref) for ref in values[field]} != set(bug.get(key))`.
- `_UPDATE_ALWAYS_RETRY: tuple[str, ...]` — fields that never confirm.
- `_delta(declared: list, observed: list) -> tuple[list, list]` — returns `(add, remove)`.
- `BugUpdateHandler.build(self, context, event) -> Invocation`, whose local `observed` is
  the `_bug_object(...)` dict read from `bug view` before the arguments are assembled.

Depends on Task 1 having landed the fold arm; nothing else crosses between them.

### Steps

1. **Delete `test_update_rejects_groups` and write four tests** in `tests/test_replay.py`,
   in the neighbouring `bug.update` fixture style. The deletion is not optional:
   `tests/test_replay.py:193-198` asserts `assertRaises(ReplayError)` on a `bug.update`
   declaring `groups` and asserts both `"groups"` and `"finding D3"` in the message, so
   step 3 makes it fail. The first test below takes its place.

   - a `bug.update` declaring `groups` passes `check_supported` without raising;
   - `build` on declared groups `{a, b}` against an observed `["b", "c"]` produces
     `--groups-add=a` and `--groups-remove=c`, and neither flag for a member already in
     agreement;
   - `reconcile` returns `"advance"` when the observed `groups` list equals the declared
     set, and `"retry"` when it differs;
   - `reconcile` returns `"retry"` for a `bug.update` declaring `estimated_hours` whose
     reply matches every other field. The field has no update-path coverage at `HEAD` —
     every existing mention is on the `bug.create` G1 path — so criterion 4's guard for it
     does not exist yet. `tests/test_replay.py:640-648` is the template.

   No `version` refusal test: `tests/test_replay.py:159-164` already is one, assertion for
   assertion, and passes before and after.

2. **Run them and confirm the red bar.**

   ```
   uv run --python 3.11 python -m unittest tests.test_replay -v
   ```

   Expect three failures and one pass, no errors: the refusal test on a raised
   `ReplayError`, the build test on absent `--groups-add`/`--groups-remove`, the `groups`
   reconcile test with `retry` where `advance` was expected. The `estimated_hours` test
   **passes already** — the field is in `_UPDATE_ALWAYS_RETRY` at `HEAD` — so it is a
   regression guard, and step 6 must not move it.

3. **Delete the `"groups"` entry from `_UPDATE_UNSUPPORTED`**, leaving `version` (G2) as
   the table's only entry.

4. **Add `groups` to the delta loop in `BugUpdateHandler.build`**, as its first tuple:

   ```python
   ("groups", "--groups-add", "--groups-remove", lambda r: r.name),
   ```

   Both flags exist at the floor (`src/cli/bug/update.rs:212-220` at `63abb94e`).

5. **Add the projection to `_UPDATE_COMPARE_SETS`**, first:

   ```python
   "groups": ("groups", lambda context, ref: ref.name),
   ```

   The set table, not `_UPDATE_COMPARE`, and equality rather than `cc`'s containment — the
   spec records both grounds.

6. **Replace the `_UPDATE_ALWAYS_RETRY` comment** so each field names its own ground; the
   tuple is unchanged and both fields stay. `estimated_hours`: Bugzilla gates the
   time-tracking fields on `timetrackinggroup` (`editbugs` here) and omits them from an
   otherwise-successful 200 for a caller that has not cleared it, so finding D8's
   unauthenticated read never sees the value and no error status fires `bzr`'s
   alternate-auth retry the way a group-restricted read does. `remaining_hours`: Bugzilla
   decrements `remaining_time` by logged work, so the declared value is not the fixture's
   final state — Bugzilla's own ground, surviving any `bzr` fix. Keep the trailing sentence
   about `check_supported`, with `groups` dropped from its list.

   What must leave: "bzr bug view never serializes either" is D3's claim and is false for
   both fields — `bzr` serializes them; Bugzilla withholds them.

7. **Correct the same stale claim at `tests/test_replay.py:640-642`**, which explains
   `test_set_retries_when_a_declared_field_is_unreadable` with "it declares
   `remaining_hours`, which bzr bug view never serializes". Replace with the field's real
   ground: Bugzilla decrements `remaining_time` by logged work. Behaviour unchanged.

8. **Run the tests and confirm they pass.**

   ```
   uv run --python 3.11 python -m unittest tests.test_replay -v
   ```

   Expect `OK`.

9. **Run the guardrails, bare, and commit.**

   ```
   make check
   make test
   ```

   Expect both to exit 0, count at **386** — Task 1 left 383, this task adds four and
   deletes one.

### Acceptance criteria

- A `bug.update` declaring `groups` is no longer refused; `version` still is, covered by
  the pre-existing `tests/test_replay.py:159-164`.
- The built argument list carries `--groups-add=` / `--groups-remove=` from the observed
  delta, and neither flag when declared and observed agree.
- Reconciliation advances on a matching `groups` set and retries on a differing one.
- Both time fields remain in `_UPDATE_ALWAYS_RETRY`, `estimated_hours` now under its own
  test, and nothing in the file cites D3.
- `test_update_rejects_groups` is gone rather than failing, and no comment in
  `tests/test_replay.py` still claims `bug view` omits the time fields.

## Task 3 — the findings register records what was measured

`docs/bzr-findings.md` only. Its conventions, read from the file: a summary-table row per
entry, a `## <ID>` section saying whether the behaviour was **observed** against a running
fixture or **read** from source, the `bzr` source at a commit, a class verdict, an
**Upstream** line, and a **What the fixture does** paragraph.

### Steps

1. **Update D3, replacing two paragraphs, not only inserting one.** It is already "defect,
   **fixed**" in the table and "Read from source" in its section; keep the description of
   the defect as found. Two present-tense paragraphs become false when Task 2 lands:

   - **`:135-139`, "What still depends on the defect"** says `actions.py` "has **not** been
     revisited: it still refuses a declared `groups` set on `bug.update`". Replace: it has
     now been revisited under issue #27 — the refusal is gone and the field confirms —
     while both time fields stay never-confirming, `estimated_hours` under D8 and
     `remaining_hours` under Bugzilla's decrement.
   - **`:146-149`, "What the fixture does"** says it "Refuses a declared `groups` set on
     `bug.update`". Replace with the post-change behaviour: executes it as an add/remove
     delta and confirms it by set comparison; both time fields still never confirm, on the
     two distinct grounds above.

   Then add the evidence: the `groups` half is now **observed** at `63abb94e` on the
   default transport, including for a group-restricted bug, by the
   401-then-alternate-auth-retry mechanism; the fix is *not* sufficient for the time
   fields, which stay unread under D8 because Bugzilla omits them from a successful 200
   rather than erroring.

2. **Add D10** — a section and a summary-table row: *`bzr` reports the first attempt's
   error when the alternate-auth retry also draws 401.* Observed at `63abb94e`, request
   captured through a local logging proxy: a `bug update --groups-add=admin` draws HTTP 401
   with code 410 on the header-authenticated PUT, and the query-parameter retry *is*
   authenticated and answered with code **120** ("not allowed to restrict bugs to this
   group in the 'checkout' product") — but because that fallback is also HTTP 401, `bzr`
   logs "auth fallback also failed, returning original 401" and reports the 410. The
   operator is told to log in when the real fault is a configuration gap.

   Class **defect**; upstream `hold: recording only` — filing on `randomparity/bzr` is not
   authorised for this campaign. What the fixture does: nothing, the path being unreachable
   while no group is settable here.

3. **Run the guardrails, bare, and commit.**

   ```
   make check
   make test
   ```

   Expect both to exit 0 with the count unchanged at 386; neither lints Markdown, so this
   checks that nothing else broke.

### Acceptance criteria

- D3 distinguishes what the upstream fix settled (`groups`, observed) from what it did not
  (the time fields, still unread under D8).
- Neither of D3's two present-tense paragraphs still says `actions.py` refuses a `groups`
  update.
- D10 exists with a table row, an observed-at revision, the captured evidence, a class
  verdict, and `hold: recording only`.

## Deferrals carried from the design review

None. The section exists so a later reader can tell an empty list from an omitted one.

## Follow-up discovered, not taken

**No bug group is settable on any product in this fixture**, so no scenario could exercise
a declared bug `groups` value even after this change. Per `AGENTS.md` the gap belongs in
`containers/`, not in a client-side refusal, and it is outside this issue's narrowed scope.
Report it to the campaign rather than taking it here. The ADR 0006 amendment holds the
measurement and the three residuals that wait on that gap.
