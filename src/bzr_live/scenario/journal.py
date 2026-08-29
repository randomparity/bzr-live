from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import secrets
import stat
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from .loader import _DECIMAL, _MEDIA_TYPE, _NAME, _SHA256, _decode_json_bytes, _error, _object
from .model import (
    JsonValue,
    PlannedValue,
    RecoveryClass,
    Reference,
    ScenarioValidationError,
    freeze_planned,
    planned_to_json,
)

_ACTION_CLASSES = {
    "bug.create": "unique-create",
    "bug.update": "idempotent-set",
    "bug.comment": "append",
    "bug.attach": "append",
    "bug.worktime": "append",
    "bug.custom-field-set": "idempotent-set",
    "bug.flag": "idempotent-set",
    "attachment.update": "idempotent-set",
}
_RECOVERY_CLASSES = frozenset(_ACTION_CLASSES.values())
_REFERENCE_KINDS = {
    "group", "actor", "product", "component", "version", "milestone",
    "custom-field", "keyword", "flag-type", "asset", "bug", "attachment",
}
_NEXT_ACTIONS = {"advance", "reconcile", "retry", "stop"}
_BOUNDARIES = {"bzr", "bugzilla-rest-custom-field"}
_ATTEMPT_FILE = re.compile(r"([a-z][a-z0-9-]{0,62})\.([0-9]{6})\.json\Z", re.ASCII)
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)
_SENSITIVE_SEGMENTS = {
    "token", "password", "secret", "cookie", "credential", "authorization", "apikey",
}


def _journal_error(field: str, message: str) -> ScenarioValidationError:
    return ScenarioValidationError("journal", field, message)


def _validate_reference(value: object, field: str, *, kind: str | None = None) -> Reference:
    if not isinstance(value, Reference):
        raise _journal_error(field, "must be a typed reference")
    if value.kind not in _REFERENCE_KINDS:
        raise _journal_error(field, "uses an unsupported reference kind")
    if kind is not None and value.kind != kind:
        raise _journal_error(field, f"must reference {kind}")
    if _NAME.fullmatch(value.name) is None:
        raise _journal_error(field, "reference name must be a slug")
    return value
def _utf8_string(value: object, field: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise _journal_error(field, "must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _journal_error(field, "must not contain an unpaired surrogate") from None
    return value


def _validate_utf8_tree(value: object, field: str) -> None:
    if isinstance(value, str):
        _utf8_string(value, field)
    elif isinstance(value, Reference):
        _utf8_string(value.kind, field)
        _utf8_string(value.name, field)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _utf8_string(key, field)
            _validate_utf8_tree(item, f"{field}.{key}")
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _validate_utf8_tree(item, f"{field}[{index}]")


def _validate_planned(value: object, field: str) -> PlannedValue:
    try:
        _validate_utf8_tree(value, field)
        return freeze_planned(value)
    except (TypeError, ValueError):
        raise _journal_error(field, "contains an unsupported value") from None


def _validate_json(value: object, field: str) -> JsonValue:
    if isinstance(value, Reference):
        raise _journal_error(field, "JSON output cannot contain typed references")
    if isinstance(value, str):
        _utf8_string(value, field)
        return value
    if value is None or type(value) in (bool, int):
        return value  # type: ignore[return-value]
    if isinstance(value, (tuple, list)):
        return tuple(_validate_json(item, f"{field}[]") for item in value)
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _journal_error(field, "mapping keys must be strings")
            result[key] = _validate_json(item, f"{field}.{key}")
        return MappingProxyType(result)
    raise _journal_error(field, "contains an unsupported JSON value")

def _keys(
    value: object,
    field: str,
    required: set[str],
    optional: set[str] = frozenset(),
) -> Mapping[str, PlannedValue]:
    if not isinstance(value, Mapping):
        raise _journal_error(field, "must be an object")
    unknown = set(value) - required - optional
    missing = required - set(value)
    if unknown:
        raise _journal_error(f"{field}.{min(unknown)}", "unknown field")
    if missing:
        raise _journal_error(f"{field}.{min(missing)}", "required field is missing")
    return value


def _ref_value(value: object, field: str, kind: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    _validate_reference(value, field, kind=kind)


def _refs_value(value: object, field: str, kind: str) -> None:
    if not isinstance(value, tuple):
        raise _journal_error(field, "must be an immutable reference sequence")
    seen: set[Reference] = set()
    for index, item in enumerate(value):
        ref = _validate_reference(item, f"{field}[{index}]", kind=kind)
        if ref in seen:
            raise _journal_error(f"{field}[{index}]", "duplicate reference")
        seen.add(ref)


def _hours_value(value: object, field: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
        raise _journal_error(field, "must be a canonical decimal string")


def _custom_values(value: object, field: str) -> None:
    if not isinstance(value, tuple):
        raise _journal_error(field, "must be an immutable assignment sequence")
    seen: set[Reference] = set()
    for index, item in enumerate(value):
        item_field = f"{field}[{index}]"
        assignment = _keys(item, item_field, {"field", "value"})
        ref = _validate_reference(assignment["field"], f"{item_field}.field", kind="custom-field")
        if ref in seen:
            raise _journal_error(f"{item_field}.field", "duplicate assignment")
        seen.add(ref)
        assigned = assignment["value"]
        if isinstance(assigned, tuple):
            if any(not isinstance(member, str) for member in assigned):
                raise _journal_error(f"{item_field}.value", "must contain strings")
        elif not isinstance(assigned, str):
            raise _journal_error(f"{item_field}.value", "must be a string or string sequence")


def _validate_update_values(values: Mapping[str, PlannedValue], field: str) -> None:
    allowed = {
        "summary", "status", "resolution", "assignee", "cc", "groups", "depends_on",
        "blocks", "duplicate_of", "version", "milestone", "keywords",
        "estimated_hours", "remaining_hours",
    }
    if not values or set(values) - allowed:
        raise _journal_error(field, "must contain only supported non-empty update fields")
    for key, value in values.items():
        child = f"{field}.{key}"
        if key in {"summary", "status"}:
            _utf8_string(value, child, nonempty=True)
        elif key == "resolution":
            if value is not None:
                _utf8_string(value, child, nonempty=True)
        elif key == "assignee":
            _ref_value(value, child, "actor", nullable=True)
        elif key == "duplicate_of":
            _ref_value(value, child, "bug", nullable=True)
        elif key in {"version", "milestone"}:
            _ref_value(value, child, key, nullable=True)
        elif key in {"estimated_hours", "remaining_hours"}:
            _hours_value(value, child)
        else:
            kind = {
                "cc": "actor", "groups": "group", "depends_on": "bug",
                "blocks": "bug", "keywords": "keyword",
            }[key]
            _refs_value(value, child, kind)


def _validate_postcondition_values(action: str, value: object) -> None:
    field = "$.expected_postcondition.values"
    if action == "bug.create":
        required = {
            "product", "component", "summary", "description", "version", "milestone",
            "assignee", "cc", "groups", "depends_on", "blocks", "duplicate_of",
            "keywords", "estimated_hours", "remaining_hours", "custom_fields",
            "server_alias",
        }
        values = _keys(value, field, required)
        _ref_value(values["product"], f"{field}.product", "product")
        _ref_value(values["component"], f"{field}.component", "component")
        _utf8_string(values["summary"], f"{field}.summary", nonempty=True)
        _utf8_string(values["description"], f"{field}.description")
        for key in ("version", "milestone", "assignee", "duplicate_of"):
            kind = {"assignee": "actor", "duplicate_of": "bug"}.get(key, key)
            _ref_value(values[key], f"{field}.{key}", kind, nullable=True)
        for key, kind in (
            ("cc", "actor"), ("groups", "group"), ("depends_on", "bug"),
            ("blocks", "bug"), ("keywords", "keyword"),
        ):
            _refs_value(values[key], f"{field}.{key}", kind)
        _hours_value(values["estimated_hours"], f"{field}.estimated_hours", nullable=True)
        _hours_value(values["remaining_hours"], f"{field}.remaining_hours", nullable=True)
        _custom_values(values["custom_fields"], f"{field}.custom_fields")
        alias = values["server_alias"]
        if not isinstance(alias, str) or re.fullmatch(r"bzr-live-[0-9a-f]{31}", alias) is None:
            raise _journal_error(f"{field}.server_alias", "must be a deterministic server alias")
    elif action == "bug.update":
        values = _keys(value, field, set(), {
            "summary", "status", "resolution", "assignee", "cc", "groups", "depends_on",
            "blocks", "duplicate_of", "version", "milestone", "keywords",
            "estimated_hours", "remaining_hours",
        })
        _validate_update_values(values, field)
    elif action == "bug.comment":
        values = _keys(value, field, {"body", "private"})
        _utf8_string(values["body"], f"{field}.body", nonempty=True)
        if type(values["private"]) is not bool:
            raise _journal_error(f"{field}.private", "must be a boolean")
    elif action == "bug.attach":
        values = _keys(value, field, {"bug", "asset", "asset_sha256", "description", "content_type", "private"})
        _ref_value(values["bug"], f"{field}.bug", "bug")
        _ref_value(values["asset"], f"{field}.asset", "asset")
        if not isinstance(values["asset_sha256"], str) or _SHA256.fullmatch(values["asset_sha256"]) is None:
            raise _journal_error(f"{field}.asset_sha256", "must be a lowercase SHA-256")
        _utf8_string(values["description"], f"{field}.description")
        media_type = values["content_type"]
        if not isinstance(media_type, str) or _MEDIA_TYPE.fullmatch(media_type) is None:
            raise _journal_error(f"{field}.content_type", "must be a media type")
        if type(values["private"]) is not bool:
            raise _journal_error(f"{field}.private", "must be a boolean")
    elif action == "bug.worktime":
        values = _keys(value, field, {"bug", "hours", "comment"})
        _ref_value(values["bug"], f"{field}.bug", "bug")
        _hours_value(values["hours"], f"{field}.hours")
        _utf8_string(values["comment"], f"{field}.comment", nonempty=True)
    elif action == "bug.custom-field-set":
        values = _keys(value, field, {"bug", "values"})
        _ref_value(values["bug"], f"{field}.bug", "bug")
        _custom_values(values["values"], f"{field}.values")
        if not values["values"]:
            raise _journal_error(f"{field}.values", "must not be empty")
    elif action == "bug.flag":
        values = _keys(value, field, {"bug", "flag_type", "status", "requestee"})
        _ref_value(values["bug"], f"{field}.bug", "bug")
        _ref_value(values["flag_type"], f"{field}.flag_type", "flag-type")
        if not isinstance(values["status"], str) or values["status"] not in {"?", "+", "-", "X"}:
            raise _journal_error(f"{field}.status", "unsupported flag status")
        _ref_value(values["requestee"], f"{field}.requestee", "actor", nullable=True)
        if values["requestee"] is not None and values["status"] != "?":
            raise _journal_error(f"{field}.requestee", "requestee is allowed only for ?")
    else:
        values = _keys(value, field, {"attachment", "obsolete"}, {"description"})
        _ref_value(values["attachment"], f"{field}.attachment", "attachment")
        if type(values["obsolete"]) is not bool:
            raise _journal_error(f"{field}.obsolete", "must be a boolean")
        if "description" in values:
            _utf8_string(values["description"], f"{field}.description")



def _validate_common(record: InFlightRecord | CompletedRecord) -> None:
    if not isinstance(record.scenario_digest, str) or _SHA256.fullmatch(record.scenario_digest) is None:
        raise _journal_error("$.scenario_digest", "must be a lowercase SHA-256")
    if not isinstance(record.event, str) or _NAME.fullmatch(record.event) is None:
        raise _journal_error("$.event", "must be a slug")
    if type(record.attempt) is not int or record.attempt <= 0:
        raise _journal_error("$.attempt", "must be a positive integer")
    _validate_reference(record.actor, "$.actor", kind="actor")
    if record.action_class not in _RECOVERY_CLASSES:
        raise _journal_error("$.action_class", "unsupported recovery class")
    if not isinstance(record.reconciliation_marker, str) or not record.reconciliation_marker:
        raise _journal_error("$.reconciliation_marker", "must be a non-empty string")
    postcondition = record.expected_postcondition
    if not isinstance(postcondition, Mapping) or set(postcondition) != {
        "action", "target", "values", "marker",
    }:
        raise _journal_error("$.expected_postcondition", "must contain the exact recovery fields")
    action = postcondition["action"]
    if not isinstance(action, str) or action not in _ACTION_CLASSES:
        raise _journal_error("$.expected_postcondition.action", "unsupported action")
    if record.action_class != _ACTION_CLASSES[action]:
        raise _journal_error("$.action_class", "does not match the expected action")
    target = _validate_reference(postcondition["target"], "$.expected_postcondition.target")
    expected_target = "attachment" if action in {"bug.attach", "attachment.update"} else "bug"
    if target.kind != expected_target:
        raise _journal_error("$.expected_postcondition.target", f"must reference {expected_target}")
    if not isinstance(postcondition["values"], Mapping):
        raise _journal_error("$.expected_postcondition.values", "must be an object")
    _validate_postcondition_values(action, postcondition["values"])
    if postcondition["marker"] != record.reconciliation_marker:
        raise _journal_error("$.expected_postcondition.marker", "must match reconciliation marker")


@dataclass(frozen=True, slots=True)
class InvocationMetadata:
    mutation_boundary: Literal["bzr", "bugzilla-rest-custom-field"]
    operation: str
    arguments: tuple[str, ...]
    environment_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.mutation_boundary not in _BOUNDARIES:
            raise _journal_error("$.invocation.mutation_boundary", "unsupported mutation boundary")
        if not isinstance(self.operation, str) or not self.operation.strip():
            raise _journal_error("$.invocation.operation", "must be a non-empty string")
        if not isinstance(self.arguments, tuple) or any(not isinstance(item, str) for item in self.arguments):
            raise _journal_error("$.invocation.arguments", "must contain only strings")
        _utf8_string(self.operation, "$.invocation.operation", nonempty=True)
        for index, argument in enumerate(self.arguments):
            _utf8_string(argument, f"$.invocation.arguments[{index}]")
        if not isinstance(self.environment_names, tuple):
            raise _journal_error("$.invocation.environment_names", "must be a tuple")
        seen: set[str] = set()
        for index, name in enumerate(self.environment_names):
            if not isinstance(name, str) or _ENVIRONMENT_NAME.fullmatch(name) is None:
                raise _journal_error(f"$.invocation.environment_names[{index}]", "invalid environment variable name")
            if name in seen:
                raise _journal_error(f"$.invocation.environment_names[{index}]", "duplicate environment variable name")
            _utf8_string(name, f"$.invocation.environment_names[{index}]")
            seen.add(name)


@dataclass(frozen=True, slots=True)
class InFlightRecord:
    scenario_digest: str
    event: str
    attempt: int
    actor: Reference
    action_class: RecoveryClass
    expected_postcondition: Mapping[str, PlannedValue]
    reconciliation_marker: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_postcondition", _validate_planned(self.expected_postcondition, "$.expected_postcondition"))
        _validate_common(self)
        if not isinstance(self.expected_postcondition, Mapping):
            raise _journal_error("$.expected_postcondition", "must be an object")


@dataclass(frozen=True, slots=True)
class CompletedRecord:
    scenario_digest: str
    event: str
    attempt: int
    actor: Reference
    action_class: RecoveryClass
    expected_postcondition: Mapping[str, PlannedValue]
    reconciliation_marker: str
    invocation: InvocationMetadata
    handler_output: JsonValue
    exit_status: int
    resolved_ids: Mapping[str, int]
    next_safe_action: Literal["advance", "reconcile", "retry", "stop"]

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_postcondition", _validate_planned(self.expected_postcondition, "$.expected_postcondition"))
        object.__setattr__(self, "handler_output", _validate_json(self.handler_output, "$.handler_output"))
        _validate_common(self)
        if not isinstance(self.expected_postcondition, Mapping):
            raise _journal_error("$.expected_postcondition", "must be an object")
        if not isinstance(self.invocation, InvocationMetadata):
            raise _journal_error("$.invocation", "must be invocation metadata")
        if type(self.exit_status) is not int:
            raise _journal_error("$.exit_status", "must be an integer")
        if not isinstance(self.resolved_ids, Mapping):
            raise _journal_error("$.resolved_ids", "must be an object")
        resolved: dict[str, int] = {}
        for key, value in self.resolved_ids.items():
            if not isinstance(key, str) or key.count(":") != 1:
                raise _journal_error("$.resolved_ids", "keys must be typed references")
            kind, name = key.split(":", 1)
            if kind not in {"bug", "attachment"} or _NAME.fullmatch(name) is None:
                raise _journal_error(f"$.resolved_ids.{key}", "invalid resolved identity")
            if type(value) is not int or value <= 0:
                raise _journal_error(f"$.resolved_ids.{key}", "must be a positive integer")
            resolved[key] = value
        object.__setattr__(self, "resolved_ids", MappingProxyType(resolved))
        if self.next_safe_action not in _NEXT_ACTIONS:
            raise _journal_error("$.next_safe_action", "unsupported next action")


def _reference_json(reference: Reference) -> dict[str, str]:
    return {"ref": f"{reference.kind}:{reference.name}"}


def _common_json(record: InFlightRecord | CompletedRecord) -> dict[str, object]:
    return {
        "scenario_digest": record.scenario_digest,
        "event": record.event,
        "attempt": record.attempt,
        "actor": _reference_json(record.actor),
        "action_class": record.action_class,
        "expected_postcondition": planned_to_json(record.expected_postcondition),
        "reconciliation_marker": record.reconciliation_marker,
    }


def _normalized_key(key: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", key)
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return value.strip("_").lower()


def _sensitive_key(key: str) -> bool:
    segments = [segment for segment in _normalized_key(key).split("_") if segment]
    if any(segment in _SENSITIVE_SEGMENTS for segment in segments):
        return True
    return any(left == "api" and right == "key" for left, right in zip(segments, segments[1:]))


def _secret_list(known_secrets: Collection[str]) -> tuple[str, ...]:
    return tuple(sorted({secret for secret in known_secrets if secret}, key=lambda item: (-len(item), item)))


def _remove_secrets(value: str, known_secrets: tuple[str, ...]) -> str:
    previous = None
    while previous != value:
        previous = value
        for secret in known_secrets:
            value = value.replace(secret, "")
    return value


def _redact_opaque(value: JsonValue, known_secrets: tuple[str, ...]) -> JsonValue:
    if isinstance(value, str):
        return _remove_secrets(value, known_secrets)
    if isinstance(value, tuple):
        return tuple(_redact_opaque(item, known_secrets) for item in value)
    if isinstance(value, Mapping):
        redacted: dict[str, JsonValue] = {}
        for key, item in value.items():
            redacted_key = _remove_secrets(key, known_secrets)
            if redacted_key in redacted:
                raise _journal_error("$.handler_output", "redaction creates a duplicate mapping key")
            redacted[redacted_key] = (
                None
                if _sensitive_key(key) or _sensitive_key(redacted_key)
                else _redact_opaque(item, known_secrets)
            )
        return MappingProxyType(redacted)
    return value


def _contains_secret(value: object, known_secrets: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in known_secrets)
    if isinstance(value, Reference):
        return _contains_secret(value.kind, known_secrets) or _contains_secret(
            value.name, known_secrets
        )
    if isinstance(value, Mapping):
        return any(
            _contains_secret(key, known_secrets) or _contains_secret(item, known_secrets)
            for key, item in value.items()
        )
    if isinstance(value, (tuple, list)):
        return any(_contains_secret(item, known_secrets) for item in value)
    return False

def _common_structural_values(record: InFlightRecord | CompletedRecord) -> tuple[object, ...]:
    postcondition = record.expected_postcondition
    return (
        record.scenario_digest,
        record.event,
        record.attempt,
        record.actor,
        record.action_class,
        postcondition["action"],
        postcondition["target"],
        postcondition["values"],
        postcondition["marker"],
        record.reconciliation_marker,
    )


def _redacted_completed(record: CompletedRecord, known_secrets: tuple[str, ...]) -> CompletedRecord:
    invocation = InvocationMetadata(
        record.invocation.mutation_boundary,
        record.invocation.operation,
        tuple(_remove_secrets(argument, known_secrets) for argument in record.invocation.arguments),
        record.invocation.environment_names,
    )
    return CompletedRecord(
        record.scenario_digest,
        record.event,
        record.attempt,
        record.actor,
        record.action_class,
        record.expected_postcondition,
        record.reconciliation_marker,
        invocation,
        _redact_opaque(record.handler_output, known_secrets),
        record.exit_status,
        record.resolved_ids,
        record.next_safe_action,
    )


def _record_json(record: InFlightRecord | CompletedRecord) -> dict[str, object]:
    result = {"journal_version": 1, "phase": "in_flight", **_common_json(record)}
    if isinstance(record, CompletedRecord):
        result["phase"] = "completed"
        result.update(
            invocation={
                "mutation_boundary": record.invocation.mutation_boundary,
                "operation": record.invocation.operation,
                "arguments": list(record.invocation.arguments),
                "environment_names": list(record.invocation.environment_names),
            },
            handler_output=_json_mutable(record.handler_output),
            exit_status=record.exit_status,
            resolved_ids=dict(record.resolved_ids),
            next_safe_action=record.next_safe_action,
        )
    return result


def _json_mutable(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_mutable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_mutable(item) for item in value]
    return value


def _reference_from_json(value: object, field: str, expected: str | None = None) -> Reference:
    obj = _object(value, "journal", field, {"ref"}, {"ref"})
    raw = obj["ref"]
    if not isinstance(raw, str) or raw.count(":") != 1:
        raise _journal_error(field, "must be a typed reference")
    kind, name = raw.split(":", 1)
    if expected is not None and kind != expected:
        raise _journal_error(field, f"must reference {expected}")
    return _validate_reference(Reference(kind, name), field, kind=expected)


def _planned_from_json(value: object, field: str) -> PlannedValue:
    if isinstance(value, dict):
        if set(value) == {"ref"}:
            return _reference_from_json(value, field)
        return freeze_planned({key: _planned_from_json(item, f"{field}.{key}") for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_planned_from_json(item, f"{field}[]") for item in value)
    return _validate_planned(value, field)


def _record_from_json(value: object) -> InFlightRecord | CompletedRecord:
    if not isinstance(value, dict):
        raise _journal_error("$", "record must be an object")
    phase = value.get("phase")
    common = {
        "journal_version", "phase", "scenario_digest", "event", "attempt", "actor",
        "action_class", "expected_postcondition", "reconciliation_marker",
    }
    completed = {"invocation", "handler_output", "exit_status", "resolved_ids", "next_safe_action"}
    allowed = common | completed if phase == "completed" else common
    obj = _object(value, "journal", "$", allowed, allowed)
    if obj["journal_version"] != 1 or type(obj["journal_version"]) is not int:
        raise _journal_error("$.journal_version", "unsupported journal version")
    if phase not in {"in_flight", "completed"}:
        raise _journal_error("$.phase", "unsupported phase")
    fields = dict(
        scenario_digest=obj["scenario_digest"],
        event=obj["event"],
        attempt=obj["attempt"],
        actor=_reference_from_json(obj["actor"], "$.actor", "actor"),
        action_class=obj["action_class"],
        expected_postcondition=_planned_from_json(obj["expected_postcondition"], "$.expected_postcondition"),
        reconciliation_marker=obj["reconciliation_marker"],
    )
    if phase == "in_flight":
        return InFlightRecord(**fields)  # type: ignore[arg-type]
    invocation_obj = _object(
        obj["invocation"], "journal", "$.invocation",
        {"mutation_boundary", "operation", "arguments", "environment_names"},
        {"mutation_boundary", "operation", "arguments", "environment_names"},
    )
    arguments = invocation_obj["arguments"]
    environment_names = invocation_obj["environment_names"]
    if not isinstance(arguments, list) or not isinstance(environment_names, list):
        raise _journal_error("$.invocation", "argument and environment collections must be arrays")
    invocation = InvocationMetadata(
        invocation_obj["mutation_boundary"],  # type: ignore[arg-type]
        invocation_obj["operation"],  # type: ignore[arg-type]
        tuple(arguments),  # type: ignore[arg-type]
        tuple(environment_names),  # type: ignore[arg-type]
    )
    resolved = obj["resolved_ids"]
    if not isinstance(resolved, dict):
        raise _journal_error("$.resolved_ids", "must be an object")
    return CompletedRecord(
        **fields,  # type: ignore[arg-type]
        invocation=invocation,
        handler_output=_validate_json(obj["handler_output"], "$.handler_output"),
        exit_status=obj["exit_status"],  # type: ignore[arg-type]
        resolved_ids=resolved,  # type: ignore[arg-type]
        next_safe_action=obj["next_safe_action"],  # type: ignore[arg-type]
    )


class JournalStore:
    def __init__(self, state_dir: str | Path) -> None:
        self._path = Path(state_dir)
        self._dir_fd = -1
        self._lock_fd = -1
        created = False
        try:
            os.mkdir(self._path, 0o700)
            created = True
            os.chmod(self._path, 0o700, follow_symlinks=False)
        except FileExistsError:
            pass
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._dir_fd = os.open(self._path, flags)
            if created:
                os.fchmod(self._dir_fd, 0o700)
            self._verify_fd(self._dir_fd, "state directory", stat.S_ISDIR, 0o700)
            self._lock_fd = self._open_lock()
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise _journal_error("$.lock", "state directory is already locked") from None
                raise
        except Exception:
            self.close()
            raise

    def _verify_fd(self, fd: int, field: str, kind: object, mode: int) -> None:
        details = os.fstat(fd)
        if not kind(details.st_mode):  # type: ignore[operator]
            raise _journal_error(field, "has the wrong file type")
        if stat.S_IMODE(details.st_mode) != mode:
            raise _journal_error(field, f"must have mode {mode:04o}")
        if details.st_uid != os.getuid():
            raise _journal_error(field, "must be owned by the current user")

    def _open_lock(self) -> int:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
        try:
            fd = os.open(".lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self._dir_fd)
            os.fchmod(fd, 0o600)
        except FileExistsError:
            fd = os.open(".lock", flags, dir_fd=self._dir_fd)
        try:
            self._verify_fd(fd, "$.lock", stat.S_ISREG, 0o600)
            return fd
        except Exception:
            os.close(fd)
            raise

    def close(self) -> None:
        if self._lock_fd >= 0:
            os.close(self._lock_fd)
            self._lock_fd = -1
        if self._dir_fd >= 0:
            os.close(self._dir_fd)
            self._dir_fd = -1

    def __enter__(self) -> JournalStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._dir_fd < 0:
            raise _journal_error("$", "journal store is closed")

    @staticmethod
    def _filename(event: str, attempt: int) -> str:
        return f"{event}.{attempt:06d}.json"

    def _read_file(self, name: str) -> InFlightRecord | CompletedRecord:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, dir_fd=self._dir_fd)
        except OSError as exc:
            raise _journal_error(name, f"cannot open attempt: {exc.strerror}") from None
        try:
            self._verify_fd(fd, name, stat.S_ISREG, 0o600)
            chunks: list[bytes] = []
            while chunk := os.read(fd, 65536):
                chunks.append(chunk)
        finally:
            os.close(fd)
        return _record_from_json(_decode_json_bytes(b"".join(chunks), "journal"))

    def _records(self, event: str) -> list[InFlightRecord | CompletedRecord]:
        self._require_open()
        if _NAME.fullmatch(event) is None:
            raise _journal_error("$.event", "must be a slug")
        indexed: dict[int, str] = {}
        for name in os.listdir(self._dir_fd):
            if name == ".lock" or name.startswith(".tmp-"):
                continue
            match = _ATTEMPT_FILE.fullmatch(name)
            if match is None:
                raise _journal_error("$", "state directory contains an invalid record name")
            if match.group(1) != event:
                continue
            attempt = int(match.group(2))
            if attempt in indexed:
                raise _journal_error("$", "conflicting attempt files")
            indexed[attempt] = name
        if indexed and sorted(indexed) != list(range(1, max(indexed) + 1)):
            raise _journal_error("$", "attempt history contains a gap")
        records = [self._read_file(indexed[index]) for index in sorted(indexed)]
        for index, record in enumerate(records, 1):
            if record.event != event or record.attempt != index:
                raise _journal_error("$", "attempt filename does not match record identity")
        return records

    def read(self, event: str, attempt: int | None = None) -> InFlightRecord | CompletedRecord | None:
        records = self._records(event)
        if attempt is None:
            return records[-1] if records else None
        if type(attempt) is not int or attempt <= 0:
            raise _journal_error("$.attempt", "must be a positive integer")
        return records[attempt - 1] if attempt <= len(records) else None

    def _write_temp(self, document: dict[str, object]) -> str:
        name = f".tmp-{secrets.token_hex(16)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, 0o600, dir_fd=self._dir_fd)
        try:
            os.fchmod(fd, 0o600)
            self._verify_fd(fd, name, stat.S_ISREG, 0o600)
            content = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            offset = 0
            while offset < len(content):
                offset += os.write(fd, content[offset:])
            os.fsync(fd)
        except Exception:
            os.close(fd)
            os.unlink(name, dir_fd=self._dir_fd)
            raise
        os.close(fd)
        return name

    def write_in_flight(
        self, record: InFlightRecord, *, known_secrets: Collection[str] = ()
    ) -> Path:
        self._require_open()
        if not isinstance(record, InFlightRecord) or isinstance(record, CompletedRecord):
            raise _journal_error("$", "must be an in-flight record")
        record.__post_init__()
        secrets_to_remove = _secret_list(known_secrets)
        if _contains_secret(_common_structural_values(record), secrets_to_remove):
            raise _journal_error("$", "known secret occurs in structural journal metadata")
        records = self._records(record.event)
        if record.attempt != len(records) + 1:
            raise _journal_error("$.attempt", "attempt must be consecutive")
        if record.attempt == 1:
            if records:
                raise _journal_error("$.attempt", "attempt already exists")
        else:
            prior = records[-1]
            if not isinstance(prior, CompletedRecord) or prior.next_safe_action != "retry":
                raise _journal_error("$.attempt", "prior attempt does not permit retry")
            for field in (
                "scenario_digest", "event", "actor", "action_class",
                "expected_postcondition", "reconciliation_marker",
            ):
                if getattr(record, field) != getattr(prior, field):
                    raise _journal_error(f"$.{field}", "retry identity does not match prior attempt")
        target = self._filename(record.event, record.attempt)
        temporary = self._write_temp(_record_json(record))
        try:
            os.link(
                temporary,
                target,
                src_dir_fd=self._dir_fd,
                dst_dir_fd=self._dir_fd,
                follow_symlinks=False,
            )
        except Exception:
            os.unlink(temporary, dir_fd=self._dir_fd)
            raise
        os.unlink(temporary, dir_fd=self._dir_fd)
        os.fsync(self._dir_fd)
        return self._path / target

    def replace_completed(
        self, record: CompletedRecord, *, known_secrets: Collection[str] = ()
    ) -> Path:
        self._require_open()
        if not isinstance(record, CompletedRecord):
            raise _journal_error("$", "must be a completed record")
        record.__post_init__()
        secrets_to_remove = _secret_list(known_secrets)
        structural = (
            *_common_structural_values(record),
            record.invocation.mutation_boundary,
            record.invocation.operation,
            record.invocation.environment_names,
            record.exit_status,
            tuple(record.resolved_ids),
            tuple(record.resolved_ids.values()),
            record.next_safe_action,
        )
        if _contains_secret(structural, secrets_to_remove):
            raise _journal_error("$", "known secret occurs in structural journal metadata")
        existing = self.read(record.event, record.attempt)
        if not isinstance(existing, InFlightRecord) or isinstance(existing, CompletedRecord):
            raise _journal_error("$.phase", "matching in-flight attempt is required")
        for field in (
            "scenario_digest", "event", "attempt", "actor", "action_class",
            "expected_postcondition", "reconciliation_marker",
        ):
            if getattr(record, field) != getattr(existing, field):
                raise _journal_error(f"$.{field}", "completion identity does not match in-flight record")
        redacted = _redacted_completed(record, secrets_to_remove)
        target = self._filename(record.event, record.attempt)
        temporary = self._write_temp(_record_json(redacted))
        try:
            os.replace(temporary, target, src_dir_fd=self._dir_fd, dst_dir_fd=self._dir_fd)
        except Exception:
            os.unlink(temporary, dir_fd=self._dir_fd)
            raise
        os.fsync(self._dir_fd)
        return self._path / target
