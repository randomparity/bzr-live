from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shlex
import signal
import stat
import shutil
import tarfile
import tempfile
import unittest
from contextlib import contextmanager, nullcontext, redirect_stderr
from pathlib import Path
from unittest import mock

from bzr_live import checkpoint as checkpoint_module
from bzr_live.checkpoint import (
    ARTIFACT_NAMES,
    CHECKPOINT_FORMAT,
    FINAL_NAMES,
    CheckpointContext,
    CheckpointError,
    PreLockContext,
    SignalState,
    ValidatedPaths,
    build_checkpoint_context,
    canonical_json,
    cleanup_staging,
    create_runner_archive,
    create_staging,
    extract_runner_archive,
    health_check,
    main,
    read_manifest,
    render_retry_command,
    restore_checkpoint,
    run_child,
    save_checkpoint,
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

    def test_runner_archive_rechecks_link_count_on_open(self) -> None:
        source = self.base / "source"
        source.mkdir(mode=0o700)
        runner_file = source / "state"
        runner_file.write_bytes(b"runner state")
        late_link = self.base / "late-link"
        original_open = os.open
        linked = False

        def add_link_before_source_open(path, *args, **kwargs):
            nonlocal linked
            if Path(path) == runner_file and not linked:
                os.link(runner_file, late_link)
                linked = True
            return original_open(path, *args, **kwargs)

        with mock.patch("os.open", side_effect=add_link_before_source_open):
            with self.assertRaisesRegex(CheckpointError, "hard link"):
                create_runner_archive(source, self.archive)

        self.assertTrue(linked)
        self.assertTrue(late_link.samefile(runner_file))
        self.assertFalse(self.archive.exists())


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


class ArgvRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.stdin_bytes: list[bytes | None] = []
        self.on_call = None
        self.fail_when = None

    def __call__(
        self,
        argv,
        *,
        signal_state,
        stdin=None,
        stdout=None,
        capture_stderr=True,
        on_started=None,
        allow_requested=False,
    ) -> bytes:
        fixed = tuple(argv)
        self.calls.append(fixed)
        self.stdin_bytes.append(stdin.read() if stdin is not None else None)
        invocation = len(self.calls)
        if on_started is not None:
            on_started()
        if self.on_call is not None:
            self.on_call(invocation, fixed, signal_state)
        if self.fail_when is not None and self.fail_when(invocation, fixed):
            raise CheckpointError(f"command: injected failure at invocation {invocation}")
        if hasattr(stdout, "write"):
            stdout.write(f"archive-{invocation}".encode())
            stdout.flush()
        if fixed[-3:] == ("port", "bugzilla", "80"):
            return b"127.0.0.1:8080\n"
        return b""


class OrchestrationTests(unittest.TestCase):
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
        (self.runner / "state").write_bytes(b"runner state")
        self.name = "pristine"
        self.project = "bzr-live-0123456789ab"
        self.compose = (
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--project-directory",
            str(self.root),
            "--file",
            str(self.root / "compose.yaml"),
        )
        self.paths = ValidatedPaths(
            store=self.store,
            runner_state=self.runner,
            final=self.store / self.name,
            staging=self.store / f".{self.name}.staging",
        )
        self.context = CheckpointContext(
            operation="save",
            root=self.root,
            project=self.project,
            name=self.name,
            wait_timeout=37,
            compose=self.compose,
            paths=self.paths,
            revision=REVISION,
            fingerprint="b" * 64,
        )
        self.signal_state = SignalState()

    def _restore_context(self) -> CheckpointContext:
        return CheckpointContext(
            operation="restore",
            root=self.context.root,
            project=self.context.project,
            name=self.context.name,
            wait_timeout=self.context.wait_timeout,
            compose=self.context.compose,
            paths=self.context.paths,
            revision=self.context.revision,
            fingerprint=self.context.fingerprint,
        )

    def _write_bundle(self) -> None:
        if self.paths.final.exists():
            shutil.rmtree(self.paths.final)
        self.paths.final.mkdir(mode=0o700)
        for name, content in (
            ("mariadb-volume.tar", b"mariadb tar"),
            ("bugzilla-volume.tar", b"bugzilla tar"),
        ):
            artifact = self.paths.final / name
            artifact.write_bytes(content)
            artifact.chmod(0o600)
        runner_archive = self.paths.final / "runner-state.tar"
        create_runner_archive(self.runner, runner_archive)
        artifacts = {}
        for name in ARTIFACT_NAMES:
            artifact = self.paths.final / name
            content = artifact.read_bytes()
            artifacts[name] = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        manifest = {
            "artifacts": artifacts,
            "checkpoint_format": CHECKPOINT_FORMAT,
            "checkpoint_name": self.name,
            "checkout_revision": REVISION,
            "created_at": "2026-08-30T12:34:56Z",
            "stack_fingerprint": self.context.fingerprint,
        }
        manifest_path = self.paths.final / "manifest.json"
        manifest_path.write_bytes(canonical_json(manifest))
        manifest_path.chmod(0o600)

    def _save_patches(self, recorder: ArgvRecorder, events: list[str] | None = None):
        observed = events if events is not None else []

        def create_runner(source: Path, destination: Path) -> None:
            observed.append("create runner archive")
            destination.write_bytes(b"runner tar")
            destination.chmod(0o600)

        def validate_runner(source: Path):
            observed.append("validate runner archive")
            return ()

        return (
            mock.patch.object(checkpoint_module, "run_child", side_effect=recorder),
            mock.patch.object(
                checkpoint_module,
                "create_runner_archive",
                side_effect=create_runner,
            ),
            mock.patch.object(
                checkpoint_module,
                "validate_runner_archive",
                side_effect=validate_runner,
            ),
        )

    def _run_save(self, recorder: ArgvRecorder, events: list[str] | None = None) -> None:
        first, second, third = self._save_patches(recorder, events)
        with first, second, third:
            save_checkpoint(self.context, self.signal_state)

    def _run_restore(self, recorder: ArgvRecorder) -> None:
        with mock.patch.object(checkpoint_module, "run_child", side_effect=recorder):
            restore_checkpoint(self._restore_context(), self.signal_state)

    @property
    def _start(self) -> tuple[str, ...]:
        return self.compose + (
            "up",
            "--detach",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "37",
        )

    @property
    def _health(self) -> list[tuple[str, ...]]:
        return [
            self.compose + ("config", "--quiet"),
            self.compose + ("ps",),
            self.compose + ("port", "bugzilla", "80"),
            (
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "37",
                "http://127.0.0.1:8080/",
            ),
        ]

    def _archive(self, volume: str) -> tuple[str, ...]:
        return (
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--mount",
            f"type=volume,src={volume},dst=/volume,readonly",
            checkpoint_module.HELPER_IMAGE,
            "tar",
            "-C",
            "/volume",
            "-cf",
            "-",
            ".",
        )

    def _tar_list(self) -> tuple[str, ...]:
        return (
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--interactive",
            checkpoint_module.HELPER_IMAGE,
            "tar",
            "-tf",
            "-",
        )

    def _extract(self, volume: str) -> tuple[str, ...]:
        return (
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--interactive",
            "--mount",
            f"type=volume,src={volume},dst=/volume",
            checkpoint_module.HELPER_IMAGE,
            "tar",
            "-C",
            "/volume",
            "-xf",
            "-",
        )

    def test_no_store_or_runner_access_precedes_lock(self) -> None:
        prelock = PreLockContext(
            operation="save",
            root=self.root,
            project=self.project,
            lock_dir=self.base / "lifecycle.lock",
            name=self.name,
            store_arg=str(self.store),
            runner_arg=str(self.runner),
            wait_timeout=37,
            compose=self.compose,
        )
        order: list[str] = []

        @contextmanager
        def locked(lock_dir: Path, operation: str):
            order.append("lock entered")
            yield
            order.append("lock exited")

        def build(candidate: PreLockContext) -> CheckpointContext:
            self.assertIs(candidate, prelock)
            order.append("fixture paths read")
            return self.context

        with (
            mock.patch.object(
                checkpoint_module,
                "_parse_prelock_context",
                return_value=prelock,
            ),
            mock.patch.object(checkpoint_module, "lifecycle_lock", side_effect=locked),
            mock.patch.object(
                checkpoint_module,
                "build_checkpoint_context",
                side_effect=build,
            ),
            mock.patch.object(
                checkpoint_module,
                "save_checkpoint",
                side_effect=lambda *_: order.append("save"),
            ),
        ):
            self.assertEqual(main(["save", self.name, "--store", "x", "--runner-state", "y"]), 0)
        self.assertEqual(order, ["lock entered", "fixture paths read", "save", "lock exited"])

    def test_save_order_and_exact_compose_and_helper_argv(self) -> None:
        recorder = ArgvRecorder()
        self._run_save(recorder)
        mariadb = f"{self.project}_mariadb-data"
        bugzilla = f"{self.project}_bugzilla-data"
        self.assertEqual(
            recorder.calls,
            [
                *self._health,
                self.compose + ("down", "--remove-orphans"),
                self._archive(mariadb),
                self._archive(bugzilla),
                self._tar_list(),
                self._tar_list(),
                self._start,
                *self._health,
            ],
        )

    def test_save_parses_all_three_tars_before_publication(self) -> None:
        recorder = ArgvRecorder()
        events: list[str] = []
        recorder.on_call = lambda _number, argv, _state: events.append(
            "list volume archive" if argv == self._tar_list() else "child"
        )
        real_rename = checkpoint_module._rename_directory_no_replace

        def rename(source, destination, *args, **kwargs):
            self.assertEqual(events.count("list volume archive"), 2)
            self.assertIn("validate runner archive", events)
            events.append("publish")
            return real_rename(source, destination, *args, **kwargs)

        with mock.patch.object(
            checkpoint_module,
            "_rename_directory_no_replace",
            side_effect=rename,
        ):
            self._run_save(recorder, events)
        self.assertLess(events.index("validate runner archive"), events.index("publish"))

    def test_save_never_overwrites_final_and_reports_post_publish_restart_failure(self) -> None:
        self.paths.final.mkdir(mode=0o700)
        sentinel = self.paths.final / "sentinel"
        sentinel.write_bytes(b"keep")
        recorder = ArgvRecorder()
        with self.assertRaisesRegex(CheckpointError, "already exists"):
            self._run_save(recorder)
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual(recorder.calls, [])

        shutil.rmtree(self.paths.final)
        recorder = ArgvRecorder()
        recorder.fail_when = lambda _number, argv: argv == self._start
        with self.assertRaisesRegex(CheckpointError, "published.*restart failed"):
            self._run_save(recorder)
        self.assertTrue(self.paths.final.is_dir())
        self.assertEqual(
            set(path.name for path in self.paths.final.iterdir()),
            FINAL_NAMES,
        )

    def test_publication_atomically_refuses_raced_empty_final(self) -> None:
        recorder = ArgvRecorder()
        real_require_absent = checkpoint_module._require_absent_final
        checks = 0
        raced_identity: tuple[int, int] | None = None

        def race_after_absence_check(context: CheckpointContext) -> None:
            nonlocal checks, raced_identity
            real_require_absent(context)
            checks += 1
            if checks == 2:
                context.paths.final.mkdir(mode=0o711)
                context.paths.final.chmod(0o711)
                metadata = context.paths.final.lstat()
                raced_identity = (metadata.st_dev, metadata.st_ino)

        with (
            mock.patch.object(
                checkpoint_module,
                "_require_absent_final",
                side_effect=race_after_absence_check,
            ),
            self.assertRaisesRegex(CheckpointError, "not published.*publication"),
        ):
            self._run_save(recorder)

        self.assertIsNotNone(raced_identity)
        metadata = self.paths.final.lstat()
        self.assertEqual((metadata.st_dev, metadata.st_ino), raced_identity)
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o711)
        self.assertEqual(list(self.paths.final.iterdir()), [])
        self.assertFalse(self.paths.staging.exists())
        self.assertIn(self._start, recorder.calls)

    def test_restore_validates_every_input_before_down_or_delete(self) -> None:
        self._write_bundle()
        recorder = ArgvRecorder()
        validated: list[str] = []
        real_validate_bundle = checkpoint_module.validate_bundle
        real_validate_runner = checkpoint_module.validate_runner_archive

        def validate_bundle_first(*args, **kwargs):
            validated.append("manifest")
            return real_validate_bundle(*args, **kwargs)

        def validate_runner_first(*args, **kwargs):
            validated.append("runner tar")
            return real_validate_runner(*args, **kwargs)

        def on_call(_number, argv, _state):
            if argv == self._tar_list():
                validated.append("volume tar")
            if argv == self.compose + ("down", "--remove-orphans"):
                self.assertIn("manifest", validated)
                self.assertIn("runner tar", validated)
                self.assertEqual(validated.count("volume tar"), 2)

        recorder.on_call = on_call
        with (
            mock.patch.object(
                checkpoint_module,
                "validate_bundle",
                side_effect=validate_bundle_first,
            ),
            mock.patch.object(
                checkpoint_module,
                "validate_runner_archive",
                side_effect=validate_runner_first,
            ),
        ):
            self._run_restore(recorder)

    def test_restore_uses_down_remove_orphans_not_stop_or_volumes(self) -> None:
        self._write_bundle()
        recorder = ArgvRecorder()
        self._run_restore(recorder)
        down_calls = [argv for argv in recorder.calls if "down" in argv or "stop" in argv]
        self.assertEqual(down_calls, [self.compose + ("down", "--remove-orphans")])
        self.assertNotIn("--volumes", down_calls[0])

    def test_restore_recreates_both_fixed_volumes_and_runner_before_extraction(self) -> None:
        self._write_bundle()
        recorder = ArgvRecorder()
        mariadb = f"{self.project}_mariadb-data"
        bugzilla = f"{self.project}_bugzilla-data"
        required = [
            ("docker", "volume", "rm", "--force", mariadb),
            (
                "docker",
                "volume",
                "create",
                "--label",
                f"com.docker.compose.project={self.project}",
                "--label",
                "com.docker.compose.volume=mariadb-data",
                mariadb,
            ),
            ("docker", "volume", "rm", "--force", bugzilla),
            (
                "docker",
                "volume",
                "create",
                "--label",
                f"com.docker.compose.project={self.project}",
                "--label",
                "com.docker.compose.volume=bugzilla-data",
                bugzilla,
            ),
        ]

        def before_extract(_number, argv, _state):
            if argv in (self._extract(mariadb), self._extract(bugzilla)):
                for command in required:
                    self.assertIn(command, recorder.calls)
                    self.assertLess(recorder.calls.index(command), recorder.calls.index(argv))
                self.assertTrue(self.runner.is_dir())
                self.assertEqual(list(self.runner.iterdir()), [])

        recorder.on_call = before_extract
        self._run_restore(recorder)

    def test_each_post_delete_failure_keeps_bundle_and_prints_retry(self) -> None:
        targets = (
            "volume create",
            "mariadb extraction",
            "bugzilla extraction",
            "runner cleanup",
            "runner extraction",
            "startup",
            "health",
        )
        for target in targets:
            with self.subTest(target=target):
                if self.paths.final.exists():
                    shutil.rmtree(self.paths.final)
                if self.runner.exists():
                    shutil.rmtree(self.runner)
                self.runner.mkdir(mode=0o700)
                (self.runner / "state").write_bytes(b"runner state")
                self._write_bundle()
                recorder = ArgvRecorder()
                mariadb = f"{self.project}_mariadb-data"
                bugzilla = f"{self.project}_bugzilla-data"
                matches = {
                    "volume create": lambda argv: argv[:3]
                    == ("docker", "volume", "create"),
                    "mariadb extraction": lambda argv: argv == self._extract(mariadb),
                    "bugzilla extraction": lambda argv: argv == self._extract(bugzilla),
                    "startup": lambda argv: argv == self._start,
                    "health": lambda argv: argv == self.compose + ("ps",),
                }
                recorder.fail_when = lambda _number, argv: (
                    target in matches and matches[target](argv)
                )
                patcher = nullcontext()
                if target == "runner cleanup":
                    real_cleanup = checkpoint_module._replace_runner_target

                    def fail_cleanup(*args, **kwargs):
                        real_cleanup(*args, **kwargs)
                        raise CheckpointError("injected runner cleanup failure")

                    patcher = mock.patch.object(
                        checkpoint_module,
                        "_replace_runner_target",
                        side_effect=fail_cleanup,
                    )
                elif target == "runner extraction":
                    patcher = mock.patch.object(
                        checkpoint_module,
                        "extract_runner_archive",
                        side_effect=CheckpointError("injected runner extraction failure"),
                    )
                stderr = io.StringIO()
                with (
                    patcher,
                    redirect_stderr(stderr),
                    self.assertRaises(CheckpointError) as raised,
                ):
                    self._run_restore(recorder)
                message = str(raised.exception)
                retry = render_retry_command(self.name, self.store, self.runner)
                self.assertIn(str(self.paths.final), message)
                self.assertIn(retry, message)
                self.assertIn(retry, stderr.getvalue())
                self.assertTrue(self.paths.final.is_dir())

    def test_repeat_restore_recreates_targets_instead_of_accumulating_output(self) -> None:
        self._write_bundle()
        recorder = ArgvRecorder()
        self._run_restore(recorder)
        extra = self.runner / "must disappear"
        extra.write_bytes(b"partial output")
        self.signal_state = SignalState()
        self._run_restore(recorder)
        self.assertFalse(extra.exists())
        for volume in (
            f"{self.project}_mariadb-data",
            f"{self.project}_bugzilla-data",
        ):
            self.assertEqual(
                recorder.calls.count(("docker", "volume", "rm", "--force", volume)),
                2,
            )

    def test_start_never_builds_or_pulls(self) -> None:
        recorder = ArgvRecorder()
        self._run_save(recorder)
        starts = [argv for argv in recorder.calls if "up" in argv]
        self.assertEqual(starts, [self._start])
        self.assertIn("--no-build", starts[0])
        self.assertNotIn("--build", starts[0])
        self.assertEqual(starts[0][starts[0].index("--pull") + 1], "never")
        self.assertNotIn(("docker", "pull"), [argv[:2] for argv in recorder.calls])
        self.assertNotIn(("docker", "build"), [argv[:2] for argv in recorder.calls])

    def test_helper_has_no_network_socket_or_extra_mount(self) -> None:
        recorder = ArgvRecorder()
        self._run_save(recorder)
        helpers = [argv for argv in recorder.calls if argv[:2] == ("docker", "run")]
        self.assertTrue(helpers)
        for argv in helpers:
            self.assertIn(("--network", "none"), list(zip(argv, argv[1:])))
            self.assertNotIn("docker.sock", " ".join(argv))
            self.assertLessEqual(argv.count("--mount"), 1)
            if argv == self._tar_list():
                self.assertEqual(argv.count("--mount"), 0)

    def test_health_uses_exact_config_ps_port_curl_and_bounded_logs_argv(self) -> None:
        recorder = ArgvRecorder()
        with mock.patch.object(checkpoint_module, "run_child", side_effect=recorder):
            health_check(self.context, self.signal_state)
        self.assertEqual(recorder.calls, self._health)

        failing = ArgvRecorder()
        failing.fail_when = lambda _number, argv: argv == self.compose + ("ps",)
        with (
            mock.patch.object(checkpoint_module, "run_child", side_effect=failing),
            self.assertRaisesRegex(CheckpointError, "health"),
        ):
            health_check(self.context, SignalState())
        self.assertEqual(
            failing.calls[-1],
            self.compose + ("logs", "--tail", "100", "db", "bugzilla"),
        )

    def test_context_fields_requiring_fixture_access_are_built_only_under_lock(self) -> None:
        self.assertEqual(
            tuple(PreLockContext.__dataclass_fields__),
            (
                "operation",
                "root",
                "project",
                "lock_dir",
                "name",
                "store_arg",
                "runner_arg",
                "wait_timeout",
                "compose",
            ),
        )
        prelock = PreLockContext(
            operation="save",
            root=self.root,
            project=self.project,
            lock_dir=self.base / "lifecycle.lock",
            name=self.name,
            store_arg=str(self.store),
            runner_arg=str(self.runner),
            wait_timeout=37,
            compose=self.compose,
        )

        def require_lock(value):
            self.assertTrue(prelock.lock_dir.is_dir())
            return value

        with (
            mock.patch.object(
                checkpoint_module,
                "validate_paths",
                side_effect=lambda *_: require_lock(self.paths),
            ),
            mock.patch.object(
                checkpoint_module,
                "checkout_revision",
                side_effect=lambda *_: require_lock(REVISION),
            ),
            mock.patch.object(
                checkpoint_module,
                "stack_fingerprint",
                side_effect=lambda *_: require_lock("b" * 64),
            ),
            checkpoint_module.lifecycle_lock(prelock.lock_dir, "save"),
        ):
            locked = build_checkpoint_context(prelock)
        self.assertEqual(locked.paths, self.paths)
        self.assertEqual(locked.revision, REVISION)
        self.assertEqual(locked.fingerprint, "b" * 64)


class _FakeProcess:
    def __init__(self, events: list[str]) -> None:
        self.pid = 4242
        self.returncode = 0
        self.events = events

    def wait(self):
        self.events.append("reap")
        return self.returncode


class SignalTests(unittest.TestCase):
    setUp = OrchestrationTests.setUp
    _restore_context = OrchestrationTests._restore_context
    _write_bundle = OrchestrationTests._write_bundle
    _save_patches = OrchestrationTests._save_patches
    _run_save = OrchestrationTests._run_save
    _run_restore = OrchestrationTests._run_restore
    _start = OrchestrationTests._start
    _health = OrchestrationTests._health
    _archive = OrchestrationTests._archive
    _tar_list = OrchestrationTests._tar_list
    _extract = OrchestrationTests._extract

    def _inject_signal(self, state: SignalState, events: list[str]) -> None:
        process = _FakeProcess(events)
        state.active_process = process  # type: ignore[assignment]

        def killpg(pid: int, requested_signal: int) -> None:
            self.assertEqual(pid, process.pid)
            self.assertEqual(requested_signal, signal.SIGTERM)
            events.append("forward")

        with (
            mock.patch.object(checkpoint_module.os, "killpg", side_effect=killpg),
            mock.patch.object(checkpoint_module.signal, "signal"),
        ):
            checkpoint_module._handle_signal(signal.SIGTERM, None, state)
        state.active_process = None
        raise checkpoint_module._HandledSignal(signal.SIGTERM)

    def test_subprocess_signals_reap_before_save_recovery(self) -> None:
        recorder = ArgvRecorder()
        events: list[str] = []
        down = self.compose + ("down", "--remove-orphans")

        def on_call(_number, argv, state):
            events.append("recovery" if state.requested else "normal")
            if argv == down and state.requested is None:
                self._inject_signal(state, events)

        recorder.on_call = on_call
        with self.assertRaises(checkpoint_module._HandledSignal):
            self._run_save(recorder, events)
        self.assertLess(events.index("forward"), events.index("reap"))
        self.assertLess(events.index("reap"), events.index("recovery"))
        self.assertFalse(self.paths.staging.exists())
        self.assertIn(self._start, recorder.calls)
        for command in self._health:
            self.assertIn(command, recorder.calls)

    def test_pre_delete_restore_signal_restarts_unchanged_fixture(self) -> None:
        self._write_bundle()
        recorder = ArgvRecorder()
        events: list[str] = []
        down = self.compose + ("down", "--remove-orphans")

        def on_call(_number, argv, state):
            if argv == down and state.requested is None:
                self._inject_signal(state, events)

        recorder.on_call = on_call
        with self.assertRaises(checkpoint_module._HandledSignal):
            self._run_restore(recorder)
        self.assertEqual(events[:2], ["forward", "reap"])
        self.assertIn(self._start, recorder.calls)
        self.assertTrue((self.runner / "state").exists())
        self.assertFalse(any(argv[:3] == ("docker", "volume", "rm") for argv in recorder.calls))

    def test_signal_after_down_before_delete_spawn_recovers_unchanged_fixture(self) -> None:
        self._write_bundle()
        recorder = ArgvRecorder()
        real_remove = checkpoint_module._remove_and_create_volumes

        def interrupt_before_spawn(*args, **kwargs):
            with mock.patch.object(checkpoint_module.signal, "signal"):
                checkpoint_module._handle_signal(
                    signal.SIGTERM,
                    None,
                    self.signal_state,
                )
            return real_remove(*args, **kwargs)

        stderr = io.StringIO()
        with (
            mock.patch.object(
                checkpoint_module,
                "_remove_and_create_volumes",
                side_effect=interrupt_before_spawn,
            ),
            redirect_stderr(stderr),
            self.assertRaises(checkpoint_module._HandledSignal),
        ):
            self._run_restore(recorder)
        self.assertFalse(any(argv[:3] == ("docker", "volume", "rm") for argv in recorder.calls))
        self.assertTrue((self.runner / "state").exists())
        self.assertIn(self._start, recorder.calls)
        self.assertNotIn("Keep runners stopped and retry:", stderr.getvalue())

    def test_signal_after_first_delete_spawn_reports_retry_without_restart(self) -> None:
        self._write_bundle()
        recorder = ArgvRecorder()
        events: list[str] = []

        def interrupt_delete(_number, argv, state):
            if argv[:3] == ("docker", "volume", "rm") and state.requested is None:
                self._inject_signal(state, events)

        recorder.on_call = interrupt_delete
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(checkpoint_module._HandledSignal):
            self._run_restore(recorder)
        self.assertEqual(events, ["forward", "reap"])
        self.assertNotIn(self._start, recorder.calls)
        self.assertIn(
            render_retry_command(self.name, self.store, self.runner),
            stderr.getvalue(),
        )

    def test_each_post_delete_subprocess_signal_leaves_down_and_reports_retry(self) -> None:
        phases = ("mariadb extraction", "bugzilla extraction", "startup", "health")
        for phase in phases:
            with self.subTest(phase=phase):
                self.signal_state = SignalState()
                if self.paths.final.exists():
                    shutil.rmtree(self.paths.final)
                if self.runner.exists():
                    shutil.rmtree(self.runner)
                self.runner.mkdir(mode=0o700)
                (self.runner / "state").write_bytes(b"runner state")
                self._write_bundle()
                recorder = ArgvRecorder()
                events: list[str] = []
                mariadb = f"{self.project}_mariadb-data"
                bugzilla = f"{self.project}_bugzilla-data"
                targets = {
                    "mariadb extraction": self._extract(mariadb),
                    "bugzilla extraction": self._extract(bugzilla),
                    "startup": self._start,
                    "health": self.compose + ("ps",),
                }

                def on_call(_number, argv, state):
                    if argv == targets[phase] and state.requested is None:
                        self._inject_signal(state, events)
                    if state.requested is not None:
                        events.append("cleanup")

                recorder.on_call = on_call
                stderr = io.StringIO()
                with (
                    redirect_stderr(stderr),
                    self.assertRaises(checkpoint_module._HandledSignal),
                ):
                    self._run_restore(recorder)
                self.assertEqual(events[:2], ["forward", "reap"])
                retry = render_retry_command(self.name, self.store, self.runner)
                self.assertIn(retry, stderr.getvalue())
                self.assertTrue(self.paths.final.is_dir())
                if phase in ("startup", "health"):
                    self.assertGreater(
                        recorder.calls.count(self.compose + ("down", "--remove-orphans")),
                        1,
                    )

    def test_runner_extraction_signal_has_no_child_and_launches_no_later_phase(self) -> None:
        self._write_bundle()
        recorder = ArgvRecorder()
        events: list[str] = []
        real_extract = checkpoint_module.extract_runner_archive

        def interrupted_extract(source: Path, destination: Path) -> None:
            real_extract(source, destination)
            events.append("runner extraction completed")
            with mock.patch.object(checkpoint_module.signal, "signal"):
                checkpoint_module._handle_signal(
                    signal.SIGTERM,
                    None,
                    self.signal_state,
                )

        stderr = io.StringIO()
        with (
            mock.patch.object(
                checkpoint_module,
                "extract_runner_archive",
                side_effect=interrupted_extract,
            ),
            redirect_stderr(stderr),
            self.assertRaises(checkpoint_module._HandledSignal),
        ):
            self._run_restore(recorder)
        self.assertEqual(events, ["runner extraction completed"])
        self.assertNotIn(self._start, recorder.calls)
        self.assertNotIn("forward", events)
        self.assertNotIn("reap", events)
        self.assertIn(
            render_retry_command(self.name, self.store, self.runner),
            stderr.getvalue(),
        )

    def test_first_signal_installs_default_second_signal_behavior(self) -> None:
        state = SignalState()
        installed: list[tuple[int, object]] = []
        with mock.patch.object(
            checkpoint_module.signal,
            "signal",
            side_effect=lambda sig, handler: installed.append((sig, handler)),
        ):
            checkpoint_module._handle_signal(signal.SIGINT, None, state)
        self.assertEqual(state.requested, signal.SIGINT)
        self.assertIn((signal.SIGINT, signal.SIG_DFL), installed)
        self.assertIn((signal.SIGTERM, signal.SIG_DFL), installed)

    def test_run_child_starts_new_session_forwards_and_reaps(self) -> None:
        events: list[str] = []

        class Popen:
            def __init__(self, argv, **kwargs):
                self.pid = 4242
                self.returncode = 0
                self.argv = tuple(argv)
                self.kwargs = kwargs

            def communicate(self):
                checkpoint_module._handle_signal(
                    signal.SIGTERM,
                    None,
                    state,
                )
                return b"", b""

            def wait(self):
                events.append("reap")
                return self.returncode

        state = SignalState()

        def killpg(pid: int, requested_signal: int) -> None:
            self.assertEqual((pid, requested_signal), (4242, signal.SIGTERM))
            events.append("forward")

        with (
            mock.patch.object(checkpoint_module.subprocess, "Popen", Popen),
            mock.patch.object(checkpoint_module.os, "killpg", side_effect=killpg),
            mock.patch.object(checkpoint_module.signal, "signal"),
            self.assertRaises(checkpoint_module._HandledSignal),
        ):
            run_child(("fixed", "argv"), signal_state=state)
        self.assertEqual(events, ["forward", "reap"])
        self.assertIsNone(state.active_process)

    def test_spawn_registration_blocks_signal_window(self) -> None:
        events: list[str] = []
        state = SignalState()

        class Popen:
            def __init__(self, argv, **kwargs):
                self.pid = 4242
                self.returncode = 0
                events.append("spawn")

            def communicate(self):
                return b"", b""

            def wait(self):
                events.append("reap")
                return self.returncode

        def pthread_sigmask(how, mask):
            if how == signal.SIG_BLOCK:
                events.append("block")
                return set()
            self.assertEqual(how, signal.SIG_SETMASK)
            self.assertIsNotNone(state.active_process)
            events.append("unblock")
            checkpoint_module._handle_signal(signal.SIGTERM, None, state)
            return set()

        def killpg(pid: int, requested_signal: int) -> None:
            self.assertEqual((pid, requested_signal), (4242, signal.SIGTERM))
            events.append("forward")

        with (
            mock.patch.object(checkpoint_module.subprocess, "Popen", Popen),
            mock.patch.object(
                checkpoint_module.signal,
                "pthread_sigmask",
                side_effect=pthread_sigmask,
            ),
            mock.patch.object(checkpoint_module.signal, "signal"),
            mock.patch.object(checkpoint_module.os, "killpg", side_effect=killpg),
            self.assertRaises(checkpoint_module._HandledSignal),
        ):
            run_child(("fixed", "argv"), signal_state=state)
        self.assertEqual(events, ["block", "spawn", "unblock", "forward", "reap"])
if __name__ == "__main__":
    unittest.main()
