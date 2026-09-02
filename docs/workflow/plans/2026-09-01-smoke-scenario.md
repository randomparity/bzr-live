# Implementation plan: smoke scenario (issue #19)

**Goal.** Commit `scenarios/smoke/` — 20 bugs, 47 events, two products — and prove it loads
offline and replays against the live pinned Bugzilla fixture through a real `bzr` binary.

**Architecture and stack** are the spec's; this plan does not restate them. The one thing to
carry into every task: the fixture is data validated by the existing loader, and no module
under `src/` changes.

Expected implementation size: 480–560 changed lines (M) — derived from the file map below:
five fixture files (250–320 lines, dominated by 28 pretty-printed resources and 48 event
lines), two proof files (~220), and Makefile/README edits (~30). The band stays M: the volume
is declarative data, the cyclomatic surface is two files, and no reviewed logic path changes.
An earlier 320–430 understated the fixture against this repository's existing formatting,
which the scope audit measured against `tests/fixtures/replay-scenario/`.

Spec: [`docs/workflow/specs/2026-09-01-smoke-scenario-design.md`](../specs/2026-09-01-smoke-scenario-design.md).
Decision record: [`docs/adr/0007-committed-smoke-scenario.md`](../../adr/0007-committed-smoke-scenario.md).

## Global constraints

Every task's requirements include this section.

**The twelve contract constraints in the spec's "Contract constraints that shape the fixture"
table bind every task here, with their citations.** They are not restated: a second copy is a
second thing to keep in agreement with the loader, and the spec's table is the one that was
reviewed. Read it before writing a single event.

Only these four are not in that table:

- **Python 3.11**, invoked as `uv run --python 3.11`. The package under `src/bzr_live/` has no
  runtime dependencies and gains none here.
- **No change to `src/`.** The loader, replay engine, journal, and provisioning executor are
  read, not modified. An expressive gap is a finding for `docs/bzr-findings.md`, never a
  contract change.
- **Line length** 100 characters **for shell and Python only**. It cannot bind
  `events.jsonl`, whose format is one JSON object per line and whose lines run to several
  hundred characters. No repository gate enforces line length in any case — `make check` runs
  `bash -n`, `shellcheck`, `compileall`, and `docker compose config`.
- **Secrets** never appear in committed files, command lines, or script output.
- **Guardrails**: `make check` and `make test` must be green at every commit.

## File map

| Path | Status | Responsibility |
|---|---|---|
| `scenarios/smoke/scenario.json` | new | Format version, scenario name `smoke`, description, asset manifest with SHA-256 digests |
| `scenarios/smoke/resources.json` | new | The resource catalog: groups referenced, actors, products, components, versions, milestones, keywords, custom fields, flag types |
| `scenarios/smoke/events.jsonl` | new | 47 ordered events, one JSON object per line |
| `scenarios/smoke/assets/triage-notes.txt` | new | Attachment payload for `cart-double-charge` |
| `scenarios/smoke/assets/retry-fix.patch` | new | Attachment payload for `pay-retry-loop` |
| `tests/test_smoke_scenario.py` | new | Offline invariants over the committed scenario |
| `tests/smoke_scenario.sh` | new | Live provision + replay proof, reports observed duration |
| `Makefile` | modified | `smoke` target; the new script added to `check`'s `bash -n` and `shellcheck` lists |
| `README.md` | modified | Smoke scenario section: what it covers, how to run it, observed duration |
| `docs/bzr-findings.md` | modified if the live run finds something | New or promoted findings only |

## Task 1 — The scenario fixture and its offline proof

**Where this fits.** The whole deliverable except the live script. It ends at something
testable on its own: `make test` passes with the new offline case exercising the committed
fixture.

**Creates:** `scenarios/smoke/scenario.json`, `scenarios/smoke/resources.json`,
`scenarios/smoke/events.jsonl`, `scenarios/smoke/assets/triage-notes.txt`,
`scenarios/smoke/assets/retry-fix.patch`, `tests/test_smoke_scenario.py`.

**Interfaces consumed.** From `src/bzr_live/scenario` (confirmed present at 27d37a4):

- `load_scenario(path: str | Path) -> ValidatedScenario` — `src/bzr_live/scenario/loader.py:815`
- `ScenarioValidationError` — raised as `<file>:<line>:<json-path>: <message>`
- `ValidatedScenario` fields used here: `.name`, `.digest`, `.events`, `.resources`
  (`src/bzr_live/scenario/model.py`)
- `PlannedEvent` fields used here: `.name`, `.actor`, `.action`, `.action_class`,
  `.expected_postcondition`, `.reconciliation_marker`, `.creates`
  (`src/bzr_live/scenario/model.py:44-54`)
- `PlannedResource` fields used here: `.kind`, `.name`, `.data`

From `src/bzr_live/replay`:

- `HANDLERS: dict[str, ActionHandler]` — `src/bzr_live/replay/actions.py:691`
- `ATTACHMENT_SUMMARY_BYTE_LIMIT: int` — `src/bzr_live/replay/actions.py:13`
- `render_attachment_summary(description, marker, sha256) -> str` —
  `src/bzr_live/replay/actions.py:81`

**Interfaces produced.** None importable; Task 2 depends only on the directory
`scenarios/smoke/` existing and loading.

### Steps

1. **Write the two asset files.** `triage-notes.txt` holds a few lines of plausible triage
   prose; `retry-fix.patch` holds a short unified diff. Neither may contain `[bzr-live:`.

2. **Compute their digests.**

   ```sh
   cd "$(git rev-parse --show-toplevel)"
   shasum -a 256 scenarios/smoke/assets/triage-notes.txt scenarios/smoke/assets/retry-fix.patch
   ```

   Expect two 64-character lowercase hex digests. Transcribe each into `scenario.json`.

3. **Write `scenarios/smoke/scenario.json`** with `format_version: 1`, `name: "smoke"`, a
   one-line description, and an `assets` array of two entries — `{"name": "notes", "path":
   "assets/triage-notes.txt", "sha256": "<digest>"}` and `{"name": "patch", "path":
   "assets/retry-fix.patch", "sha256": "<digest>"}`.

4. **Write `scenarios/smoke/resources.json`** with `format_version: 1` and a `resources`
   array holding, in dependency order:

   - three groups: `admin`, `editbugs`, `canconfirm`, each with a description. These are
     Bugzilla system groups, reconciled as existing furniture
     (`src/bzr_live/provision/executor.py:10-13,162-172`); they are declared so actors can
     reference them.
   - five actors exactly as the spec's actor table gives them, each with `email`,
     `display_name`, and `groups` as a list of `{"ref": "group:<name>"}`.
   - two products: `checkout`, `billing`, each with a description.
   - four components with `product`, `description`, and `default_assignee`:
     `cart` (checkout, `triager`), `payment` (checkout, `developer`),
     `invoicing` (billing, `triager`), `dunning` (billing, `developer`).
   - three versions: `checkout-v1`, `checkout-v2` (product `checkout`), `billing-v1`
     (product `billing`).
   - three milestones: `checkout-m1`, `checkout-m2` (checkout), `billing-m1` (billing).
   - three keywords: `regression`, `security`, `perf`, each with a description.
   - three custom fields: `risk` (`single-select`, values `["low","medium","high"]`),
     `subsystem` (`multi-select`, values `["cart","payment","invoicing","dunning"]`),
     `tracker` (`text`, no `values` key).
   - two flag types: `review` and `signoff`, each `{"target": "bug"}` with a description and
     no `products` or `components` key, which yields the unscoped inclusion pair.

5. **Write `scenarios/smoke/events.jsonl`**, one JSON object per line, in the order below.
   Every object carries `format_version: 1`, `name`, `actor`, `action`, `payload`. The
   spec's bug table gives each create's product, component, reporter, version, and extras.

   **Phase 1 — 20 creates**, named `create-<alias>`, in the spec's table order. Each payload
   carries `alias`, `product`, `component`, `summary`, `description`, `version`, and the
   extras the table names. Two carry a backward edge: `create-inv-duplicate-line` adds
   `"depends_on": [{"ref": "bug:inv-tax-mismatch"}]`, and `create-dun-grace-window` adds
   `"blocks": [{"ref": "bug:dun-silent-fail"}]`.

   **No create filed by `reporter` may declare an `assignee`, a `depends_on`, or a
   `blocks`** — `reporter` holds no `editbugs`, and Bugzilla would discard all three
   silently while reporting success (`Bugzilla/Bug.pm:1449-1454`, `:1707-1709`). Both
   edge-carrying creates above are filed by privileged actors for exactly that reason:
   `create-inv-duplicate-line` by `triager`, `create-dun-grace-window` by `releaser`.
   `create-cart-double-charge` declares **no** assignee; `triage-double-charge` in phase 3
   sets it from a privileged actor instead. The step-7 guard test enforces this over the
   whole event stream, so a later edit cannot quietly reintroduce it.

   **Phase 2 — 5 topology updates.** Each is `bug.update` with a `set` object.

   | Event name | Actor | Bug | `set` |
   |---|---|---|---|
   | `link-chain-cart` | triager | `cart-double-charge` | `depends_on: [inv-tax-mismatch]` |
   | `link-chain-inv` | triager | `inv-tax-mismatch` | `depends_on: [dun-retry-storm]` |
   | `link-diamond-apex` | admin-ops | `pay-token-leak` | `blocks: [pay-retry-loop, inv-currency-drift]` |
   | `link-diamond-sink` | developer | `dun-wrong-locale` | `depends_on: [pay-retry-loop, inv-currency-drift]` |
   | `mark-duplicate` | triager | `cart-dupe-report` | `duplicate_of: cart-double-charge` — this field alone |

   **Phase 3 — 7 lifecycle updates.**

   | Event name | Actor | Bug | `set` |
   |---|---|---|---|
   | `triage-double-charge` | triager | `cart-double-charge` | `assignee: developer`, `cc: [triager, reporter]` — no `status`: every bug is filed CONFIRMED (spec constraint 9b), so a confirm would write nothing |
   | `resolve-decline-copy` | developer | `pay-decline-copy` | `status: "RESOLVED"`, `resolution: "FIXED"` |
   | `reopen-decline-copy` | triager | `pay-decline-copy` | `status: "CONFIRMED"` |
   | `refix-decline-copy` | developer | `pay-decline-copy` | `status: "RESOLVED"`, `resolution: "FIXED"` |
   | `estimate-double-charge` | developer | `cart-double-charge` | `estimated_hours: "8"`, `remaining_hours: "6"` |
   | `retarget-double-charge` | releaser | `cart-double-charge` | `milestone: checkout-m2`, `keywords: [regression, perf]` |
   | `assign-decline-copy` | triager | `pay-decline-copy` | `assignee: releaser` |

   `assign-decline-copy` is what gives criterion 4 a second declared assignee; without it
   `developer` is the only actor the scenario ever names in that role, and the criterion would
   be met only by Bugzilla's component defaults — a substitution, not a declaration. Place it
   before `resolve-decline-copy`, so the bug is assigned
   before it is resolved. `triager` holds `editbugs`, so the privilege invariant is unaffected.

   **Phase 4 — 5 comments.** `bug.comment` with `bug`, `body`, `private`.

   | Event name | Actor | Bug | `private` |
   |---|---|---|---|
   | `comment-triage-double-charge` | triager | `cart-double-charge` | `false` |
   | `comment-private-token-leak` | admin-ops | `pay-token-leak` | `true` |
   | `comment-repro-inv-tax` | reporter | `inv-tax-mismatch` | `false` |
   | `comment-dun-storm` | developer | `dun-retry-storm` | `false` |
   | `comment-release-note` | releaser | `pay-decline-copy` | `false` |

   **Phase 5 — 3 attachment events.**

   | Event name | Actor | Action | Payload |
   |---|---|---|---|
   | `attach-triage-notes` | triager | `bug.attach` | `alias: "triage-notes"`, bug `cart-double-charge`, asset `notes`, `description: "Triage notes"`, `content_type: "text/plain"`, `private: false` |
   | `attach-retry-fix` | developer | `bug.attach` | `alias: "retry-fix"`, bug `pay-retry-loop`, asset `patch`, `description: "Retry loop fix"`, `content_type: "text/plain"`, `private: false` |
   | `obsolete-triage-notes` | triager | `attachment.update` | `attachment: {"ref": "attachment:triage-notes"}`, `obsolete: true` |

   **Phase 6 — 2 flags.**

   | Event name | Actor | Payload |
   |---|---|---|
   | `flag-review-request` | developer | bug `pay-retry-loop`, `flag_type: review`, `status: "?"`, `requestee: releaser` |
   | `flag-signoff-grant` | releaser | bug `pay-decline-copy`, `flag_type: signoff`, `status: "+"` |

   **Phase 7 — 2 work-time events.** `bug.worktime` with `bug`, `hours`, `comment`.

   | Event name | Actor | Bug | `hours` |
   |---|---|---|---|
   | `worktime-double-charge` | developer | `cart-double-charge` | `"2.5"` |
   | `worktime-inv-tax` | triager | `inv-tax-mismatch` | `"1.75"` |

   **Phase 8 — 3 custom-field events.** `bug.custom-field-set` with `bug` and a `values`
   array of `{"field": {"ref": "custom-field:<name>"}, "value": <value>}`.

   | Event name | Actor | Bug | Assignments |
   |---|---|---|---|
   | `custom-double-charge` | admin-ops | `cart-double-charge` | `risk: "high"`, `subsystem: ["cart","payment"]`, `tracker: "OPS-1042"` |
   | `custom-token-leak` | admin-ops | `pay-token-leak` | `risk: "high"`, `tracker: "SEC-77"` |
   | `custom-inv-tax` | triager | `inv-tax-mismatch` | `risk: "medium"`, `subsystem: ["invoicing"]` |

6. **Confirm the fixture loads before writing the test.**

   ```sh
   uv run --python 3.11 python -c \
     "from bzr_live.scenario import load_scenario; s = load_scenario('scenarios/smoke'); \
      print(s.name, len(s.events), s.digest[:16])"
   ```

   Expect `smoke 47 <16 hex chars>`. A `ScenarioValidationError` names the file, line, and
   JSON path to fix; fix the fixture, not the loader.

7. **Write the failing test.** Create `tests/test_smoke_scenario.py` with a
   `unittest.TestCase` holding the assertions below. Follow the existing suite's style
   (`tests/test_replay.py`). Load the scenario once in `setUpClass` from a path derived as
   `pathlib.Path(__file__).resolve().parent.parent / "scenarios" / "smoke"`, so the test does
   not depend on the working directory.

   Assertions, one test method each:

   - `test_loads_with_stable_digest` — a second `load_scenario` of the same path returns an
     equal `.digest`.
   - `test_twenty_bugs_across_two_products` — exactly 20 events have `action == "bug.create"`,
     and the set of their `expected_postcondition["values"]["product"].name` values has at
     least two members.
   - `test_dependency_chain_is_three_deep_and_crosses_products` — over the shared graph
     defined below, assert `dun-retry-storm → inv-tax-mismatch → cart-double-charge` is a
     path (the chain read in the graph's `blocks` direction, which is the reverse of how the
     spec's prose names it) and that the three bugs do not all share one product.
   - `test_diamond_has_two_distinct_paths` — over the same shared graph, assert
     `pay-token-leak` reaches `dun-wrong-locale` by exactly two distinct paths.

   - `test_exactly_one_duplicate_assignment` — exactly one event's `set` carries
     `duplicate_of`, and it carries no `status` or `resolution`.
   - `test_reopening_present` — the ordered status values declared for `pay-decline-copy`
     contain a `RESOLVED` followed later by `CONFIRMED`.
   - `test_every_handler_action_is_exercised` — the set of `.action` values over all events
     equals `set(HANDLERS)`.
   - `test_all_custom_field_types_assigned` — the union of custom-field names assigned by
     `bug.custom-field-set` events covers all three declared `custom-field` resources.
   - `test_private_comment_author_is_an_insider` — at least one `bug.comment` event has
     `private` true, and its actor's resource declares `group:admin`.
   - `test_attachment_summaries_fit` — for every `bug.attach` event,
     `len(render_attachment_summary(description, marker, asset_sha256).encode("utf-8"))` is at
     most `ATTACHMENT_SUMMARY_BYTE_LIMIT`.
   - `test_creates_declaring_assignee_or_edges_are_privileged` — **guard.** For every
     `bug.create` event whose payload declares a non-`None` `assignee`, a non-empty
     `depends_on`, or a non-empty `blocks`, assert the event's actor resource declares
     `group:editbugs`. Bugzilla silently substitutes the component default assignee
     (`Bugzilla/Bug.pm:1449-1454`) and silently discards create-time edges (`:1707-1709`)
     when the filer lacks `editbugs`, so without this assertion a mis-assigned reporter makes
     the fixture stop proving what it declares while every gate stays green.

   **The shared topology graph.** The two topology tests read one directed graph, built once
   in a module-level helper and oriented consistently as `blocks` — an edge `(a, b)` means
   "a blocks b". Walk every `bug.create` payload and every `bug.update` `set`, and add:

   - `(target, b)` for every reference `b` in a `blocks` list, and
   - `(b, target)` for every reference `b` in a `depends_on` list.

   Merging both orientations into one graph is what makes the diamond traversable. Its apex
   edges are declared as `blocks` on `pay-token-leak` and its sink edges as `depends_on` on
   `dun-wrong-locale`, so a `depends_on`-only graph holds no path from apex to sink at all and
   the diamond test could never pass over it. Do not "fix" that by adding a redundant
   `depends_on` edge to the fixture: the spec declares each edge exactly once, and duplicating
   one changes the topology the design chose.

8. **Run it and confirm it passes.**

   ```sh
   uv run --python 3.11 python -m unittest tests.test_smoke_scenario -v
   ```

   Expect `OK` with eleven tests. A failure is usually the fixture — correct the fixture. But
   a test that disagrees with the topology the spec declares is a test defect: fix the test,
   not the fixture.

9. **Prove the guards bite — one injection per guard, all three.** The spec names three
   guard tests, and these are the assertions whose failure mode is otherwise invisible, so
   these are the ones worth proving. Injecting against a count assertion proves nothing and
   is not done here. Break the fixture, observe red, revert, one at a time:

   - **Insider guard.** Change `comment-private-token-leak`'s actor from `admin-ops` to
     `triager` — a one-token edit; `triager` holds `editbugs` and `canconfirm` but not
     `admin`. Expect `test_private_comment_author_is_an_insider` to fail.
   - **Attachment ceiling guard.** Lengthen `attach-triage-notes`' description to 160 bytes.
     Expect `test_attachment_summaries_fit` to fail. 160 is the figure that actually goes
     red, not the spec's 140-byte authoring margin: the marker
     `bzr-live:smoke:attach-triage-notes` is 34 bytes, so the fixed overhead is 109 and a
     140-byte description renders to 249 — under the 255 limit. At 160 it renders to 269.
   - **Privilege guard.** Add `"assignee": {"ref": "actor:triager"}` to
     `create-cart-double-charge`, which `reporter` files without `editbugs`. Expect
     `test_creates_declaring_assignee_or_edges_are_privileged` to fail. This is the exact
     defect the guard exists for: an earlier draft of this design shipped it, and it was
     invisible because `cart`'s default assignee is `triager` too.

   Revert each edit after observing red. **If an injection stays green, the guard is at
   fault, not the fixture** — the assertion is not measuring what it claims to. Fix the test
   and re-inject. Do not proceed with a guard that has never been seen to fail.

10. **Run the guardrails and commit.**

    ```sh
    make check
    make test
    ```

    Both exit 0. Commit as `feat(scenario): add the 20-bug smoke scenario and its offline proof`.

**Acceptance criteria.** `scenarios/smoke/` loads with 47 events and 20 creates;
`tests/test_smoke_scenario.py` passes with eleven tests; **all three** guard tests —
`test_private_comment_author_is_an_insider`, `test_attachment_summaries_fit`, and
`test_creates_declaring_assignee_or_edges_are_privileged` — were each observed failing
against their own deliberate fault and passing after revert; `make check` and `make test`
are green; no file under `src/` changed.

## Task 2 — The live proof

**Where this fits.** The composition proof: real containers, real `bzr`, the committed
scenario. It ends at a `make smoke` that an operator can run and that reports a duration.

**Creates:** `tests/smoke_scenario.sh`. **Modifies:** `Makefile`, `README.md`, and
`docs/bzr-findings.md` if the live run surfaces a finding.

**Interfaces consumed.**

- `python -m bzr_live.provision <scenario> --state-root <dir> --bzr <path> --project-root <dir>
  --base-url <url>` — `src/bzr_live/provision/__main__.py`
- `python -m bzr_live.replay replay <scenario> --state-root <dir> --bzr <path> --base-url <url>`
  — `src/bzr_live/replay/__main__.py:15-24`; both exit 1 with a message on stderr on failure.
- `BZR_LIVE_BZR` and `BZ_PORT` conventions, and the `.env` port fallback — established by
  `tests/replay_smoke.sh:14,20-25`.

**Interfaces produced.** `make smoke`, taking `BZR_LIVE_BZR` from the environment.

### Steps

1. **Write `tests/smoke_scenario.sh`.** Model it on `tests/replay_smoke.sh`, which is the
   repository's established shape for an operator-run live proof. It must:

   - start `#!/usr/bin/env bash` and `set -euo pipefail`;
   - carry a header comment naming issue #19, what the script proves, and its prerequisites —
     a healthy `make up`, and `BZR_LIVE_BZR` pointing at a `bzr` binary;
   - resolve `ROOT` with `cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P` and `cd "$ROOT"`,
     because `uv` resolves the project from the working directory;
   - require `BZR_LIVE_BZR` with `${BZR_LIVE_BZR:?set BZR_LIVE_BZR to the bzr binary to
     validate with}`;
   - create the state root with `mktemp -d`, `chmod 700` it, and remove it in an `EXIT` trap;
   - fall back to the checkout's `.env` for `BZ_PORT` when the shell does not export it, and
     build `BASE_URL` as `http://127.0.0.1:${BZ_PORT:-8080}/`;
   - print `"$BZR" --version` output as its **first** progress line. Every refusal in the
     spec's constraint table is pinned to a `bzr` revision — `actions.py:15-16` names
     `b80303b7` — and `BZR_LIVE_BZR` is by charter an operator-selected binary that may be a
     different one. A run whose output does not name the revision cannot tell a live finding
     from a stale one;
   - run provisioning, then replay, printing a progress line before each;
   - time **only the replay**, at sub-second resolution. Capture `date +%s%N` immediately
     before and immediately after the `python -m bzr_live.replay` invocation and report the
     difference in seconds to two decimals, then print
     `smoke scenario: replayed <n> events in <elapsed>s`, with `<n>` read from the loaded
     scenario rather than hardcoded. Do **not** use bash's `SECONDS`: it counts from shell
     start, so without a reset the figure silently includes provisioning twenty-eight
     resources — plausibly the larger interval — under a label saying "replayed", and even
     reset it has one-second granularity, which can publish `0s` as though it were a
     measurement.

     `%N` is available on both targets — verified `/bin/date +%s%N` on Darwin 25 (BSD date,
     which rejects `--version`) and GNU date on the `ubuntu-24.04` runner — but it is not in
     the README's stated prerequisites, so degrade rather than assume: if the captured value
     is not all digits, fall back to whole seconds from `date +%s` and label the figure as
     second-resolution. `EPOCHREALTIME` is not an option; this repository targets Bash 3.2+
     and it is unset there (confirmed on bash 3.2.57);
   - print `smoke scenario: OK` on success;
   - never write to `docs/bzr-findings.md`.

   Read the event count from the scenario the same way `tests/replay_smoke.sh:40-46` reads its
   values — a short `uv run --python 3.11 python -c` that loads the scenario — rather than
   hardcoding 47, so the script does not drift from the fixture.

2. **Check it parses and lints.**

   ```sh
   bash -n tests/smoke_scenario.sh
   shellcheck tests/smoke_scenario.sh
   ```

   Both exit 0 with no output.

3. **Add the `Makefile` target.** Add `smoke` to the `.PHONY` list and:

   ```make
   smoke:
   	@bash tests/smoke_scenario.sh
   ```

   Add `tests/smoke_scenario.sh` to both the `bash -n` and the `shellcheck` argument lists in
   the `check` target, keeping the existing continuation-line style.

4. **Confirm `make check` still passes.**

   ```sh
   make check
   ```

   Exit 0, no output.

5. **Bring up a fresh fixture.** The scenario declares resources a pre-existing fixture may
   already hold under different definitions, and Bugzilla reads `checksetup_answers.txt` only
   at install, so the proof starts from a clean stack:

   ```sh
   CONFIRM_RESET=1 make reset
   make up
   ```

   `make up` returns only after MariaDB initialization, `checksetup.pl`, Apache start, and the
   HTTP health check. Expect it to take several minutes on first build.

   Then save a pristine baseline immediately, before anything has been provisioned into it.
   `scripts/checkpoint` requires both `--store` and `--runner-state`, exactly as
   `tests/checkpoint_smoke.sh:100` invokes it. Two details are not optional, both established
   by running this on the target host:

   - **the paths must be canonical.** `mktemp -d` under `$TMPDIR` on macOS returns
     `/var/folders/...`, a symlink to `/private/var/folders/...`, and the tool rejects it —
     `paths: store must be canonical: expected /private/var/...`. Resolve with `pwd -P`, the
     same idiom `tests/checkpoint_smoke.sh:6` uses.
   - **the directories must already exist.** `scripts/checkpoint` does not create them;
     it fails with `paths: cannot inspect ...: No such file or directory`.

   ```sh
   TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/bzr-live-smoke.XXXXXX")
   TEMP_ROOT=$(cd "$TEMP_ROOT" && pwd -P)
   mkdir "$TEMP_ROOT/store" "$TEMP_ROOT/runner"
   scripts/checkpoint save pristine --store "$TEMP_ROOT/store" \
     --runner-state "$TEMP_ROOT/runner"
   ```

   Expect `Saved checkpoint pristine at <store>/pristine`. Observed on this host: about 20
   seconds, 228 MB.

   This is what makes step 7's retry loop cheap. Without it every fixture-defect iteration
   pays the full reset-and-rebuild above; with it the recovery is a restore measured in
   seconds. The replay engine's own refusal text already points the operator at this command
   (`src/bzr_live/replay/engine.py:113-115`).

6. **Run the live proof.**

   ```sh
   BZR_LIVE_BZR="$(command -v bzr)" make smoke
   ```

   Expect the provisioning lines, the replay lines, the duration line, and
   `smoke scenario: OK`, exiting 0.

7. **Triage any failure honestly.** A failure is one of three things, and the response differs:

   - **A fixture-configuration gap** — Bugzilla rejects an honest payload because the install
     lacks something. Fix `containers/`, recreate the fixture from step 5 in full — a
     `containers/` edit changes the checkpoint stack fingerprint, so the saved `pristine`
     stops restoring ("bundle: stack fingerprint is incompatible") and must be re-saved — and
     say in the commit message that the fingerprint changed.
   - **A `bzr` limitation** — the engine refuses before mutating and names the boundary. Record
     it in `docs/bzr-findings.md` with the `bzr` source citation, the observed behaviour,
     **the `bzr` revision the run used**, and whether it is a defect or a design choice. The
     revision is not optional: the existing refusals are pinned to `b80303b7`
     (`src/bzr_live/replay/actions.py:15-16`), and an entry recorded against a different
     binary is a different observation. Do not alter the declared payload to route around it.
   - **A defect in this fixture** — a wrong reference, a privilege the actor does not hold, a
     mistyped status. Fix the fixture, then restore the baseline rather than rebuilding:

     ```sh
     scripts/checkpoint restore pristine --store "$TEMP_ROOT/store" \
       --runner-state "$TEMP_ROOT/runner"
     ```

     and re-run step 6. This is the common branch, and it is the one the checkpoint saved in
     step 5 exists to make cheap.

8. **Record the observed duration in `README.md`.** Add a "Smoke scenario" section after
   "Scenario replay" giving what the scenario covers, the `make smoke` invocation with
   `BZR_LIVE_BZR`, the fresh-fixture prerequisite, and the duration observed in step 6. State
   the host **and the `bzr` revision** it was observed on, and say which interval the figure
   covers — replay only, excluding provisioning and `make up`. A duration with no host is not
   a measurement, and a figure whose interval is unstated will be read as throughput.

9. **Run the guardrails and commit.**

   ```sh
   make check
   make test
   ```

   Both exit 0. Commit as `feat(scenario): prove the smoke scenario against the live fixture`.
   Any `docs/bzr-findings.md` edit is its own commit, as `docs: record <finding>`.

**Acceptance criteria.** `make smoke` provisions and replays `scenarios/smoke/` against a
freshly reset live fixture and exits 0; the observed duration is printed by the script and
recorded in `README.md` with its host; `bash -n` and `shellcheck` cover the new script through
`make check`; every limitation the run surfaced is in `docs/bzr-findings.md` with a citation;
no declared payload was altered to make a failure go away.

## Deferrals carried from review

None recorded yet. Any deferral a review of this design produces is added here with its owning
record path or tracker issue before implementation begins.

## Rollback

Every artifact is additive except the `Makefile` and `README.md` edits. Reverting the branch
removes `scenarios/smoke/` and both proof files and restores those two.

Three things that revert does **not** cover:

- **`docs/bzr-findings.md` is excluded from the revert.** A finding records what the live
  `bzr` binary actually did; that evidence is true whether or not this change ships, and
  `AGENTS.md` calls the register the most valuable thing this repository produces. Its commit
  stands on its own — which is why step 9 keeps it in a separate commit.
- **A `containers/` edit under Task 2 step 7 invalidates saved checkpoints.** Reverting
  restores the old stack fingerprint, so any checkpoint saved *during* the work stops
  restoring in the other direction. Recovery is to re-save `pristine` after the fixture is
  recreated (`README.md:127-130`); `CONFIRM_RESET=1 make reset` alone does not do it.
- **The live fixture and the checkpoint store persist outside the repository.** The operator
  clears them with `CONFIRM_RESET=1 make reset` and by removing the `mktemp -d` store and
  runner-state directories from step 5.
