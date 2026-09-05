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
Bugzilla image. `unittest`. Docker Compose.

Design: [spec](../specs/2026-09-02-settable-bug-group-design.md),
[ADR 0013](../../adr/0013-scenario-declared-bug-group-product-controls.md).
Issue: [#34](https://github.com/randomparity/bzr-live/issues/34).

Expected implementation size: 250–330 changed lines (M) — from the file map below: four source files at ~95 lines, two JSON fixtures at ~6, one findings entry at ~32, three test files at ~120.

## Global constraints

- **Never raw SQL in the bridge.** `containers/bugzilla/bridge.pl:5`: *"Bugzilla object
  layer only — never raw SQL."*
- **Never silently substitute.** Where a declaration cannot be executed, refuse it as a
  precondition and name the reason (`AGENTS.md`, "Purpose: prove `bzr`").
- **Bugzilla is pinned** at `644c66f45ce0b1b2746a31a061fbd96886278225`
  (`containers/bugzilla/Dockerfile`); every Bugzilla citation is against that SHA.
- **`bzr` at the documented floor** `0.8.3-dev (63abb94e)`:
  `/Volumes/Source Code Volume/src/bzr/target/release/bzr`. Raising it is issue #35's.
- **`format_version` stays 1.** `products` is optional; a scenario omitting it is unaffected.
- **Control values are fixed**: `entry = 0`, `membercontrol = CONTROLMAPSHOWN`,
  `othercontrol = CONTROLMAPSHOWN`, no privilege column (ADR 0013).
- **Do not touch** (concurrent issues own them): `src/bzr_live/replay/actions.py`,
  `src/bzr_live/verify/expected.py`, `.github/workflows/**`, `tests/smoke_scenario.sh`,
  `scripts/lifecycle`, `tests/lifecycle_test.sh`, `tests/test_smoke_trap_status.py`,
  `README.md`, `docs/adr/0008-*`.
- **Guardrails, run bare** — no pipes, no `|| true`, no `>/dev/null`: `make check`,
  `make test`, `make replay-smoke`, `make smoke`, `make checkpoint-smoke`.
- Match the style of each file edited: `from __future__ import annotations`, 100-char lines,
  no new dependencies.

## File map

| File | Answerable for |
|---|---|
| `src/bzr_live/scenario/loader.py` | Parsing and validating `group.products` |
| `containers/bugzilla/bridge.pl` | The two object-layer group-control operations |
| `src/bzr_live/provision/adapters.py` | Allowlisting them on `BridgeClient` |
| `src/bzr_live/provision/executor.py` | Preflight refusal, create, classify |
| `scenarios/smoke/resources.json` | Declaring the `restricted` bug group |
| `tests/test_smoke_scenario.py` | The pinned digest |
| `tests/test_scenario_resources.py` | Contract tests for `products` |
| `tests/test_provision.py` | Provisioner tests and the bridge fake |
| `docs/bzr-findings.md` | Finding G11 |

## Task 1 — the scenario contract accepts `group.products`

**Files:** `src/bzr_live/scenario/loader.py`, `tests/test_scenario_resources.py`.

**Interfaces.** Provides, for later tasks: `PlannedResource.data["products"]` is a
`tuple[Reference, ...]` of `product:` references on every `group` — `()` when omitted — and
those references also appear in `PlannedResource.dependencies`, so
`ValidatedScenario.resource_plan` orders the group after each product it names.

### 1.1 Write the failing tests

Append these methods to `ScenarioResourceTests` in `tests/test_scenario_resources.py`. They
use its existing `write_json`, `assert_invalid` and `self.root`, and the module's existing
`load_scenario`, `Reference`, `ScenarioValidationError` imports. Add no imports; change no
existing test.

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
        self.assertEqual(load_scenario(self.root).resources[0].data["products"], ())

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

### 1.2 Confirm they fail

`uv run --python 3.11 python -m unittest tests.test_scenario_resources -v` — four failures:
the three declaring `products` raise an unexpected-field error naming
`$.resources[N].products`; the empty-tuple test raises `KeyError: 'products'`.

### 1.3 Allow and parse the field

In `_RESOURCE_FIELDS`, the `group` row's optional set changes from `set()` to
`{"products"}`, giving `"group": ({"kind", "name", "description"}, {"products"}),`.

In `_parse_resource`, the first branch of the kind chain currently sets only `description`
for `group`, `product` and `keyword`. Extend it, leaving the `elif kind == "actor":` that
follows untouched:

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
```

`all_fields` at the top of `_parse_resource` needs no change: it is the union over every
kind, and `flag-type` already contributes `products`.

### 1.4 Confirm they pass, then commit

`uv run --python 3.11 python -m unittest tests.test_scenario_resources -v` — `OK`, every
test in the file passing.

```sh
git add src/bzr_live/scenario/loader.py tests/test_scenario_resources.py
git commit -m "feat: let a group declare the products it is settable on"
```

**Acceptance.** A `group` may carry `products`; entries must resolve to declared products;
duplicates are rejected; omission yields `()`; the plan orders the group after its products;
no other kind changes.

## Task 2 — the bridge sets and reads product group controls

**Files:** `containers/bugzilla/bridge.pl`, `src/bzr_live/provision/adapters.py`,
`tests/test_provision.py`.

**Interfaces.** Provides, for Task 3:
`BridgeClient.call("set-group-control", {"product": str, "group": str})` returns
`{"product": str, "group": str, "settable": True}`;
`BridgeClient.call("get-group-control", {"product": str, "group": str})` returns `None` when
the group is not mapped to that product, otherwise a dict carrying at least
`"settable": bool`.

### 2.1 Write the failing test

Append to the `tests/test_provision.py` class holding the existing `BridgeClient` tests:

```python
    def test_group_control_operations_are_allowlisted(self) -> None:
        self.assertIn("set-group-control", adapters.BridgeClient.OPERATIONS)
        self.assertIn("get-group-control", adapters.BridgeClient.OPERATIONS)
```

### 2.2 Confirm it fails

`uv run --python 3.11 python -m unittest tests.test_provision -v` — one failure:
`'set-group-control' not found in frozenset({...})`.

### 2.3 Allowlist them on the client

In `src/bzr_live/provision/adapters.py`, add `"set-group-control", "get-group-control",` as a
final line inside `BridgeClient.OPERATIONS`. Then append two lines to the comment above
`BOUNDARIES`, after its existing "Fixed by issue #4's implementation boundaries." line:

```python
# A group's optional product-control mapping is a bridge sub-step of a "bzr" kind
# (ADR 0013); the table records each kind's own boundary, not its sub-steps.
```

### 2.4 Confirm it passes

`uv run --python 3.11 python -m unittest tests.test_provision -v` — `OK`.

### 2.5 Add the operations to the bridge

In `containers/bugzilla/bridge.pl`, add one import line so the block reads
`use Bugzilla::FlagType;` / `use Bugzilla::Group;` / `use Bugzilla::Keyword;` — the only new
line is `use Bugzilla::Group;`, keeping the block alphabetical.

Add `set-group-control get-group-control` as a third line inside the `%OPERATIONS` `qw(...)`
list.

In `sub dispatch`, immediately before the final `die "unreachable operation\n";`, insert:

```perl
  if ($operation eq 'set-group-control') {
    my $product = product_of($request->{product});
    my $group   = Bugzilla::Group->check({name => $request->{group}});
    # Settability needs only these two columns: group_is_settable reads
    # groups_mandatory/groups_available, which select on membercontrol and
    # othercontrol alone (Product.pm:659-736, :740-748 at the pinned SHA). The privilege
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
    # ->new, not ->check: a product the scenario declares but the fixture has not
    # created yet must read as "not settable", not die. The loader already proved
    # the name is a declared product, so this cannot be hiding a typo.
    my $product = Bugzilla::Product->new({name => $request->{product}});
    my $group   = Bugzilla::Group->new({name => $request->{group}});
    return undef unless $product && $group;
    # group_controls without $full_data constrains the join on product_id, so an
    # unmapped group is simply absent (Product.pm:604-657 at the pinned SHA).
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

`CONTROLMAPSHOWN` is already in scope — `bridge.pl` does `use Bugzilla::Constants;`, which
exports it (`Bugzilla/Constants.pm:35`, `:257`).

### 2.6 Check and commit

`make check` — passes. It does not lint Perl, so also run
`perl -c containers/bugzilla/bridge.pl`: on the host it reports `Can't locate Bugzilla.pm`,
which is expected (the modules live only in the image) and still surfaces a syntax error
first. Task 5 is the real proof.

```sh
git add containers/bugzilla/bridge.pl src/bzr_live/provision/adapters.py tests/test_provision.py
git commit -m "feat: add object-layer group-control operations to the bridge"
```

**Acceptance.** Both operations allowlisted client-side and bridge-side; both resolve
arguments with `->check`; the setter uses `set_group_controls` + `update()` and asserts
settability afterwards; the getter returns `undef` for an unmapped group; no SQL in the diff.

## Task 3 — provisioning applies and verifies the mapping

**Files:** `src/bzr_live/provision/executor.py`, `tests/test_provision.py`.

**Interfaces.** Consumes Task 1's `data["products"]` and Task 2's two operations. Provides: a
system group declaring `products` raises `ProvisionError` from
`Provisioner._reject_reserved_values` before any mutation; a group whose declared products
are not all settable raises
`ProvisionConflictError(identity, "products", declared, observed, multi_step=True)`.

### 3.1 Write the failing tests

Add to `_FakeBridge` in `tests/test_provision.py` — two branches immediately before its
final `raise AssertionError`, and one helper method:

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

```python
    def calls_of(self, operation):
        return [payload for op, payload in self.calls if op == operation]
```

Append to `ExecutorTests`:

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
        self.assertEqual(
            sorted(p["product"] for p in bridge.calls_of("set-group-control")),
            ["q4-billing", "q4-checkout"])

    def test_rerun_over_a_mapped_group_is_unchanged_and_writes_nothing(self) -> None:
        self.scenario = self._scenario_from([
            {"kind": "product", "name": "q4-checkout", "description": "Checkout"},
            {"kind": "group", "name": "q4-secret", "description": "Secret",
             "products": [{"ref": "product:q4-checkout"}]}])
        bzr = _FakeBzr()
        bridge = _FakeBridge(bzr)
        self._provisioner(bzr, bridge).run()
        rerun = _FakeBridge(bzr, dict(bridge.state))
        report = self._provisioner(bzr, rerun).run()
        self.assertEqual([status for status, _ in report], ["unchanged", "unchanged"])
        self.assertEqual(rerun.calls_of("set-group-control"), [])
        # the rerun must actually have asked, or "unchanged" proves nothing
        self.assertEqual(
            [p["product"] for p in rerun.calls_of("get-group-control")], ["q4-checkout"])

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

### 3.2 Confirm they fail

`uv run --python 3.11 python -m unittest tests.test_provision -v` — exactly four failures:

- refusal test — no `ProvisionError` raised;
- create test — the `set-group-control` product list is `[]`, not the two names;
- rerun test — the `get-group-control` list is `[]`, not `["q4-checkout"]`. Its `unchanged`
  and empty-`set-group-control` assertions pass trivially before the change, which is why
  the test also asserts the rerun asked;
- conflict test — no `ProvisionConflictError` raised.

### 3.3 Refuse an unsatisfiable declaration

In `src/bzr_live/provision/executor.py`, add a second clause to the loop in
`_reject_reserved_values`, after the existing `custom-field` clause:

```python
            if (resource.kind == "group" and resource.data["products"]
                    and resource.name in SYSTEM_GROUPS):
                raise ProvisionError(
                    f"group {resource.name!r} declares products, but Bugzilla's "
                    "checksetup groups carry isbuggroup = 0 and only a bug group can "
                    "be made settable on a product; declare a group the fixture "
                    "creates instead")
```

### 3.4 Map the products on create

Append to `_create_group`, after its existing `self._bzr.write([...])` call:

```python
        for ref in resource.data["products"]:
            self._bridge.call(
                "set-group-control", {"product": ref.name, "group": resource.name})
```

### 3.5 Compare the mapping on classify

In `_classify_group`, bind `identity = _identity(resource)` before the existing
`self._compare(...)` description check and pass `identity` to it. Then, between that check
and the closing `return "unchanged"`, insert:

```python
        declared = {ref.name for ref in resource.data["products"]}
        settable = {name for name in declared if self._group_is_settable(resource, name)}
        if settable != declared:
            # the group exists but its mapping does not: the actor path's multi-step
            # case, carrying the same reset instruction
            raise ProvisionConflictError(
                identity, "products", sorted(declared), sorted(settable),
                multi_step=True)
```

Add this method beside it:

```python
    def _group_is_settable(self, resource, product: str) -> bool:
        result = self._bridge.call(
            "get-group-control", {"product": product, "group": resource.name})
        return bool(result.get("settable")) if isinstance(result, dict) else False
```

### 3.6 Confirm they pass, then commit

`uv run --python 3.11 python -m unittest tests.test_provision -v` — `OK`, with the
pre-existing `test_plan_order_is_respected` (13 created) and
`test_identical_rerun_is_all_unchanged_with_no_writes` (13 unchanged) still passing; their
fixture's one group declares no `products`.

`make test` — `OK`, at 387 tests plus the ones added here.

```sh
git add src/bzr_live/provision/executor.py tests/test_provision.py
git commit -m "feat: provision a group's declared product controls"
```

**Acceptance.** A system group declaring `products` is refused, naming the group and
`isbuggroup`, with no write and no bridge call. Creating a group calls `set-group-control`
once per declared product. A rerun reports `unchanged`, asks per product, and writes nothing.
A group present without its mapping raises `ProvisionConflictError` naming `products` with
the reset hint. A group declaring no products behaves exactly as before.

## Task 4 — the smoke scenario declares a settable bug group

**Files:** `scenarios/smoke/resources.json`, `tests/test_smoke_scenario.py`.

### 4.1 Declare the group and a member

In `scenarios/smoke/resources.json`, after the `canconfirm` group line, add:

```json
  {"kind": "group", "name": "restricted", "description": "Restricted-visibility bugs",
   "products": [{"ref": "product:checkout"}, {"ref": "product:billing"}]},
```

and append `{"ref": "group:restricted"}` to the `admin-ops` actor's `groups` list. A bug
restricted to a memberless group is invisible to the actor that restricted it, so the
capability needs one member to be usable (ADR 0013).

### 4.2 Re-pin the digest

```sh
uv run --python 3.11 python -c "from bzr_live.scenario import load_scenario; print(load_scenario('scenarios/smoke').digest)"
```

Expect a 64-character hex digest different from the pinned one; a `ScenarioValidationError`
instead means the JSON edit is wrong. Put that value in `EXPECTED_DIGEST` in
`tests/test_smoke_scenario.py`, leaving the comment above it unchanged.

### 4.3 Verify and commit

`make test` — `OK`. `test_digest_matches_the_pinned_value` passes with the new value;
`test_twenty_bugs_across_two_products` is unaffected because no event changed.

```sh
git add scenarios/smoke/resources.json tests/test_smoke_scenario.py
git commit -m "feat: declare a settable bug group in the smoke scenario"
```

**Acceptance.** `scenarios/smoke` declares one bug group mapped to both products with one
member; the pinned digest matches; no event changed.

## Task 5 — prove it live, and record the `bzr` gap

**Files:** `docs/bzr-findings.md`. No source change.

### 5.1 Provision a fresh fixture

From the worktree root: `make up` — both containers healthy. Then

```sh
BZR_LIVE_BZR="/Volumes/Source Code Volume/src/bzr/target/release/bzr" make smoke
```

Expect `created group:restricted` in the provisioning report and no `ProvisionError`. A
`bridge set-group-control failed: ...` message means the Perl is wrong; read the named
Bugzilla error before changing anything.

### 5.2 Confirm the end state directly

Invoke the bridge exactly as `src/bzr_live/provision/__main__.py:14-21` does:

```sh
ROOT=$(pwd -P)
PROJECT=$(uv run --python 3.11 python -c "from bzr_live.provision.adapters import compose_project_name; import os, sys; print(compose_project_name(os.path.realpath(sys.argv[1])))" "$ROOT")
printf '%s' '{"product":"checkout","group":"restricted"}' | docker compose \
  --project-name "$PROJECT" --project-directory "$ROOT" --file "$ROOT/compose.yaml" \
  exec -T --user www-data bugzilla bzr-live-bridge get-group-control
```

Expect one JSON line whose `result` carries `"settable":true`, `"membercontrol":1` and
`"othercontrol":1`. Repeat with `"product":"billing"` for the same result.

### 5.3 Confirm the rerun is idempotent

Re-run the `make smoke` command from 5.1. Expect `unchanged group:restricted`.

### 5.4 Record finding G11

In `docs/bzr-findings.md`, add a summary-table row after the `D9` row:

```markdown
| [G11](#g11) | gap | No `bzr` command maps a bug group to a product, so no group is settable without the admin bridge | — |
```

and this section at the end of the file:

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

### 5.5 Run the remaining guardrails, then commit

`make check` — passes.
`BZR_LIVE_BZR=... make replay-smoke` — the probe report prints and the script exits 0.
`BZR_LIVE_BZR=... make checkpoint-smoke` — a save/restore round trip exits 0.

```sh
make down
git add docs/bzr-findings.md
git commit -m "docs: record G11, no bzr surface maps a bug group to a product"
```

**Acceptance.** A live fixture provisions `group:restricted` as settable on both products; a
rerun reports it unchanged; `docs/bzr-findings.md` carries G11 with its `bzr` citation,
observed behaviour and class; all five guardrails pass.

## Rollback

Every task is one commit on `feat/settable-bug-group-34`; nothing migrates persistent state
outside the fixture. A fixture left in a bad state is discarded with `make down` and
`CONFIRM_RESET=1 make reset`, which is what AGENTS.md prescribes for a disposable fixture.

## Deferrals carried from design review

None recorded at authoring time. Any deferral the design review disposes of is appended here
with its owning record path or tracker issue before implementation begins.
