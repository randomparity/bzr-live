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
_STEP_NAME = re.compile(r"^      - name: (.+)$")


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
        re-added named record would be the class coming back, so it fails here.
        """
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
