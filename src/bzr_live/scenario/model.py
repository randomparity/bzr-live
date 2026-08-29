from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping, TypeAlias



class ScenarioValidationError(ValueError):
    def __init__(self, source: str, field: str, message: str) -> None:
        self.source = source
        self.field = field
        super().__init__(f"{source}:{field}: {message}")


@dataclass(frozen=True, slots=True)
class Reference:
    kind: str
    name: str

RecoveryClass: TypeAlias = Literal["unique-create", "idempotent-set", "append"]
JsonValue: TypeAlias = bool | int | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"] | None
PlannedValue: TypeAlias = (
    bool | int | str | Reference | tuple["PlannedValue", ...] | Mapping[str, "PlannedValue"] | None
)


@dataclass(frozen=True, slots=True)
class Asset:
    name: str
    path: str
    sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class PlannedResource:
    kind: str
    name: str
    data: Mapping[str, PlannedValue]
    dependencies: tuple[Reference, ...]


@dataclass(frozen=True, slots=True)
class PlannedEvent:
    name: str
    actor: Reference
    action: str
    action_class: RecoveryClass
    payload: Mapping[str, PlannedValue]
    dependencies: tuple[Reference, ...]
    reconciliation_marker: str
    expected_postcondition: Mapping[str, PlannedValue]
    creates: Reference | None


@dataclass(frozen=True, slots=True)
class ValidatedScenario:
    name: str
    description: str
    format_version: int
    resources: tuple[PlannedResource, ...]
    resource_plan: tuple[PlannedResource, ...]
    events: tuple[PlannedEvent, ...]
    assets: Mapping[str, Asset]
    digest: str


def freeze_planned(value: object) -> PlannedValue:
    if value is None or type(value) in (bool, int, str) or isinstance(value, Reference):
        return value  # type: ignore[return-value]
    if isinstance(value, (list, tuple)):
        return tuple(freeze_planned(item) for item in value)
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_planned(item) for key, item in value.items()})
    raise TypeError(f"unsupported planned value type: {type(value).__name__}")


def planned_to_json(value: PlannedValue) -> JsonValue:
    if isinstance(value, Reference):
        return {"ref": f"{value.kind}:{value.name}"}
    if isinstance(value, tuple):
        return tuple(planned_to_json(item) for item in value)
    if isinstance(value, Mapping):
        return {key: planned_to_json(item) for key, item in value.items()}
    return value
