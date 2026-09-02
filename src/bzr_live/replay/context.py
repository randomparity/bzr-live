from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from ..provision.adapters import (
    BUG_ABSENT_CODES,
    BUG_CUSTOM_FIELD_BOUNDARY,
    BzrClient,
    _KEY_ENV,
    assign_bug_custom_fields,
)
from ..provision.keys import KeyStore
from ..scenario import PlannedResource, Reference, ValidatedScenario

KEY_ENV = _KEY_ENV
REST_BOUNDARY = BUG_CUSTOM_FIELD_BOUNDARY


class ReplayError(Exception):
    """Actionable replay failure; str(exc) is the operator-facing message."""


class ReplayContext:
    """Credentials, symbolic identity, and the scratch workspace for one run."""

    def __init__(self, scenario: ValidatedScenario, keys: KeyStore, *, bzr_path: str,
                 base_url: str, workspace: str | Path, run=subprocess.run,
                 opener=urllib.request.urlopen) -> None:
        self._scenario = scenario
        self._keys = keys
        self._bzr_path = bzr_path
        self._base_url = base_url
        self._workspace = Path(workspace)
        self._run = run
        self._opener = opener
        self._clients: dict[str, BzrClient] = {}
        self._keys_seen: dict[str, str] = {}
        self._ids: dict[str, int] = {}
        self._resources = {f"{r.kind}:{r.name}": r for r in scenario.resources}
        self._files = 0

    # --- resources and credentials ---------------------------------------

    def resource(self, kind: str, name: str) -> PlannedResource:
        try:
            return self._resources[f"{kind}:{name}"]
        except KeyError:
            raise ReplayError(f"{kind}:{name} is not declared in this scenario") from None

    def actor_email(self, actor: Reference) -> str:
        return self.resource("actor", actor.name).data["email"]

    def actor_key(self, actor: Reference) -> str:
        if actor.name not in self._keys_seen:
            key = self._keys.actor_key(actor.name)
            if key is None:
                raise ReplayError(
                    f"no API key for actor {actor.name!r}; run "
                    f"python -m bzr_live.provision <scenario_dir> first")
            self._keys_seen[actor.name] = key
        return self._keys_seen[actor.name]

    def client(self, actor: Reference) -> BzrClient:
        key = self.actor_key(actor)
        if actor.name not in self._clients:
            self._clients[actor.name] = BzrClient(
                self._bzr_path, self._base_url, key,
                admin_email=self.actor_email(actor), run=self._run)
        return self._clients[actor.name]

    @property
    def known_secrets(self) -> frozenset[str]:
        return frozenset(self._keys_seen.values())

    # --- identity ---------------------------------------------------------

    def read_bug(self, actor: Reference, positionals: list[str]) -> object | None:
        """Read a bug by id or alias; None means absent, 102 (access denied) raises."""
        return self.client(actor).read(
            ["bug", "view"], positionals=positionals, absent_codes=BUG_ABSENT_CODES)

    def resolve(self, ref: Reference) -> int:
        try:
            return self._ids[f"{ref.kind}:{ref.name}"]
        except KeyError:
            raise ReplayError(
                f"{ref.kind}:{ref.name} has no server id; the event that creates it has "
                "not completed") from None

    def resolve_all(self, refs: tuple[Reference, ...]) -> list[int]:
        return [self.resolve(ref) for ref in refs]

    def adopt(self, resolved_ids: Mapping[str, int]) -> None:
        self._ids.update(resolved_ids)

    # --- workspace --------------------------------------------------------

    def _write_private(self, name: str, content: bytes) -> str:
        path = self._workspace / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(fd, content[offset:])
        finally:
            os.close(fd)
        return str(path)

    def text_file(self, stem: str, text: str) -> str:
        self._files += 1
        return self._write_private(
            f"{self._files:04d}-{stem}.txt", text.encode("utf-8"))

    def json_file(self, stem: str, document: dict) -> str:
        self._files += 1
        encoded = json.dumps(document, ensure_ascii=False).encode("utf-8")
        return self._write_private(f"{self._files:04d}-{stem}.json", encoded)

    def asset_file(self, name: str, expected_sha256: str) -> str:
        asset = self._scenario.assets[name]
        if asset.sha256 != expected_sha256:
            raise ReplayError(
                f"asset {name!r} ({asset.path}) hashes to {asset.sha256} but the event "
                f"expects {expected_sha256}; the scenario and its journal disagree")
        # The attachment carries the asset's own basename, so each materialization needs
        # its own directory to stay unique.
        self._files += 1
        directory = self._workspace / f"{self._files:04d}-asset"
        os.mkdir(directory, 0o700)
        return self._write_private(
            f"{directory.name}/{Path(asset.path).name}", asset.content)

    # --- the REST boundary -------------------------------------------------

    def rest(self, actor: Reference, bug_id: int, values: dict) -> object:
        return assign_bug_custom_fields(
            self._base_url, self.actor_key(actor), bug_id, values, opener=self._opener)
