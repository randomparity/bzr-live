from __future__ import annotations

from typing import Callable

from .adapters import ProvisionError

# Bugzilla's checksetup pre-creates these groups (Install.pm SYSTEM_GROUPS at the
# pinned SHA); each is a legal scenario slug, so a declared group with one of these
# names reconciles as existing fixture furniture without description comparison.
SYSTEM_GROUPS = frozenset({
    "admin", "tweakparams", "editusers", "creategroups", "editclassifications",
    "editcomponents", "editkeywords", "editbugs", "canconfirm",
})

_MULTI_STEP_HINT = (
    " If an earlier run was interrupted mid-create, the fixture holds a partial "
    "resource; recover with a fixture reset (CONFIRM_RESET=1 make reset)."
)


class ProvisionConflictError(ProvisionError):
    def __init__(self, identity, field, declared, observed, *, multi_step=False):
        self.identity, self.field = identity, field
        self.declared, self.observed = declared, observed
        hint = _MULTI_STEP_HINT if multi_step else ""
        super().__init__(
            f"{identity} differs in declared field {field!r}: declared "
            f"{declared!r}, observed {observed!r}.{hint}")


def custom_field_name(slug: str) -> str:
    return "cf_" + slug.replace("-", "_")


def _identity(resource) -> str:
    return f"{resource.kind}:{resource.name}"


def _entry_field(entry, names) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for name in names:
            value = entry.get(name)
            if isinstance(value, str) and value:
                return value
    return None


def _login_of(entry) -> str | None:
    return _entry_field(entry, ("email", "login", "name"))


def _display_of(entry) -> str | None:
    return _entry_field(entry, ("real_name", "full_name", "display_name"))


def _name_set(values) -> set[str]:
    names = set()
    for value in values or []:
        name = _entry_field(value, ("name", "value"))
        if name is not None:
            names.add(name)
    return names


def _user_rows(payload) -> list:
    if isinstance(payload, dict):
        inner = payload.get("users")
        return inner if isinstance(inner, list) else [payload]
    return payload if isinstance(payload, list) else []


class Provisioner:
    """Two-pass reconciliation over ValidatedScenario.resource_plan (ADR 0004)."""

    def __init__(self, scenario, bzr_factory, bridge, keys,
                 out: Callable[[str], None] = print) -> None:
        self._scenario = scenario
        self._bzr_factory = bzr_factory
        self._bridge = bridge
        self._keys = keys
        self._out = out
        self._bzr = None
        self._admin_login = ""
        self._index = {_identity(r): r for r in scenario.resources}

    def run(self) -> list[tuple[str, str]]:
        self._reject_reserved_values()
        self._ensure_admin()
        states: dict[str, str] = {}
        cache: dict[str, object] = {}  # pass-1 product views only
        for resource in self._scenario.resource_plan:
            states[_identity(resource)] = self._classify(resource, cache)
        report: list[tuple[str, str]] = []
        for resource in self._scenario.resource_plan:
            identity = _identity(resource)
            if states[identity] == "absent":
                self._create(resource)
                self._verify_readback(resource)
                status = "created"
            else:
                status = "unchanged"
            report.append((status, identity))
            # emitted as each resource is handled, so a failed run still shows
            # what it already created
            self._out(f"{status} {identity}")
            if resource.kind == "actor":
                self._ensure_actor_key(resource)
        created = sum(1 for status, _ in report if status == "created")
        self._out(f"summary: {created} created, {len(report) - created} unchanged")
        return report

    # --- pre-flight -------------------------------------------------------

    def _reject_reserved_values(self) -> None:
        for resource in self._scenario.resources:
            if resource.kind == "custom-field" and "---" in resource.data["values"]:
                raise ProvisionError(
                    f"custom-field {resource.name!r} declares the reserved value "
                    "'---' (Bugzilla's single-select placeholder); remove it from "
                    "the scenario")

    def _ensure_admin(self) -> None:
        key = self._keys.admin_key()
        if key is None:
            result = self._bridge.call("create-api-key", {"login": None})
            key = result["api_key"]
            self._keys.store_admin_key(key)
        self._bzr = self._bzr_factory(key)
        try:
            self._admin_login = self._bzr.whoami()
        except ProvisionError as exc:
            raise ProvisionError(
                f"stored admin key was rejected ({exc}). The fixture and the state "
                f"root disagree: remove {self._keys.admin_key_path()} to re-mint, or "
                "reset the fixture (CONFIRM_RESET=1 make reset)") from None

    def _ensure_actor_key(self, resource) -> None:
        if self._keys.actor_key(resource.name) is not None:
            return
        email = resource.data["email"]
        result = self._bridge.call("create-api-key", {"login": email})
        self._keys.store_actor_key(resource.name, result["api_key"])

    # --- pass 1: classify -------------------------------------------------

    def _classify(self, resource, cache) -> str:
        method = getattr(self, f"_classify_{resource.kind.replace('-', '_')}")
        return method(resource, cache)

    def _product_view(self, name: str, cache) -> object | None:
        if name not in cache:
            cache[name] = self._bzr.read(["product", "view"], positionals=[name])
        return cache[name]

    def _compare(self, identity, field, declared, observed, *, multi_step=False):
        if declared != observed:
            raise ProvisionConflictError(
                identity, field, declared, observed, multi_step=multi_step)

    def _classify_group(self, resource, cache) -> str:
        if resource.name in SYSTEM_GROUPS:
            return "unchanged"  # fixture furniture; never compared, never created
        payload = self._bzr.read(["group", "view"], positionals=[resource.name])
        if payload is None:
            return "absent"
        self._compare(_identity(resource), "description",
                      resource.data["description"],
                      payload.get("description") if isinstance(payload, dict) else None)
        return "unchanged"

    def _classify_actor(self, resource, cache) -> str:
        email = resource.data["email"]
        payload = self._bzr.read(
            ["user", "search", "--details"], positionals=[email])
        entry = None
        for row in _user_rows(payload):
            login = _login_of(row)
            if login is not None and login.lower() == email.lower():
                entry = row
                break
        if entry is None:
            return "absent"
        identity = _identity(resource)
        self._compare(identity, "display_name",
                      resource.data["display_name"], _display_of(entry))
        memberships = _name_set(entry.get("groups"))
        declared = {ref.name for ref in resource.data["groups"]}
        missing = declared - memberships
        if missing:
            raise ProvisionConflictError(
                identity, "groups", sorted(declared), sorted(memberships),
                multi_step=True)
        return "unchanged"

    def _classify_product(self, resource, cache) -> str:
        payload = self._product_view(resource.name, cache)
        if payload is None:
            return "absent"
        self._compare(_identity(resource), "description",
                      resource.data["description"],
                      payload.get("description") if isinstance(payload, dict) else None)
        return "unchanged"

    def _classify_component(self, resource, cache) -> str:
        product = resource.data["product"].name
        payload = self._bzr.read(
            ["component", "view"], positionals=[product, resource.name])
        if payload is None:
            return "absent"
        identity = _identity(resource)
        self._compare(identity, "description", resource.data["description"],
                      payload.get("description") if isinstance(payload, dict) else None)
        declared_assignee = resource.data["default_assignee"]
        if declared_assignee is not None:
            declared_email = self._actor_email(declared_assignee.name)
            observed = _login_of(payload.get("default_assignee"))
            if observed is None or observed.lower() != declared_email.lower():
                raise ProvisionConflictError(
                    identity, "default_assignee", declared_email, observed)
        return "unchanged"

    def _classify_version(self, resource, cache) -> str:
        return self._classify_product_child(resource, cache, "versions")

    def _classify_milestone(self, resource, cache) -> str:
        return self._classify_product_child(resource, cache, "milestones")

    def _classify_product_child(self, resource, cache, field) -> str:
        payload = self._product_view(resource.data["product"].name, cache)
        if payload is None:
            return "absent"  # absent parent classifies its children absent
        names = _name_set(payload.get(field) if isinstance(payload, dict) else [])
        return "unchanged" if resource.name in names else "absent"

    def _classify_custom_field(self, resource, cache) -> str:
        mapped = custom_field_name(resource.name)
        result = self._bridge.call("get-custom-field", {"name": mapped})
        if result is None:
            return "absent"
        identity = _identity(resource)
        self._compare(identity, "field_type",
                      resource.data["field_type"], result.get("field_type"))
        declared = set(resource.data["values"])
        observed = {v for v in result.get("values", []) if v != "---"}
        if declared != observed:
            raise ProvisionConflictError(
                identity, "values", sorted(declared), sorted(observed),
                multi_step=True)
        return "unchanged"

    def _classify_keyword(self, resource, cache) -> str:
        result = self._bridge.call("get-keyword", {"name": resource.name})
        if result is None:
            return "absent"
        self._compare(_identity(resource), "description",
                      resource.data["description"], result.get("description"))
        return "unchanged"

    def _classify_flag_type(self, resource, cache) -> str:
        result = self._bridge.call("get-flag-type", {"name": resource.name})
        if result is None:
            return "absent"
        identity = _identity(resource)
        self._compare(identity, "description",
                      resource.data["description"], result.get("description"))
        self._compare(identity, "target",
                      resource.data["target"], result.get("target"))
        declared = self._inclusion_pairs(resource)
        observed = {
            (pair.get("product"), pair.get("component"))
            for pair in result.get("inclusions", [])
            if isinstance(pair, dict)
        }
        if declared != observed:
            raise ProvisionConflictError(
                identity, "inclusions", sorted(declared, key=repr),
                sorted(observed, key=repr), multi_step=True)
        return "unchanged"

    # --- pass 2: create and read back ------------------------------------

    def _create(self, resource) -> None:
        method = getattr(self, f"_create_{resource.kind.replace('-', '_')}")
        method(resource)

    def _create_group(self, resource) -> None:
        self._bzr.write([
            "group", "create", f"--name={resource.name}",
            f"--description={resource.data['description']}"])

    def _create_actor(self, resource) -> None:
        email = resource.data["email"]
        self._bzr.write([
            "user", "create", f"--email={email}",
            f"--full-name={resource.data['display_name']}"])
        for ref in resource.data["groups"]:
            self._bzr.write([
                "group", "add-user", f"--group={ref.name}", f"--user={email}"])

    def _create_product(self, resource) -> None:
        self._bzr.write([
            "product", "create", f"--name={resource.name}",
            f"--description={resource.data['description']}"])

    def _create_component(self, resource) -> None:
        declared = resource.data["default_assignee"]
        assignee = (
            self._actor_email(declared.name) if declared is not None
            else self._admin_login)
        self._bzr.write([
            "component", "create", f"--product={resource.data['product'].name}",
            f"--name={resource.name}",
            f"--description={resource.data['description']}",
            f"--default-assignee={assignee}"])

    def _create_version(self, resource) -> None:
        self._bridge.call("create-version", {
            "product": resource.data["product"].name, "name": resource.name})

    def _create_milestone(self, resource) -> None:
        self._bridge.call("create-milestone", {
            "product": resource.data["product"].name, "name": resource.name})

    def _create_custom_field(self, resource) -> None:
        self._bridge.call("create-custom-field", {
            "name": custom_field_name(resource.name),
            "field_type": resource.data["field_type"],
            "values": list(resource.data["values"])})

    def _create_keyword(self, resource) -> None:
        self._bridge.call("create-keyword", {
            "name": resource.name, "description": resource.data["description"]})

    def _create_flag_type(self, resource) -> None:
        pairs = sorted(self._inclusion_pairs(resource), key=repr)
        self._bridge.call("create-flag-type", {
            "name": resource.name,
            "description": resource.data["description"],
            "target": resource.data["target"],
            "inclusions": [
                {"product": product, "component": component}
                for product, component in pairs]})

    def _verify_readback(self, resource) -> None:
        state = self._classify(resource, {})  # fresh reads; never the pass-1 cache
        if state != "unchanged":
            raise ProvisionError(
                f"{_identity(resource)} was created but the readback classified it "
                f"{state!r}; the fixture did not persist the declared definition")
        if resource.kind == "custom-field" and resource.data["field_type"] != "text":
            mapped = custom_field_name(resource.name)
            listed = self._bzr.read(["field", "list"], positionals=[mapped])
            observed = {v for v in _name_set(listed) if v != "---"}
            declared = set(resource.data["values"])
            if declared != observed:
                raise ProvisionError(
                    f"{_identity(resource)}: bzr field list {mapped} shows "
                    f"{sorted(observed)!r}, expected {sorted(declared)!r}")

    # --- helpers ----------------------------------------------------------

    def _actor_email(self, actor_name: str) -> str:
        return self._index[f"actor:{actor_name}"].data["email"]

    def _inclusion_pairs(self, resource) -> set[tuple[str | None, str | None]]:
        pairs: set[tuple[str | None, str | None]] = set()
        for ref in resource.data["products"]:
            pairs.add((ref.name, None))
        for ref in resource.data["components"]:
            component = self._index[f"component:{ref.name}"]
            pairs.add((component.data["product"].name, ref.name))
        if not pairs:
            pairs.add((None, None))
        return pairs
