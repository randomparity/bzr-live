# Implementation plan — settable bug groups

**Goal.** Let a scenario declare which products a bug group is settable on, and have
provisioning establish that mapping through the container-local admin bridge.

**Architecture.** The `group` resource kind gains an optional `products` list of product
references, validated in `src/bzr_live/scenario/loader.py` exactly as `flag-type`'s
`products` already is. Two new allowlisted operations on `containers/bugzilla/bridge.pl` —
`set-group-control` and `get-group-control` — reach `Bugzilla::Product`'s object-layer group
control API. `src/bzr_live/provision/executor.py` calls the setter when it creates a group
and the getter when it classifies one, refusing before any mutation where the declaration
cannot hold.

**Tech stack.** Python 3.11+ via `uv`, no runtime dependencies. Perl 5 inside the pinned
Bugzilla image. `unittest` for tests. Docker Compose for the fixture.

Design: [spec](../specs/2026-09-02-settable-bug-group-design.md),
[ADR 0013](../../adr/0013-scenario-declared-bug-group-product-controls.md).
Issue: [#34](https://github.com/randomparity/bzr-live/issues/34).

Expected implementation size: 250–330 changed lines (M) — derived from the file map below: four source files at roughly 100 lines total, two JSON fixtures at 4, one findings entry at 30, and three test files at roughly 150.

## Global constraints

- **Never raw SQL in the bridge.** `containers/bugzilla/bridge.pl:5`: *"Bugzilla object
  layer only — never raw SQL."* Reach the end state through `Bugzilla::Product`.
- **Never silently substitute.** Where a declaration cannot be executed, refuse it as a
  precondition and name the reason (`AGENTS.md`, "Purpose: prove `bzr`").
- **Bugzilla is pinned** at `644c66f45ce0b1b2746a31a061fbd96886278225`
  (`containers/bugzilla/Dockerfile`). Every Bugzilla line citation is against that SHA.
- **`bzr` is used at the documented floor**, `0.8.3-dev (63abb94e)`. Use the binary at
  `/Volumes/Source Code Volume/src/bzr/target/release/bzr`. Raising the floor belongs to
  issue #35 and is out of scope here.
- **`format_version` stays 1.** `products` is optional on `group`; a scenario omitting it
  parses and provisions exactly as before.
- **Control values are fixed**: `entry = 0`, `membercontrol = CONTROLMAPSHOWN`,
  `othercontrol = CONTROLMAPSHOWN`. No privilege-granting column is set (ADR 0013).
- **Do not touch**, they belong to concurrent issues: `src/bzr_live/replay/actions.py`,
  `src/bzr_live/verify/expected.py`, `.github/workflows/**`, `tests/smoke_scenario.sh`,
  `scripts/lifecycle`, `tests/lifecycle_test.sh`, `tests/test_smoke_trap_status.py`,
  `README.md`, `docs/adr/0008-*`.
- **Guardrails**, run bare — no pipes, no `|| true`, no `>/dev/null`:
  `make check`, `make test`, `make replay-smoke`, `make smoke`, `make checkpoint-smoke`.
- Python style follows the files being edited: `from __future__ import annotations`, 100-char
  lines, no new dependencies.

## File map

| File | Change | Answerable for |
|---|---|---|
| `src/bzr_live/scenario/loader.py` | modify | Parsing and validating `group.products` |
| `containers/bugzilla/bridge.pl` | modify | The two object-layer group-control operations |
| `src/bzr_live/provision/adapters.py` | modify | Allowlisting them on `BridgeClient` |
| `src/bzr_live/provision/executor.py` | modify | Preflight refusal, create, classify |
| `scenarios/smoke/resources.json` | modify | Declaring the `restricted` bug group |
| `tests/test_smoke_scenario.py` | modify | The pinned digest |
| `tests/test_scenario_resources.py` | modify | Contract tests for `products` |
| `tests/test_provision.py` | modify | Provisioner tests and the bridge fake |
| `docs/bzr-findings.md` | modify | Finding G11 |

## Task 1 — the scenario contract accepts `group.products`

**Files:** modifies `src/bzr_live/scenario/loader.py`; tests in
`tests/test_scenario_resources.py`.

**Interfaces.** Consumes nothing from earlier tasks. Later tasks rely on:
`PlannedResource.data["products"]` being a `tuple[Reference, ...]` of `product:` references
on every `group` resource — an empty tuple when the field is omitted — and those references
also appearing in `PlannedResource.dependencies`, so `ValidatedScenario.resource_plan` orders
the group after each product it names.

**Where it fits.** Everything downstream reads this field; nothing works without it.

### Step 1.1 — write the failing tests

Append these four methods to `ScenarioResourceTests` in `tests/test_scenario_resources.py`.
They use the class's existing `write_json`, `assert_invalid` and `self.root` members and the
module-level `load_scenario`, `Reference` and `ScenarioValidationError` imports, all of which
are already present — add no imports and change no existing test.

```python
    def test_group_products_resolve_and_order_the_plan(self) -> None:
        self.write_json("resources.json", {"format_version": 1, "resources": [
            {"kind": "group", "name": "restricted", "description": "Restricted",
             "products": [{"ref": "product:checkout"}]},
            {"kind": "product", "name": "checkout", "description": "Checkout"},
        ]})
        scenario = load_scenario(self.root)
        group = {resource.name: resource for resource in scenario.resources}["restricted"]
        self.assertEqual(group.data["products"], (Reference("product", "checkout"),))
        self.assertIn(Reference("product", "checkout"), group.dependencies)
        self.assertEqual(
            [resource.name for resource in scenario.resource_plan],
            ["checkout", "restricted"])

    def test_group_without_products_carries_an_empty_tuple(self) -> None:
        self.write_json("resources.json", {"format_version": 1, "resources": [
            {"kind": "group", "name": "plain", "description": "Plain"}]})
        scenario = load_scenario(self.root)
        self.assertEqual(scenario.resources[0].data["products"], ())

    def test_group_products_must_name_a_declared_product(self) -> None:
        self.write_json("resources.json", {"format_version": 1, "resources": [
            {"kind": "group", "name": "restricted", "description": "Restricted",
             "products": [{"ref": "product:absent"}]}]})
        message = self.assert_invalid("resources.json", "$.resources[0]")
        self.assertIn("missing dependency product:absent", message)

    def test_group_products_reject_a_duplicate_reference(self) -> None:
        self.write_json("resources.json", {"format_version": 1, "resources": [
            {"kind": "product", "name": "checkout", "description": "Checkout"},
            {"kind": "group", "name": "restricted", "description": "Restricted",
             "products": [{"ref": "product:checkout"}, {"ref": "product:checkout"}]}]})
        message = self.assert_invalid("resources.json", "$.resources[1].products[1]")
        self.assertIn("duplicate reference", message)
```

### Step 1.2 — run them and confirm they fail

```sh
uv run --python 3.11 python -m unittest tests.test_scenario_resources -v
```

Expect four failures. The three that declare `products` fail with an unexpected-field
error naming `$.resources[N].products`; the empty-tuple test fails with
`KeyError: 'products'`.

### Step 1.3 — allow the field

In `src/bzr_live/scenario/loader.py`, change the `group` row of `_RESOURCE_FIELDS` from

```python
    "group": ({"kind", "name", "description"}, set()),
```

to

```python
    "group": ({"kind", "name", "description"}, {"products"}),
```

### Step 1.4 — parse it

In `_parse_resource`, replace

```python
    if kind in {"group", "product", "keyword"}:
        data["description"] = _text(obj["description"], source, f"{field}.description")
    elif kind == "actor":
```

with

```python
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
```

No change is needed to `all_fields` at the top of `_parse_resource`: it is the union of every
kind's fields, and `flag-type` already contributes `products`.

### Step 1.5 — run them and confirm they pass

```sh
uv run --python 3.11 python -m unittest tests.test_scenario_resources -v
```

Expect `OK` with every test in the file passing.

### Step 1.6 — commit

```sh
git add src/bzr_live/scenario/loader.py tests/test_scenario_resources.py
git commit -m "feat: let a group declare the products it is settable on"
```

**Acceptance criteria.** A `group` may carry `products`; each entry must resolve to a
declared product; duplicates are rejected; omission yields `()`; the resource plan orders the
group after its products; every other kind is unchanged.

## Task 2 — the bridge sets and reads product group controls

**Files:** modifies `containers/bugzilla/bridge.pl` and
`src/bzr_live/provision/adapters.py`; tests in `tests/test_provision.py`.

**Interfaces.** Consumes nothing from Task 1. Task 3 relies on:
`BridgeClient.call("set-group-control", {"product": str, "group": str})` returning
`{"product": str, "group": str, "settable": True}`, and
`BridgeClient.call("get-group-control", {"product": str, "group": str})` returning `None`
when the group is not mapped to that product, otherwise a dict carrying at least
`"settable": bool`.

**Where it fits.** It is the only route to the end state; nothing else in this repository may
write `group_control_map`.

### Step 2.1 — write the failing test

Append to `tests/test_provision.py`, inside the class that exercises `BridgeClient` (the one
holding the existing `OPERATIONS` and reply-shape tests):

```python
    def test_group_control_operations_are_allowlisted(self) -> None:
        self.assertIn("set-group-control", adapters.BridgeClient.OPERATIONS)
        self.assertIn("get-group-control", adapters.BridgeClient.OPERATIONS)
```

### Step 2.2 — run it and confirm it fails

```sh
uv run --python 3.11 python -m unittest tests.test_provision -v
```

Expect one failure: `'set-group-control' not found in frozenset({...})`.

### Step 2.3 — allowlist them on the client

In `src/bzr_live/provision/adapters.py`, change `BridgeClient.OPERATIONS` from

```python
    OPERATIONS = frozenset({
        "create-version", "create-milestone", "create-custom-field",
        "create-keyword", "create-flag-type", "create-api-key",
        "get-custom-field", "get-keyword", "get-flag-type",
    })
```

to

```python
    OPERATIONS = frozenset({
        "create-version", "create-milestone", "create-custom-field",
        "create-keyword", "create-flag-type", "create-api-key",
        "get-custom-field", "get-keyword", "get-flag-type",
        "set-group-control", "get-group-control",
    })
```

In the same file, extend the comment above `BOUNDARIES` so the table's scope stays honest.
Replace

```python
# Resource kind -> mutation boundary. Fixed by issue #4's implementation boundaries.
```

with

```python
# Resource kind -> mutation boundary. Fixed by issue #4's implementation boundaries.
# A group's optional product-control mapping is a bridge sub-step of a "bzr" kind
# (ADR 0013); the table records each kind's own boundary, not its sub-steps.
```

### Step 2.4 — run it and confirm it passes

```sh
uv run --python 3.11 python -m unittest tests.test_provision -v
```

Expect `OK`.

### Step 2.5 — add the operations to the bridge

In `containers/bugzilla/bridge.pl`, add one line to the import block so it reads

```perl
use Bugzilla::Field::Choice;
use Bugzilla::FlagType;
use Bugzilla::Group;
use Bugzilla::Keyword;
```

The only new line is `use Bugzilla::Group;`; the block stays alphabetical.

Change the allowlist from

```perl
my %OPERATIONS = map { $_ => 1 } qw(
  create-version create-milestone create-custom-field create-keyword
  create-flag-type create-api-key get-custom-field get-keyword get-flag-type
);
```

to

```perl
my %OPERATIONS = map { $_ => 1 } qw(
  create-version create-milestone create-custom-field create-keyword
  create-flag-type create-api-key get-custom-field get-keyword get-flag-type
  set-group-control get-group-control
);
```

Then, in `sub dispatch`, immediately before the final `die "unreachable operation\n";`, insert:

```perl
  if ($operation eq 'set-group-control') {
    my $product = product_of($request->{product});
    my $group   = Bugzilla::Group->check({name => $request->{group}});
    # Settability needs only these two columns: group_is_settable reads
    # groups_mandatory/groups_available, which select on membercontrol and
    # othercontrol alone (Product.pm:659-748 at the pinned SHA). The privilege
    # columns -- canedit, editbugs, canconfirm -- would grant the group's members
    # product rights no scenario declared, so they stay unset (ADR 0013).
    $product->set_group_controls($group, {
      entry         => 0,
      membercontrol => CONTROLMAPSHOWN,
      othercontrol  => CONTROLMAPSHOWN,
    });
    $product->update();
    # re-read, so a write that did not take cannot report success
    my $fresh = product_of($request->{product});
    unless ($fresh->group_is_settable($group)) {
      die "group " . $group->name . " is still not settable on product "
        . $fresh->name . "\n";
    }
    return {
      product  => $fresh->name,
      group    => $group->name,
      settable => JSON::XS::true,
    };
  }
  if ($operation eq 'get-group-control') {
    my $product = product_of($request->{product});
    my $group   = Bugzilla::Group->check({name => $request->{group}});
    # group_controls without $full_data joins on product_id, so an unmapped group
    # is simply absent from the hash (Product.pm:604-637 at the pinned SHA).
    my $controls = $product->group_controls->{$group->id};
    return undef unless $controls;
    return {
      product       => $product->name,
      group         => $group->name,
      entry         => $controls->{entry},
      membercontrol => $controls->{membercontrol},
      othercontrol  => $controls->{othercontrol},
      settable      => $product->group_is_settable($group)
                       ? JSON::XS::true : JSON::XS::false,
    };
  }
```

`CONTROLMAPSHOWN` is already in scope: `bridge.pl` does `use Bugzilla::Constants;`, which
exports it (`Bugzilla/Constants.pm:35`, `:257`).

### Step 2.6 — verify the bridge parses

```sh
make check
```

Expect it to pass. `make check` runs `bash -n`, `shellcheck`, `python -m compileall` and
`docker compose config`; it does not lint Perl, so also run

```sh
perl -c containers/bugzilla/bridge.pl
```

which will report `Can't locate Bugzilla.pm` on the host — that is expected, the modules only
exist inside the image, and it still surfaces a syntax error before the module load. A clean
syntax error report is the pass condition; the real check is Task 5's live run.

### Step 2.7 — commit

```sh
git add containers/bugzilla/bridge.pl src/bzr_live/provision/adapters.py tests/test_provision.py
git commit -m "feat: add object-layer group-control operations to the bridge"
```

**Acceptance criteria.** Both operations are allowlisted on the client and in the bridge;
both resolve their arguments with `->check`; the setter uses
`Bugzilla::Product::set_group_controls` and `update()` and asserts settability afterwards;
the getter returns `undef` for an unmapped group; no SQL statement appears in the diff.

## Task 3 — provisioning applies and verifies the mapping

**Files:** modifies `src/bzr_live/provision/executor.py`; tests in `tests/test_provision.py`.

**Interfaces.** Consumes `PlannedResource.data["products"]` from Task 1 and both bridge
operations from Task 2. Later tasks rely on: a group with unsatisfiable `products` raising
`ProvisionError` from `Provisioner._reject_reserved_values` before any mutation, and a group
whose declared products are not all settable raising
`ProvisionConflictError(identity, "products", declared, observed, multi_step=True)`.

**Where it fits.** It is the behaviour issue #34 asks for; Tasks 1 and 2 only make it
expressible.

### Step 3.1 — write the failing tests

Append to `tests/test_provision.py`'s `ExecutorTests` class:

```python
    def test_system_group_declaring_products_is_refused_before_mutation(self) -> None:
        self.scenario = self._scenario_from([
            {"kind": "product", "name": "q4-checkout", "description": "Checkout"},
            {"kind": "group", "name": "editbugs", "description": "Edit bugs",
             "products": [{"ref": "product:q4-checkout"}]}])
        bzr = _FakeBzr()
        bridge = _FakeBridge(bzr)
        with self.assertRaises(ProvisionError) as ctx:
            self._provisioner(bzr, bridge).run()
        message = str(ctx.exception)
        self.assertIn("editbugs", message)
        self.assertIn("isbuggroup", message)
        self.assertEqual(bzr.writes, [])
        self.assertEqual(bridge.calls, [])

    def test_creating_a_group_maps_every_declared_product(self) -> None:
        self.scenario = self._scenario_from([
            {"kind": "product", "name": "q4-checkout", "description": "Checkout"},
            {"kind": "product", "name": "q4-billing", "description": "Billing"},
            {"kind": "group", "name": "q4-secret", "description": "Secret",
             "products": [{"ref": "product:q4-checkout"},
                          {"ref": "product:q4-billing"}]}])
        bzr = _FakeBzr()
        bridge = _FakeBridge(bzr)
        report = self._provisioner(bzr, bridge).run()
        self.assertEqual(dict((i, s) for s, i in report)["group:q4-secret"], "created")
        mapped = sorted(
            payload["product"] for op, payload in bridge.calls
            if op == "set-group-control")
        self.assertEqual(mapped, ["q4-billing", "q4-checkout"])

    def test_rerun_over_a_mapped_group_is_unchanged_and_writes_nothing(self) -> None:
        self.scenario = self._scenario_from([
            {"kind": "product", "name": "q4-checkout", "description": "Checkout"},
            {"kind": "group", "name": "q4-secret", "description": "Secret",
             "products": [{"ref": "product:q4-checkout"}]}])
        bzr = _FakeBzr()
        bridge = _FakeBridge(bzr)
        self._provisioner(bzr, bridge).run()
        rerun_bridge = _FakeBridge(bzr, dict(bridge.state))
        report = self._provisioner(bzr, rerun_bridge).run()
        self.assertEqual([status for status, _ in report], ["unchanged", "unchanged"])
        self.assertEqual(rerun_bridge.calls_of("set-group-control"), [])
        # the rerun must actually have asked, or "unchanged" proves nothing
        self.assertEqual(
            [payload["product"]
             for payload in rerun_bridge.calls_of("get-group-control")],
            ["q4-checkout"])

    def test_group_present_without_its_mapping_is_a_multi_step_conflict(self) -> None:
        self.scenario = self._scenario_from([
            {"kind": "product", "name": "q4-checkout", "description": "Checkout"},
            {"kind": "group", "name": "q4-secret", "description": "Secret",
             "products": [{"ref": "product:q4-checkout"}]}])
        bzr = _FakeBzr({
            "group:q4-secret": {"name": "q4-secret", "description": "Secret"},
            "product:q4-checkout": {
                "name": "q4-checkout", "description": "Checkout",
                "versions": [{"name": "unspecified"}],
                "milestones": [{"name": "---"}]}})
        bridge = _FakeBridge(bzr)  # no group-control state
        with self.assertRaises(ProvisionConflictError) as ctx:
            self._provisioner(bzr, bridge).run()
        message = str(ctx.exception)
        self.assertIn("group:q4-secret", message)
        self.assertIn("products", message)
        self.assertIn("CONFIRM_RESET=1 make reset", message)
        self.assertEqual(bzr.writes, [])
```

Extend `_FakeBridge.call` so it models both operations. Insert these branches immediately
before its final `raise AssertionError`:

```python
        if operation == "set-group-control":
            key = f"group-control:{payload['product']}:{payload['group']}"
            self.state[key] = {
                "product": payload["product"], "group": payload["group"],
                "entry": 0, "membercontrol": 1, "othercontrol": 1, "settable": True}
            return dict(self.state[key])
        if operation == "get-group-control":
            return self.state.get(
                f"group-control:{payload['product']}:{payload['group']}")
```

and add this helper method to `_FakeBridge`:

```python
    def calls_of(self, operation):
        return [payload for op, payload in self.calls if op == operation]
```

### Step 3.2 — run them and confirm they fail

```sh
uv run --python 3.11 python -m unittest tests.test_provision -v
```

Expect exactly four failures:

- the refusal test — no `ProvisionError` is raised;
- the create test — `mapped` is `[]`, not `["q4-billing", "q4-checkout"]`;
- the rerun test — the `get-group-control` list is `[]`, not `["q4-checkout"]`. Its
  `unchanged` and empty-`set-group-control` assertions pass trivially before the change,
  which is why the test also asserts the rerun asked;
- the conflict test — no `ProvisionConflictError` is raised.

### Step 3.3 — refuse an unsatisfiable declaration

In `src/bzr_live/provision/executor.py`, replace the body of `_reject_reserved_values`

```python
    def _reject_reserved_values(self) -> None:
        for resource in self._scenario.resources:
            if resource.kind == "custom-field" and "---" in resource.data["values"]:
                raise ProvisionError(
                    f"custom-field {resource.name!r} declares the reserved value "
                    "'---' (Bugzilla's single-select placeholder); remove it from "
                    "the scenario")
```

with

```python
    def _reject_reserved_values(self) -> None:
        for resource in self._scenario.resources:
            if resource.kind == "custom-field" and "---" in resource.data["values"]:
                raise ProvisionError(
                    f"custom-field {resource.name!r} declares the reserved value "
                    "'---' (Bugzilla's single-select placeholder); remove it from "
                    "the scenario")
            if (resource.kind == "group" and resource.data["products"]
                    and resource.name in SYSTEM_GROUPS):
                raise ProvisionError(
                    f"group {resource.name!r} declares products, but Bugzilla's "
                    "checksetup groups carry isbuggroup = 0 and only a bug group can "
                    "be made settable on a product; declare a group the fixture "
                    "creates instead")
```

### Step 3.4 — map the products on create

Replace `_create_group`

```python
    def _create_group(self, resource) -> None:
        self._bzr.write([
            "group", "create", f"--name={resource.name}",
            f"--description={resource.data['description']}"])
```

with

```python
    def _create_group(self, resource) -> None:
        self._bzr.write([
            "group", "create", f"--name={resource.name}",
            f"--description={resource.data['description']}"])
        for ref in resource.data["products"]:
            self._bridge.call(
                "set-group-control", {"product": ref.name, "group": resource.name})
```

### Step 3.5 — compare the mapping on classify

Replace `_classify_group`

```python
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
```

with

```python
    def _classify_group(self, resource, cache) -> str:
        if resource.name in SYSTEM_GROUPS:
            return "unchanged"  # fixture furniture; never compared, never created
        payload = self._bzr.read(["group", "view"], positionals=[resource.name])
        if payload is None:
            return "absent"
        identity = _identity(resource)
        self._compare(identity, "description",
                      resource.data["description"],
                      payload.get("description") if isinstance(payload, dict) else None)
        declared = {ref.name for ref in resource.data["products"]}
        settable = {name for name in declared if self._group_is_settable(resource, name)}
        if settable != declared:
            # the group exists but its mapping does not: the actor path's multi-step
            # case, carrying the same reset instruction
            raise ProvisionConflictError(
                identity, "products", sorted(declared), sorted(settable),
                multi_step=True)
        return "unchanged"

    def _group_is_settable(self, resource, product: str) -> bool:
        result = self._bridge.call(
            "get-group-control", {"product": product, "group": resource.name})
        return bool(result.get("settable")) if isinstance(result, dict) else False
```

### Step 3.6 — run them and confirm they pass

```sh
uv run --python 3.11 python -m unittest tests.test_provision -v
```

Expect `OK`, with every pre-existing test in the file still passing — in particular
`test_plan_order_is_respected` (13 created) and
`test_identical_rerun_is_all_unchanged_with_no_writes` (13 unchanged), which use a fixture
whose one group declares no `products`.

### Step 3.7 — run the whole unit suite

```sh
make test
```

Expect `OK`, at 387 tests plus the ones added here.

### Step 3.8 — commit

```sh
git add src/bzr_live/provision/executor.py tests/test_provision.py
git commit -m "feat: provision a group's declared product controls"
```

**Acceptance criteria.** A system group declaring `products` is refused with a message naming
the group and `isbuggroup`, with no write and no bridge call. Creating a group calls
`set-group-control` once per declared product. A rerun over a mapped group reports
`unchanged` and issues no write. A group present without its mapping raises
`ProvisionConflictError` naming `products` and carrying the reset hint. A group declaring no
products behaves exactly as before.

## Task 4 — the smoke scenario declares a settable bug group

**Files:** modifies `scenarios/smoke/resources.json`, `tests/test_smoke_scenario.py`.

**Interfaces.** Consumes Task 1's contract. Task 5 verifies this live.

**Where it fits.** It is what makes the capability exist in the fixture the guardrails run.

### Step 4.1 — declare the group

In `scenarios/smoke/resources.json`, after the `canconfirm` group line, add:

```json
  {"kind": "group", "name": "restricted", "description": "Restricted-visibility bugs",
   "products": [{"ref": "product:checkout"}, {"ref": "product:billing"}]},
```

and change the `admin-ops` actor from

```json
  {"kind": "actor", "name": "admin-ops", "email": "admin-ops@example.test",
   "display_name": "Avery Ops",
   "groups": [{"ref": "group:admin"}, {"ref": "group:editbugs"}, {"ref": "group:canconfirm"}]},
```

to

```json
  {"kind": "actor", "name": "admin-ops", "email": "admin-ops@example.test",
   "display_name": "Avery Ops",
   "groups": [{"ref": "group:admin"}, {"ref": "group:editbugs"},
              {"ref": "group:canconfirm"}, {"ref": "group:restricted"}]},
```

A bug restricted to a group with no member is invisible to the actor that restricted it, so
the fixture needs at least one member for the capability to be usable (ADR 0013).

### Step 4.2 — read the new digest

```sh
uv run --python 3.11 python -c "from bzr_live.scenario import load_scenario; print(load_scenario('scenarios/smoke').digest)"
```

Expect a 64-character hex digest different from the pinned one. If the command instead raises
`ScenarioValidationError`, the JSON edit is wrong — fix it before continuing.

### Step 4.3 — pin it

In `tests/test_smoke_scenario.py`, replace the value of `EXPECTED_DIGEST` with the digest
step 4.2 printed. Leave the comment above it unchanged; it already says why this is a
deliberate two-file change.

### Step 4.4 — run the suite

```sh
make test
```

Expect `OK`. `test_digest_matches_the_pinned_value` passes with the new value, and
`test_twenty_bugs_across_two_products` is unaffected because no event changed.

### Step 4.5 — commit

```sh
git add scenarios/smoke/resources.json tests/test_smoke_scenario.py
git commit -m "feat: declare a settable bug group in the smoke scenario"
```

**Acceptance criteria.** `scenarios/smoke` declares one bug group mapped to both products
with one member; the pinned digest matches; no event changed.

## Task 5 — prove it live, and record the `bzr` gap

**Files:** modifies `docs/bzr-findings.md`. No source change.

**Interfaces.** Consumes everything above. Produces nothing later tasks read.

**Where it fits.** `make test` proves the fakes agree with the code; only the Docker fixture
proves the Perl is right. AGENTS.md requires the `bzr` gap be recorded.

### Step 5.1 — bring up a fresh fixture and provision it

From the worktree root, with the floor binary:

```sh
make up
```

Expect both containers healthy. Then

```sh
BZR_LIVE_BZR="/Volumes/Source Code Volume/src/bzr/target/release/bzr" make smoke
```

Expect the provisioning report to include `created group:restricted` and the run to finish
without a `ProvisionError`. A `bridge set-group-control failed: ...` message means the Perl
is wrong; read the named Bugzilla error before changing anything.

### Step 5.2 — confirm the end state directly

Invoke the bridge exactly as `src/bzr_live/provision/__main__.py:14-21` does, from the
worktree root:

```sh
ROOT=$(pwd -P)
PROJECT=$(uv run --python 3.11 python -c "from bzr_live.provision.adapters import compose_project_name; import os, sys; print(compose_project_name(os.path.realpath(sys.argv[1])))" "$ROOT")
printf '%s' '{"product":"checkout","group":"restricted"}' | docker compose \
  --project-name "$PROJECT" --project-directory "$ROOT" --file "$ROOT/compose.yaml" \
  exec -T --user www-data bugzilla bzr-live-bridge get-group-control
```

Expect one JSON line whose `result` carries `"settable":true`, `"membercontrol":1` and
`"othercontrol":1`. Repeat with `"product":"billing"` and expect the same.

### Step 5.3 — confirm the rerun is idempotent

```sh
BZR_LIVE_BZR="/Volumes/Source Code Volume/src/bzr/target/release/bzr" make smoke
```

Expect `unchanged group:restricted` and no `set-group-control` failure.

### Step 5.4 — record finding G11

In `docs/bzr-findings.md`, add a row to the summary table after the `D9` row:

```markdown
| [G11](#g11) | gap | No `bzr` command maps a bug group to a product, so no group is settable without the admin bridge | — |
```

and add this section at the end of the file:

```markdown
---

## G11

**No `bzr` surface maps a bug group to a product.** *Read from source at `63abb94e`, and
observed against the fixture.*

For Bugzilla to accept a bug's `groups` value, the group must be an active bug group that
`group_control_map` lists as available or mandatory for the bug's product
(`Bugzilla/Product.pm:740-748` at the pinned image SHA `644c66f4`). Nothing in `bzr` writes
that mapping. `bzr product update` takes `--description`, `--default-milestone` and
`--is-open` (`src/cli/product.rs:118-133`); `bzr group` offers `add-user`, `remove-user`,
`list-users`, `view`, `create` and `update` (`src/cli/group.rs`), none of which mention a
product. `bzr group create` does produce a usable bug group — it sends `is_active: true`
(`src/commands/group/create.rs:63-70`) and Bugzilla's `WebService::Group::create` forces
`isbuggroup => 1` — but the group stays unsettable on every product.

Observed consequence before this was addressed: `PUT /rest/bug/<id> {"groups":{"add":[...]}}`
returned Bugzilla error 120 for a system group, a freshly created group, and a nonexistent
name alike (issue #34's measurement).

**Class: gap.** Product group controls are an administrative surface `bzr` does not model,
in the same family as versions, milestones and flag types — all of which this fixture already
reaches through the container-local admin bridge (ADR 0004).

**Upstream.** Not filed; recording only is what the operator authorized.

**What the fixture does.** [ADR 0013](adr/0013-scenario-declared-bug-group-product-controls.md)
adds `set-group-control` and `get-group-control` to that bridge, over
`Bugzilla::Product`'s object layer. The gap is not routed around: a scenario declares the
mapping, and provisioning refuses before mutating anything when it cannot be established.
```

### Step 5.5 — run the remaining guardrails

```sh
make check
```
Expect it to pass.

```sh
BZR_LIVE_BZR="/Volumes/Source Code Volume/src/bzr/target/release/bzr" make replay-smoke
```
Expect the probe report to print and the script to exit 0.

```sh
BZR_LIVE_BZR="/Volumes/Source Code Volume/src/bzr/target/release/bzr" make checkpoint-smoke
```
Expect a save/restore round trip that exits 0.

### Step 5.6 — tear down and commit

```sh
make down
git add docs/bzr-findings.md
git commit -m "docs: record G11, no bzr surface maps a bug group to a product"
```

**Acceptance criteria.** A live fixture provisions `group:restricted` as settable on both
products; a rerun reports it unchanged; `docs/bzr-findings.md` carries G11 with its `bzr`
source citation, its observed behaviour, and a defect-or-gap verdict; all five guardrails
pass.

## Rollback

Every task is one commit on `feat/settable-bug-group-34`; nothing migrates persistent state
outside the fixture. A fixture left in a bad state is discarded with `make down` and
`CONFIRM_RESET=1 make reset`, which is what AGENTS.md prescribes for a disposable fixture.

## Deferrals carried from design review

None recorded at authoring time. Any deferral the design review disposes of is appended here
with its owning record path or tracker issue before implementation begins.
