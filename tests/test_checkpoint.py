from __future__ import annotations

import copy
import hashlib
import json
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path

from bzr_live.checkpoint import (
    ARTIFACT_NAMES,
    CHECKPOINT_FORMAT,
    CheckpointError,
    canonical_json,
    read_manifest,
    render_retry_command,
    stack_fingerprint,
    validate_bundle,
    validate_name,
)


VALID_NAMES = ("a", "pristine", "named-state_2", "z" * 64)
INVALID_NAMES = ("", "A", "-bad", "bad.name", "z" * 65)
REVISION = "a" * 40


class BundleContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "checkout"
        self.runner_state = Path(self._temporary.name) / "runner state"
        self.runner_state.mkdir()
        self._create_stack_inputs()
        self.fingerprint = stack_fingerprint(self.root, self.runner_state)
        self.final = self.root / "store" / "pristine"
        self.manifest = self._manifest()
        self._write_bundle()

    def _create_stack_inputs(self) -> None:
        inputs = {
            ".env": b"DATABASE_PASSWORD=secret fixture bytes\n",
            "compose.yaml": b"services: {}\n",
            "containers/bugzilla/Containerfile": b"FROM scratch\n",
            "containers/mariadb/init.sql": b"SELECT 1;\n",
            "scripts/lifecycle": b"#!/bin/sh\n",
            "scripts/checkpoint": b"#!/usr/bin/env python3\n",
            "src/bzr_live/checkpoint.py": b"CHECKPOINT_FORMAT = 1\n",
        }
        for relative, content in inputs.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.root / ".env").chmod(0o600)

    def _manifest(self) -> dict[str, object]:
        artifact_bytes = {
            "bugzilla-volume.tar": b"bugzilla tar bytes",
            "mariadb-volume.tar": b"mariadb tar bytes",
            "runner-state.tar": b"runner tar bytes",
        }
        artifacts = {
            name: {
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
            for name, content in artifact_bytes.items()
        }
        return {
            "artifacts": artifacts,
            "checkpoint_format": CHECKPOINT_FORMAT,
            "checkpoint_name": "pristine",
            "checkout_revision": REVISION,
            "created_at": "2026-08-30T12:34:56.123456Z",
            "stack_fingerprint": self.fingerprint,
        }

    def _write_bundle(self, manifest: dict[str, object] | None = None) -> None:
        if self.final.exists() or self.final.is_symlink():
            if self.final.is_dir() and not self.final.is_symlink():
                shutil.rmtree(self.final)
            else:
                self.final.unlink()
        self.final.mkdir(parents=True)
        contents = {
            "bugzilla-volume.tar": b"bugzilla tar bytes",
            "mariadb-volume.tar": b"mariadb tar bytes",
            "runner-state.tar": b"runner tar bytes",
        }
        for name, content in contents.items():
            path = self.final / name
            path.write_bytes(content)
            path.chmod(0o600)
        manifest_path = self.final / "manifest.json"
        manifest_path.write_bytes(canonical_json(manifest or self.manifest))
        manifest_path.chmod(0o600)

    def _write_manifest(self, manifest: dict[str, object]) -> Path:
        path = self.root / "candidate-manifest.json"
        path.write_bytes(canonical_json(manifest))
        return path

    def test_name_grammar_is_closed(self) -> None:
        for name in VALID_NAMES:
            with self.subTest(valid=name):
                self.assertEqual(validate_name(name), name)
        for name in INVALID_NAMES:
            with self.subTest(invalid=name):
                with self.assertRaises(CheckpointError):
                    validate_name(name)

    def test_canonical_json_is_sorted_compact_utf8_with_final_newline(self) -> None:
        self.assertEqual(canonical_json({"z": 1, "a": "é"}), b'{"a":"\xc3\xa9","z":1}\n')
        emitted_manifest = canonical_json(self.manifest)
        self.assertNotIn((self.root / ".env").read_bytes(), emitted_manifest)
        self.assertNotIn(str(self.runner_state.resolve()).encode(), emitted_manifest)

        noncanonical = self.root / "noncanonical-manifest.json"
        noncanonical.write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            read_manifest(noncanonical)

    def test_manifest_rejects_unknown_missing_and_wrong_typed_fields(self) -> None:
        self.assertEqual(read_manifest(self._write_manifest(self.manifest)), self.manifest)

        invalid_manifests: list[tuple[str, dict[str, object]]] = []
        unknown = copy.deepcopy(self.manifest)
        unknown["unknown"] = "value"
        invalid_manifests.append(("unknown top-level field", unknown))

        missing = copy.deepcopy(self.manifest)
        del missing["created_at"]
        invalid_manifests.append(("missing top-level field", missing))

        wrong_format_type = copy.deepcopy(self.manifest)
        wrong_format_type["checkpoint_format"] = True
        invalid_manifests.append(("boolean format", wrong_format_type))

        wrong_artifacts_type = copy.deepcopy(self.manifest)
        wrong_artifacts_type["artifacts"] = []
        invalid_manifests.append(("non-object artifacts", wrong_artifacts_type))

        unknown_artifact_field = copy.deepcopy(self.manifest)
        unknown_artifact_field["artifacts"][ARTIFACT_NAMES[0]]["unknown"] = 1  # type: ignore[index]
        invalid_manifests.append(("unknown artifact field", unknown_artifact_field))

        missing_artifact = copy.deepcopy(self.manifest)
        del missing_artifact["artifacts"][ARTIFACT_NAMES[0]]  # type: ignore[index]
        invalid_manifests.append(("missing artifact", missing_artifact))

        boolean_size = copy.deepcopy(self.manifest)
        boolean_size["artifacts"][ARTIFACT_NAMES[0]]["size"] = True  # type: ignore[index]
        invalid_manifests.append(("boolean size", boolean_size))

        negative_size = copy.deepcopy(self.manifest)
        negative_size["artifacts"][ARTIFACT_NAMES[0]]["size"] = -1  # type: ignore[index]
        invalid_manifests.append(("negative size", negative_size))

        uppercase_hash = copy.deepcopy(self.manifest)
        uppercase_hash["artifacts"][ARTIFACT_NAMES[0]]["sha256"] = "A" * 64  # type: ignore[index]
        invalid_manifests.append(("uppercase hash", uppercase_hash))

        uppercase_revision = copy.deepcopy(self.manifest)
        uppercase_revision["checkout_revision"] = "A" * 40
        invalid_manifests.append(("uppercase revision", uppercase_revision))

        malformed_timestamp = copy.deepcopy(self.manifest)
        malformed_timestamp["created_at"] = "2026-08-30T12:34:56+00:00"
        invalid_manifests.append(("non-Z timestamp", malformed_timestamp))

        impossible_timestamp = copy.deepcopy(self.manifest)
        impossible_timestamp["created_at"] = "2026-02-30T12:34:56Z"
        invalid_manifests.append(("impossible timestamp", impossible_timestamp))

        for reason, manifest in invalid_manifests:
            with self.subTest(reason=reason):
                with self.assertRaises(CheckpointError):
                    read_manifest(self._write_manifest(manifest))

        duplicate_path = self.root / "duplicate-manifest.json"
        duplicate_path.write_text('{"checkpoint_format":1,"checkpoint_format":1}\n', encoding="utf-8")
        with self.assertRaises(CheckpointError):
            read_manifest(duplicate_path)

    def test_manifest_name_must_match_cli_and_directory(self) -> None:
        renamed = self.final.with_name("renamed")
        self.final.rename(renamed)
        with self.assertRaises(CheckpointError):
            validate_bundle(
                renamed,
                name="pristine",
                revision=REVISION,
                fingerprint=self.fingerprint,
            )
        renamed.rename(self.final)

        mismatched = copy.deepcopy(self.manifest)
        mismatched["checkpoint_name"] = "other"
        self._write_bundle(mismatched)
        with self.assertRaises(CheckpointError):
            validate_bundle(
                self.final,
                name="pristine",
                revision=REVISION,
                fingerprint=self.fingerprint,
            )

    def test_bundle_rejects_extra_missing_nonregular_and_wrong_mode_files(self) -> None:
        mutations = (
            ("extra", lambda: (self.final / "extra").write_bytes(b"extra")),
            ("missing", lambda: (self.final / ARTIFACT_NAMES[0]).unlink()),
            (
                "nonregular",
                lambda: (
                    (self.final / ARTIFACT_NAMES[0]).unlink(),
                    (self.final / ARTIFACT_NAMES[0]).mkdir(),
                ),
            ),
            ("wrong mode", lambda: (self.final / ARTIFACT_NAMES[0]).chmod(0o644)),
            ("manifest wrong mode", lambda: (self.final / "manifest.json").chmod(0o644)),
        )
        for reason, mutate in mutations:
            with self.subTest(reason=reason):
                self._write_bundle()
                mutate()
                with self.assertRaises(CheckpointError):
                    validate_bundle(
                        self.final,
                        name="pristine",
                        revision=REVISION,
                        fingerprint=self.fingerprint,
                    )

    def test_bundle_rejects_size_checksum_revision_and_fingerprint_mismatch(self) -> None:
        wrong_size = copy.deepcopy(self.manifest)
        wrong_size["artifacts"][ARTIFACT_NAMES[0]]["size"] = 999  # type: ignore[index]
        wrong_checksum = copy.deepcopy(self.manifest)
        wrong_checksum["artifacts"][ARTIFACT_NAMES[0]]["sha256"] = "b" * 64  # type: ignore[index]

        for reason, manifest, revision, fingerprint in (
            ("size", wrong_size, REVISION, self.fingerprint),
            ("checksum", wrong_checksum, REVISION, self.fingerprint),
            ("revision", self.manifest, "b" * 40, self.fingerprint),
            ("fingerprint", self.manifest, REVISION, "b" * 64),
        ):
            with self.subTest(reason=reason):
                self._write_bundle(manifest)
                with self.assertRaises(CheckpointError):
                    validate_bundle(
                        self.final,
                        name="pristine",
                        revision=revision,
                        fingerprint=fingerprint,
                    )

    def test_fingerprint_changes_for_env_runner_path_and_each_declared_file(self) -> None:
        baseline = stack_fingerprint(self.root, self.runner_state)
        self.assertRegex(baseline, r"^[0-9a-f]{64}$")

        other_runner = Path(self._temporary.name) / "other runner"
        other_runner.mkdir()
        self.assertNotEqual(stack_fingerprint(self.root, other_runner), baseline)

        declared_files = (
            ".env",
            "compose.yaml",
            "containers/bugzilla/Containerfile",
            "containers/mariadb/init.sql",
            "scripts/lifecycle",
            "scripts/checkpoint",
            "src/bzr_live/checkpoint.py",
        )
        for relative in declared_files:
            with self.subTest(relative=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                self.assertNotEqual(stack_fingerprint(self.root, self.runner_state), baseline)
                path.write_bytes(original)
                if relative == ".env":
                    path.chmod(0o600)

    def test_retry_command_round_trips_spaces_quotes_and_leading_hyphens(self) -> None:
        store = Path("-store with spaces") / "quoted'component"
        runner_state = Path("runner state") / 'double"quote'
        command = render_retry_command("named-state_2", store, runner_state)
        self.assertEqual(
            shlex.split(command),
            [
                "scripts/checkpoint",
                "restore",
                "named-state_2",
                "--store",
                str(store),
                "--runner-state",
                str(runner_state),
            ],
        )


if __name__ == "__main__":
    unittest.main()
