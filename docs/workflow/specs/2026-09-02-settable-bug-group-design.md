# Settable bug groups — design

Issue: [#34](https://github.com/randomparity/bzr-live/issues/34).
Decision record: [ADR 0013](../../adr/0013-scenario-declared-bug-group-product-controls.md).

## Problem

A scenario cannot declare a bug `groups` value, because no group in this fixture is settable
on any product. `Product::group_is_settable` requires an active bug group that
`group_control_map` lists as mandatory or available for the product; the fixture has never
created a bug group and has never written a `group_control_map` row.

Issue #27 is blocked on it: removing the client-side `groups` refusal without this turns a
fail-before-mutating precondition into a fail-mid-replay error for a payload the server still
rejects.

## Outcome

A scenario declares which products a bug group is settable on. Provisioning establishes that
mapping through the container-local admin bridge, refuses before mutating when it cannot, and
reports `unchanged` on a rerun.

## Contract change

The `group` resource kind gains one optional field:

```json
{"kind": "group", "name": "restricted",
 "description": "Restricted-visibility bugs",
 "products": [{"ref": "product:checkout"}, {"ref": "product:billing"}]}
```

`products` is a list of unique `product:` references, validated by the same `_unique_refs`
helper `flag-type` uses, and contributing the same dependency edges — so the resource plan
orders the group after every product it names. Omitting it is the existing behaviour
unchanged, so `format_version` stays 1.

Semantics: **each named product may carry this group on a bug.** Nothing else. Membership in
the group is declared where it already is, on `actor.groups`.

## Behaviour

**Refusal, before any mutation.** A group whose name is in `executor.SYSTEM_GROUPS` may not
declare `products`. Bugzilla's `checksetup` groups carry `isbuggroup = 0`, and
`set_group_controls` refuses a non-bug-group with `product_illegal_group`. The provisioner
rejects the scenario in `_reject_reserved_values`, naming the group, the field, and
`isbuggroup = 0` as the reason. This is the AGENTS.md contract: refuse and name the
limitation rather than substitute.

**Creation.** `_create_group` runs `bzr group create` as it does today — which sends
`is_active: true` (`bzr` `src/commands/group/create.rs:63-70` at `63abb94e`), and Bugzilla's
`WebService::Group::create` forces `isbuggroup => 1` — then calls the bridge's
`set-group-control` once per declared product.

**Classification.** For a non-system group that already exists, `_classify_group` calls
`get-group-control` for each declared product. All present and settable is `unchanged`. Any
absent or not settable raises `ProvisionConflictError(identity, "products", declared,
observed, multi_step=True)`, where `declared` is the sorted list of product names the
scenario names and `observed` is the sorted list of those the fixture reports settable. Its
message already carries the reset hint. This is the `actor`-with-missing-group-membership
path, reused rather than reinvented.

**Readback.** `_verify_readback` reclassifies from fresh reads, so a create whose mapping did
not persist fails the run at the resource that failed.

**Idempotency.** A second run over a complete fixture reports `unchanged` for the group.
`set_group_controls` is itself idempotent — `Product::update` writes only columns whose value
changed — so even a repeated set is a no-op.

## Bridge operations

Both are added to the allowlist in `containers/bugzilla/bridge.pl` and to
`BridgeClient.OPERATIONS`. Neither writes SQL.

| Operation | Request | Reply |
|---|---|---|
| `set-group-control` | `{"product": <name>, "group": <name>}` | `{"product", "group", "settable": true}` |
| `get-group-control` | `{"product": <name>, "group": <name>}` | `null` when the group is unmapped, else `{"product", "group", "membercontrol", "othercontrol", "entry", "settable"}` |

`set-group-control` resolves both objects with `->check` (which dies on an unknown name, and
the bridge turns that into `{"ok": false, "error": ...}`), then calls
`$product->set_group_controls($group, {entry => 0, membercontrol => CONTROLMAPSHOWN,
othercontrol => CONTROLMAPSHOWN})` and `$product->update()`. It then asserts
`$product->group_is_settable($group)` on a freshly loaded product and dies if false, so a
silently ineffective write cannot report success.

`get-group-control` resolves both objects with `->new` rather than `->check`, and returns
`undef` when either is absent. "Not settable" is the honest answer to a getter when the
product does not exist yet, and the loader has already guaranteed every `products` entry
names a declared product, so a typo cannot reach here. The case this serves is a scenario
edited to add a product to an existing group's `products`: pass 1 classifies the group before
pass 2 creates the new product, so `->check` would have died inside the bridge and replaced
the designed `ProvisionConflictError` — reset hint and all — with a raw Bugzilla error. The
setter keeps `->check`, so a name that cannot be resolved at mutation time still fails loudly.

Otherwise it reads `$product->group_controls()` and returns `undef` when the group id is
absent from it — without `$full_data` that call constrains the join on `product_id`, so an
unmapped group simply does not appear — and reports the stored control values plus
`$product->group_is_settable($group)`.

`settable` — not the raw control integers — is what the executor compares. The control values
are how the end state is reached; settability is the end state issue #34 asks for.

## Fixture change

`scenarios/smoke/resources.json` gains the `restricted` group above, mapped to both products,
and `admin-ops` gains `{"ref": "group:restricted"}`. A group with no member would make the
first bug restricted to it invisible to the actor that restricted it. The digest changes, so
`tests/test_smoke_scenario.py`'s `EXPECTED_DIGEST` is updated in the same change.

## Failure modes

| Condition | Behaviour |
|---|---|
| System group declares `products` | Refused in preflight; nothing is mutated |
| Declared product missing from the scenario | Existing loader error: `missing dependency product:<name>` |
| Duplicate product ref | Existing loader error: `duplicate reference` |
| Group exists, mapping absent | `ProvisionConflictError` with the reset hint; nothing is mutated |
| Group exists, a newly declared product does not yet | Same `ProvisionConflictError`, because `get-group-control` reports an absent product as unmapped rather than dying. Matches `_classify_actor`, which already refuses an existing actor missing a declared membership |
| Bridge cannot reach the fixture | Existing `BridgeClient` message naming `--project-root` |
| `set_group_controls` refuses (inactive or non-bug group) | Bridge returns `ok: false`; `ProvisionError` names the Bugzilla error |
| Mapping written but not settable | Bridge dies on its own post-assert; the run fails at that resource |

## Threat model

**Boundaries added.** One: two new operations on the container-local admin bridge, reachable
only through `docker compose exec` by whoever already runs `make up`. Their inputs are two
resource names taken from a scenario file in the repository.

**Boundaries widened.** One: a bug group becomes settable on a product, so a bug can be
restricted to it. This is a permission grant inside the fixture, and it is the point of the
change.

**Actors.** Per AGENTS.md this fixture binds to 127.0.0.1, and its threat model contains no
remote attacker and no hostile local user. The actors that exist are the operator running
`make up`, and the fabricated scenario accounts. The scenario file is repository content
under review, not untrusted input.

**Controls.** The bridge's existing allowlist gates the operation names (`bridge.pl:50-58`,
`BridgeClient.OPERATIONS`) — an unlisted name exits 2 without reaching Bugzilla. Both
resource names are resolved with `Bugzilla::Product->check` and `Bugzilla::Group->check`, so
an unknown name dies rather than creating an object, and nothing is interpolated into a
query. The getter resolves with `->new` instead and answers `null`, which reads no more than
`->check` would and mutates nothing either way. The control values are fixed Perl constants, never scenario input. The bridge runs as
the admin user it already resolves (`bridge.pl:69-71`); this change grants it nothing new.
Neither operation touches a secret, so neither needs `create-api-key`'s output suppression.

**Out of scope.** Confidentiality of the restricted bugs themselves — every datum in this
fixture is fabricated and transitory. Privilege escalation through group membership — the
mapping deliberately sets no privilege-granting column (ADR 0013). Bugzilla's own group
enforcement — verifying it is issue #27's job, not this one's.
