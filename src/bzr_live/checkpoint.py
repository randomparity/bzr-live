from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Mapping


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


def _fingerprint_fields(root: Path, runner_state: Path) -> list[tuple[bytes, bytes]]:
    fields = [
        (b"format", _FINGERPRINT_FORMAT),
        (b"runner-state-path", os.fsencode(runner_state.resolve(strict=False))),
    ]
    for relative in _REQUIRED_STACK_FILES:
        path = root / relative
        _regular_file(path, owner_only=relative == ".env")
        fields.append((f"file:{relative}".encode(), path.read_bytes()))

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
        fields.append((f"file:{relative}".encode(), path.read_bytes()))
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
