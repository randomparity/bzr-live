from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import MappingProxyType

from bzr_live.scenario import (
    CompletedRecord,
    InFlightRecord,
    InvocationMetadata,
    JournalStore,
    Reference,
    freeze_planned,
    load_scenario,
)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke_installed.py SCENARIO_DIRECTORY")
    scenario = load_scenario(Path(sys.argv[1]))
    marker = f"bzr-live:{scenario.name}:smoke"
    postcondition = freeze_planned(
        {
            "action": "bug.comment",
            "target": Reference("bug", "smoke"),
            "values": {"body": "installed smoke"},
            "marker": marker,
        }
    )
    in_flight = InFlightRecord(
        scenario.digest,
        "smoke",
        1,
        Reference("actor", "smoke"),
        "append",
        postcondition,
        marker,
    )
    completed = CompletedRecord(
        scenario.digest,
        "smoke",
        1,
        Reference("actor", "smoke"),
        "append",
        postcondition,
        marker,
        InvocationMetadata("bzr", "smoke", (), ()),
        freeze_planned({"result": "completed"}),
        0,
        MappingProxyType({"bug:smoke": 1}),
        "advance",
    )
    with tempfile.TemporaryDirectory() as temporary:
        with JournalStore(Path(temporary) / "journal") as store:
            store.write_in_flight(in_flight)
            store.replace_completed(completed)
            stored = store.read("smoke")
    if not isinstance(stored, CompletedRecord):
        raise RuntimeError("installed journal smoke did not return a completed record")
    print(f"{scenario.digest} completed")


if __name__ == "__main__":
    main()
