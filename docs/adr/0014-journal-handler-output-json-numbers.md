# 0014. The journal admits finite JSON numbers in captured handler output

## Status

Accepted (2026-09-05)

## Context

[ADR 0002](0002-versioned-scenario-contract.md) fixed one JSON value domain for the scenario
package: *"The accepted JSON domain excludes floats and non-finite numbers."* That sentence
sits in the paragraph defining the scenario digest, and it is right for what it was written
about — authored fixture content is hand-written and digest-bearing.

The journal's `handler_output` is not authored content. It is whatever the mutation boundary
returned, decoded by `provision/adapters.py`'s `_payload` bare `json.loads` — which has no float
rejection of its own — and captured verbatim. Bugzilla returns `estimated_time` and
`remaining_time` as JSON numbers. Issue #44 measured the result: a completed record carrying
`estimated_time: 8.0` raises `journal:$.handler_output.estimated_time: contains an
unsupported JSON value`, and the replay aborts. Two layers reject it independently:

- `_validate_json` admits scalars by an exact type test, `type(value) in (bool, int)`. A
  `float` matches no branch and falls through to its final `raise`.
- `_decode_json_bytes` hardcodes `parse_float=_reject_number(...)`, and
  `JournalStore._read_file` decodes through it. Admitting `float` in the
  validator alone therefore yields a record that writes and then cannot be re-read — a
  strictly worse failure than the abort.

`_decode_json_bytes` is shared: `scenario.json` and `resources.json` (via `_load_json`) and every
`events.jsonl` line (via `_parse_events`) decode through it, where ADR 0002's exclusion still governs.

The trigger is narrow today — only a group-restricted bug, where `bzr`'s alternate-auth
retry authenticates, returns the `timetrackinggroup`-gated fields. Issue #30 proposes to
authenticate reads generally, at which point every reconciliation carries them.

## Decision

**Diverge the policy between authored input and captured output, in the one decoder, by an
explicit opt-in.** `_decode_json_bytes` takes a keyword-only `allow_float: bool = False`.
The default preserves ADR 0002 for every existing caller; `JournalStore._read_file` is the
sole caller passing `True`. No second decoder, no global loosening.

**Admit finite floats; reject non-finite ones, by two mechanisms that divide the input
between them.** Bugzilla does not send non-finite values. The division is worth stating
exactly, because it is not symmetric:

- `parse_constant` refuses the three bare tokens `NaN`, `Infinity`, `-Infinity`, in both
  modes, because it is not gated on `allow_float`.
- An **overflow literal is a different path**: `1e400` is valid JSON, reaches `parse_float`,
  and with `parse_float=float` returns `inf` without `parse_constant` ever being called
  (checked on CPython 3.11.15). Only `_validate_json`'s `math.isfinite` branch refuses it —
  on write from `__post_init__`, and on read via `_record_from_json`.

Both mechanisms are reachable and both are tested. No third guard is added; see the rejected
alternatives.

**Preserve the exact type test, extended to float as `type(value) is float`.** The exactness
is load-bearing for numbers, and the codebase already depends on it. `bool` is an `int`
subclass, so the tuple lists both or `True` would be rejected; an `IntEnum` *is* rejected
today (checked), and `replay/actions.py`'s `_usable_id` docstring records why that
matters — a value that passes every upstream `isinstance` guard and is then refused by the
journal blames the record for a boundary reply after the in-flight record has landed. `float`
gets the same exact test for the same reason. The closed-set property is about numbers only:
`_validate_json`'s `str` branch uses `isinstance`, so a `str` subclass survives validation as
itself (checked), and this change neither widens nor narrows that.

**The canonical form is what `json.dumps` already writes.** CPython emits a float via `repr`,
the shortest decimal that round-trips exactly since 3.1 — checked on CPython 3.11.15 for
`8.0`, `0.0`, `-0.0`, `0.1`, `1/3`, `1e308`, `5e-324`. The encoding is otherwise unchanged
and no canonicalization step is added.

## Consequences

- A reconciliation that reads back a bug with time-tracking fields records its entry and the
  replay continues, on the group-restricted path reachable today and on the ordinary path
  issue #30 would create.
- `handler_output` is carried on a digest-bearing record but is **not** an input to
  `scenario_digest`, which is computed over the normalized envelope `load_scenario` builds.
  No float can enter the digest domain.
- **`allow_float=True` applies to the whole record document**, not to `handler_output` alone:
  `_read_file` decodes the entire file through that one call. The exact `int` guards at
  `_validate_common` (attempt), `CompletedRecord.__post_init__` (exit_status and each
  `resolved_ids` value) and `_record_from_json` (journal_version), plus `freeze_planned`'s
  `type(value) in (bool, int, str)`, become the sole float refusal for every other field. Each refuses a float
  because `float` is not an `int` subclass, so the refactor that would reintroduce one is
  dropping a guard or widening it to a number type (`(int, float)`, `numbers.Number`) — not
  relaxing `type(...) is int` to `isinstance`, which still refuses a float and only admits
  `bool` and `IntEnum`.
- `JsonValue` gains `float`. The alias is exported from `bzr_live.scenario`; `handler_output`
  is produced by `replay/engine.py`'s `_write_completed` and consumed by `replay/actions.py`,
  whose guards are `isinstance` on dict/list and `_usable_id`'s `type(value) is int`, so none of
  them changes behaviour under the widened alias.
- Authored input is unchanged and ADR 0002 is not superseded; its sentence is now read as
  scoped to its own paragraph. `test_rejects_floats_and_non_finite_numbers`
  (`tests/test_scenario_resources.py:134`) does not hold that line by itself — it asserts only
  that loading fails at field `$`, which a later `unknown field` error also satisfies — so
  this change adds a test asserting the decoder's own message on both authored decode paths.
- A future third decode path that forgets `allow_float=True` reintroduces the write-only
  record. The round-trip test is the guard, not the flag's default.
- `_redact_opaque` and `_contains_secret` already fall through to a bare return for a float,
  so redaction stays a no-op on a number and no secret can hide in one.
- **One follow-up leaves this record unowned**, named here so it is unambiguous: `_validate_json`
  admits a `str` by `isinstance`, so a `str` subclass — or a `str`-based `Enum` — is validated
  as itself and does not come back as itself across the round trip, while the numeric branches
  reject the equivalent `IntEnum` (both checked on CPython 3.11.15). It predates this change,
  which neither depends on it nor worsens it, since the widening here is numbers-only. It is
  recorded rather than filed; filing is outside this change's authority.

## Considered & rejected

- **Admit floats in `_validate_json` only.** verified: `_decode_json_bytes(b'{"a":1.5}',
  "journal")` raises `journal:$: floating-point numbers are not supported` at `226b607b` on
  CPython 3.11.15, the interpreter `make test` pins. A write-only record is worse than the
  abort it replaces.
- **Drop the float rejection from `_decode_json_bytes` for every caller.** verified: the
  scenario digest is unaffected either way — it is computed over the normalized envelope
  that `load_scenario` builds, and every authored value first passes a typed helper —
  `_decimal` requiring a canonical decimal *string* for exactly the time values at
  issue. What is lost is the decoder's named `floating-point numbers are not supported`,
  which degrades to an incidental `unknown field` wherever the float happens to land, and one
  gate becomes per-field coverage. ADR 0002's sentence would also be contradicted at the
  letter rather than scoped.
- **Key the policy off the existing `source` tag instead of a flag.** verified: `source` is
  an exact discriminator — `"journal"` from `_read_file`, `"scenario.json"` /
  `"resources.json"` from `_load_json`, `f"events.jsonl:{n}"` from `_parse_events`
  — so `parse_float=float if source == "journal" else ...` needs no signature or call-site
  change and no future decode path could forget it. judgment: it makes an error-message tag
  load-bearing for validation policy, where a later rewording silently changes behaviour; the
  keyword says what it means at the point of use.
- **Give the journal its own decoder beside `_decode_json_bytes`.** judgment: a second copy
  of the duplicate-key, UTF-8, and error-shaping logic, kept in step by hand, to express one
  boolean.
- **Encode numbers as strings in `handler_output`.** verified: `AGENTS.md` forbids silently
  substituting a value the boundary did not return. It also loses the type across the round
  trip.
- **Admit non-finite floats too.** verified: `json.dumps(float("nan"))` emits bare `NaN` on
  CPython 3.11.15, which RFC 8259 does not permit and which `json.loads` accepts only through
  `parse_constant`.
- **Add `allow_nan=False` to `_write_temp`'s `json.dumps` as a serializer backstop.**
  verified: it is unreachable — `CompletedRecord.__post_init__` validates `handler_output`
  in `__post_init__` and `replace_completed` re-runs `__post_init__`, so no
  write path reaches `_write_temp` with a non-finite float — which also makes it untestable
  by construction. It would raise a bare `ValueError('Out of range float values are not JSON
  compliant')` (checked), escaping the `ScenarioValidationError` contract every other journal
  refusal honours. `AGENTS.md` is explicit that production-grade infrastructure here is scope
  overreach; the two mechanisms above satisfy the requirement without it.
- **Do nothing and wait for issue #30.** verified: the abort is reachable now on any
  group-restricted bug — issue #44 reproduced it from a control declaring `groups` at create
  time only, a path `main` supported before issue #27 and which issue #27 never touched.
