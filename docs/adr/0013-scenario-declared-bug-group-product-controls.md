# 0013. Bug-group product controls are scenario-declared and set through the bridge

## Status

Accepted (2026-09-02)

## Context

No bug group is settable on any product in this fixture, so a scenario declaring a bug
`groups` value cannot be provisioned or replayed. Issue #34 measured it: `group_control_map`
holds zero rows, and every one of the 14 `groups` rows carries `isbuggroup = 0`.

The reason is structural, not accidental. `scenarios/smoke/resources.json:2-4` declares
exactly three groups — `admin`, `editbugs`, `canconfirm` — and all three are in
`executor.SYSTEM_GROUPS`, which `_classify_group` short-circuits to `unchanged` so the
fixture never creates them. Bugzilla's own `checksetup` made those 14 rows as system groups
(`isbuggroup = 0`). So the fixture has never created a bug group at all, and
`Product::group_is_settable` (`Bugzilla/Product.pm:740-748` at the pinned SHA
`644c66f4`) refuses anything that is not an active bug group listed as mandatory or
available for the product — both sourced from `group_control_map`.

`bzr` cannot close this. At the documented floor `0.8.3-dev (63abb94e)`, `bzr product
update` takes `--description`, `--default-milestone` and `--is-open` and nothing else
(`src/cli/product.rs:118-133`), and `bzr group` has no product-facing subcommand
(`src/cli/group.rs`). This is recorded as [G11](../bzr-findings.md#g11).

Upstream `bzr` solves the same problem in its own functional fixture
(`tests/functional/phases/07-groups.sh:52-67`) by seeding `group_control_map` with a raw
`INSERT`. `containers/bugzilla/bridge.pl:5` forbids that here: *"Bugzilla object layer only
— never raw SQL."* Upstream's statement is taken as the specification of the required end
state, not as the method.

Two questions follow, and neither has an obvious answer: where the mapping is declared, and
what control values it sets.

## Decision

**The mapping is declared in the scenario contract, as an optional `products` list of
product references on the existing `group` resource kind.** It mirrors `flag-type`'s
`products` inclusion list exactly — the same idiom for "this thing applies to these
products", the same `_unique_refs` validation, the same dependency edges into the
topological resource plan.

**It is applied through two new bridge operations, `set-group-control` and
`get-group-control`, over `Bugzilla::Product`'s object-layer API.** `set-group-control`
calls `$product->set_group_controls($group, ...)` and `$product->update()`
(`Bugzilla/Product.pm:519-584`, `:137-288`); `get-group-control` reads
`$product->group_controls()` (`:604-657`). No SQL statement is written by this repository.

**The control values are `entry = 0`, `membercontrol = CONTROLMAPSHOWN`,
`othercontrol = CONTROLMAPSHOWN`, and nothing else.** That is the minimum
`group_is_settable` requires: `groups_available` (`:659-711`) admits a group whose
`othercontrol` is `SHOWN` regardless of the acting user's membership. The three
privilege-granting columns upstream also sets — `canedit`, `editbugs`, `canconfirm` — are
deliberately left at 0.

**A group named in `SYSTEM_GROUPS` may not declare `products`.** The provisioner refuses it
before any mutation, naming `isbuggroup = 0` as the reason, alongside the existing
reserved-value refusal in `_reject_reserved_values`.

**A group that exists without its declared mapping is a conflict, not something to
reconcile.** `_classify_group` raises `ProvisionConflictError(..., "products", ...,
multi_step=True)`, exactly as `_classify_actor` already does for a user missing a declared
group membership.

## Consequences

- The scenario schema widens by one optional field on one existing kind. `format_version`
  stays 1: a scenario that omits `products` parses and provisions unchanged, so no existing
  contract is broken.
- Every scenario's digest moves regardless, including one that declares no `products` at
  all. `loader._resource_json` emits each key of `PlannedResource.data`, so every `group`
  now contributes `"products": []` to the canonical envelope. That is a digest change
  without a behaviour change — journals written against the old content are invalidated by
  the schema widening itself, not only by the smoke edit below. Measured: `scenarios/smoke`
  moved to `21695fe0…` on the loader change alone, before its own `resources.json` was
  touched.
- `containers/bugzilla/bridge.pl` changes, so the Bugzilla image rebuilds and CI's
  `Container lifecycle / x86_64-linux` job runs — roughly 45 minutes.
- `scenarios/smoke/resources.json` gains a `restricted` bug group mapped to both products,
  and `admin-ops` joins it. Without a member, the first bug restricted to the group would be
  invisible to the actor that restricted it, which is a capability that exists on paper only.
  That edit changes the scenario digest, so `tests/test_smoke_scenario.py`'s
  `EXPECTED_DIGEST` moves and any journal written against the old content is invalid —
  the two-file change ADR 0007 designed that constant to force.
- `adapters.BOUNDARIES` keeps `"group": "bzr"`. The group itself is still created by `bzr
  group create`; only the optional mapping crosses the bridge, which makes `group` a
  two-boundary kind in the same way `actor` is already a two-call one. The table records the
  kind's own mutation boundary and is not restructured for a sub-step.
- Mapping a group is non-destructive to existing bugs. Only `CONTROLMAPMANDATORY` sweeps
  bugs into a group and only `CONTROLMAPNA` sweeps them out (`Bugzilla/Product.pm:214-268`);
  `SHOWN` does neither, so a rerun over a populated fixture moves no bug.
- Any user can restrict a bug to the group, but only members can see it afterwards. That is
  the intended shape — it is what makes group visibility observable — and it means a
  scenario that restricts a bug without giving its later readers membership will produce
  invisibility, correctly.

## Considered & rejected

- **Put the mapping in the fixture's `containers/` baseline.** verified: the products it
  would have to name do not exist at baseline time. `checkout` and `billing` are scenario
  resources created by `bzr product create` during provisioning
  (`src/bzr_live/provision/executor.py:301-305`), long after `checksetup` and the image
  build. A baseline step has nothing to map a group to. This is the option issue #34 asked
  to be weighed, and it is refuted rather than merely disfavoured.
- **Copy upstream's raw `INSERT ... ON DUPLICATE KEY UPDATE` into a bridge operation.**
  verified: `containers/bugzilla/bridge.pl:5` states the opposite invariant, and
  `Bugzilla::Product::set_group_controls` plus `update()` reach the same end state through
  the object layer, including the `group_control_map` row and its delete-when-all-zero
  behaviour (`Bugzilla/Product.pm:576-583`, `:181-211`).
- **Add a new `group-control` resource kind joining a product and a group.** judgment: a
  third join kind for a two-column relation, when `flag-type` already establishes
  `products:` on the non-product resource as this contract's idiom for the same shape.
- **Also set `canedit`, `editbugs` and `canconfirm`, as upstream's `INSERT` does.**
  verified: `group_is_settable` (`Bugzilla/Product.pm:740-748`) reads only `isactive`,
  `isbuggroup`, `groups_mandatory` and `groups_available`, and the latter two select on
  `membercontrol`/`othercontrol` alone (`:659-736`). Those three columns instead grant
  product privileges to the group's members, which would hand `admin-ops` `editbugs` and
  `canconfirm` on both products by a route no scenario declared — the fixture would stop
  being able to observe a missing permission.
- **Map one of the 14 `checksetup` groups instead of creating one.** verified: all 14 carry
  `isbuggroup = 0` (issue #34's measurement), and `set_group_controls` refuses a
  non-bug-group outright with `product_illegal_group` (`Bugzilla/Product.pm:522-523`, via `Group::is_active_bug_group`, `Bugzilla/Group.pm:319-322`).
- **Reconcile a missing mapping in place on rerun rather than refusing.** judgment: every
  other kind in the provisioner refuses on divergence, and reconciling would mutate a
  fixture whose state already disagrees with the contract — the case the reset hint exists
  for.
- **Do nothing and let issue #27 remove the `groups` refusal anyway.** verified: `bzr bug
  update` carries `--groups-add`/`--groups-remove` and finding D3 is fixed, but Bugzilla
  answers error 120 for every group name on this fixture (issue #34's measurement), so the
  refusal would become a mid-replay failure for a payload that still cannot succeed.
