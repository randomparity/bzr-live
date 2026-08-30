from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shlex
import stat
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bzr_live.checkpoint import (
    ARTIFACT_NAMES,
    CHECKPOINT_FORMAT,
    FINAL_NAMES,
    CheckpointError,
    canonical_json,
    cleanup_staging,
    create_runner_archive,
    create_staging,
    extract_runner_archive,
    read_manifest,
    render_retry_command,
    stack_fingerprint,
    validate_bundle,
    validate_name,
    validate_paths,
    validate_runner_archive,
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

    def test_fingerprint_read_errors_are_actionable(self) -> None:
        original_read_bytes = Path.read_bytes
        for relative in ("compose.yaml", "containers/bugzilla/Containerfile"):
            target = self.root / relative

            def read_bytes(path: Path, target: Path = target) -> bytes:
                if path == target:
                    raise OSError("read denied")
                return original_read_bytes(path)

            with self.subTest(relative=relative):
                with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=read_bytes):
                    with self.assertRaises(CheckpointError) as raised:
                        stack_fingerprint(self.root, self.runner_state)
                message = str(raised.exception)
                self.assertTrue(message.startswith("fingerprint:"))
                self.assertIn(f"cannot read {target}", message)
                self.assertIn("read denied", message)

    def test_fingerprint_preserves_surrogateescaped_container_path_bytes(self) -> None:
        baseline = stack_fingerprint(self.root, self.runner_state)
        raw_name = b"surrogate-\xff"
        path = self.root / "containers" / os.fsdecode(raw_name)
        regular_metadata = (self.root / "containers/bugzilla/Containerfile").lstat()
        original_lstat = Path.lstat
        original_read_bytes = Path.read_bytes

        def lstat(candidate: Path) -> os.stat_result:
            return regular_metadata if candidate == path else original_lstat(candidate)

        def read_bytes(candidate: Path) -> bytes:
            return b"surrogate path content" if candidate == path else original_read_bytes(candidate)

        with (
            mock.patch.object(Path, "rglob", autospec=True, return_value=[path]),
            mock.patch.object(Path, "lstat", autospec=True, side_effect=lstat),
            mock.patch.object(Path, "read_bytes", autospec=True, side_effect=read_bytes),
        ):
            changed = stack_fingerprint(self.root, self.runner_state)

        self.assertNotEqual(changed, baseline)
        relative = path.relative_to(self.root).as_posix()
        self.assertEqual(os.fsencode(relative), b"containers/" + raw_name)

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


class HostPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.base = Path(self._temporary.name).resolve()
        self.root = self.base / "checkout"
        self.store = self.base / "checkpoint store"
        self.runner_parent = self.base / "runner parent"
        self.runner = self.runner_parent / "runner state"
        for path in (self.root, self.store, self.runner_parent, self.runner):
            path.mkdir(mode=0o700)
        self.name = "pristine"

    def _validate(
        self,
        operation: str = "save",
        *,
        store: Path | None = None,
        runner: Path | None = None,
    ):
        return validate_paths(
            operation,
            self.root,
            self.name,
            str(store or self.store),
            str(runner or self.runner),
        )

    def _marker(self, paths) -> bytes:
        return canonical_json(
            {
                "checkpoint_name": self.name,
                "final_path": str(paths.final),
                "staging_format": 1,
            }
        )

    def test_directory_arguments_reject_ascii_controls(self) -> None:
        for codepoint in (*range(0x20), 0x7F):
            with self.subTest(codepoint=codepoint), self.assertRaises(CheckpointError):
                validate_paths(
                    "save",
                    self.root,
                    self.name,
                    f"{self.store}{chr(codepoint)}suffix",
                    str(self.runner),
                )
            with self.subTest(codepoint=codepoint, argument="runner"), self.assertRaises(
                CheckpointError
            ):
                validate_paths(
                    "save",
                    self.root,
                    self.name,
                    str(self.store),
                    f"{self.runner}{chr(codepoint)}suffix",
                )

    def test_store_and_runner_must_be_canonical_owned_and_nonoverlapping(self) -> None:
        noncanonical_store = str(self.store / ".." / self.store.name)
        noncanonical_runner = str(self.runner / ".." / self.runner.name)
        for store, runner in (
            (noncanonical_store, str(self.runner)),
            (str(self.store), noncanonical_runner),
            (str(self.store), str(self.store / "runner")),
            (str(self.runner / "store"), str(self.runner)),
        ):
            with self.subTest(store=store, runner=runner), self.assertRaises(CheckpointError):
                validate_paths("save", self.root, self.name, store, runner)

        original_lstat = os.lstat
        store_inode = self.store.lstat().st_ino

        def foreign_store(path, *args, **kwargs):
            metadata = original_lstat(path, *args, **kwargs)
            if metadata.st_ino == store_inode:
                values = list(metadata)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return metadata

        with mock.patch("os.lstat", side_effect=foreign_store), self.assertRaises(CheckpointError):
            self._validate()

    def test_runner_rejects_root_home_checkout_store_and_their_ancestors(self) -> None:
        dangerous = {
            Path("/"),
            Path.home(),
            self.root,
            self.root.parent,
            self.store,
            self.store.parent,
        }
        for runner in dangerous:
            with self.subTest(runner=runner), self.assertRaises(CheckpointError):
                self._validate(runner=runner)

    def test_save_rejects_absent_runner_before_health_or_docker(self) -> None:
        self.runner.rmdir()
        with self.assertRaisesRegex(CheckpointError, "save.*runner"):
            self._validate("save")

    def test_restore_accepts_absent_runner_only_at_fingerprinted_safe_path(self) -> None:
        self.runner.rmdir()
        paths = self._validate("restore")
        self.assertEqual(paths.runner_state, self.runner)

        self.runner_parent.chmod(0o750)
        with self.assertRaises(CheckpointError):
            self._validate("restore")
        self.runner_parent.chmod(0o700)

        missing_parent_runner = self.base / "missing" / "runner"
        with self.assertRaises(CheckpointError):
            self._validate("restore", runner=missing_parent_runner)

    def test_runner_rejects_links_mounts_cross_filesystem_and_foreign_owners(self) -> None:
        linked = self.runner / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(CheckpointError):
            self._validate()
        linked.unlink()
        outside = self.base / "outside-runner"
        outside.write_bytes(b"shared inode")
        hard_link = self.runner / "hard-linked"
        os.link(outside, hard_link)
        archive = self.base / "must-not-exist.tar"
        with self.assertRaisesRegex(CheckpointError, "hard link"):
            self._validate()
        with self.assertRaisesRegex(CheckpointError, "hard link"):
            create_runner_archive(self.runner, archive)
        self.assertFalse(archive.exists())
        hard_link.unlink()
        outside.unlink()


        mounted = self.runner / "mounted"
        mounted.mkdir(mode=0o700)
        original_is_mount = Path.is_mount
        with mock.patch.object(
            Path,
            "is_mount",
            autospec=True,
            side_effect=lambda path: path == mounted or original_is_mount(path),
        ), self.assertRaises(CheckpointError):
            self._validate()

        original_lstat = os.lstat
        mounted_inode = mounted.lstat().st_ino

        def mutate_metadata(path, *args, **kwargs):
            metadata = original_lstat(path, *args, **kwargs)
            if metadata.st_ino != mounted_inode:
                return metadata
            values = list(metadata)
            values[2] = metadata.st_dev + 1
            return os.stat_result(values)

        with mock.patch("os.lstat", side_effect=mutate_metadata), self.assertRaises(CheckpointError):
            self._validate()

        def foreign_metadata(path, *args, **kwargs):
            metadata = original_lstat(path, *args, **kwargs)
            if metadata.st_ino != mounted_inode:
                return metadata
            values = list(metadata)
            values[4] = os.getuid() + 1
            return os.stat_result(values)

        with mock.patch("os.lstat", side_effect=foreign_metadata), self.assertRaises(
            CheckpointError
        ):
            self._validate()

    def test_runner_rejects_directories_without_owner_rwx(self) -> None:
        child = self.runner / "restricted"
        child.mkdir(mode=0o700)
        self.addCleanup(child.chmod, 0o700)
        for mode in (0o600, 0o500, 0o300):
            with self.subTest(mode=oct(mode)):
                child.chmod(mode)
                with self.assertRaises(CheckpointError):
                    self._validate()
        child.chmod(0o755)
        self._validate()

    def test_staging_cleanup_requires_exact_matching_marker(self) -> None:
        paths = self._validate()
        marker_path = paths.staging / ".checkpoint-staging.json"
        mismatches = (
            b"",
            canonical_json(
                {
                    "checkpoint_name": "other",
                    "final_path": str(paths.final),
                    "staging_format": 1,
                }
            ),
            canonical_json(
                {
                    "checkpoint_name": self.name,
                    "final_path": str(paths.final.with_name("other")),
                    "staging_format": 1,
                }
            ),
            canonical_json(
                {
                    "checkpoint_name": self.name,
                    "final_path": str(paths.final),
                    "staging_format": 2,
                }
            ),
            self._marker(paths).rstrip(b"\n"),
        )
        for marker in mismatches:
            with self.subTest(marker=marker):
                paths.staging.mkdir(mode=0o700)
                marker_path.write_bytes(marker)
                marker_path.chmod(0o600)
                with self.assertRaisesRegex(CheckpointError, "inspect.*remove.*manually"):
                    cleanup_staging(paths, self.name)
                marker_path.unlink()
                paths.staging.rmdir()

    def test_staging_cleanup_rejects_unknown_nested_linked_and_mounted_content(self) -> None:
        paths = self._validate()

        def make_staging() -> Path:
            paths.staging.mkdir(mode=0o700)
            marker = paths.staging / ".checkpoint-staging.json"
            marker.write_bytes(self._marker(paths))
            marker.chmod(0o600)
            return marker

        marker = make_staging()
        unknown = paths.staging / "unknown"
        unknown.write_bytes(b"x")
        unknown.chmod(0o600)
        with self.assertRaises(CheckpointError):
            cleanup_staging(paths, self.name)
        unknown.unlink()
        marker.unlink()
        paths.staging.rmdir()

        marker = make_staging()
        nested = paths.staging / "manifest.json"
        nested.mkdir(mode=0o700)
        with self.assertRaises(CheckpointError):
            cleanup_staging(paths, self.name)
        nested.rmdir()
        marker.unlink()
        paths.staging.rmdir()

        marker = make_staging()
        linked = paths.staging / "manifest.json"
        linked.symlink_to(self.root)
        with self.assertRaises(CheckpointError):
            cleanup_staging(paths, self.name)
        linked.unlink()
        marker.unlink()
        paths.staging.rmdir()

        marker = make_staging()
        mounted = paths.staging / "manifest.json"
        mounted.write_bytes(b"x")
        mounted.chmod(0o600)
        original_is_mount = Path.is_mount
        with mock.patch.object(
            Path,
            "is_mount",
            autospec=True,
            side_effect=lambda path: path == mounted or original_is_mount(path),
        ), self.assertRaises(CheckpointError):
            cleanup_staging(paths, self.name)
        mounted.unlink()
        marker.unlink()
        paths.staging.rmdir()

    def test_staging_cleanup_unlinks_valid_children_then_removes_directory(self) -> None:
        paths = self._validate()
        created = create_staging(paths, self.name)
        self.assertEqual(created, paths.staging)
        self.assertEqual(stat.S_IMODE(paths.staging.lstat().st_mode), 0o700)
        marker = paths.staging / ".checkpoint-staging.json"
        self.assertEqual(marker.read_bytes(), self._marker(paths))
        self.assertEqual(stat.S_IMODE(marker.lstat().st_mode), 0o600)
        for filename in FINAL_NAMES:
            child = paths.staging / filename
            child.write_bytes(filename.encode())
            child.chmod(0o600)

        cleanup_staging(paths, self.name)
        self.assertFalse(paths.staging.exists())


class RunnerArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.base = Path(self._temporary.name)
        self.archive = self.base / "runner-state.tar"

    def _write_archive(self, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> None:
        with tarfile.open(self.archive, "w") as archive:
            for member, payload in members:
                archive.addfile(member, io.BytesIO(payload) if payload is not None else None)

    def _regular(self, name: str, payload: bytes = b"x", mode: int = 0o640) -> tuple:
        member = tarfile.TarInfo(name)
        member.type = tarfile.REGTYPE
        member.mode = mode
        member.size = len(payload)
        return member, payload

    def _directory(self, name: str, mode: int = 0o700) -> tuple:
        member = tarfile.TarInfo(name)
        member.type = tarfile.DIRTYPE
        member.mode = mode
        return member, None

    def test_runner_archive_rejects_unsafe_member_names(self) -> None:
        for name in (
            "",
            "/absolute",
            "../parent",
            "a/../parent",
            "a//b",
            "a\\b",
            ".",
            "a/./b",
        ):
            with self.subTest(name=name):
                self._write_archive([self._regular(name)])
                with self.assertRaises(CheckpointError):
                    validate_runner_archive(self.archive)

    def test_runner_archive_rejects_duplicates_links_special_sparse_and_unknown_types(self) -> None:
        cases: list[list[tuple[tarfile.TarInfo, bytes | None]]] = [
            [self._regular("duplicate"), self._regular("duplicate")],
        ]
        for member_type in (
            tarfile.SYMTYPE,
            tarfile.LNKTYPE,
            tarfile.CHRTYPE,
            tarfile.BLKTYPE,
            tarfile.FIFOTYPE,
            tarfile.GNUTYPE_SPARSE,
            b"s",
            b"Z",
        ):
            member = tarfile.TarInfo(f"type-{member_type!r}")
            member.type = member_type
            cases.append([(member, None)])
        sparse = tarfile.TarInfo("sparse")
        sparse.type = tarfile.REGTYPE
        sparse.size = 0
        sparse.pax_headers = {"GNU.sparse.size": "0"}
        cases.append([(sparse, b"")])

        for members in cases:
            with self.subTest(member=members[0][0].name, type=members[0][0].type):
                self._write_archive(members)
                with self.assertRaises(CheckpointError):
                    validate_runner_archive(self.archive)

    def test_runner_archive_rejects_directories_without_owner_rwx(self) -> None:
        for mode in (0o600, 0o500, 0o300):
            with self.subTest(mode=oct(mode)):
                self._write_archive([self._directory("directory", mode)])
                with self.assertRaises(CheckpointError):
                    validate_runner_archive(self.archive)

    def test_runner_archive_rejects_regular_file_before_descendant(self) -> None:
        self._write_archive([self._regular("a"), self._regular("a/b")])
        with self.assertRaises(CheckpointError):
            validate_runner_archive(self.archive)

    def test_runner_archive_rejects_descendant_before_regular_file(self) -> None:
        self._write_archive([self._regular("a/b"), self._regular("a")])
        with self.assertRaises(CheckpointError):
            validate_runner_archive(self.archive)

    def test_runner_archive_accepts_implicit_directories_empty_files_and_unicode(self) -> None:
        self._write_archive(
            [
                self._regular("implicit/nested/empty", b"", 0o700),
                self._regular("unicodé/雪", b"payload", 0o500),
            ]
        )
        members = validate_runner_archive(self.archive)
        self.assertEqual(
            [(member.name, member.size, member.mode & 0o700) for member in members],
            [("implicit/nested/empty", 0, 0o700), ("unicodé/雪", 7, 0o500)],
        )

    def test_runner_archive_nested_tree_round_trips_modes_and_content(self) -> None:
        source = self.base / "source"
        source.mkdir(mode=0o750)
        (source / "empty").write_bytes(b"")
        (source / "empty").chmod(0o600)
        nested = source / "nested"
        nested.mkdir(mode=0o750)
        unicode_file = nested / "unicodé-雪"
        unicode_file.write_bytes(b"runner payload")
        unicode_file.chmod(0o510)

        create_runner_archive(source, self.archive)
        self.assertEqual(stat.S_IMODE(self.archive.lstat().st_mode), 0o600)
        validate_runner_archive(self.archive)

        destination = self.base / "restored"
        extract_runner_archive(self.archive, destination)
        self.assertEqual(stat.S_IMODE(destination.lstat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((destination / "nested").lstat().st_mode), 0o700)
        self.assertEqual((destination / "empty").read_bytes(), b"")
        self.assertEqual(stat.S_IMODE((destination / "empty").lstat().st_mode), 0o600)
        restored_unicode = destination / "nested" / unicode_file.name
        self.assertEqual(unicode_file.read_bytes(), restored_unicode.read_bytes())
        self.assertEqual(stat.S_IMODE(restored_unicode.lstat().st_mode), 0o500)

if __name__ == "__main__":
    unittest.main()
