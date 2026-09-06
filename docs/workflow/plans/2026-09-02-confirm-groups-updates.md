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

- **`bzr` floor.** `README.md:223` — "Use `b80303b7` or later, and treat anything below
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
| `src/bzr_live/verify/expected.py` | modify — Task 1: `_NAME_SETS`, `_create` loop, `UNVERIFIABLE_FIELDS` note and its `estimated_hours` entry. Task 2: `_update_other` docstring, which lands with the refusal removal it describes |
| `src/bzr_live/replay/actions.py` | modify — refusal table, `build` delta loop, set-comparison table, always-retry comment |
| `tests/fixtures/verify-groups-update/{scenario.json,resources.json,events.jsonl}` | create — the scenario the fold test folds |
| `tests/test_verify_expected.py` | modify — fold regression test; correct the stale D3 comment at `:172-174` |
| `tests/test_replay.py` | modify — **delete** `test_update_rejects_groups`, add four tests, correct the stale D3 comments at `:486-487` and `:640-642` |
| `tests/test_fault_injection.py` | modify — correct the stale D3 comment at `:461-464`, which cites the finding by name |
| `docs/bzr-findings.md` | modify — D3's two now-false paragraphs ONLY. The masking defect's entry and G11's dangling sentence both belong to issue #39 |
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

7. *(moved to Task 2 step 7.)* `_update_other`'s docstring describes the refusal table's
   contents, which Task 2 is what changes. Rewriting it here would commit one intermediate
   state whose docstring says `_UPDATE_UNSUPPORTED` holds only `version` while it still holds
   two entries — breaking the inertness this task's whole ordering argument rests on, in the
   one direction a reader can see.

8. **Correct the `UNVERIFIABLE_FIELDS` header note**, which infers the `groups` readback
   from the upstream commit. Replace the inference with the measurement: at `63abb94e`
   against the running fixture, a default-transport `bug view` returns `[]` for an
   unrestricted bug and the real member list for a bug restricted to a group — D8's header
   read draws a 401 there and `bzr`'s alternate-auth retry recovers it, the loud failure
   the time fields do not get on a bug that never 401s. Keep the closing sentence: no
   `scenarios/smoke` bug declares one, so the assertion has unit coverage only.

   **Scope `UNVERIFIABLE_FIELDS["estimated_hours"]` in the same pass.** Its text —
   "`bug view` returns no `estimated_time` on this verifier's transport; only `--api xmlrpc`
   reads it back" — is the same over-broad claim Task 2 step 6 corrects in `actions.py`, and
   it sits three lines from the note being rewritten. It is false for a group-restricted bug,
   which this change is what makes reachable. Qualify it to the bugs the reader can read
   anonymously; leave the entry in the mapping, since that is the general case.

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

    Expect both to exit 0, count at **403** — 402 at `HEAD` plus this task's one test.
    (402, not the 382 an earlier draft of this plan named: the branch has since been
    refreshed onto `origin/main` at `0e8d8a2e`, which brought #25's and #34's test files.)

### Acceptance criteria

- A declared `bug.update` `groups` value lands in `names["groups"]` and contributes one
  `ExpectedChange` for the members it added.
- The regression test has been observed red against a fold that drops the update.
- No comment in `tests/test_verify_expected.py` still claims a `groups` update is refused.
- `UNVERIFIABLE_FIELDS`' header note cites the measurement rather than the upstream commit,
  and its `estimated_hours` entry no longer claims the field can never be read back.
- `_update_other`'s docstring is untouched here; it moves with the refusal it describes.
  This task changes nothing a reader could observe as inconsistent with `actions.py`.

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
   unauthenticated read never sees the value and no error status fires `bzr`'s alternate-auth
   retry. **Write that as a floor, not an absolute** — a group-restricted bug *does* 401 the
   whole read, the retry authenticates, and the time fields come back (measured at
   `63abb94e`), so the entry is conservative over the bugs a caller can read anonymously,
   which is every bug this fixture holds but one. Say the qualifier in the comment rather
   than shipping the unconditional claim; the spec's *Why `estimated_time` does not recover*
   section carries the measurement. `remaining_hours`: Bugzilla
   decrements `remaining_time` by logged work, so the declared value is not the fixture's
   final state — Bugzilla's own ground, surviving any `bzr` fix. Keep the trailing sentence
   about `check_supported`, with `groups` dropped from its list.

   What must leave: "bzr bug view never serializes either" is D3's claim and is false for
   both fields — `bzr` serializes them; Bugzilla withholds them.

7. **Rewrite `_update_other`'s docstring in `src/bzr_live/verify/expected.py`** — moved here
   from Task 1, because it describes the refusal table step 3 just emptied. It currently names
   `groups` among the held-back keys and forward-references issue #27 as pending. Replacement:
   ignoring anything else is safe only because `_UPDATE_UNSUPPORTED` refuses `version` before
   any mutation; `groups` was in that set until issue #27 and is now folded through
   `_update_names`; any key a future change releases from the refusal table needs an arm here,
   or the verifier will quietly stop asserting it.

   It is the one hunk of this task outside `actions.py`, and it belongs in this commit rather
   than the previous one: written in Task 1 it would have asserted a table state that did not
   exist yet.

8. **Correct the same stale claim at its three remaining sites**, all explaining why a
   `remaining_hours` payload can only reconcile as `retry`, and all giving D3's dead reason
   for it. Replace each with the field's real ground — Bugzilla decrements `remaining_time`
   by logged work, so the declared value is not the server's final state. Behaviour unchanged
   at every site; these are comments.

   - `tests/test_replay.py:640-642` — "it declares `remaining_hours`, which bzr bug view
     never serializes".
   - `tests/test_replay.py:486-487` — `_set_event`'s "which bzr bug view never serializes and
     would force retry regardless of what this checks".
   - `tests/test_fault_injection.py:461-464` — `_readable_update`'s "`remaining_hours`, which
     `bzr bug view` never serializes (finding D3)". **This one cites D3 by name**, so the
     Global Constraint "cite D3 nowhere after this change" fails without it. It is the only
     D3 citation outside `actions.py` and the register.

   Found by sweeping `rg 'never serializes|_UPDATE_UNSUPPORTED|does not return groups'` over
   `src tests docs README.md` rather than by trusting this plan's earlier file map, which
   named only the first.

9. **Run the tests and confirm they pass.**

   ```
   uv run --python 3.11 python -m unittest tests.test_replay -v
   ```

   Expect `OK`.

10. **Run the guardrails, bare, and commit.**

   ```
   make check
   make test
   ```

   Expect both to exit 0, count at **406** — Task 1 left 403, this task adds four and
   deletes one.

### Acceptance criteria

- A `bug.update` declaring `groups` is no longer refused; `version` still is, covered by
  the pre-existing `tests/test_replay.py:159-164`.
- The built argument list carries `--groups-add=` / `--groups-remove=` from the observed
  delta, and neither flag when declared and observed agree.
- Reconciliation advances on a matching `groups` set and retries on a differing one.
- Both time fields remain in `_UPDATE_ALWAYS_RETRY`, `estimated_hours` now under its own
  test, and nothing in the file cites D3.
- `estimated_hours`' rationale is written as a floor over the anonymously-readable case, not
  as a claim the field can never be read.
- `_update_other`'s docstring names only `version`, in the same commit that leaves only
  `version` in the table.
- `rg 'finding D3' src tests` returns nothing outside `docs/`.
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

   - **`:137-141`, "What still depends on the defect"** says `actions.py` "has **not** been
     revisited: it still refuses a declared `groups` set on `bug.update`". Replace: it has
     now been revisited under issue #27 — the refusal is gone and the field confirms —
     while both time fields stay never-confirming, `estimated_hours` under D8 and
     `remaining_hours` under Bugzilla's decrement.
   - **`:148-151`, "What the fixture does"** says it "Refuses a declared `groups` set on
     `bug.update`". Replace with the post-change behaviour: executes it as an add/remove
     delta and confirms it by set comparison; both time fields still never confirm, on the
     two distinct grounds above.

   Then add the evidence: the `groups` half is now **observed** at `63abb94e` on the
   default transport, including for a group-restricted bug, by the
   401-then-alternate-auth-retry mechanism; the fix is *not* sufficient for the time
   fields, which stay unread under D8 because Bugzilla omits them from a successful 200
   rather than erroring.

2. **Do NOT add an entry for the masking defect, and do not mint an identifier for it.**
   This run measured it completely — both halves on one fixture at the floor, on the add path
   and the removal path — but **issue #39 owns recording it**, filed while this work was in
   flight precisely because the identifier "D10" was cited across issues and campaign notes
   and had never been written to this file. `rg '\bD10\b'` matches nothing in the tree; the
   register runs D1 and D3-D9 plus G1-G11.

   Two consequences for this task. Nothing is added here. And `G11`'s sentence at `:602` —
   "That masking is its own finding and is not yet recorded in this file" — is **left alone**:
   it is a cross-reference #39 resolves when it writes the entry, and resolving it here would
   leave it pointing at nothing.

   The evidence is handed to #39 rather than written: `bzr bug update --groups-add=editbugs`
   reports `api_code 410` while the same write over raw REST with query-parameter auth reports
   `code 120`; `WebService/Constants.pm:144-145` maps both `group_restriction_not_allowed` and
   `group_invalid_removal` to 120 and `:276` maps 120 to `STATUS_NOT_AUTHORIZED`, so both the
   add and the removal refusal draw HTTP 401 — and because the *fallback* is also 401, `bzr`
   discards its body and reports the first attempt's error.

3. **Run the guardrails, bare, and commit.**

   ```
   make check
   make test
   ```

   Expect both to exit 0 with the count unchanged at 406; neither lints Markdown, so this
   checks that nothing else broke.

### Acceptance criteria

- D3 distinguishes what the upstream fix settled (`groups`, observed) from what it did not
  (the time fields, still unread under D8).
- Neither of D3's two present-tense paragraphs still says `actions.py` refuses a `groups`
  update.
- No identifier is minted for the masking defect anywhere in this change, and `G11`'s `:602`
  sentence is untouched — both belong to issue #39.

## Task 4 — the path is proven against the real fixture, once

Unit tests over a fake `bzr` prove the argument list this change builds; nothing in them
proves `bzr` accepts it or that Bugzilla answers it. Task 4 runs the change end to end and
records the transcript. No repository file changes here except the evidence quoted into
`docs/bzr-findings.md` and the PR body.

### Steps

1. **Establish the binary and the fixture, and state both.** `BZR_LIVE_BZR` is
   operator-selected at every call site, so name which binary produced the measurement.
   Use the documented floor, `/Volumes/Source Code Volume/src/bzr/target/release/bzr` =
   `bzr 0.8.3-dev (63abb94e)` — **not** the installed `0.9.0 (173772b3)`, which is issue
   #35's to adopt. The fixture must carry #34's provisioning; confirm the
   `group_control_map` row exists before trusting any result, because a container predating
   the merge would fail for the wrong reason. That check is the one an earlier worker in
   this campaign skipped.

2. **Replay a scenario declaring a `groups` update** through the real engine: a `bug.create`
   on `checkout` by `admin-ops`, then a `bug.update` on it declaring
   `groups: [{"ref": "group:restricted"}]`, then a second `bug.update` **narrowing the set
   back to empty**. Scratch scenario, not a repository file — the surface exclusion in the
   design's *Follow-up* section is what keeps it out of `scenarios/`. Step 6 folds this
   transcript into the positive fault run rather than replaying it separately.

   `admin-ops` is not a convenience. `add_group` (`Bugzilla/Bug.pm:3162-3167`) and
   `remove_group` (`:3195-3212`) both refuse a caller outside the group, so any other actor
   would prove only that Bugzilla says no.

   The narrowing event is what puts `--groups-remove` on a real server. The two flags are not
   symmetric there — `remove_group` carries a mandatory-group refusal `add_group` does not —
   so an add-only run would ship half the pair on the strength of a mock, which is the thing
   this task exists to prevent.

3. **Record what the engine emitted and what came back**: each `bug update` argument list,
   carrying `--groups-add=restricted` and then `--groups-remove=restricted`, and each exit
   status.

4. **Read the result back independently** with `bug view --fields=id,groups`, so the proof
   does not rest on the same code path it is testing.

5. **Drive the reconciliation arm deliberately, both ways.** A clean run never reaches it:
   `engine._execute` writes `next_action` `"advance"` at `engine.py:197` straight after a
   zero-exit invocation, and `BugUpdateHandler` inherits `ActionHandler.resolved_ids`
   returning `{}` (`actions.py:264-265`), which cannot raise. `reconcile` is reached only
   from `_settle`, inside the `except (ProvisionError, ReplayError)` arm. So a plain replay
   would record `"advance"` whether or not Task 2 step 5 ever added the projection — the
   criterion this step replaces was satisfiable by code that does not exist.

   Reach it with a `BZR_LIVE_BZR` wrapper, the boundary the repository already treats as
   operator-selected. **The wrapper must be a pass-through that faults only on `bug update`.**
   `BZR_LIVE_BZR` names the binary the engine uses for *every* invocation, and three reads
   have to succeed before the comparison can happen at all: `_sweep_pristine`'s `bug view`
   (`engine.py:117-119` → `_require_absent` `:105-115`), `BugUpdateHandler.build`'s pre-read
   that computes the delta (`actions.py:371-372`), and `reconcile`'s own read (`:415`). A
   wrapper that failed every call would abort at the sweep having executed no event. The
   `build` read is the one that must not be touched under any circumstance: it is called at
   `engine.py:174`, *outside* `_execute`'s `try`, so a failure there is not routed into
   `_settle` — it kills the run instead of reconciling it.

   So: forward `argv` to the floor binary unchanged and return its exit status, except when
   `argv` contains the `bug update` subcommand. Two runs, and the pair is the proof:

   - **positive** — on `bug update`, run the binary, forward its stdout and stderr, then exit
     non-zero. (Run it; do not `exec` it — `exec` replaces the process image and nothing after
     it would execute.) The server applied the change; `reconcile` reads the bug back, the
     declared set matches, and `_execute` returns `"reconciled"` with `_RECONCILED_EXIT` in
     the journal.
   - **negative** — on `bug update`, exit non-zero *without* running the binary. Nothing was
     applied; `reconcile` reads the bug back, the declared set differs, and the run must fail
     with `declared 'groups' differs from the fixture`.

   The negative is the half that bites. The positive alone advances even for a field absent
   from `_UPDATE_COMPARE_SETS`, because `reconcile` skips what it does not know; only the
   mismatch distinguishes a live projection from a missing one. This is the repository's own
   controlled-fault discipline applied to a live server rather than to a double.

6. **Give each run its own scenario name and its own state root.** Two engine preconditions
   otherwise refuse the second and third replays outright, and neither is negotiable:
   `_require_empty_journal` (`engine.py:53-67`) refuses `replay` if the journal under the
   state root holds *any* attempt file, and `_require_absent` (`:105-115`) refuses when the
   bug alias already exists in the fixture. The alias is not derived from the scenario
   digest — `loader.py:719-723` hashes the scenario **name** and the bug alias — so editing a
   scenario's events does not produce a fresh one, but renaming the scenario does.

   That is the whole of the fix, and it is cheaper than a fixture reset per run: each replay
   gets a scratch scenario whose `name` is unique to it and a fresh `--state-root`. The
   positive fault run also carries step 2's transcript evidence, since it applies both updates
   for real *and* exercises the reconciliation arm, so the task needs two replays rather than
   three.

7. **Report what was left in the fixture.** The runs deliberately leave their bugs behind
   rather than resetting between them; the negative run additionally leaves a completed record
   holding `retry`. Say so, and say whether the fixture was reset afterwards.

### Acceptance criteria

- Both `bug update` invocations `bzr` actually ran are quoted verbatim — one carrying
  `--groups-add=restricted`, one carrying `--groups-remove=restricted` — and the binary that
  ran them is named by version and revision.
- The declared group is present in an independent `bug view` read-back after the add, and
  absent after the remove.
- The positive fault run records `"reconciled"`; the negative fault run fails naming
  `groups`. If either does not, the divergence is reported as the finding rather than worked
  around.

## Deferrals carried from the design review

None. The section exists so a later reader can tell an empty list from an omitted one.

## Follow-up discovered, not taken

**No scenario under `scenarios/` declares a bug `groups` value**, so the repository's own
live tier does not cover the path this change opens. The fixture-side blocker is gone — #34
made `restricted` settable on `checkout` and `billing` — leaving a `scenarios/smoke` edit
that is off this issue's surface and would stale an event count issue #35 is queued to
re-measure. Report it to the campaign rather than taking it here, and note that the author
must use `admin-ops`: the ADR 0006 amendment records why any other smoke actor would abort
the run.
