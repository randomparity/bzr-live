# CI gate for the scenarios tree and the live smoke path — implementation plan

**Goal.** Make a fixture-only edit under `scenarios/` trigger CI, and make the existing
x86_64 Linux job prove the live path end to end: provision, replay, verify, checkpoint save,
mutate, restore, re-verify, resume.

**Architecture.** Two GitHub Actions workflow files gain path entries; the existing
`x86_64-linux` job in `container-lifecycle.yml` gains three steps; `tests/smoke_scenario.sh`
gains five stages after its verify stage, all inside one invocation (see the spec's *Script
stages*); `README.md` states what CI now proves and which runner is unavailable for the
arm64 half. One new unit test asserts the path filters, the workflow files' structure and
the pinned revision, because `make check` reads no YAML.

**Tech stack.** Bash (`set -euo pipefail`), Python 3.11 under `uv` with no runtime
dependencies, GitHub Actions, Docker Compose, `cargo` on the runner.

Expected implementation size: 320–360 changed lines (M) — summed from the embedded blocks
below: ~24 changed lines of path entries across both workflows (the offline enumeration is
replaced, not appended to), ~28 lines of job steps, ~100 lines of shell (the stages, the
state-root canonicalization and the interpreter guard), ~156 lines of new test, ~35 lines of
README. The `effort:S` label on issue #25 sized the two
workflow edits; the test file and the script stages are what put it in M.

Spec: [`docs/workflow/specs/2026-09-02-ci-scenario-gate-design.md`](../specs/2026-09-02-ci-scenario-gate-design.md).
Decision record: [`docs/adr/0010-ci-gated-live-scenario-proof.md`](../../adr/0010-ci-gated-live-scenario-proof.md).

## Global Constraints

- Python 3.11+, run as `uv run --python 3.11 …`. The package under `src/bzr_live/` has no
  runtime dependencies, and tests add none — no YAML parser.
- Guardrail commands, run bare (no pipe, no `|| true`, no `>/dev/null`): `make check`,
  `make test`, `make smoke`. `make replay-smoke` is broken on macOS on `main` and is not
  this change's to fix.
- The `bzr` binary for every live run on this host is `/Users/dave/src/bzr/target/release/bzr`,
  which reports `0.8.3-dev (63abb94e)`. The PATH binary is `0.8.2` and below the floor
  `README.md` states.
- The pinned CI revision is the full SHA `63abb94e7e14a2db79efe0ddf0011a1f32ed8640`.
- `.github/workflows/container-lifecycle.yml` job `x86_64-linux`: `runs-on: ubuntu-24.04`,
  `timeout-minutes: 45`, `permissions: contents: read`. Do not change any of the three.
- Every existing `paths` entry stays; entries are appended, none reordered or removed.
- `AGENTS.md`: never substitute a value the scenario did not declare. The one undeclared
  value this change writes is the mutation probe, written after verification passes and
  reverted before the re-verify.

## File map

| File | Change | Answerable for |
|---|---|---|
| `.github/workflows/scenario-contract.yml` | modified | gating the offline tier on `scenarios/**` |
| `.github/workflows/container-lifecycle.yml` | modified | gating the live tier, and running it |
| `tests/test_ci_workflow_gates.py` | created | asserting both of the above without a YAML parser |
| `tests/smoke_scenario.sh` | modified | the whole live sequence in one state root |
| `README.md` | modified | what CI proves, and the arm64 position |

## Task 1 — gate the scenarios tree

**Where this fits.** The half of issue #25 that does not depend on a running fixture. It
stands alone: after it, a pull request editing only `scenarios/smoke/events.jsonl` runs both
workflows' existing jobs.

**Interfaces.** Consumes nothing. Task 3 appends one more test class to the file this task
creates, and relies on `path_filters(workflow: Path) -> dict[str, list[str]]` defined here.

### Step 1.1 — write the failing test

Create `tests/test_ci_workflow_gates.py`:

```python
"""The CI path filters, asserted without a YAML parser.

Two workflows gate this repository and `make check` does not read either: it runs
`bash -n`, shellcheck, `compileall` and `docker compose config`. A dropped
`scenarios/**` entry is therefore invisible to every other test in the suite, which
is exactly the gap issue #25 closes. The parse below is line-oriented on purpose --
the package carries no runtime dependencies, and the `paths:` blocks it reads are
flat lists of scalars at a fixed indent.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

_TRIGGER = re.compile(r"^  (pull_request|push):$")
_PATHS = re.compile(r"^    paths:$")
_ENTRY = re.compile(r'^      - "?([^"]+?)"?$')


def path_filters(workflow: Path) -> dict[str, list[str]]:
    """Map each trigger name to the entries of its `paths:` list.

    Collection stops at the first line under a `paths:` block that is not an entry,
    so a sibling key at the same indent -- `branches: [main]` sits between `push:`
    and its `paths:` -- ends the list rather than being read as one.
    """
    filters: dict[str, list[str]] = {}
    trigger: str | None = None
    collecting = False
    for line in workflow.read_text(encoding="utf-8").splitlines():
        matched = _TRIGGER.match(line)
        if matched is not None:
            trigger, collecting = matched.group(1), False
            continue
        if trigger is not None and _PATHS.match(line):
            filters[trigger] = []
            collecting = True
            continue
        if collecting:
            entry = _ENTRY.match(line)
            if entry is None:
                collecting = False
                continue
            filters[trigger].append(entry.group(1))
    return filters


_INDENT = re.compile(r"^( *)\S")


class WorkflowFilesAreStructurallySound(unittest.TestCase):
    """Nothing local parses these files, and a broken one does not fail loudly.

    A workflow GitHub cannot parse, or whose filters stopped matching, simply runs no
    job -- which from the outside looks exactly like a gate that passed. These are the
    structural facts a dependency-free line-oriented reader can still hold.
    """

    def test_no_tabs_and_every_indent_is_even(self) -> None:
        for name in ("scenario-contract.yml", "container-lifecycle.yml"):
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                self.assertNotIn("\t", line, f"{name}:{number} contains a tab")
                indent = _INDENT.match(line)
                if indent is not None:
                    self.assertEqual(
                        len(indent.group(1)) % 2, 0,
                        f"{name}:{number} is indented by an odd number of spaces")


class ScenarioTreeIsGated(unittest.TestCase):
    def test_the_parse_sees_the_filters_it_is_asked_about(self) -> None:
        """Guard the parser itself: a regex that matched nothing would pass every
        `assertIn` below by never running one."""
        for name in ("scenario-contract.yml", "container-lifecycle.yml"):
            filters = path_filters(WORKFLOWS / name)
            self.assertEqual(sorted(filters), ["pull_request", "push"], name)
            for trigger, entries in filters.items():
                self.assertIn(
                    f".github/workflows/{name}", entries, f"{name}:{trigger}")

    def test_both_workflows_gate_the_scenarios_tree(self) -> None:
        for name in ("scenario-contract.yml", "container-lifecycle.yml"):
            for trigger, entries in path_filters(WORKFLOWS / name).items():
                self.assertIn("scenarios/**", entries, f"{name}:{trigger}")

    def test_the_live_workflow_gates_the_script_it_now_runs(self) -> None:
        filters = path_filters(WORKFLOWS / "container-lifecycle.yml")
        for trigger, entries in filters.items():
            self.assertIn("tests/smoke_scenario.sh", entries, trigger)
        self.assertTrue((ROOT / "tests/smoke_scenario.sh").is_file())

    def test_the_offline_workflow_gates_records_by_glob_not_by_name(self) -> None:
        """The per-record enumeration this replaces had already missed all three of
        issue #20's records, in both lists, without anyone noticing -- `src/**` and
        `tests/**` kept the job running on code changes and masked it. A single
        re-added named record would be the class coming back, so it fails here."""
        for trigger, entries in path_filters(
                WORKFLOWS / "scenario-contract.yml").items():
            self.assertIn("docs/adr/**", entries, trigger)
            self.assertIn("docs/workflow/**", entries, trigger)
            named = [entry for entry in entries
                     if entry.startswith(("docs/adr/", "docs/workflow/"))
                     and entry.endswith(".md")]
            self.assertEqual(named, [], f"{trigger}: enumerated records are back")


if __name__ == "__main__":
    unittest.main()
```

### Step 1.2 — confirm it fails

Run:

    uv run --python 3.11 python -m unittest tests.test_ci_workflow_gates -v

Expect `test_no_tabs_and_every_indent_is_even` and
`test_the_parse_sees_the_filters_it_is_asked_about` to pass, and the other three to fail,
each naming the workflow and trigger whose list lacks the entry. A failure in the parse
guard means the parse is wrong, not the workflow — fix the parse before continuing.

### Step 1.3 — add the entries

In `.github/workflows/scenario-contract.yml`, in **each** of the two `paths:` lists, replace
the seven per-record entries (currently lines 12-18 and 28-34: ADR 0002, ADR 0003, the two
2026-08-29 versioned-scenario-contract records, ADR 0006, and the two 2026-09-01
replay-actor-scoped-events records) with two globs, and append `scenarios/**`. Each list then
ends:

```yaml
      - src/**
      - tests/**
      - "!tests/lifecycle_test.sh"
      - docs/adr/**
      - docs/workflow/**
      - scenarios/**
```

Do both lists. The omission this closes — issue #20's ADR 0008, spec and plan — is present in
both, and fixing one reproduces it on the other.

In `.github/workflows/container-lifecycle.yml`, append these two lines to the
`pull_request` `paths:` list (after `tests/test_checkpoint.py`, currently line 20) and the
identical two to the `push` `paths:` list (after `tests/test_checkpoint.py`, currently
line 38):

```yaml
      - scenarios/**
      - tests/smoke_scenario.sh
```

Add nothing else — in particular not this change's own ADR, spec or plan. ADR 0010
decision 1 records why, and departs from the ADR-0005 precedent in the same file to do it.

### Step 1.4 — confirm it passes

    uv run --python 3.11 python -m unittest tests.test_ci_workflow_gates -v

Expect `OK` and five tests run.

### Step 1.5 — guardrails and commit

    make check
    make test

Expect `make check` silent and exit 0, and `make test` to end `OK` with five more tests
than the 382 that ran before this change. Commit as
`ci: gate the scenarios tree in both workflows`.

**Acceptance criteria.** Both workflows name `scenarios/**` under both triggers; the
offline workflow gates records by glob and names none individually, so issue #20's three
records are covered; the live workflow also names `tests/smoke_scenario.sh`; `tests/test_ci_workflow_gates.py` fails if
either entry is later removed, if a workflow line grows a tab, or if any indent turns odd.

## Task 2 — prove the checkpoint round trip inside the smoke run

**Where this fits.** The stages issue #25 lists after `verify`, which must run inside
`tests/smoke_scenario.sh` for the reason the spec's *Script stages* gives.

**Interfaces.** Consumes, from the existing script: `$ROOT`, `$BZR`, `$SCENARIO`, `$STATE`,
`$BASE_URL`, and `elapsed_since <start-ns>` defined at line 79. Provides nothing to later
tasks except the stages themselves, which Task 3's job step invokes through `make smoke`.

### Step 2.1 — guard the interpreter, canonicalize the state root, then append the stages

First, in `tests/smoke_scenario.sh`, insert this immediately after `set -euo pipefail`
(line 14), before the `ROOT=` assignment:

```bash
# Issue #29 tracks a status-masking EXIT trap in the sibling smoke scripts, and this script
# has the same `trap 'rm -rf "$STATE"' EXIT` shape -- but measured on bash 3.2.57 and 5.3.15,
# the shape is not the defect and no trap discipline is the fix. An ordinary `set -e` failure,
# which is what every stage below produces, propagates through that trap on both. A *fatal
# expansion error* does not: on 3.2, `set -euo pipefail; trap ':' EXIT; echo "$NOPE"` exits 0,
# and so does the status-preserving `cleanup(){ local s=$?; ...; exit "$s"; }` pattern
# tests/checkpoint_smoke.sh:13-18 uses, because $? is already 0 when the trap runs. bash 5.3
# exits 1 for the same input. macOS still ships 3.2 as /bin/bash, so refuse it: a live proof
# that can exit 0 while failing is worse than one that does not run.
if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 3) )); then
  echo "smoke scenario: needs bash >= 4.3, found ${BASH_VERSION}." >&2
  echo "  On bash 3.2 a fatal expansion error exits 0, so a failed run would look green." >&2
  echo "  Install a newer bash (brew install bash) and put it ahead of /bin/bash." >&2
  exit 1
fi
```

Then insert one assignment between the existing `STATE=$(mktemp -d …)` at line 20 and the
`trap 'rm -rf "$STATE"' EXIT` at line 21, so it reads:

```bash
STATE=$(mktemp -d "${TMPDIR:-/tmp}/bzr-live-smoke-scenario.XXXXXX")
# scripts/checkpoint requires every path argument to equal its own resolve()
# (src/bzr_live/checkpoint.py:155-167), and on macOS TMPDIR sits under the
# /var -> /private/var symlink, so the mktemp spelling is refused with
# "paths: store must be canonical". Resolve once here rather than leaving two spellings of
# one directory in the script; tests/checkpoint_smoke.sh:5-6 does the same, for the same
# caller. Nothing above this line uses $STATE, and the EXIT trap below expands it at exit.
STATE=$(cd "$STATE" && pwd -P)
trap 'rm -rf "$STATE"' EXIT
```

Then append, after the existing `echo "smoke scenario: verified in ${VERIFY_ELAPSED}s"` line
and before the final `echo "smoke scenario: OK"`:

```bash
# --- checkpoint round trip over the verified fixture --------------------------------
# Issue #25 asks the live path to prove that a saved checkpoint restores the state the
# scenario declares. These stages run in the replay's own state root because `verify` and
# `resume` read the journal and actor keys it holds. The store is a sibling of that root:
# scripts/checkpoint refuses a store containing the runner state or the reverse
# (src/bzr_live/checkpoint.py:334-335).
STORE="$STATE/store"
mkdir "$STORE"
echo "smoke scenario: saving checkpoint 'smoke' over the verified fixture"
scripts/checkpoint save smoke --store "$STORE" --runner-state "$STATE/state"

# The mutation target is read from the scenario rather than pasted, for the same reason
# the counts above are: the first bug.create's declared alias, the actor that files it,
# and that actor's address. Bugzilla validates an API key against its own address, so key
# and address must belong to one actor (tests/replay_smoke.sh:70-73). Assign first and
# split second -- `read ... <<<"$(...)"` would take its status from `read` and a scenario
# that failed to load would leave three empty values behind.
PROBE=$(uv run --python 3.11 python -c '
import sys
from bzr_live.scenario import load_scenario
s = load_scenario(sys.argv[1])
create = next(e for e in s.events if e.action == "bug.create")
emails = {r.name: r.data["email"] for r in s.resources if r.kind == "actor"}
print(create.expected_postcondition["values"]["server_alias"], create.actor.name,
      emails[create.actor.name])
' "$SCENARIO")
read -r PROBE_ALIAS PROBE_ACTOR PROBE_EMAIL <<<"$PROBE"
PROBE_KEY=$(cat "$STATE/state/actor-keys/$PROBE_ACTOR.key")

# `bug view` reads the alias but `bug update` cannot: at the pinned revision UpdateArgs
# declares `pub ids: Vec<u64>` (src/cli/bug/update.rs:79) where ViewArgs declares
# `Vec<String>` and documents "Bug ID(s) or alias(es)" (view.rs:60-62). That asymmetry is
# finding G10 in docs/bzr-findings.md; the id is resolved here rather than worked around.
# `bzr --json` wraps its result in a schema envelope; unwrap "data" exactly as
# BzrClient._payload does (src/bzr_live/provision/adapters.py:76-77), including its
# tolerance of a reply that carries no envelope.
probe_view() {
  BZR_LIVE_API_KEY=$PROBE_KEY "$BZR" --json \
    --server-url "$BASE_URL" \
    --server-api-key-env BZR_LIVE_API_KEY \
    --server-email "$PROBE_EMAIL" \
    bug view -- "$PROBE_ALIAS"
}
probe_field() {
  uv run --python 3.11 python -c \
    "import json, sys; d = json.load(sys.stdin); print(d.get('data', d)[sys.argv[1]])" "$1"
}
PROBE_ID=$(probe_view | probe_field id)

# A summary the scenario does not declare, written only after the scenario's own
# verification has passed and reverted by the restore below. `summary` is the field because
# every declared summary is one of the scalars compared against `bug view`
# (src/bzr_live/verify/checks.py:70-73); ADR 0010 decision 6 records why, and which
# alternatives verify would not have caught.
PROBE_SUMMARY="checkpoint probe: not the summary $PROBE_ALIAS declares"
echo "smoke scenario: mutating $PROBE_ALIAS (bug $PROBE_ID) as $PROBE_ACTOR"
BZR_LIVE_API_KEY=$PROBE_KEY "$BZR" --json \
  --server-url "$BASE_URL" \
  --server-api-key-env BZR_LIVE_API_KEY \
  --server-email "$PROBE_EMAIL" \
  bug update "--summary=$PROBE_SUMMARY" -- "$PROBE_ID"

# Read it back before restoring. Without this, a mutation that never reached the server and
# a restore that reverted it are the same observation.
OBSERVED_SUMMARY=$(probe_view | probe_field summary)
if [[ "$OBSERVED_SUMMARY" != "$PROBE_SUMMARY" ]]; then
  echo "smoke scenario: the mutation did not reach $PROBE_ALIAS; observed" \
    "'$OBSERVED_SUMMARY'" >&2
  exit 1
fi

echo "smoke scenario: restoring checkpoint 'smoke'"
scripts/checkpoint restore smoke --store "$STORE" --runner-state "$STATE/state"

RESTORE_START=$(date +%s%N)
echo "smoke scenario: re-verifying the restored fixture"
uv run --python 3.11 python -m bzr_live.replay verify "$SCENARIO" \
  --state-root "$STATE/state" --bzr "$BZR" --base-url "$BASE_URL"
RESTORE_ELAPSED=$(elapsed_since "$RESTORE_START")
echo "smoke scenario: re-verified the restored fixture in ${RESTORE_ELAPSED}s"

# Every event's journal record is complete, so resume adopts each one's resolved ids and
# reports it already complete without sending a mutation
# (src/bzr_live/replay/engine.py:145-148). It asserts that the restored journal and the
# restored fixture still describe one run.
echo "smoke scenario: resuming the restored journal"
uv run --python 3.11 python -m bzr_live.replay resume "$SCENARIO" \
  --state-root "$STATE/state" --bzr "$BZR" --base-url "$BASE_URL"
```

### Step 2.2 — confirm the shell guardrails pass

    make check

Expect exit 0 and no shellcheck output. `bash -n` and shellcheck both already cover
`tests/smoke_scenario.sh` (`Makefile:46,54`).

### Step 2.3 — run the live proof on this host

    CONFIRM_RESET=1 make reset && make up
    BZR_LIVE_BZR=/Users/dave/src/bzr/target/release/bzr make smoke

Expect, in order: the `bzr` revision line naming `0.8.3-dev (63abb94e)`; provisioning;
`replayed 47 events in <t>s`; `verified in <t>s` with `0 divergences`; the checkpoint save;
the mutation and its read-back with no error; the restore; `re-verified the restored fixture
in <t>s` again reporting 0 divergences; the resume summary reporting every event already
complete; and `smoke scenario: OK`. Record each duration — they are the arm64 evidence
Task 3 writes into `README.md` and the input to the CI budget claim.

Two failures here are findings about `scripts/checkpoint`, not reasons to weaken the stage:
a `summary` divergence on `$PROBE_ALIAS` at the re-verify means the restore did not revert
the probe, and a mode-0700 complaint from `verify` or `resume` means it did not preserve the
runner tree's modes. Record either in the pull request.

### Step 2.4 — prove the gate bites

A green run does not show the new stages can go red, and the restore is the stage whose
failure is otherwise indistinguishable from success: a restore that reverts nothing leaves a
fixture that still looks replayed. Make one controlled fault, observe red, revert it.

Comment out the single `scripts/checkpoint restore smoke …` line, then:

    BZR_LIVE_BZR=/Users/dave/src/bzr/target/release/bzr make smoke

Expect exit 1, with the re-verify printing a `summary` divergence naming the probe bug's
alias and the summary the scenario declares against the probe text. Record the exact finding
line — it is the evidence for R8.

Restore the line and re-run the same command; expect `smoke scenario: OK` and exit 0 again.
Do not commit with the line commented out; `git diff` must be empty of that change before
Step 2.5.

### Step 2.5 — commit

Commit as `test(smoke): prove the checkpoint round trip against the verified fixture`.

**Acceptance criteria.** `make smoke` runs R2's eight stages plus the two reads around the
mutation in one invocation and exits 0; a commented-out restore makes it exit 1 at the re-verify with
a `summary` divergence, and the observed line is recorded;
each stage is bare, so any failure fails the script; the mutation is reverted before the
run ends; `make check` is green.

## Task 3 — run the live path in CI and state what it proves

**Where this fits.** The last half of issue #25: the job steps that make the sequence a
merge gate, plus the documentation criteria.

**Interfaces.** Consumes `path_filters` from Task 1's module and `make smoke` from Task 2.
Provides nothing to later tasks.

### Step 3.1 — write the failing pin and step-order tests

Append to `tests/test_ci_workflow_gates.py`, before the `if __name__` block:

```python
_STEP_NAME = re.compile(r"^      - name: (.+)$")


class LiveJobSteps(unittest.TestCase):
    def test_the_live_job_declares_every_step_in_order(self) -> None:
        """The order is a design decision, not an accident: `make smoke` documents a
        fresh fixture as its precondition, so it runs against the one `make up` just
        installed rather than whatever `make checkpoint-smoke` leaves behind. A step
        whose indent slipped also disappears from this list rather than passing."""
        lines = (WORKFLOWS / "container-lifecycle.yml").read_text(
            encoding="utf-8").splitlines()
        names = [match.group(1)
                 for match in map(_STEP_NAME.match, lines) if match is not None]
        self.assertEqual(names, [
            "Check out repository",
            "Verify checked out commit",
            "Install uv and Python",
            "Verify native architecture",
            "Run lifecycle contract checks",
            "Install the pinned bzr build",
            "Start the fixture",
            "Exercise the live scenario smoke path",
            "Exercise checkpoint round trip",
            "Clean project resources",
        ])


class PinnedBzrRevision(unittest.TestCase):
    """CI's pin and the revision README claims the scenario is proven at are one fact.

    They live in two files, so nothing but this test stops them drifting apart -- and a
    CI run against an unproven revision reports a green gate for a claim nobody made.
    """

    def test_ci_pins_the_revision_the_readme_proves(self) -> None:
        workflow = (WORKFLOWS / "container-lifecycle.yml").read_text(encoding="utf-8")
        pins = re.findall(r"--rev ([0-9a-f]{40})", workflow)
        self.assertEqual(len(pins), 1, "expected exactly one pinned bzr revision")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        proven = re.search(r"proven at `bzr` `([0-9a-f]{8,40})`", readme)
        self.assertIsNotNone(proven, "README no longer states a proven bzr revision")
        self.assertTrue(
            pins[0].startswith(proven.group(1)),
            f"CI pins {pins[0]}, README proves {proven.group(1)}")
```

### Step 3.2 — confirm it fails

    uv run --python 3.11 python -m unittest tests.test_ci_workflow_gates -v

Expect `test_ci_pins_the_revision_the_readme_proves` to fail on `expected exactly one
pinned bzr revision` (0 != 1), and `test_the_live_job_declares_every_step_in_order` to fail
with the three new step names missing from the observed list.

### Step 3.3 — add the job steps

In `.github/workflows/container-lifecycle.yml`, insert between the `Run lifecycle contract
checks` step and the `Exercise checkpoint round trip` step:

```yaml
      - name: Install the pinned bzr build
        # libdbus-1-dev is not preinstalled on GitHub's Ubuntu images; bzr's own CI
        # installs it in every job that compiles (randomparity/bzr
        # .github/workflows/ci.yml:21-22). cargo and rustup are preinstalled, but the
        # toolchain the image defaults to drifts, so 1.89.0 -- the channel the pinned
        # tree's own rust-toolchain.toml names -- is requested explicitly. The revision is
        # the one README.md states this scenario is proven at, pinned by full SHA with
        # --locked. The prefix is outside the workspace because compose.yaml builds from
        # context "." and this repository has no .dockerignore.
        run: |
          sudo apt-get update
          sudo apt-get install -y libdbus-1-dev pkg-config
          rustup toolchain install 1.89.0 --profile minimal
          cargo +1.89.0 install --git https://github.com/randomparity/bzr \
            --rev 63abb94e7e14a2db79efe0ddf0011a1f32ed8640 \
            --locked --root "$RUNNER_TEMP/bzr" bzr

      - name: Start the fixture
        run: make up

      - name: Exercise the live scenario smoke path
        # Provision, replay, verify, checkpoint save, mutate, restore, re-verify, resume --
        # one invocation, because every stage after the replay reads the state root that
        # script mktemps and removes on its EXIT trap.
        env:
          BZR_LIVE_BZR: ${{ runner.temp }}/bzr/bin/bzr
        run: make smoke
```

The step order matters: `make smoke` documents a fresh fixture as its precondition
(`README.md:189-191`), so it runs against the fixture `make up` just installed rather than
whatever `make checkpoint-smoke` leaves behind.

### Step 3.4 — confirm the pin test passes

    uv run --python 3.11 python -m unittest tests.test_ci_workflow_gates -v

Expect `OK` and seven tests run.

### Step 3.5 — update README.md

In the "Smoke scenario" section:

- In the live-tier paragraph, list R2's eight stages and the two reads that bracket the
  mutation — resolving the target's numeric id, and confirming the mutation landed — and
  note that `make smoke` now stops and restarts the Compose stack twice, because a cold
  checkpoint does.
- Keep the sentence "**`make smoke` has been proven at `bzr` `63abb94e` and nowhere else.**"
  verbatim — Step 3.1's test reads the revision out of it.
- Replace the observed-measurement paragraph's figures with the ones Step 2.3 recorded,
  including the re-verify.
- Replace the closing sentence "Fault injection and x86_64 CI wiring are tracked separately,
  so `make smoke` is operator-run rather than a merge gate today." with a statement of what
  now holds: `Container lifecycle`'s `x86_64-linux` job runs the whole sequence against the
  pinned revision on every pull request that touches `scenarios/`, the containers, the
  lifecycle scripts or this section, and the measured CI duration.
- Add the arm64 statement: no GitHub-hosted macOS runner can run this fixture — container
  operations are Linux-only and nested virtualization is unsupported on arm64 macOS runners
  — so the arm64 proof stays operator-run, and the figures above are its current record.

### Step 3.6 — guardrails and commit

    make check
    make test

Expect both green, with `make test` reporting seven more tests than the 382 on `main`.
Commit as `ci: run the live scenario smoke path in the x86_64 job`.

### Step 3.7 — measure the CI run

After the pull request opens, state in the pull-request body how the gate was proven to bite
(Step 2.4's controlled fault, with the divergence line it produced), then read the
`Container lifecycle / x86_64-linux` job's total duration and each new step's duration from the run summary, and record them in the pull
request body against the 45-minute budget and the 3.5-5.8 minute pre-change baseline.

Never raise `timeout-minutes`: the 45-minute budget is issue #25's stated constraint. If
the measured run exceeds it, apply the remedy ADR 0010's rejected-alternatives list already
holds ready -- cache the built prefix, keyed on the pinned SHA, so the compile is a
first-run cost:

```yaml
      - name: Restore the pinned bzr build
        id: bzr-cache
        uses: actions/cache@<pinned full SHA> # <version tag>
        with:
          path: ${{ runner.temp }}/bzr
          key: bzr-${{ runner.os }}-63abb94e7e14a2db79efe0ddf0011a1f32ed8640
```

with `if: steps.bzr-cache.outputs.cache-hit != 'true'` on the install step. Resolve
`actions/cache`'s current release and its commit SHA at that moment rather than from this
plan, promote the rejected bullet to a decision, and say in the pull request what the
measurement was that forced it.

**Acceptance criteria.** The `x86_64-linux` job installs a full-SHA-pinned `bzr`, starts
the fixture and runs `make smoke` to completion; the job stays inside 45 minutes and the
measured duration is reported; `README.md` states what CI proves, at which revision, and
why the arm64 half is not a GitHub-hosted job; the pin test fails if the two files disagree.

## Deferrals carried into this plan

None recorded at the time of writing. Any deferral a review of this design disposes of as
`deferred-tracked` is appended here with its owning record path or tracker issue.
