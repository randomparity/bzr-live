from __future__ import annotations

from collections.abc import Mapping
import hashlib
import heapq
import json
import os
import re
import stat
from pathlib import Path
from types import MappingProxyType
from typing import Callable

from .model import (
    Asset,
    PlannedEvent,
    PlannedResource,
    Reference,
    ScenarioValidationError,
    ValidatedScenario,
    freeze_planned,
    planned_to_json,
)

_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}\Z", re.ASCII)
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\Z", re.ASCII)
_RESOURCE_KINDS = {
    "group",
    "actor",
    "product",
    "component",
    "version",
    "milestone",
    "custom-field",
    "keyword",
    "flag-type",
}


class _Pairs(list[tuple[str, object]]):
    pass


def _error(source: str, field: str, message: str) -> ScenarioValidationError:
    return ScenarioValidationError(source, field, message)


def _read_regular(root_fd: int, basename: str, source: str) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(basename, flags, dir_fd=root_fd)
    except OSError as exc:
        raise _error(source, "$", f"cannot open required regular file: {exc.strerror}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _error(source, "$", "required input is not a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _decode_pairs(value: object, source: str, field: str) -> object:
    if isinstance(value, _Pairs):
        result: dict[str, object] = {}
        for key, item in value:
            child = f"{field}.{key}"
            if key in result:
                raise _error(source, child, "duplicate object key")
            result[key] = _decode_pairs(item, source, child)
        return result
    if isinstance(value, list):
        return [_decode_pairs(item, source, f"{field}[{index}]") for index, item in enumerate(value)]
    return value


def _reject_number(source: str, field: str) -> Callable[[str], object]:
    def reject(_: str) -> object:
        raise _error(source, field, "floating-point numbers are not supported")

    return reject


def _reject_constant(source: str, field: str) -> Callable[[str], object]:
    # Separate from `_reject_number` because it fires on both decode policies: the journal
    # path supports floats, so "floating-point numbers are not supported" would be false
    # there. Non-finite is the thing neither path accepts.
    def reject(_: str) -> object:
        raise _error(source, field, "non-finite numbers are not supported")

    return reject


def _decode_json_bytes(
    content: bytes, source: str, field: str = "$", *, allow_float: bool = False
) -> object:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise _error(source, field, "input is not valid UTF-8") from None
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_Pairs,
            # Authored scenario input excludes floats (ADR 0002); captured journal handler
            # output admits finite ones (ADR 0014). `parse_constant` is unconditional, so the
            # bare NaN/Infinity tokens are refused in both modes -- but an overflow literal
            # like 1e400 reaches parse_float, not parse_constant, and is caught downstream by
            # `_validate_json`'s math.isfinite branch.
            parse_float=float if allow_float else _reject_number(source, field),
            parse_constant=_reject_constant(source, field),
        )
    except ScenarioValidationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise _error(source, field, f"invalid JSON: {exc}") from None
    return _decode_pairs(raw, source, field)


def _load_json(root_fd: int, basename: str, source: str) -> dict[str, object]:
    value = _decode_json_bytes(_read_regular(root_fd, basename, source), source)
    if not isinstance(value, dict):
        raise _error(source, "$", "document must be an object")
    return value


def _object(
    value: object,
    source: str,
    field: str,
    allowed: set[str],
    required: set[str],
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _error(source, field, "must be an object")
    unknown = set(value) - allowed
    if unknown:
        key = min(unknown)
        raise _error(source, f"{field}.{key}", "unknown field")
    missing = required - set(value)
    if missing:
        key = min(missing)
        raise _error(source, f"{field}.{key}", "required field is missing")
    return value


def _version(value: object, source: str, field: str) -> int:
    if type(value) is not int or value != 1:
        raise _error(source, field, "must be integer 1")
    return 1


def _utf8(value: str, source: str, field: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _error(source, field, "must not contain an unpaired surrogate") from None
    return value


def _name(value: object, source: str, field: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise _error(source, field, "must be an ASCII slug")
    return value


def _text(value: object, source: str, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise _error(source, field, "must be a non-empty string")
    return _utf8(value, source, field)


def _list(value: object, source: str, field: str) -> list[object]:
    if not isinstance(value, list):
        raise _error(source, field, "must be an array")
    return value


def _reference(value: object, expected: str, source: str, field: str) -> Reference:
    obj = _object(value, source, field, {"ref"}, {"ref"})
    raw = obj["ref"]
    if not isinstance(raw, str) or raw.count(":") != 1:
        raise _error(source, f"{field}.ref", "must be a typed reference")
    kind, name = raw.split(":", 1)
    if kind not in _RESOURCE_KINDS | {"asset", "bug", "attachment"} or not _NAME.fullmatch(name):
        raise _error(source, f"{field}.ref", "must use a supported kind and slug")
    if kind != expected:
        raise _error(source, field, f"must reference {expected}")
    return Reference(kind, name)


def _unique_refs(value: object, expected: str, source: str, field: str) -> tuple[Reference, ...]:
    refs: list[Reference] = []
    seen: set[Reference] = set()
    for index, item in enumerate(_list(value, source, field)):
        ref = _reference(item, expected, source, f"{field}[{index}]")
        if ref in seen:
            raise _error(source, f"{field}[{index}]", "duplicate reference")
        seen.add(ref)
        refs.append(ref)
    return tuple(refs)


def _unique_strings(value: object, source: str, field: str, *, nonempty: bool) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(_list(value, source, field)):
        if not isinstance(item, str) or (nonempty and not item.strip()):
            raise _error(source, f"{field}[{index}]", "must be a non-empty string")
        _utf8(item, source, f"{field}[{index}]")
        if item in seen:
            raise _error(source, f"{field}[{index}]", "duplicate value")
        seen.add(item)
        result.append(item)
    return tuple(result)


def _parse_resource(item: object, index: int, source: str) -> PlannedResource:
    field = f"$.resources[{index}]"
    all_fields = set().union(*(required | optional for required, optional in _RESOURCE_FIELDS.values()))
    base = _object(item, source, field, all_fields, {"kind", "name"})
    kind = base["kind"]
    if not isinstance(kind, str) or kind not in _RESOURCE_FIELDS:
        raise _error(source, f"{field}.kind", "unsupported resource kind")
    required, optional = _RESOURCE_FIELDS[kind]
    obj = _object(item, source, field, required | optional, required)
    name = _name(obj["name"], source, f"{field}.name")
    data: dict[str, object] = {}
    dependencies: list[Reference] = []

    if kind in {"group", "product", "keyword"}:
        data["description"] = _text(obj["description"], source, f"{field}.description")
        if kind == "group":
            # products this bug group may be set on; same shape and validation as
            # flag-type's inclusions, so the plan orders the group after them
            products = _unique_refs(
                obj.get("products", []), "product", source, f"{field}.products")
            data["products"] = products
            dependencies.extend(products)
    elif kind == "actor":
        email = obj["email"]
        if not isinstance(email, str) or _EMAIL.fullmatch(email) is None:
            raise _error(source, f"{field}.email", "must be an email address")
        _utf8(email, source, f"{field}.email")
        data["email"] = email
        data["display_name"] = _text(obj["display_name"], source, f"{field}.display_name")
        groups = _unique_refs(obj.get("groups", []), "group", source, f"{field}.groups")
        data["groups"] = groups
        dependencies.extend(groups)
    elif kind == "component":
        product = _reference(obj["product"], "product", source, f"{field}.product")
        data["product"] = product
        data["description"] = _text(obj["description"], source, f"{field}.description")
        dependencies.append(product)
        if "default_assignee" in obj:
            assignee = _reference(obj["default_assignee"], "actor", source, f"{field}.default_assignee")
            data["default_assignee"] = assignee
            dependencies.append(assignee)
        else:
            data["default_assignee"] = None
    elif kind in {"version", "milestone"}:
        product = _reference(obj["product"], "product", source, f"{field}.product")
        data["product"] = product
        dependencies.append(product)
    elif kind == "custom-field":
        field_type = obj["field_type"]
        if not isinstance(field_type, str) or field_type not in {"text", "single-select", "multi-select"}:
            raise _error(source, f"{field}.field_type", "unsupported custom-field type")
        values = _unique_strings(obj.get("values", []), source, f"{field}.values", nonempty=True)
        if field_type == "text" and values:
            raise _error(source, f"{field}.values", "text fields cannot declare values")
        if field_type != "text" and not values:
            raise _error(source, f"{field}.values", "select fields require values")
        data.update(field_type=field_type, values=values)
    elif kind == "flag-type":
        data["description"] = _text(obj["description"], source, f"{field}.description")
        target = obj["target"]
        if not isinstance(target, str) or target not in {"bug", "attachment"}:
            raise _error(source, f"{field}.target", "must be bug or attachment")
        products = _unique_refs(obj.get("products", []), "product", source, f"{field}.products")
        components = _unique_refs(obj.get("components", []), "component", source, f"{field}.components")
        data.update(target=target, products=products, components=components)
        dependencies.extend(products)
        dependencies.extend(components)
    return PlannedResource(kind, name, freeze_planned(data), tuple(dependencies))


_RESOURCE_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "group": ({"kind", "name", "description"}, {"products"}),
    "actor": ({"kind", "name", "email", "display_name"}, {"groups"}),
    "product": ({"kind", "name", "description"}, set()),
    "component": ({"kind", "name", "product", "description"}, {"default_assignee"}),
    "version": ({"kind", "name", "product"}, set()),
    "milestone": ({"kind", "name", "product"}, set()),
    "custom-field": ({"kind", "name", "field_type"}, {"values"}),
    "keyword": ({"kind", "name", "description"}, set()),
    "flag-type": ({"kind", "name", "description", "target"}, {"products", "components"}),
}


def _validate_resources(
    document: object, source: str
) -> tuple[tuple[PlannedResource, ...], tuple[PlannedResource, ...], dict[str, PlannedResource]]:
    obj = _object(document, source, "$", {"format_version", "resources"}, {"format_version", "resources"})
    _version(obj["format_version"], source, "$.format_version")
    resources = tuple(_parse_resource(item, index, source) for index, item in enumerate(_list(obj["resources"], source, "$.resources")))
    identities: dict[str, PlannedResource] = {}
    indexes: dict[str, int] = {}
    for index, resource in enumerate(resources):
        identity = f"{resource.kind}:{resource.name}"
        if identity in identities:
            raise _error(source, f"$.resources[{index}]", "duplicate resource identity")
        identities[identity] = resource
        indexes[identity] = index
    for index, resource in enumerate(resources):
        for dependency in resource.dependencies:
            identity = f"{dependency.kind}:{dependency.name}"
            if identity not in identities:
                raise _error(source, f"$.resources[{index}]", f"missing dependency {identity}")
        if resource.kind == "flag-type":
            products = set(resource.data["products"])
            for component_ref in resource.data["components"]:  # type: ignore[union-attr]
                component = identities[f"component:{component_ref.name}"]
                owner = component.data["product"]
                if products and owner not in products:
                    raise _error(source, f"$.resources[{index}].components", "component product is outside flag scope")

    dependents: dict[str, list[str]] = {identity: [] for identity in identities}
    indegree: dict[str, int] = {}
    for identity, resource in identities.items():
        unique = {f"{ref.kind}:{ref.name}" for ref in resource.dependencies}
        indegree[identity] = len(unique)
        for dependency in unique:
            dependents[dependency].append(identity)
    ready = [(indexes[identity], identity) for identity, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    plan: list[PlannedResource] = []
    while ready:
        _, identity = heapq.heappop(ready)
        plan.append(identities[identity])
        for dependent in dependents[identity]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, (indexes[dependent], dependent))
    if len(plan) != len(resources):
        raise _error(source, "$.resources", "resource dependency cycle")
    return resources, tuple(plan), identities


def _resource_json(resource: PlannedResource) -> dict[str, object]:
    value = {"kind": resource.kind, "name": resource.name}
    value.update({key: planned_to_json(item) for key, item in resource.data.items()})
    return value


_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?\Z", re.ASCII)
_MEDIA_TYPE = re.compile(
    r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z", re.ASCII
)


def _boolean(value: object, source: str, field: str) -> bool:
    if type(value) is not bool:
        raise _error(source, field, "must be a boolean")
    return value


def _decimal(value: object, source: str, field: str) -> str:
    if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
        raise _error(source, field, "must be a canonical non-negative decimal string")
    return value


def _read_asset(root_fd: int, path: str, source: str, field: str) -> bytes:
    parts = path.split("/")
    current = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            if final:
                flags |= os.O_NONBLOCK
            else:
                flags |= getattr(os, "O_DIRECTORY", 0)
            try:
                child = os.open(part, flags, dir_fd=current)
            except OSError as exc:
                raise _error(source, field, f"cannot open asset: {exc.strerror}") from None
            os.close(current)
            current = child
            mode = os.fstat(current).st_mode
            if (final and not stat.S_ISREG(mode)) or (not final and not stat.S_ISDIR(mode)):
                raise _error(source, field, "asset path component has the wrong file type")
        chunks: list[bytes] = []
        while chunk := os.read(current, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(current)


def _validate_assets(
    value: object, root_fd: int, source: str
) -> tuple[MappingProxyType[str, Asset], list[dict[str, object]]]:
    assets: dict[str, Asset] = {}
    paths: set[str] = set()
    normalized: list[dict[str, object]] = []
    for index, item in enumerate(_list(value, source, "$.assets")):
        field = f"$.assets[{index}]"
        obj = _object(item, source, field, {"name", "path", "sha256"}, {"name", "path", "sha256"})
        name = _name(obj["name"], source, f"{field}.name")
        path = obj["path"]
        if not isinstance(path, str):
            raise _error(source, f"{field}.path", "must be a canonical path below assets/")
        _utf8(path, source, f"{field}.path")
        if (
            "\0" in path or "\\" in path
            or path.startswith("/")
            or re.match(r"[A-Za-z]:", path)
            or len(path.split("/")) < 2
            or path.split("/")[0] != "assets"
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise _error(source, f"{field}.path", "must be a canonical path below assets/")
        checksum = obj["sha256"]
        if not isinstance(checksum, str) or _SHA256.fullmatch(checksum) is None:
            raise _error(source, f"{field}.sha256", "must be a lowercase SHA-256")
        if name in assets:
            raise _error(source, f"{field}.name", "duplicate asset name")
        if path in paths:
            raise _error(source, f"{field}.path", "duplicate asset path")
        content = _read_asset(root_fd, path, source, f"{field}.path")
        actual = hashlib.sha256(content).hexdigest()
        if actual != checksum:
            raise _error(source, f"{field}.sha256", "does not match the asset bytes")
        paths.add(path)
        assets[name] = Asset(name, path, checksum, content)
        normalized.append({"name": name, "path": path, "sha256": checksum})
    normalized.sort(key=lambda item: str(item["path"]))
    return MappingProxyType(assets), normalized


def _parse_events(content: bytes) -> list[tuple[str, dict[str, object]]]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise _error("events.jsonl", "$", "input is not valid UTF-8") from None
    result: list[tuple[str, dict[str, object]]] = []
    for line_number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        source = f"events.jsonl:{line_number}"
        value = _decode_json_bytes(line.encode("utf-8"), source)
        if not isinstance(value, dict):
            raise _error(source, "$", "event must be an object")
        result.append((source, value))
    return result


def _resolve(
    value: object,
    expected: str,
    source: str,
    field: str,
    available: set[Reference],
) -> Reference:
    ref = _reference(value, expected, source, field)
    if ref not in available:
        raise _error(source, field, "reference does not resolve to an earlier value")
    return ref


def _resolved_list(
    value: object,
    expected: str,
    source: str,
    field: str,
    available: set[Reference],
) -> tuple[Reference, ...]:
    refs = _unique_refs(value, expected, source, field)
    for index, ref in enumerate(refs):
        if ref not in available:
            raise _error(source, f"{field}[{index}]", "reference does not resolve")
    return refs


def _append_dependencies(target: list[Reference], values: object) -> None:
    if isinstance(values, Reference):
        if values not in target:
            target.append(values)
    elif isinstance(values, (tuple, list)):
        for value in values:
            _append_dependencies(target, value)
    elif isinstance(values, Mapping):
        for value in values.values():
            _append_dependencies(target, value)


def _custom_assignments(
    value: object,
    source: str,
    field: str,
    available: set[Reference],
    resources: dict[str, PlannedResource],
) -> tuple[MappingProxyType[str, object], ...]:
    assignments: list[MappingProxyType[str, object]] = []
    seen: set[Reference] = set()
    items = _list(value, source, field)
    if not items:
        raise _error(source, field, "must contain at least one assignment")
    for index, item in enumerate(items):
        item_field = f"{field}[{index}]"
        obj = _object(item, source, item_field, {"field", "value"}, {"field", "value"})
        ref = _resolve(obj["field"], "custom-field", source, f"{item_field}.field", available)
        if ref in seen:
            raise _error(source, f"{item_field}.field", "duplicate custom-field assignment")
        seen.add(ref)
        catalog = resources[f"custom-field:{ref.name}"].data
        assigned = obj["value"]
        field_type = catalog["field_type"]
        choices = catalog["values"]
        if field_type == "text":
            if not isinstance(assigned, str):
                raise _error(source, f"{item_field}.value", "text field requires a string")
            _utf8(assigned, source, f"{item_field}.value")
        elif field_type == "single-select":
            if not isinstance(assigned, str) or assigned not in choices:
                raise _error(source, f"{item_field}.value", "value is outside the select catalog")
        else:
            selected = _unique_strings(
                assigned, source, f"{item_field}.value", nonempty=True
            )
            if any(choice not in choices for choice in selected):
                raise _error(source, f"{item_field}.value", "value is outside the select catalog")
            assigned = selected
        assignments.append(MappingProxyType({"field": ref, "value": freeze_planned(assigned)}))
    return tuple(assignments)


def _bug_scope(
    ref: Reference,
    source: str,
    field: str,
    scopes: dict[Reference, tuple[Reference, Reference]],
) -> tuple[Reference, Reference]:
    try:
        return scopes[ref]
    except KeyError:
        raise _error(source, field, "bug scope is unavailable") from None


def _create_payload(
    obj: dict[str, object],
    source: str,
    available: set[Reference],
    resources: dict[str, PlannedResource],
    bug_scopes: dict[Reference, tuple[Reference, Reference]],
) -> tuple[dict[str, object], Reference, list[Reference]]:
    required = {"alias", "product", "component", "summary"}
    optional = {
        "description", "version", "milestone", "assignee", "cc", "groups",
        "depends_on", "blocks", "duplicate_of", "keywords", "estimated_hours",
        "remaining_hours", "custom_fields",
    }
    payload = _object(obj, source, "$.payload", required | optional, required)
    alias = _name(payload["alias"], source, "$.payload.alias")
    creates = Reference("bug", alias)
    if creates in available:
        raise _error(source, "$.payload.alias", "duplicate created identity")
    product = _resolve(payload["product"], "product", source, "$.payload.product", available)
    component = _resolve(payload["component"], "component", source, "$.payload.component", available)
    if resources[f"component:{component.name}"].data["product"] != product:
        raise _error(source, "$.payload.component", "component is outside the selected product")
    normalized: dict[str, object] = {
        "alias": alias,
        "product": product,
        "component": component,
        "summary": _text(payload["summary"], source, "$.payload.summary"),
        "description": _text(payload.get("description", ""), source, "$.payload.description", empty=True),
    }
    for key, kind in (("version", "version"), ("milestone", "milestone")):
        raw = payload.get(key)
        if raw is None:
            normalized[key] = None
        else:
            ref = _resolve(raw, kind, source, f"$.payload.{key}", available)
            if resources[f"{kind}:{ref.name}"].data["product"] != product:
                raise _error(source, f"$.payload.{key}", f"{kind} is outside the selected product")
            normalized[key] = ref
    raw_assignee = payload.get("assignee")
    normalized["assignee"] = (
        None
        if raw_assignee is None
        else _resolve(raw_assignee, "actor", source, "$.payload.assignee", available)
    )
    for key, kind in (
        ("cc", "actor"), ("groups", "group"), ("depends_on", "bug"),
        ("blocks", "bug"), ("keywords", "keyword"),
    ):
        normalized[key] = _resolved_list(
            payload.get(key, []), kind, source, f"$.payload.{key}", available
        )
    raw_duplicate = payload.get("duplicate_of")
    normalized["duplicate_of"] = (
        None
        if raw_duplicate is None
        else _resolve(raw_duplicate, "bug", source, "$.payload.duplicate_of", available)
    )
    for key in ("estimated_hours", "remaining_hours"):
        raw = payload.get(key)
        normalized[key] = None if raw is None else _decimal(raw, source, f"$.payload.{key}")
    raw_custom = payload.get("custom_fields")
    normalized["custom_fields"] = (
        ()
        if raw_custom is None
        else _custom_assignments(raw_custom, source, "$.payload.custom_fields", available, resources)
    )
    bug_scopes[creates] = (product, component)
    dependencies: list[Reference] = []
    for key in (
        "product", "component", "version", "milestone", "assignee", "cc", "groups",
        "depends_on", "blocks", "duplicate_of", "keywords", "custom_fields",
    ):
        _append_dependencies(dependencies, normalized[key])
    return normalized, creates, dependencies


def _update_set(
    value: object,
    bug: Reference,
    source: str,
    available: set[Reference],
    resources: dict[str, PlannedResource],
    bug_scopes: dict[Reference, tuple[Reference, Reference]],
) -> tuple[dict[str, object], list[Reference]]:
    allowed = {
        "summary", "status", "resolution", "assignee", "cc", "groups", "depends_on",
        "blocks", "duplicate_of", "version", "milestone", "keywords",
        "estimated_hours", "remaining_hours",
    }
    obj = _object(value, source, "$.payload.set", allowed, set())
    if not obj:
        raise _error(source, "$.payload.set", "must not be empty")
    normalized: dict[str, object] = {}
    product, _ = _bug_scope(bug, source, "$.payload.bug", bug_scopes)
    for key in allowed:
        if key not in obj:
            continue
        raw = obj[key]
        field = f"$.payload.set.{key}"
        if key in {"summary", "status"}:
            normalized[key] = _text(raw, source, field)
        elif key == "resolution":
            normalized[key] = None if raw is None else _text(raw, source, field)
        elif key in {"assignee"}:
            normalized[key] = None if raw is None else _resolve(raw, "actor", source, field, available)
        elif key in {"duplicate_of"}:
            normalized[key] = None if raw is None else _resolve(raw, "bug", source, field, available)
        elif key in {"version", "milestone"}:
            if raw is None:
                normalized[key] = None
            else:
                ref = _resolve(raw, key, source, field, available)
                if resources[f"{key}:{ref.name}"].data["product"] != product:
                    raise _error(source, field, f"{key} is outside the target product")
                normalized[key] = ref
        elif key in {"estimated_hours", "remaining_hours"}:
            normalized[key] = _decimal(raw, source, field)
        else:
            kind = {
                "cc": "actor", "groups": "group", "depends_on": "bug",
                "blocks": "bug", "keywords": "keyword",
            }[key]
            normalized[key] = _resolved_list(raw, kind, source, field, available)
    ordered = {key: normalized[key] for key in (
        "summary", "status", "resolution", "assignee", "cc", "groups", "depends_on",
        "blocks", "duplicate_of", "version", "milestone", "keywords",
        "estimated_hours", "remaining_hours",
    ) if key in normalized}
    dependencies: list[Reference] = []
    _append_dependencies(dependencies, ordered)
    return ordered, dependencies


def _validate_events(
    raw_events: list[tuple[str, dict[str, object]]],
    resources: dict[str, PlannedResource],
    assets: MappingProxyType[str, Asset],
    scenario_name: str,
) -> tuple[tuple[PlannedEvent, ...], list[dict[str, object]]]:
    available = {Reference(item.kind, item.name) for item in resources.values()}
    available.update(Reference("asset", name) for name in assets)
    bug_scopes: dict[Reference, tuple[Reference, Reference]] = {}
    names: set[str] = set()
    planned: list[PlannedEvent] = []
    normalized_events: list[dict[str, object]] = []
    classes = {
        "bug.create": "unique-create",
        "bug.update": "idempotent-set",
        "bug.comment": "append",
        "bug.attach": "append",
        "bug.worktime": "append",
        "bug.custom-field-set": "idempotent-set",
        "bug.flag": "idempotent-set",
        "attachment.update": "idempotent-set",
    }
    for source, raw in raw_events:
        event = _object(
            raw, source, "$", {"format_version", "name", "actor", "action", "payload"},
            {"format_version", "name", "actor", "action", "payload"},
        )
        _version(event["format_version"], source, "$.format_version")
        name = _name(event["name"], source, "$.name")
        if name in names:
            raise _error(source, "$.name", "duplicate event name")
        names.add(name)
        actor = _resolve(event["actor"], "actor", source, "$.actor", available)
        action = event["action"]
        if not isinstance(action, str) or action not in classes:
            raise _error(source, "$.action", "unsupported action")
        payload_obj = event["payload"]
        dependencies = [actor]
        creates: Reference | None = None
        values: dict[str, object]
        target: Reference

        if action == "bug.create":
            payload, creates, refs = _create_payload(
                payload_obj, source, available, resources, bug_scopes
            )
            dependencies.extend(ref for ref in refs if ref not in dependencies)
            target = creates
            values = {key: value for key, value in payload.items() if key != "alias"}
            server_hash = hashlib.sha256(
                b"v1\0" + scenario_name.encode("utf-8") + b"\0" + creates.name.encode("utf-8")
            ).hexdigest()[:31]
            values["server_alias"] = f"bzr-live-{server_hash}"
        elif action == "bug.update":
            payload = _object(payload_obj, source, "$.payload", {"bug", "set"}, {"bug", "set"})
            bug = _resolve(payload["bug"], "bug", source, "$.payload.bug", available)
            updates, refs = _update_set(payload["set"], bug, source, available, resources, bug_scopes)
            payload = {"bug": bug, "set": updates}
            dependencies.extend(ref for ref in [bug, *refs] if ref not in dependencies)
            target, values = bug, updates
        elif action == "bug.comment":
            payload = _object(payload_obj, source, "$.payload", {"bug", "body", "private"}, {"bug", "body"})
            bug = _resolve(payload["bug"], "bug", source, "$.payload.bug", available)
            payload = {"bug": bug, "body": _text(payload["body"], source, "$.payload.body"), "private": _boolean(payload.get("private", False), source, "$.payload.private")}
            dependencies.append(bug)
            target, values = bug, {"body": payload["body"], "private": payload["private"]}
        elif action == "bug.attach":
            payload = _object(payload_obj, source, "$.payload", {"alias", "bug", "asset", "description", "content_type", "private"}, {"alias", "bug", "asset", "description", "content_type"})
            alias = _name(payload["alias"], source, "$.payload.alias")
            creates = Reference("attachment", alias)
            if creates in available:
                raise _error(source, "$.payload.alias", "duplicate created identity")
            bug = _resolve(payload["bug"], "bug", source, "$.payload.bug", available)
            asset = _resolve(payload["asset"], "asset", source, "$.payload.asset", available)
            content_type = payload["content_type"]
            if not isinstance(content_type, str) or _MEDIA_TYPE.fullmatch(content_type) is None:
                raise _error(source, "$.payload.content_type", "invalid media type")
            payload = {"alias": alias, "bug": bug, "asset": asset, "description": _text(payload["description"], source, "$.payload.description", empty=True), "content_type": content_type, "private": _boolean(payload.get("private", False), source, "$.payload.private")}
            dependencies.extend([bug, asset])
            target = creates
            values = {"bug": bug, "asset": asset, "asset_sha256": assets[asset.name].sha256, "description": payload["description"], "content_type": content_type, "private": payload["private"]}
        elif action == "bug.worktime":
            payload = _object(payload_obj, source, "$.payload", {"bug", "hours", "comment"}, {"bug", "hours", "comment"})
            bug = _resolve(payload["bug"], "bug", source, "$.payload.bug", available)
            payload = {"bug": bug, "hours": _decimal(payload["hours"], source, "$.payload.hours"), "comment": _text(payload["comment"], source, "$.payload.comment")}
            dependencies.append(bug)
            target, values = bug, dict(payload)
        elif action == "bug.custom-field-set":
            payload = _object(payload_obj, source, "$.payload", {"bug", "values"}, {"bug", "values"})
            bug = _resolve(payload["bug"], "bug", source, "$.payload.bug", available)
            assignments = _custom_assignments(payload["values"], source, "$.payload.values", available, resources)
            payload = {"bug": bug, "values": assignments}
            dependencies.append(bug)
            _append_dependencies(dependencies, assignments)
            target, values = bug, dict(payload)
        elif action == "bug.flag":
            payload = _object(payload_obj, source, "$.payload", {"bug", "flag_type", "status", "requestee"}, {"bug", "flag_type", "status"})
            bug = _resolve(payload["bug"], "bug", source, "$.payload.bug", available)
            flag_type = _resolve(payload["flag_type"], "flag-type", source, "$.payload.flag_type", available)
            status_value = payload["status"]
            if not isinstance(status_value, str) or status_value not in {"?", "+", "-", "X"}:
                raise _error(source, "$.payload.status", "unsupported flag status")
            requestee_raw = payload.get("requestee")
            requestee = None if requestee_raw is None else _resolve(requestee_raw, "actor", source, "$.payload.requestee", available)
            if requestee is not None and status_value != "?":
                raise _error(source, "$.payload.requestee", "requestee is allowed only for ?")
            catalog = resources[f"flag-type:{flag_type.name}"].data
            if catalog["target"] != "bug":
                raise _error(source, "$.payload.flag_type", "flag type does not target bugs")
            product, component = _bug_scope(bug, source, "$.payload.bug", bug_scopes)
            if catalog["products"] and product not in catalog["products"]:
                raise _error(source, "$.payload.flag_type", "flag type is outside product scope")
            if catalog["components"] and component not in catalog["components"]:
                raise _error(source, "$.payload.flag_type", "flag type is outside component scope")
            payload = {"bug": bug, "flag_type": flag_type, "status": status_value, "requestee": requestee}
            dependencies.extend(ref for ref in (bug, flag_type, requestee) if ref is not None)
            target, values = bug, dict(payload)
        else:
            payload = _object(payload_obj, source, "$.payload", {"attachment", "obsolete", "description"}, {"attachment", "obsolete"})
            attachment = _resolve(payload["attachment"], "attachment", source, "$.payload.attachment", available)
            normalized_update: dict[str, object] = {"attachment": attachment, "obsolete": _boolean(payload["obsolete"], source, "$.payload.obsolete")}
            if "description" in payload:
                normalized_update["description"] = _text(payload["description"], source, "$.payload.description", empty=True)
            payload = normalized_update
            dependencies.append(attachment)
            target, values = attachment, dict(payload)

        marker = f"bzr-live:{scenario_name}:{name}"
        frozen_payload = freeze_planned(payload)
        postcondition = freeze_planned(
            {"action": action, "target": target, "values": values, "marker": marker}
        )
        planned_event = PlannedEvent(
            name, actor, action, classes[action], frozen_payload,
            tuple(dict.fromkeys(dependencies)), marker, postcondition, creates,
        )
        planned.append(planned_event)
        normalized_events.append(
            {
                "format_version": 1,
                "name": name,
                "actor": planned_to_json(actor),
                "action": action,
                "payload": planned_to_json(frozen_payload),
            }
        )
        if creates is not None:
            available.add(creates)
    return tuple(planned), normalized_events


def load_scenario(path: str | Path) -> ValidatedScenario:
    source_path = os.fspath(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(source_path, flags)
    except OSError as exc:
        raise _error("scenario", "$", f"cannot open scenario directory: {exc.strerror}") from None
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise _error("scenario", "$", "scenario path is not a directory")
        scenario_doc = _load_json(root_fd, "scenario.json", "scenario.json")
        resources_doc = _load_json(root_fd, "resources.json", "resources.json")
        events_content = _read_regular(root_fd, "events.jsonl", "events.jsonl")
        raw_events = _parse_events(events_content)
        scenario_obj = _object(
            scenario_doc, "scenario.json", "$",
            {"format_version", "name", "description", "assets"},
            {"format_version", "name"},
        )
        _version(scenario_obj["format_version"], "scenario.json", "$.format_version")
        scenario_name = _name(scenario_obj["name"], "scenario.json", "$.name")
        description = _text(
            scenario_obj.get("description", ""), "scenario.json", "$.description", empty=True
        )
        assets, asset_table = _validate_assets(
            scenario_obj.get("assets", []), root_fd, "scenario.json"
        )
    finally:
        os.close(root_fd)

    resources, resource_plan, resource_index = _validate_resources(
        resources_doc, "resources.json"
    )
    events, normalized_events = _validate_events(
        raw_events, resource_index, assets, scenario_name
    )
    normalized_scenario = {
        "format_version": 1,
        "name": scenario_name,
        "description": description,
        "assets": asset_table,
    }
    normalized_resources = {
        "format_version": 1,
        "resources": [_resource_json(resource) for resource in resources],
    }
    envelope = {
        "format_version": 1,
        "inputs": {
            "scenario.json": normalized_scenario,
            "resources.json": normalized_resources,
            "events.jsonl": normalized_events,
        },
        "assets": asset_table,
    }
    canonical = json.dumps(
        envelope, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ValidatedScenario(
        scenario_name,
        description,
        1,
        resources,
        resource_plan,
        events,
        assets,
        digest,
    )
