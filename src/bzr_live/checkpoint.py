from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Mapping


NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
ARTIFACT_NAMES = (
    "bugzilla-volume.tar",
    "mariadb-volume.tar",
    "runner-state.tar",
)
FINAL_NAMES = frozenset(("manifest.json", *ARTIFACT_NAMES))
CHECKPOINT_FORMAT = 1

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
_STAGING_FORMAT = 1
_ASCII_CONTROLS_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class ValidatedPaths:
    store: Path
    runner_state: Path
    final: Path
    staging: Path


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
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
            or opened.st_uid != expected.st_uid
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


def validate_bundle(
    final: Path,
    *,
    name: str,
    revision: str,
    fingerprint: str,
) -> dict[str, object]:
    validated_name = validate_name(name)
    try:
        final_metadata = final.lstat()
    except OSError as error:
        raise CheckpointError(f"bundle: cannot inspect {final}: {error}") from error
    if not stat.S_ISDIR(final_metadata.st_mode):
        raise CheckpointError(f"bundle: expected a directory at {final}")
    try:
        entries = {entry.name: entry for entry in final.iterdir()}
    except OSError as error:
        raise CheckpointError(f"bundle: cannot list {final}: {error}") from error
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
    if manifest_name != validated_name or manifest_name != final.name:
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
