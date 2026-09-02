from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from ..provision.adapters import ProvisionError
from ..provision.keys import KeyStore
from ..scenario import JournalStore, ScenarioValidationError, load_scenario
from .context import ReplayContext, ReplayError
from .engine import ReplayEngine


def _parse(argv):
    parser = argparse.ArgumentParser(
        prog="python -m bzr_live.replay",
        description="Replay a scenario's events into the local Bugzilla fixture.")
    parser.add_argument("command", choices=("replay", "resume"))
    parser.add_argument("scenario_dir")
    parser.add_argument("--state-root", default="./state")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/")
    parser.add_argument("--bzr", default="bzr")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    options = _parse(argv)
    try:
        scenario = load_scenario(options.scenario_dir)
        keys = KeyStore(options.state_root)
        journal = Path(options.state_root) / "journal" / scenario.name
        journal.parent.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory() as workspace:
            Path(workspace).chmod(0o700)
            context = ReplayContext(
                scenario, keys, bzr_path=options.bzr, base_url=options.base_url,
                workspace=workspace)
            with JournalStore(journal) as store:
                engine = ReplayEngine(scenario, context, store, journal)
                getattr(engine, options.command)()
    except (ReplayError, ProvisionError, ScenarioValidationError) as exc:
        print(f"replay failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
