from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..scenario import ScenarioValidationError, load_scenario
from .adapters import BridgeClient, BzrClient, ProvisionError, compose_project_name
from .executor import Provisioner
from .keys import KeyStore


def _compose_prefix(project_root: str) -> tuple[list[str], str]:
    root = os.path.realpath(project_root)
    project = compose_project_name(root)
    prefix = [
        "docker", "compose", "--project-name", project,
        "--project-directory", root, "--file", str(Path(root) / "compose.yaml"),
        "exec", "-T", "--user", "www-data", "bugzilla", "bzr-live-bridge",
    ]
    return prefix, project


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m bzr_live.provision",
        description="Provision scenario resources into the local Bugzilla fixture.")
    parser.add_argument("scenario_dir")
    parser.add_argument("--state-root", default="./state")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/")
    parser.add_argument("--bzr", default="bzr")
    parser.add_argument("--project-root", default=".")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = _parse(argv)
    try:
        scenario = load_scenario(options.scenario_dir)
        store = KeyStore(options.state_root)
        prefix, project = _compose_prefix(options.project_root)
        bridge = BridgeClient(prefix, project)
        provisioner = Provisioner(
            scenario,
            lambda key: BzrClient(options.bzr, options.base_url, key),
            bridge,
            store,
        )
        provisioner.run()
    except (ProvisionError, ScenarioValidationError) as exc:
        print(f"provision failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
