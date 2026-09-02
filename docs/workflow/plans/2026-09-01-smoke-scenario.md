# Implementation plan: smoke scenario (issue #19)

**Goal.** Commit `scenarios/smoke/` — 20 bugs, 47 events, two products — and prove it loads
offline and replays against the live pinned Bugzilla fixture through a real `bzr` binary.

**Architecture.** The fixture is data validated by the existing loader; no Python module under
`src/` changes. Two proof tiers sit beside it: an offline `unittest` case that loads and plans
the scenario with no server, and an operator-run shell script that provisions and replays it
against a running fixture. A `make smoke` target and a `make check` registration wire the
script into the repository's existing command surface.

**Tech stack.** Python 3.11 via `uv` (no runtime dependencies), Bash, GNU Make, Docker Compose,
the operator-selected `bzr` binary.

Expected implementation size: 320–430 changed lines (M) — derived from the file map below: four
fixture files (~120 lines), two proof files (~220), and Makefile/README edits (~40).

Spec: [`docs/workflow/specs/2026-09-01-smoke-scenario-design.md`](../specs/2026-09-01-smoke-scenario-design.md).
Decision record: [`docs/adr/0007-committed-smoke-scenario.md`](../../adr/0007-committed-smoke-scenario.md).

## Global constraints

Transcribed from the spec. Every task's requirements include this section.

- **Python 3.11**, invoked as `uv run --python 3.11`. The package under `src/bzr_live/` has no
  runtime dependencies and gains none here.
- **No change to `src/`.** The loader, replay engine, journal, and provisioning executor are
  read, not modified. An expressive gap is a finding for `docs/bzr-findings.md`, never a
  contract change.
- **Names** match `[a-z][a-z0-9-]{0,62}` — resource names, bug aliases, attachment aliases, and
  event names alike (`src/bzr_live/scenario/loader.py:25`).
- **Line length** 100 characters; `shellcheck` clean; `bash -n` clean.
- **No forward references.** An event may cite only bugs created by earlier events
  (`src/bzr_live/scenario/loader.py:810`).
- **Flag type names** contain none of `+ - ? X` (`src/bzr_live/replay/actions.py:588-598`).
- **`bug.create`** must declare `version`; must not declare `custom_fields`,
  `estimated_hours`, `remaining_hours`, or `duplicate_of`
  (`src/bzr_live/replay/actions.py:23-30,281-285`).
- **`bug.update`** must not declare `groups` or `version`; must not set `resolution`,
  `milestone`, or `duplicate_of` to null; must not pair `duplicate_of` with `status` or
  `resolution` (`src/bzr_live/replay/actions.py:31-50`).
- **Declared text** — every summary, description, comment body, and attachment description —
  must not contain the literal `[bzr-live:` (`src/bzr_live/replay/actions.py:169-178`).
- **Attachment descriptions** stay under 140 bytes: the rendered summary is
  `<description> [<marker>] sha256=<64 hex>` capped at 255 bytes
  (`src/bzr_live/replay/actions.py:13,81-82`).
- **Secrets** never appear in committed files, command lines, or script output. Actor keys
  reach `bzr` through the `BZR_LIVE_API_KEY` environment variable only.
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
   | `confirm-double-charge` | triager | `cart-double-charge` | `status: "CONFIRMED"`, `assignee: developer`, `cc: [triager, reporter]` |
   | `confirm-decline-copy` | triager | `pay-decline-copy` | `status: "CONFIRMED"` |
   | `resolve-decline-copy` | developer | `pay-decline-copy` | `status: "RESOLVED"`, `resolution: "FIXED"` |
   | `reopen-decline-copy` | triager | `pay-decline-copy` | `status: "CONFIRMED"` |
   | `refix-decline-copy` | developer | `pay-decline-copy` | `status: "RESOLVED"`, `resolution: "FIXED"` |
   | `estimate-double-charge` | developer | `cart-double-charge` | `estimated_hours: "8"`, `remaining_hours: "6"` |
   | `retarget-double-charge` | releaser | `cart-double-charge` | `milestone: checkout-m2`, `keywords: [regression, perf]` |

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
   - `test_dependency_chain_is_three_deep_and_crosses_products` — build the
     `depends_on` edge set from every create payload and every `bug.update` `set`, then assert
     `cart-double-charge → inv-tax-mismatch → dun-retry-storm` is present and that the three
     bugs do not all share one product.
   - `test_diamond_has_two_distinct_paths` — from the same edge set, assert `pay-token-leak`
     reaches `dun-wrong-locale` by exactly two distinct paths.
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

8. **Run it and confirm it passes.**

   ```sh
   uv run --python 3.11 python -m unittest tests.test_smoke_scenario -v
   ```

   Expect `OK` with ten tests. If a test fails, the fixture is wrong — correct the fixture.

9. **Prove the tests bite.** Break the fixture deliberately, once per guard, and observe red
   before reverting: delete one `bug.create` line (expect `test_twenty_bugs_across_two_products`
   to fail), and lengthen an attachment description past 140 bytes (expect
   `test_attachment_summaries_fit` to fail). Revert both edits.

10. **Run the guardrails and commit.**

    ```sh
    make check
    make test
    ```

    Both exit 0. Commit as `feat(scenario): add the 20-bug smoke scenario and its offline proof`.

**Acceptance criteria.** `scenarios/smoke/` loads with 47 events and 20 creates;
`tests/test_smoke_scenario.py` passes with ten tests; both guard tests were observed failing
against a deliberate fault and passing after revert; `make check` and `make test` are green;
no file under `src/` changed.

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
   - run provisioning, then replay, printing a progress line before each;
   - time the replay with `SECONDS` and print
     `smoke scenario: replayed <n> events in <SECONDS>s` — the observed duration epic #1 asks
     for, read from the loaded scenario rather than hardcoded;
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

6. **Run the live proof.**

   ```sh
   BZR_LIVE_BZR="$(command -v bzr)" make smoke
   ```

   Expect the provisioning lines, the replay lines, the duration line, and
   `smoke scenario: OK`, exiting 0.

7. **Triage any failure honestly.** A failure is one of three things, and the response differs:

   - **A fixture-configuration gap** — Bugzilla rejects an honest payload because the install
     lacks something. Fix `containers/`, recreate the fixture (step 5), and say in the commit
     message that the checkpoint stack fingerprint changed.
   - **A `bzr` limitation** — the engine refuses before mutating and names the boundary. Record
     it in `docs/bzr-findings.md` with the `bzr` source citation, the observed behaviour, and
     whether it is a defect or a design choice. Do not alter the declared payload to route
     around it.
   - **A defect in this fixture** — a wrong reference, a privilege the actor does not hold, a
     mistyped status. Fix the fixture and re-run from step 5.

8. **Record the observed duration in `README.md`.** Add a "Smoke scenario" section after
   "Scenario replay" giving what the scenario covers, the `make smoke` invocation with
   `BZR_LIVE_BZR`, the fresh-fixture prerequisite, and the duration observed in step 6. State
   the host it was observed on — a duration with no host is not a measurement.

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
removes `scenarios/smoke/` and both proof files and restores both edited files; nothing
persists outside the repository except a live fixture the operator resets with
`CONFIRM_RESET=1 make reset`.
