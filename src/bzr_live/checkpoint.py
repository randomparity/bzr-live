from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Literal, Mapping


NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
ARTIFACT_NAMES = (
    "bugzilla-volume.tar",
    "mariadb-volume.tar",
    "runner-state.tar",
)
FINAL_NAMES = frozenset(("manifest.json", *ARTIFACT_NAMES))
CHECKPOINT_FORMAT = 1
HELPER_IMAGE = (
    "mariadb:10.6@sha256:"
    "92e50059ea0a5965a33ef751970eab37d421b91ebbd01ac909039cffe159e574"
)

_MANIFEST_NAMES = frozenset(
    (
        "artifacts",
        "checkpoint_format",
        "checkpoint_name",
        "checkout_revision",
        "created_at",
        "stack_fingerprint",
    )
)
_ARTIFACT_FIELDS = frozenset(("sha256", "size"))
_REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_UTC_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z"
)
_FINGERPRINT_FORMAT = b"checkpoint-stack-fingerprint-v1"
_REQUIRED_STACK_FILES = (
    ".env",
    "compose.yaml",
    "scripts/lifecycle",
    "scripts/checkpoint",
    "src/bzr_live/checkpoint.py",
)
_STAGING_MARKER = ".checkpoint-staging.json"
_DARWIN_RENAME_EXCL = 0x00000004
_LINUX_AT_FDCWD = -100
_LINUX_RENAME_NOREPLACE = 1
_STAGING_FORMAT = 1
_ASCII_CONTROLS_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class ValidatedPaths:
    store: Path
    runner_state: Path
    final: Path
    staging: Path


@dataclass(frozen=True)
class PreLockContext:
    operation: Literal["save", "restore"]
    root: Path
    project: str
    lock_dir: Path
    name: str
    store_arg: str
    runner_arg: str
    wait_timeout: int
    compose: tuple[str, ...]


@dataclass(frozen=True)
class CheckpointContext:
    operation: Literal["save", "restore"]
    root: Path
    project: str
    name: str
    wait_timeout: int
    compose: tuple[str, ...]
    paths: ValidatedPaths
    revision: str
    fingerprint: str


@dataclass
class SignalState:
    requested: int | None = None
    active_process: subprocess.Popen[bytes] | None = None


@dataclass
class _ArchiveNode:
    kind: Literal["directory", "file"] | None = None
    children: dict[str, _ArchiveNode] | None = None

    def descendants(self) -> dict[str, _ArchiveNode]:
        if self.children is None:
            self.children = {}
        return self.children


class CheckpointError(Exception):
    """Actionable operator error with a stable checkpoint phase."""


def validate_name(value: str) -> str:
    if not isinstance(value, str) or NAME_RE.fullmatch(value) is None:
        raise CheckpointError(
            "name: expected 1-64 lowercase letters, digits, underscores, or hyphens"
        )
    return value


def canonical_json(value: Mapping[str, object]) -> bytes:
    rendered = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return rendered.encode("utf-8") + b"\n"


def _canonical_directory_argument(label: str, argument: str) -> Path:
    if not isinstance(argument, str) or _ASCII_CONTROLS_RE.search(argument):
        raise CheckpointError(f"paths: {label} must not contain ASCII control characters")
    raw = Path(argument)
    if not raw.is_absolute():
        raise CheckpointError(f"paths: {label} must be an absolute canonical path")
    try:
        canonical = raw.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise CheckpointError(f"paths: cannot canonicalize {label} {raw}: {error}") from error
    if raw != canonical:
        raise CheckpointError(f"paths: {label} must be canonical: expected {canonical}")
    return canonical


def _inspect_path(path: Path, phase: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as error:
        raise CheckpointError(f"{phase}: cannot inspect {path}: {error}") from error


def _is_mount(path: Path, phase: str) -> bool:
    try:
        return path.is_mount()
    except OSError as error:
        raise CheckpointError(f"{phase}: cannot inspect mount boundary {path}: {error}") from error


def _directory_children(path: Path, phase: str) -> tuple[Path, ...]:
    try:
        return tuple(sorted(path.iterdir(), key=lambda child: child.name))
    except OSError as error:
        raise CheckpointError(f"{phase}: cannot list {path}: {error}") from error


def _validate_runner_tree(source: Path) -> tuple[tuple[Path, os.stat_result], ...]:
    root_metadata = _inspect_path(source, "runner")
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise CheckpointError(f"runner: expected a directory at {source}")

    invoking_uid = os.getuid()
    root_device = root_metadata.st_dev
    pending = [(source, root_metadata)]
    entries: list[tuple[Path, os.stat_result]] = []
    while pending:
        path, metadata = pending.pop()
        if metadata.st_uid != invoking_uid:
            raise CheckpointError(f"runner: {path} is not owned by the invoking user")
        if metadata.st_dev != root_device:
            raise CheckpointError(f"runner: {path} crosses a filesystem boundary")
        if _is_mount(path, "runner"):
            raise CheckpointError(f"runner: mount points are not supported at {path}")
        if stat.S_ISDIR(metadata.st_mode):
            if stat.S_IMODE(metadata.st_mode) & 0o700 != 0o700:
                raise CheckpointError(f"runner: directory {path} must grant owner rwx")
            children = _directory_children(path, "runner")
            pending.extend(
                (child, _inspect_path(child, "runner")) for child in reversed(children)
            )
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise CheckpointError(
                    f"runner: hard links are not supported at {path}"
                )
        else:
            raise CheckpointError(f"runner: expected a regular file or directory at {path}")
        entries.append((path, metadata))
    return tuple(entries)


def _path_contains(parent: Path, child: Path) -> bool:
    return parent == child or parent in child.parents


def validate_paths(
    operation: Literal["save", "restore"],
    root: Path,
    name: str,
    store_arg: str,
    runner_arg: str,
) -> ValidatedPaths:
    if operation not in ("save", "restore"):
        raise CheckpointError("paths: operation must be save or restore")
    validated_name = validate_name(name)
    store = _canonical_directory_argument("store", store_arg)
    runner_state = _canonical_directory_argument("runner state", runner_arg)
    try:
        canonical_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CheckpointError(f"paths: cannot canonicalize checkout root {root}: {error}") from error

    store_metadata = _inspect_path(store, "paths")
    if not stat.S_ISDIR(store_metadata.st_mode):
        raise CheckpointError(f"paths: expected store directory at {store}")
    if store_metadata.st_uid != os.getuid():
        raise CheckpointError(f"paths: store {store} is not owned by the invoking user")
    if stat.S_IMODE(store_metadata.st_mode) & 0o700 != 0o700:
        raise CheckpointError(f"paths: store directory {store} must grant owner rwx")

    if _path_contains(store, runner_state) or _path_contains(runner_state, store):
        raise CheckpointError("paths: store and runner state must not overlap")
    for dangerous in (Path("/"), Path.home().resolve(), canonical_root, store):
        if _path_contains(runner_state, dangerous):
            raise CheckpointError(
                f"paths: runner state {runner_state} must not be {dangerous} or its ancestor"
            )

    try:
        os.lstat(runner_state)
    except FileNotFoundError:
        if operation == "save":
            raise CheckpointError(f"paths: save runner state must exist at {runner_state}") from None
        parent_metadata = _inspect_path(runner_state.parent, "runner")
        if not stat.S_ISDIR(parent_metadata.st_mode):
            raise CheckpointError(
                f"runner: absent restore target parent is not a directory: {runner_state.parent}"
            )
        if parent_metadata.st_uid != os.getuid():
            raise CheckpointError(
                f"runner: absent restore target parent is not owned by the invoking user: "
                f"{runner_state.parent}"
            )
        if stat.S_IMODE(parent_metadata.st_mode) != 0o700:
            raise CheckpointError(
                f"runner: absent restore target parent must have mode 0700: {runner_state.parent}"
            )
        if _is_mount(runner_state.parent, "runner"):
            raise CheckpointError(
                f"runner: absent restore target parent must not be a mount point: "
                f"{runner_state.parent}"
            )
    except OSError as error:
        raise CheckpointError(f"runner: cannot inspect {runner_state}: {error}") from error
    else:
        _validate_runner_tree(runner_state)

    return ValidatedPaths(
        store=store,
        runner_state=runner_state,
        final=store / validated_name,
        staging=store / f".{validated_name}.staging",
    )


def _require_derived_paths(paths: ValidatedPaths, name: str) -> None:
    validated_name = validate_name(name)
    expected_final = paths.store / validated_name
    expected_staging = paths.store / f".{validated_name}.staging"
    if paths.final != expected_final or paths.staging != expected_staging:
        raise CheckpointError("staging: validated paths do not match the checkpoint name")


def _write_all(file_descriptor: int, content: bytes, phase: str) -> None:
    offset = 0
    try:
        while offset < len(content):
            written = os.write(file_descriptor, content[offset:])
            if written == 0:
                raise OSError("write returned zero bytes")
            offset += written
    except OSError as error:
        raise CheckpointError(f"{phase}: cannot write file: {error}") from error


def _staging_marker(paths: ValidatedPaths, name: str) -> bytes:
    return canonical_json(
        {
            "checkpoint_name": name,
            "final_path": str(paths.final),
            "staging_format": _STAGING_FORMAT,
        }
    )


def create_staging(paths: ValidatedPaths, name: str) -> Path:
    _require_derived_paths(paths, name)
    try:
        paths.staging.mkdir(mode=0o700)
        paths.staging.chmod(0o700)
    except OSError as error:
        raise CheckpointError(f"staging: cannot create {paths.staging}: {error}") from error

    marker = paths.staging / _STAGING_MARKER
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(marker, flags, 0o600)
    except OSError as error:
        try:
            paths.staging.rmdir()
        except OSError:
            pass
        raise CheckpointError(f"staging: cannot create marker {marker}: {error}") from error
    try:
        try:
            os.fchmod(file_descriptor, 0o600)
            _write_all(file_descriptor, _staging_marker(paths, name), "staging")
        finally:
            os.close(file_descriptor)
    except (OSError, CheckpointError) as error:
        try:
            marker.unlink()
            paths.staging.rmdir()
        except OSError as cleanup_error:
            raise CheckpointError(
                f"staging: marker creation failed and cannot remove partial {paths.staging}: "
                f"{cleanup_error}"
            ) from error
        if isinstance(error, CheckpointError):
            raise
        raise CheckpointError(f"staging: cannot write marker {marker}: {error}") from error
    return paths.staging


def _manual_staging_error(path: Path, reason: str) -> CheckpointError:
    return CheckpointError(f"staging: inspect {path} and remove it manually: {reason}")


def _read_validated_marker(
    marker: Path,
    metadata: os.stat_result,
    expected: bytes,
) -> bytes:
    if metadata.st_size != len(expected):
        raise _manual_staging_error(marker, "marker does not match this invocation")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(marker, flags)
    except OSError as error:
        raise _manual_staging_error(marker, f"cannot open marker: {error}") from error
    try:
        opened = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_uid != metadata.st_uid
            or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(metadata.st_mode)
            or opened.st_size != metadata.st_size
        ):
            raise _manual_staging_error(marker, "marker changed during validation")
        content = os.read(file_descriptor, len(expected) + 1)
    except OSError as error:
        raise _manual_staging_error(marker, f"cannot read marker: {error}") from error
    finally:
        os.close(file_descriptor)
    return content


def cleanup_staging(paths: ValidatedPaths, name: str) -> None:
    _require_derived_paths(paths, name)
    try:
        staging_metadata = os.lstat(paths.staging)
    except FileNotFoundError:
        return
    except OSError as error:
        raise _manual_staging_error(
            paths.staging, f"cannot inspect staging root: {error}"
        ) from error

    store_metadata = _inspect_path(paths.store, "staging")
    if not stat.S_ISDIR(staging_metadata.st_mode):
        raise _manual_staging_error(paths.staging, "staging root is not a directory")
    if staging_metadata.st_uid != os.getuid():
        raise _manual_staging_error(paths.staging, "staging root has a foreign owner")
    if stat.S_IMODE(staging_metadata.st_mode) != 0o700:
        raise _manual_staging_error(paths.staging, "staging root must have mode 0700")
    if staging_metadata.st_dev != store_metadata.st_dev:
        raise _manual_staging_error(
            paths.staging, "staging root crosses a filesystem boundary"
        )
    if _is_mount(paths.staging, "staging"):
        raise _manual_staging_error(paths.staging, "staging root is a mount point")

    children = _directory_children(paths.staging, "staging")
    allowed = FINAL_NAMES | {_STAGING_MARKER}
    metadata_by_path: dict[Path, os.stat_result] = {}
    for child in children:
        if child.name not in allowed:
            raise _manual_staging_error(child, "unknown staging child")
        metadata = _inspect_path(child, "staging")
        if not stat.S_ISREG(metadata.st_mode):
            raise _manual_staging_error(child, "staging child is not a regular file")
        if metadata.st_uid != os.getuid():
            raise _manual_staging_error(child, "staging child has a foreign owner")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise _manual_staging_error(child, "staging child must have mode 0600")
        if metadata.st_dev != staging_metadata.st_dev:
            raise _manual_staging_error(child, "staging child crosses a filesystem boundary")
        if _is_mount(child, "staging"):
            raise _manual_staging_error(child, "staging child is a mount point")
        metadata_by_path[child] = metadata

    marker = paths.staging / _STAGING_MARKER
    marker_metadata = metadata_by_path.get(marker)
    if marker_metadata is None:
        raise _manual_staging_error(paths.staging, "matching staging marker is missing")
    expected_marker = _staging_marker(paths, name)
    if _read_validated_marker(marker, marker_metadata, expected_marker) != expected_marker:
        raise _manual_staging_error(marker, "marker does not match this invocation")

    try:
        for child in children:
            if child != marker:
                child.unlink()
        marker.unlink()
        paths.staging.rmdir()
    except OSError as error:
        raise _manual_staging_error(
            paths.staging, f"cannot remove validated staging: {error}"
        ) from error


def _archive_info(
    name: str,
    metadata: os.stat_result,
    member_type: bytes,
    mode: int,
) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.type = member_type
    member.mode = mode
    member.uid = metadata.st_uid
    member.gid = metadata.st_gid
    member.uname = ""
    member.gname = ""
    member.mtime = 0
    return member


def _open_verified_regular(path: Path, expected: os.stat_result):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(path, flags)
    except OSError as error:
        raise CheckpointError(f"runner archive: cannot open {path}: {error}") from error
    try:
        opened = os.fstat(file_descriptor)
        if opened.st_nlink != 1:
            raise CheckpointError(f"runner archive: hard links are not supported at {path}")
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
            or opened.st_uid != expected.st_uid
            or opened.st_nlink != expected.st_nlink
            or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(expected.st_mode)
            or opened.st_size != expected.st_size
        ):
            raise CheckpointError(f"runner archive: {path} changed during validation")
        return os.fdopen(file_descriptor, "rb")
    except (OSError, CheckpointError):
        os.close(file_descriptor)
        raise


def create_runner_archive(source: Path, destination: Path) -> None:
    entries = _validate_runner_tree(source)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(destination, flags, 0o600)
    except OSError as error:
        raise CheckpointError(f"runner archive: cannot create {destination}: {error}") from error
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "wb") as output:
            file_descriptor = -1
            with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path, metadata in entries[1:]:
                    member_name = path.relative_to(source).as_posix()
                    if stat.S_ISDIR(metadata.st_mode):
                        member = _archive_info(
                            member_name,
                            metadata,
                            tarfile.DIRTYPE,
                            stat.S_IMODE(metadata.st_mode),
                        )
                        archive.addfile(member)
                    else:
                        member = _archive_info(
                            member_name,
                            metadata,
                            tarfile.REGTYPE,
                            stat.S_IMODE(metadata.st_mode) & 0o700,
                        )
                        member.size = metadata.st_size
                        with _open_verified_regular(path, metadata) as content:
                            archive.addfile(member, content)
    except (OSError, tarfile.TarError, CheckpointError) as error:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            raise CheckpointError(
                f"runner archive: creation failed and cannot remove partial {destination}: "
                f"{cleanup_error}"
            ) from error
        if isinstance(error, CheckpointError):
            raise
        raise CheckpointError(f"runner archive: cannot create {destination}: {error}") from error


def _validated_member_parts(member: tarfile.TarInfo) -> tuple[str, ...]:
    name = member.name
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(component in ("", ".", "..") for component in name.split("/"))
    ):
        raise CheckpointError(f"runner archive: unsafe member path {name!r}")
    return tuple(name.split("/"))


def _member_is_sparse(member: tarfile.TarInfo) -> bool:
    if member.sparse is not None or member.type == tarfile.GNUTYPE_SPARSE:
        return True
    for key, value in member.pax_headers.items():
        if key.lower().startswith("gnu.sparse"):
            return True
        if key == "SCHILY.filetype" and value == "sparse":
            return True
    return False


def _validate_archive_topology(members: tuple[tarfile.TarInfo, ...]) -> None:
    root = _ArchiveNode()
    seen: set[tuple[str, ...]] = set()
    for member in members:
        parts = _validated_member_parts(member)
        if parts in seen:
            raise CheckpointError(f"runner archive: duplicate member {member.name!r}")
        seen.add(parts)
        if member.size < 0:
            raise CheckpointError(
                f"runner archive: member has a negative size at {member.name!r}"
            )
        if _member_is_sparse(member):
            raise CheckpointError(
                f"runner archive: sparse member is not supported: {member.name}"
            )
        if member.isdir():
            kind: Literal["directory", "file"] = "directory"
            if member.mode & 0o700 != 0o700:
                raise CheckpointError(
                    f"runner archive: directory {member.name!r} must grant owner rwx"
                )
        elif member.isreg() and member.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
            kind = "file"
        else:
            raise CheckpointError(
                f"runner archive: unsupported member type at {member.name!r}"
            )

        node = root
        for component in parts[:-1]:
            child = node.descendants().setdefault(component, _ArchiveNode())
            if child.kind == "file":
                raise CheckpointError(
                    f"runner archive: regular file has descendants at {member.name!r}"
                )
            node = child
        leaf = node.descendants().setdefault(parts[-1], _ArchiveNode())
        if kind == "file" and leaf.children:
            raise CheckpointError(
                f"runner archive: regular file has descendants at {member.name!r}"
            )
        if leaf.kind is not None and leaf.kind != kind:
            raise CheckpointError(f"runner archive: member changes type at {member.name!r}")
        leaf.kind = kind


def validate_runner_archive(source: Path) -> tuple[tarfile.TarInfo, ...]:
    try:
        with tarfile.open(source, mode="r:") as archive:
            members = tuple(archive.getmembers())
            _validate_archive_topology(members)
            for member in members:
                if not member.isreg():
                    continue
                content = archive.extractfile(member)
                if content is None:
                    raise CheckpointError(
                        f"runner archive: cannot read regular member {member.name!r}"
                    )
                remaining = member.size
                while remaining:
                    chunk = content.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise CheckpointError(
                            f"runner archive: truncated member {member.name!r}"
                        )
                    remaining -= len(chunk)
            return members
    except CheckpointError:
        raise
    except (OSError, EOFError, tarfile.TarError) as error:
        raise CheckpointError(f"runner archive: cannot validate {source}: {error}") from error


def _ensure_extraction_directory(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for component in parts:
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
                current.chmod(0o700)
            except OSError as error:
                raise CheckpointError(
                    f"runner archive: cannot create directory {current}: {error}"
                ) from error
        except OSError as error:
            raise CheckpointError(f"runner archive: cannot inspect {current}: {error}") from error
        else:
            if not stat.S_ISDIR(metadata.st_mode):
                raise CheckpointError(
                    f"runner archive: extraction path is not a directory: {current}"
                )
            try:
                current.chmod(0o700)
            except OSError as error:
                raise CheckpointError(
                    f"runner archive: cannot set directory mode on {current}: {error}"
                ) from error
    return current


def extract_runner_archive(source: Path, destination: Path) -> None:
    members = validate_runner_archive(source)
    try:
        destination.mkdir(mode=0o700)
        destination.chmod(0o700)
    except FileExistsError:
        metadata = _inspect_path(destination, "runner archive")
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or _is_mount(destination, "runner archive")
            or _directory_children(destination, "runner archive")
        ):
            raise CheckpointError(
                f"runner archive: existing destination must be an empty owner-owned "
                f"mode-0700 directory: {destination}"
            ) from None
    except OSError as error:
        raise CheckpointError(
            f"runner archive: cannot create destination {destination}: {error}"
        ) from error
    try:
        canonical_destination = destination.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CheckpointError(
            f"runner archive: cannot canonicalize destination {destination}: {error}"
        ) from error

    try:
        with tarfile.open(source, mode="r:") as archive:
            for member in members:
                parts = _validated_member_parts(member)
                parent = _ensure_extraction_directory(canonical_destination, parts[:-1])
                target = parent / parts[-1]
                if not target.is_relative_to(canonical_destination):
                    raise CheckpointError(
                        f"runner archive: extraction escapes destination at {member.name!r}"
                    )
                if member.isdir():
                    _ensure_extraction_directory(canonical_destination, parts)
                    continue

                content = archive.extractfile(member)
                if content is None:
                    raise CheckpointError(
                        f"runner archive: cannot read regular member {member.name!r}"
                    )
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                try:
                    file_descriptor = os.open(target, flags, 0o600)
                except OSError as error:
                    raise CheckpointError(
                        f"runner archive: cannot create regular file {target}: {error}"
                    ) from error
                try:
                    remaining = member.size
                    while remaining:
                        chunk = content.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise CheckpointError(
                                f"runner archive: truncated member {member.name!r}"
                            )
                        _write_all(file_descriptor, chunk, "runner archive")
                        remaining -= len(chunk)
                    os.fchmod(file_descriptor, member.mode & 0o700)
                finally:
                    os.close(file_descriptor)
    except CheckpointError:
        raise
    except (OSError, EOFError, tarfile.TarError) as error:
        raise CheckpointError(
            f"runner archive: cannot extract {source} to {destination}: {error}"
        ) from error


def checkout_revision(root: Path) -> str:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), "rev-parse", "HEAD"),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise CheckpointError(f"revision: cannot run git: {error}") from error
    revision = completed.stdout.strip()
    if completed.returncode != 0 or _REVISION_RE.fullmatch(revision) is None:
        raise CheckpointError("revision: checkout HEAD is not a 40-character lowercase revision")
    return revision


def _regular_file(path: Path, *, owner_only: bool = False) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CheckpointError(f"fingerprint: cannot inspect {path}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise CheckpointError(f"fingerprint: expected a regular file at {path}")
    if owner_only:
        if metadata.st_uid != os.getuid():
            raise CheckpointError(f"fingerprint: {path} is not owned by the invoking user")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise CheckpointError(f"fingerprint: {path} must have mode 0600")
    return metadata


def _read_fingerprint_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise CheckpointError(f"fingerprint: cannot read {path}: {error}") from error


def _fingerprint_fields(root: Path, runner_state: Path) -> list[tuple[bytes, bytes]]:
    fields = [
        (b"format", _FINGERPRINT_FORMAT),
        (b"runner-state-path", os.fsencode(runner_state.resolve(strict=False))),
    ]
    for relative in _REQUIRED_STACK_FILES:
        path = root / relative
        _regular_file(path, owner_only=relative == ".env")
        fields.append((f"file:{relative}".encode(), _read_fingerprint_bytes(path)))

    containers = root / "containers"
    try:
        containers_metadata = containers.lstat()
    except OSError as error:
        raise CheckpointError(f"fingerprint: cannot inspect {containers}: {error}") from error
    if not stat.S_ISDIR(containers_metadata.st_mode):
        raise CheckpointError(f"fingerprint: expected a directory at {containers}")

    paths = sorted(containers.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for path in paths:
        relative = path.relative_to(root).as_posix()
        try:
            metadata = path.lstat()
        except OSError as error:
            raise CheckpointError(f"fingerprint: cannot inspect {path}: {error}") from error
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise CheckpointError(f"fingerprint: expected a regular file at {path}")
        fields.append((b"file:" + os.fsencode(relative), _read_fingerprint_bytes(path)))
    return fields


def stack_fingerprint(root: Path, runner_state: Path) -> str:
    digest = hashlib.sha256()
    for tag, value in sorted(_fingerprint_fields(root, runner_state)):
        digest.update(len(tag).to_bytes(8, "big"))
        digest.update(tag)
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointError(f"manifest: duplicate key {key!r}")
        result[key] = value
    return result


def _require_exact_keys(value: Mapping[str, object], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise CheckpointError(f"manifest: {field} has {'; '.join(details)}")


def _validate_timestamp(value: object) -> None:
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise CheckpointError("manifest: created_at must be an RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise CheckpointError("manifest: created_at must be an RFC 3339 UTC timestamp") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise CheckpointError("manifest: created_at must be an RFC 3339 UTC timestamp")


def _validate_manifest(manifest: dict[str, object]) -> None:
    _require_exact_keys(manifest, _MANIFEST_NAMES, "root")
    if type(manifest["checkpoint_format"]) is not int:
        raise CheckpointError("manifest: checkpoint_format must be integer 1")
    if manifest["checkpoint_format"] != CHECKPOINT_FORMAT:
        raise CheckpointError(f"manifest: unsupported checkpoint_format {manifest['checkpoint_format']}")

    name = manifest["checkpoint_name"]
    if not isinstance(name, str):
        raise CheckpointError("manifest: checkpoint_name must be a string")
    validate_name(name)

    revision = manifest["checkout_revision"]
    if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
        raise CheckpointError("manifest: checkout_revision must be 40 lowercase hexadecimal characters")
    fingerprint = manifest["stack_fingerprint"]
    if not isinstance(fingerprint, str) or _SHA256_RE.fullmatch(fingerprint) is None:
        raise CheckpointError("manifest: stack_fingerprint must be 64 lowercase hexadecimal characters")
    _validate_timestamp(manifest["created_at"])

    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, dict):
        raise CheckpointError("manifest: artifacts must be an object")
    _require_exact_keys(artifacts, frozenset(ARTIFACT_NAMES), "artifacts")
    for name in ARTIFACT_NAMES:
        record = artifacts[name]
        if not isinstance(record, dict):
            raise CheckpointError(f"manifest: artifacts.{name} must be an object")
        _require_exact_keys(record, _ARTIFACT_FIELDS, f"artifacts.{name}")
        size = record["size"]
        if type(size) is not int or size < 0:
            raise CheckpointError(f"manifest: artifacts.{name}.size must be a non-negative integer")
        checksum = record["sha256"]
        if not isinstance(checksum, str) or _SHA256_RE.fullmatch(checksum) is None:
            raise CheckpointError(
                f"manifest: artifacts.{name}.sha256 must be 64 lowercase hexadecimal characters"
            )


def read_manifest(path: Path) -> dict[str, object]:
    try:
        encoded = path.read_bytes()
        decoded = encoded.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_object_without_duplicates)
    except CheckpointError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckpointError(f"manifest: cannot read valid UTF-8 JSON from {path}: {error}") from error
    if not isinstance(value, dict):
        raise CheckpointError("manifest: root must be an object")
    _validate_manifest(value)
    if encoded != canonical_json(value):
        raise CheckpointError("manifest: JSON must use canonical encoding")
    return value


def _final_regular_file(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CheckpointError(f"bundle: cannot inspect {path}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise CheckpointError(f"bundle: expected a regular file at {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise CheckpointError(f"bundle: {path} must have mode 0600")
    return metadata


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise CheckpointError(f"bundle: cannot read {path}: {error}") from error
    return digest.hexdigest()


def _validate_bundle_at(
    bundle: Path,
    *,
    name: str,
    directory_name: str,
    revision: str,
    fingerprint: str,
) -> dict[str, object]:
    validated_name = validate_name(name)
    try:
        bundle_metadata = bundle.lstat()
    except OSError as error:
        raise CheckpointError(f"bundle: cannot inspect {bundle}: {error}") from error
    if not stat.S_ISDIR(bundle_metadata.st_mode):
        raise CheckpointError(f"bundle: expected a directory at {bundle}")
    try:
        entries = {entry.name: entry for entry in bundle.iterdir()}
    except OSError as error:
        raise CheckpointError(f"bundle: cannot list {bundle}: {error}") from error
    if frozenset(entries) != FINAL_NAMES:
        missing = sorted(FINAL_NAMES - entries.keys())
        unknown = sorted(entries.keys() - FINAL_NAMES)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise CheckpointError(f"bundle: final file set has {'; '.join(details)}")

    metadata = {entry_name: _final_regular_file(path) for entry_name, path in entries.items()}
    manifest = read_manifest(entries["manifest.json"])
    manifest_name = manifest["checkpoint_name"]
    if manifest_name != validated_name or manifest_name != directory_name:
        raise CheckpointError("bundle: checkpoint name does not match CLI name and final directory")
    if manifest["checkout_revision"] != revision:
        raise CheckpointError("bundle: checkout revision is incompatible")
    if manifest["stack_fingerprint"] != fingerprint:
        raise CheckpointError("bundle: stack fingerprint is incompatible")

    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, dict)
    for artifact_name in ARTIFACT_NAMES:
        record = artifacts[artifact_name]
        assert isinstance(record, dict)
        if metadata[artifact_name].st_size != record["size"]:
            raise CheckpointError(f"bundle: {artifact_name} size does not match manifest")
        if _sha256_file(entries[artifact_name]) != record["sha256"]:
            raise CheckpointError(f"bundle: {artifact_name} checksum does not match manifest")
    return manifest


def validate_bundle(
    final: Path,
    *,
    name: str,
    revision: str,
    fingerprint: str,
) -> dict[str, object]:
    return _validate_bundle_at(
        final,
        name=name,
        directory_name=final.name,
        revision=revision,
        fingerprint=fingerprint,
    )


def render_retry_command(name: str, store: Path, runner_state: Path) -> str:
    return shlex.join(
        [
            "scripts/checkpoint",
            "restore",
            name,
            "--store",
            str(store),
            "--runner-state",
            str(runner_state),
        ]
    )


_VOLUME_ARTIFACTS = (
    ("mariadb-data", "mariadb-volume.tar"),
    ("bugzilla-data", "bugzilla-volume.tar"),
)
_STDERR_LIMIT = 4096


class _HandledSignal(Exception):
    def __init__(self, requested_signal: int):
        super().__init__(requested_signal)
        self.requested_signal = requested_signal


class _ReportedCheckpointError(CheckpointError):
    pass


def _forward_and_reap(
    process: subprocess.Popen[bytes],
    requested_signal: int,
    signal_state: SignalState,
) -> None:
    try:
        os.killpg(process.pid, requested_signal)
    except OSError:
        pass
    finally:
        process.wait()
        if signal_state.active_process is process:
            signal_state.active_process = None


def _handle_signal(
    requested_signal: int,
    _frame: object,
    signal_state: SignalState,
) -> None:
    if signal_state.requested is not None:
        return
    signal_state.requested = requested_signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    process = signal_state.active_process
    if process is None:
        return
    _forward_and_reap(process, requested_signal, signal_state)


def _check_signal(signal_state: SignalState) -> None:
    if signal_state.requested is not None:
        raise _HandledSignal(signal_state.requested)


def _bounded_text(content: bytes) -> str:
    bounded = content[:_STDERR_LIMIT]
    rendered = bounded.decode("utf-8", errors="replace").strip()
    if len(content) > _STDERR_LIMIT:
        rendered += "\n[diagnostics truncated]"
    return rendered


def run_child(
    argv: Sequence[str],
    *,
    signal_state: SignalState,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | int | None = None,
    capture_stderr: bool = True,
    on_started: Callable[[], None] | None = None,
    allow_requested: bool = False,
) -> bytes:
    if isinstance(argv, (str, bytes)) or not argv or any(not isinstance(arg, str) for arg in argv):
        raise CheckpointError("command: argv must be a nonempty fixed sequence of strings")
    fixed_argv = tuple(argv)
    requested_before = signal_state.requested
    if requested_before is not None and not allow_requested:
        raise _HandledSignal(requested_before)
    try:
        process = subprocess.Popen(
            fixed_argv,
            stdin=stdin,
            stdout=subprocess.PIPE if stdout is None else stdout,
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        if signal_state.requested is not None and requested_before is None:
            raise _HandledSignal(signal_state.requested) from error
        raise CheckpointError(f"command: cannot start {fixed_argv[0]}: {error}") from error
    if on_started is not None:
        on_started()
    signal_state.active_process = process
    if (
        requested_before is None
        and signal_state.requested is not None
        and signal_state.active_process is process
    ):
        _forward_and_reap(process, signal_state.requested, signal_state)

    try:
        captured_stdout, captured_stderr = process.communicate()
    except OSError as error:
        if signal_state.requested is not None and requested_before is None:
            raise _HandledSignal(signal_state.requested) from error
        raise CheckpointError(f"command: failed while running {fixed_argv[0]}: {error}") from error
    finally:
        if signal_state.active_process is process:
            signal_state.active_process = None

    if signal_state.requested is not None and requested_before is None:
        raise _HandledSignal(signal_state.requested)
    if process.returncode != 0:
        diagnostics = _bounded_text(captured_stderr or b"")
        detail = f": {diagnostics}" if diagnostics else ""
        raise CheckpointError(
            f"command: {shlex.join(fixed_argv)} exited {process.returncode}{detail}"
        )
    return captured_stdout or b""


def _run_normal(
    argv: Sequence[str],
    *,
    signal_state: SignalState,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | int | None = None,
    on_started: Callable[[], None] | None = None,
) -> bytes:
    _check_signal(signal_state)
    result = run_child(
        argv,
        signal_state=signal_state,
        stdin=stdin,
        stdout=stdout,
        on_started=on_started,
    )
    _check_signal(signal_state)
    return result


def _lock_owner_state(lock_dir: Path) -> str:
    owner_path = lock_dir / "owner"
    try:
        with owner_path.open("r", encoding="ascii") as owner_file:
            owner = owner_file.read(64).strip()
    except (OSError, UnicodeError):
        return "unknown"
    if re.fullmatch(r"[0-9]+", owner) is None:
        return "unknown"
    owner_pid = int(owner)
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        return f"stale PID {owner_pid}"
    except PermissionError:
        return f"live PID {owner_pid}"
    except OSError:
        return "unknown"
    return f"live PID {owner_pid}"


@contextmanager
def lifecycle_lock(lock_dir: Path, operation: str) -> Iterator[None]:
    held = False
    try:
        try:
            lock_dir.mkdir(mode=0o700)
        except FileExistsError:
            owner_state = _lock_owner_state(lock_dir)
            raise CheckpointError(
                f"{operation}: lifecycle lock is held ({owner_state}) at {lock_dir}. "
                f"Next action: verify no lifecycle process is running, then remove "
                f"{lock_dir} manually."
            ) from None
        except OSError as error:
            raise CheckpointError(
                f"{operation}: cannot create lifecycle lock {lock_dir}: {error}"
            ) from error
        held = True
        try:
            owner = lock_dir / "owner"
            owner.write_text(f"{os.getpid()}\n", encoding="ascii")
            owner.chmod(0o600)
        except OSError:
            pass
        yield
    finally:
        if held:
            try:
                (lock_dir / "owner").unlink()
            except OSError:
                pass
            try:
                lock_dir.rmdir()
            except OSError:
                pass


def build_checkpoint_context(prelock: PreLockContext) -> CheckpointContext:
    paths = validate_paths(
        prelock.operation,
        prelock.root,
        prelock.name,
        prelock.store_arg,
        prelock.runner_arg,
    )
    revision = checkout_revision(prelock.root)
    fingerprint = stack_fingerprint(prelock.root, paths.runner_state)
    return CheckpointContext(
        operation=prelock.operation,
        root=prelock.root,
        project=prelock.project,
        name=prelock.name,
        wait_timeout=prelock.wait_timeout,
        compose=prelock.compose,
        paths=paths,
        revision=revision,
        fingerprint=fingerprint,
    )


def _loopback_endpoint(raw: bytes) -> str:
    try:
        endpoint = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise CheckpointError("health: Compose port was not ASCII") from error
    match = re.fullmatch(r"(127\.0\.0\.1|\[::1\]):([0-9]{1,5})", endpoint)
    if match is None:
        raise CheckpointError(
            f"health: Compose port must be a loopback host and decimal port, got {endpoint!r}"
        )
    port = int(match.group(2))
    if port < 1 or port > 65535:
        raise CheckpointError(f"health: Compose port is outside 1..65535: {port}")
    return endpoint


def _health_check(
    context: CheckpointContext,
    signal_state: SignalState,
    *,
    recovery: bool,
) -> None:
    def invoke(
        argv: Sequence[str],
        *,
        stdout: BinaryIO | int | None = None,
    ) -> bytes:
        if recovery:
            return run_child(
                argv,
                signal_state=signal_state,
                stdout=stdout,
                allow_requested=True,
            )
        return _run_normal(
            argv,
            signal_state=signal_state,
            stdout=stdout,
        )

    try:
        invoke((*context.compose, "config", "--quiet"))
        invoke((*context.compose, "ps"))
        endpoint = _loopback_endpoint(
            invoke((*context.compose, "port", "bugzilla", "80"))
        )
        invoke(
            (
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                str(context.wait_timeout),
                f"http://{endpoint}/",
            ),
            stdout=subprocess.DEVNULL,
        )
    except _HandledSignal:
        raise
    except CheckpointError as error:
        try:
            diagnostics = invoke(
                (*context.compose, "logs", "--tail", "100", "db", "bugzilla")
            )
        except _HandledSignal:
            raise
        except CheckpointError:
            diagnostics = b""
        rendered = _bounded_text(diagnostics)
        if rendered:
            print(rendered, file=sys.stderr)
        message = str(error)
        if message.startswith("health:"):
            raise CheckpointError(message) from error
        raise CheckpointError(f"health: {message}") from error


def health_check(context: CheckpointContext, signal_state: SignalState) -> None:
    _health_check(context, signal_state, recovery=False)


def _start_argv(context: CheckpointContext) -> tuple[str, ...]:
    return (
        *context.compose,
        "up",
        "--detach",
        "--no-build",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        str(context.wait_timeout),
    )


def _restart_fixture(context: CheckpointContext, signal_state: SignalState) -> None:
    run_child(
        _start_argv(context),
        signal_state=signal_state,
        allow_requested=True,
    )
    _health_check(context, signal_state, recovery=True)


def _volume_name(context: CheckpointContext, compose_name: str) -> str:
    return f"{context.project}_{compose_name}"


def _archive_argv(volume: str) -> tuple[str, ...]:
    return (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--mount",
        f"type=volume,src={volume},dst=/volume,readonly",
        HELPER_IMAGE,
        "tar",
        "-C",
        "/volume",
        "-cf",
        "-",
        ".",
    )


def _tar_list_argv() -> tuple[str, ...]:
    return (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--interactive",
        HELPER_IMAGE,
        "tar",
        "-tf",
        "-",
    )


def _extract_argv(volume: str) -> tuple[str, ...]:
    return (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--interactive",
        "--mount",
        f"type=volume,src={volume},dst=/volume",
        HELPER_IMAGE,
        "tar",
        "-C",
        "/volume",
        "-xf",
        "-",
    )


def _open_private_output(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        output = os.fdopen(descriptor, "wb")
        descriptor = None
        return output
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise CheckpointError(f"checkpoint: cannot create {path}: {error}") from error


def _archive_volume(
    context: CheckpointContext,
    signal_state: SignalState,
    compose_name: str,
    destination: Path,
) -> None:
    try:
        with _open_private_output(destination) as output:
            _run_normal(
                _archive_argv(_volume_name(context, compose_name)),
                signal_state=signal_state,
                stdout=output,
            )
    except OSError as error:
        raise CheckpointError(f"checkpoint: cannot finish {destination}: {error}") from error


def _validate_volume_archive(path: Path, signal_state: SignalState) -> None:
    try:
        with path.open("rb") as source:
            _run_normal(
                _tar_list_argv(),
                signal_state=signal_state,
                stdin=source,
                stdout=subprocess.DEVNULL,
            )
    except OSError as error:
        raise CheckpointError(f"volume archive: cannot read {path}: {error}") from error


def _write_private_bytes(path: Path, content: bytes) -> None:
    try:
        with _open_private_output(path) as output:
            output.write(content)
            output.flush()
    except OSError as error:
        raise CheckpointError(f"checkpoint: cannot write {path}: {error}") from error


def _manifest_for(context: CheckpointContext) -> dict[str, object]:
    artifacts = {}
    for artifact_name in ARTIFACT_NAMES:
        artifact = context.paths.staging / artifact_name
        try:
            size = artifact.stat().st_size
        except OSError as error:
            raise CheckpointError(f"manifest: cannot inspect {artifact}: {error}") from error
        artifacts[artifact_name] = {
            "sha256": _sha256_file(artifact),
            "size": size,
        }
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    return {
        "artifacts": artifacts,
        "checkpoint_format": CHECKPOINT_FORMAT,
        "checkpoint_name": context.name,
        "checkout_revision": context.revision,
        "created_at": created_at.replace("+00:00", "Z"),
        "stack_fingerprint": context.fingerprint,
    }


def _require_absent_final(context: CheckpointContext) -> None:
    try:
        context.paths.final.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise CheckpointError(
            f"save: cannot inspect final checkpoint {context.paths.final}: {error}"
        ) from error
    raise CheckpointError(f"save: final checkpoint already exists at {context.paths.final}")


def _restore_marker_for_cleanup(context: CheckpointContext) -> None:
    marker = context.paths.staging / _STAGING_MARKER
    if not context.paths.staging.exists() or marker.exists():
        return
    _write_private_bytes(marker, _staging_marker(context.paths, context.name))


def _cleanup_save_staging(context: CheckpointContext) -> None:
    _restore_marker_for_cleanup(context)
    cleanup_staging(context.paths, context.name)


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    encoded_source = os.fsencode(source)
    encoded_destination = os.fsencode(destination)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            rename = libc.renamex_np
        except AttributeError as error:
            raise CheckpointError(
                "save: native exclusive rename is unavailable on Darwin"
            ) from error
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(
            encoded_source,
            encoded_destination,
            _DARWIN_RENAME_EXCL,
        )
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as error:
            raise CheckpointError(
                "save: native no-replace rename is unavailable on Linux"
            ) from error
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        result = rename(
            _LINUX_AT_FDCWD,
            encoded_source,
            _LINUX_AT_FDCWD,
            encoded_destination,
            _LINUX_RENAME_NOREPLACE,
        )
    else:
        raise CheckpointError(
            f"save: native no-replace publication is unsupported on {sys.platform}"
        )
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(
            error_number,
            os.strerror(error_number),
            str(destination),
        )


def _publish_staging(
    context: CheckpointContext,
    signal_state: SignalState,
) -> None:
    _require_absent_final(context)
    _check_signal(signal_state)
    try:
        _rename_directory_no_replace(context.paths.staging, context.paths.final)
    except OSError as error:
        raise CheckpointError(
            f"save: cannot publish {context.paths.final} without overwrite: {error}"
        ) from error


def _save_failure(
    context: CheckpointContext,
    signal_state: SignalState,
    error: Exception,
    *,
    phase: str,
    published: bool,
) -> CheckpointError:
    cleanup_error: CheckpointError | None = None
    if not published:
        try:
            _cleanup_save_staging(context)
        except CheckpointError as failure:
            cleanup_error = failure
    restart_error: CheckpointError | None = None
    try:
        _restart_fixture(context, signal_state)
    except CheckpointError as failure:
        restart_error = failure

    state = (
        f"checkpoint published at {context.paths.final} but fixture restart failed"
        if published
        else "checkpoint not published"
    )
    details = [f"{state} during {phase}: {error}"]
    if cleanup_error is not None:
        details.append(f"staging cleanup failed: {cleanup_error}")
    if restart_error is not None:
        details.append(
            "fixture restart failed; keep runners stopped, restore expected local images "
            "if needed, then run scripts/lifecycle up: "
            f"{restart_error}"
        )
    else:
        details.append("fixture restart and health check succeeded")
    return CheckpointError("; ".join(details))


def save_checkpoint(context: CheckpointContext, signal_state: SignalState) -> None:
    if context.operation != "save":
        raise CheckpointError("save: checkpoint context operation is not save")
    _require_absent_final(context)
    _check_signal(signal_state)
    cleanup_staging(context.paths, context.name)
    _check_signal(signal_state)
    health_check(context, signal_state)
    print("Keep runners stopped until checkpoint save and fixture health complete.")

    shutdown_started = False
    published = False
    phase = "stack shutdown"
    try:
        shutdown_started = True
        _run_normal(
            (*context.compose, "down", "--remove-orphans"),
            signal_state=signal_state,
        )
        phase = "staging creation"
        create_staging(context.paths, context.name)
        for compose_name, artifact_name in _VOLUME_ARTIFACTS:
            phase = f"{compose_name} archive"
            _archive_volume(
                context,
                signal_state,
                compose_name,
                context.paths.staging / artifact_name,
            )
        phase = "runner archive"
        runner_archive = context.paths.staging / "runner-state.tar"
        create_runner_archive(context.paths.runner_state, runner_archive)
        _check_signal(signal_state)
        for _compose_name, artifact_name in _VOLUME_ARTIFACTS:
            phase = f"{artifact_name} validation"
            _validate_volume_archive(context.paths.staging / artifact_name, signal_state)
        phase = "runner archive validation"
        validate_runner_archive(runner_archive)
        _check_signal(signal_state)
        phase = "manifest creation"
        manifest = canonical_json(_manifest_for(context))
        _check_signal(signal_state)
        _write_private_bytes(
            context.paths.staging / "manifest.json",
            manifest,
        )
        _check_signal(signal_state)
        try:
            (context.paths.staging / _STAGING_MARKER).unlink()
        except OSError as error:
            raise CheckpointError(f"save: cannot remove staging marker: {error}") from error
        _check_signal(signal_state)
        phase = "staged bundle validation"
        _validate_bundle_at(
            context.paths.staging,
            name=context.name,
            directory_name=context.name,
            revision=context.revision,
            fingerprint=context.fingerprint,
        )
        _check_signal(signal_state)
        phase = "checkpoint publication"
        _publish_staging(context, signal_state)
        published = True
        phase = "fixture startup"
        _run_normal(_start_argv(context), signal_state=signal_state)
        phase = "fixture health"
        health_check(context, signal_state)
    except _HandledSignal as error:
        if shutdown_started:
            failure = _save_failure(
                context,
                signal_state,
                error,
                published=published,
                phase=phase,
            )
            print(failure, file=sys.stderr)
        raise
    except CheckpointError as error:
        if not shutdown_started:
            raise
        raise _save_failure(
            context,
            signal_state,
            error,
            published=published,
            phase=phase,
        ) from error
    print(f"Saved checkpoint {context.name} at {context.paths.final}.")


def _create_volume_argv(
    context: CheckpointContext,
    compose_name: str,
) -> tuple[str, ...]:
    volume = _volume_name(context, compose_name)
    return (
        "docker",
        "volume",
        "create",
        "--label",
        f"com.docker.compose.project={context.project}",
        "--label",
        f"com.docker.compose.volume={compose_name}",
        volume,
    )


def _validated_runner_entries(path: Path) -> tuple[tuple[Path, os.stat_result], ...]:
    try:
        path.lstat()
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise CheckpointError(f"runner: cannot inspect {path}: {error}") from error
    return _validate_runner_tree(path)


def _replace_runner_target(
    context: CheckpointContext,
    signal_state: SignalState,
    entries: tuple[tuple[Path, os.stat_result], ...],
) -> None:
    for path, metadata in reversed(entries):
        try:
            if stat.S_ISDIR(metadata.st_mode):
                path.rmdir()
            else:
                path.unlink()
        except OSError as error:
            raise CheckpointError(f"runner cleanup: cannot remove {path}: {error}") from error
        _check_signal(signal_state)
    try:
        context.paths.runner_state.mkdir(mode=0o700)
        context.paths.runner_state.chmod(0o700)
    except OSError as error:
        raise CheckpointError(
            f"runner cleanup: cannot create {context.paths.runner_state}: {error}"
        ) from error
    _check_signal(signal_state)


def _remove_and_create_volumes(
    context: CheckpointContext,
    signal_state: SignalState,
    *,
    on_first_delete_started: Callable[[], None],
) -> None:
    for index, (compose_name, _artifact_name) in enumerate(_VOLUME_ARTIFACTS):
        volume = _volume_name(context, compose_name)
        _run_normal(
            ("docker", "volume", "rm", "--force", volume),
            signal_state=signal_state,
            on_started=on_first_delete_started if index == 0 else None,
        )
        _run_normal(
            _create_volume_argv(context, compose_name),
            signal_state=signal_state,
        )


def _extract_volume_archive(
    context: CheckpointContext,
    signal_state: SignalState,
    compose_name: str,
    source: Path,
) -> None:
    try:
        with source.open("rb") as archive:
            _run_normal(
                _extract_argv(_volume_name(context, compose_name)),
                signal_state=signal_state,
                stdin=archive,
            )
    except OSError as error:
        raise CheckpointError(f"volume extraction: cannot read {source}: {error}") from error


def _restore_retry_message(
    context: CheckpointContext,
    phase: str,
    error: Exception,
) -> str:
    retry = render_retry_command(
        context.name,
        context.paths.store,
        context.paths.runner_state,
    )
    return (
        f"restore {phase} failed after destructive replacement: {error}. "
        f"Checkpoint remains unchanged at {context.paths.final}. "
        f"Keep runners stopped and retry: {retry}"
    )


def _recover_unchanged_fixture(
    context: CheckpointContext,
    signal_state: SignalState,
    phase: str,
    error: Exception,
) -> CheckpointError:
    try:
        _restart_fixture(context, signal_state)
    except CheckpointError as restart_error:
        return CheckpointError(
            f"restore {phase} stopped before deletion: {error}; fixture restart failed; "
            "keep runners stopped and run scripts/lifecycle up after restoring expected "
            f"local images if needed: {restart_error}"
        )
    return CheckpointError(
        f"restore {phase} stopped before deletion: {error}; unchanged fixture restart "
        "and health check succeeded"
    )


def _best_effort_signal_shutdown(
    context: CheckpointContext,
    signal_state: SignalState,
) -> None:
    try:
        run_child(
            (*context.compose, "down", "--remove-orphans"),
            signal_state=signal_state,
            allow_requested=True,
        )
    except (CheckpointError, _HandledSignal):
        pass


def restore_checkpoint(context: CheckpointContext, signal_state: SignalState) -> None:
    if context.operation != "restore":
        raise CheckpointError("restore: checkpoint context operation is not restore")
    validate_bundle(
        context.paths.final,
        name=context.name,
        revision=context.revision,
        fingerprint=context.fingerprint,
    )
    for _compose_name, artifact_name in _VOLUME_ARTIFACTS:
        _validate_volume_archive(context.paths.final / artifact_name, signal_state)
    validate_runner_archive(context.paths.final / "runner-state.tar")
    _check_signal(signal_state)
    runner_entries = _validated_runner_entries(context.paths.runner_state)
    _run_normal(
        (*context.compose, "config", "--quiet"),
        signal_state=signal_state,
    )
    print("Keep runners stopped until checkpoint restore and fixture health complete.")

    shutdown_started = False
    destructive = False
    startup_attempted = False
    phase = "stack shutdown"
    try:
        shutdown_started = True
        _run_normal(
            (*context.compose, "down", "--remove-orphans"),
            signal_state=signal_state,
        )
        _check_signal(signal_state)
        phase = "target deletion and recreation"

        def mark_destructive() -> None:
            nonlocal destructive
            destructive = True

        _remove_and_create_volumes(
            context,
            signal_state,
            on_first_delete_started=mark_destructive,
        )
        _replace_runner_target(context, signal_state, runner_entries)
        for compose_name, artifact_name in _VOLUME_ARTIFACTS:
            phase = f"{compose_name} volume extraction"
            _extract_volume_archive(
                context,
                signal_state,
                compose_name,
                context.paths.final / artifact_name,
            )
        phase = "runner extraction"
        extract_runner_archive(
            context.paths.final / "runner-state.tar",
            context.paths.runner_state,
        )
        _check_signal(signal_state)
        phase = "fixture startup"
        startup_attempted = True
        _run_normal(_start_argv(context), signal_state=signal_state)
        phase = "fixture health"
        health_check(context, signal_state)
    except _HandledSignal as error:
        if destructive:
            if startup_attempted:
                _best_effort_signal_shutdown(context, signal_state)
            print(_restore_retry_message(context, phase, error), file=sys.stderr)
        elif shutdown_started:
            print(
                _recover_unchanged_fixture(context, signal_state, phase, error),
                file=sys.stderr,
            )
        raise
    except CheckpointError as error:
        if destructive:
            message = _restore_retry_message(context, phase, error)
            print(message, file=sys.stderr)
            raise _ReportedCheckpointError(message) from error
        if shutdown_started:
            raise _recover_unchanged_fixture(
                context,
                signal_state,
                phase,
                error,
            ) from error
        raise
    print(f"Restored checkpoint {context.name} at revision {context.revision}.")


def _validate_prelock_path_text(label: str, value: str) -> str:
    if not value or _ASCII_CONTROLS_RE.search(value):
        raise CheckpointError(f"{label}: directory argument is empty or contains control bytes")
    return value


def _checkout_root() -> Path:
    configured = os.environ.get("BZ_LIVE_ROOT")
    candidate = Path(configured).expanduser() if configured else Path(__file__).parents[2]
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CheckpointError(f"root: cannot resolve checkout root {candidate}: {error}") from error


def _wait_timeout() -> int:
    raw = os.environ.get("BZ_WAIT_TIMEOUT", "300")
    if re.fullmatch(r"[0-9]+", raw) is None or int(raw) < 1:
        raise CheckpointError("timeout: BZ_WAIT_TIMEOUT must be a positive decimal integer")
    return int(raw)


def _parse_prelock_context(argv: Sequence[str] | None) -> PreLockContext:
    parser = argparse.ArgumentParser(
        prog="scripts/checkpoint",
        description=(
            "Save or restore a cold fixture checkpoint. Keep all runners stopped until "
            "the command completes and fixture health passes."
        ),
    )
    parser.add_argument("operation", choices=("save", "restore"))
    parser.add_argument("name")
    parser.add_argument("--store", required=True)
    parser.add_argument("--runner-state", required=True)
    arguments = parser.parse_args(argv)

    operation: Literal["save", "restore"] = arguments.operation
    name = validate_name(arguments.name)
    store_arg = _validate_prelock_path_text("store", arguments.store)
    runner_arg = _validate_prelock_path_text("runner state", arguments.runner_state)
    root = _checkout_root()
    project_hash = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:12]
    project = f"bzr-live-{project_hash}"
    compose = (
        "docker",
        "compose",
        "--project-name",
        project,
        "--project-directory",
        str(root),
        "--file",
        str(root / "compose.yaml"),
    )
    return PreLockContext(
        operation=operation,
        root=root,
        project=project,
        lock_dir=Path("/tmp") / f"{project}.lifecycle.lock",
        name=name,
        store_arg=store_arg,
        runner_arg=runner_arg,
        wait_timeout=_wait_timeout(),
        compose=compose,
    )


def main(argv: Sequence[str] | None = None) -> int:
    signal_state = SignalState()
    previous_handlers: dict[int, object] = {}

    def handle(requested_signal: int, frame: object) -> None:
        _handle_signal(requested_signal, frame, signal_state)

    for handled in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[handled] = signal.signal(handled, handle)
    try:
        try:
            prelock = _parse_prelock_context(argv)
        except SystemExit as error:
            return os.EX_USAGE if error.code == 2 else int(error.code)
        _check_signal(signal_state)
        with lifecycle_lock(prelock.lock_dir, prelock.operation):
            _check_signal(signal_state)
            context = build_checkpoint_context(prelock)
            _check_signal(signal_state)
            if context.operation == "save":
                save_checkpoint(context, signal_state)
            else:
                restore_checkpoint(context, signal_state)
        return 0
    except _HandledSignal as error:
        print(
            f"checkpoint interrupted by signal {error.requested_signal}",
            file=sys.stderr,
        )
        return 128 + error.requested_signal
    except _ReportedCheckpointError:
        return 1
    except CheckpointError as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        for handled, previous in previous_handlers.items():
            signal.signal(handled, previous)
