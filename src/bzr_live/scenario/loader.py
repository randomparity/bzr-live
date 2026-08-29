from __future__ import annotations

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


def _decode_json_bytes(content: bytes, source: str, field: str = "$") -> object:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise _error(source, field, "input is not valid UTF-8") from None
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_Pairs,
            parse_float=_reject_number(source, field),
            parse_constant=_reject_number(source, field),
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


def _name(value: object, source: str, field: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise _error(source, field, "must be an ASCII slug")
    return value


def _text(value: object, source: str, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise _error(source, field, "must be a non-empty string")
    return value


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
    elif kind == "actor":
        email = obj["email"]
        if not isinstance(email, str) or _EMAIL.fullmatch(email) is None:
            raise _error(source, f"{field}.email", "must be an email address")
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
        if field_type not in {"text", "single-select", "multi-select"}:
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
        if target not in {"bug", "attachment"}:
            raise _error(source, f"{field}.target", "must be bug or attachment")
        products = _unique_refs(obj.get("products", []), "product", source, f"{field}.products")
        components = _unique_refs(obj.get("components", []), "component", source, f"{field}.components")
        data.update(target=target, products=products, components=components)
        dependencies.extend(products)
        dependencies.extend(components)
    return PlannedResource(kind, name, freeze_planned(data), tuple(dependencies))


_RESOURCE_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "group": ({"kind", "name", "description"}, set()),
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
    finally:
        os.close(root_fd)

    scenario_obj = _object(
        scenario_doc,
        "scenario.json",
        "$",
        {"format_version", "name", "description", "assets"},
        {"format_version", "name"},
    )
    _version(scenario_obj["format_version"], "scenario.json", "$.format_version")
    scenario_name = _name(scenario_obj["name"], "scenario.json", "$.name")
    description = _text(scenario_obj.get("description", ""), "scenario.json", "$.description", empty=True)
    assets = _list(scenario_obj.get("assets", []), "scenario.json", "$.assets")
    if assets:
        raise _error("scenario.json", "$.assets", "assets are not yet supported")
    try:
        event_text = events_content.decode("utf-8")
    except UnicodeDecodeError:
        raise _error("events.jsonl", "$", "input is not valid UTF-8") from None
    if event_text.strip():
        raise _error("events.jsonl", "$", "events are not yet supported")

    resources, resource_plan, _ = _validate_resources(resources_doc, "resources.json")
    normalized_scenario = {
        "format_version": 1,
        "name": scenario_name,
        "description": description,
        "assets": [],
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
            "events.jsonl": [],
        },
        "assets": [],
    }
    canonical = json.dumps(envelope, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ValidatedScenario(
        scenario_name,
        description,
        1,
        resources,
        resource_plan,
        (),
        MappingProxyType({}),
        digest,
    )
