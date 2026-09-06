# 0014. The journal admits finite JSON numbers in captured handler output

## Status

Accepted (2026-09-05)

## Context

[ADR 0002](0002-versioned-scenario-contract.md) fixed one JSON value domain for the scenario
package: *"The accepted JSON domain excludes floats and non-finite numbers."* That sentence
sits in the paragraph defining the scenario digest, and it is right for what it was written
about — authored fixture content is hand-written and digest-bearing.

The journal's `handler_output` is not authored content. It is whatever the mutation boundary
returned, decoded by `provision/adapters.py:73-75`'s bare `json.loads` — which has no float
rejection of its own — and captured verbatim. Bugzilla returns `estimated_time` and
`remaining_time` as JSON numbers. Issue #44 measured the result: a completed record carrying
`estimated_time: 8.0` raises `journal:$.handler_output.estimated_time: contains an
unsupported JSON value`, and the replay aborts. Two layers reject it independently:

- `journal.py:121` admits scalars by an exact type test, `type(value) in (bool, int)`. A
  `float` matches no branch and falls to `:132`.
- `_decode_json_bytes` (`loader.py:86-95`) hardcodes `parse_float=_reject_number(...)`, and
  `JournalStore._read_file` (`journal.py:800`) decodes through it. Admitting `float` in the
  validator alone therefore yields a record that writes and then cannot be re-read — a
  strictly worse failure than the abort.

`_decode_json_bytes` is shared: `scenario.json` and `resources.json` (via `_load_json`,
`loader.py:105-106`) and every `events.jsonl` line (via `_parse_events`, `loader.py:438-439`)
decode through it, where ADR 0002's exclusion still governs.

The trigger is narrow today — only a group-restricted bug, where `bzr`'s alternate-auth
retry authenticates, returns the `timetrackinggroup`-gated fields. Issue #30 proposes to
authenticate reads generally, at which point every reconciliation carries them.

## Decision

**Diverge the policy between authored input and captured output, in the one decoder, by an
explicit opt-in.** `_decode_json_bytes` takes a keyword-only `allow_float: bool = False`.
The default preserves ADR 0002 for every existing caller; `JournalStore._read_file` is the
sole caller passing `True`. No second decoder, no global loosening.

**Admit finite floats; reject non-finite ones structurally on both sides.** `NaN` and
`Infinity` are not JSON (RFC 8259), so a record carrying one is unreadable by a conformant
parser, and Bugzilla does not send them. On read, `parse_constant` keeps rejecting them
whatever `allow_float` says. On write, `_validate_json` rejects a non-finite float with
`"must be a finite number"`, and `_write_temp`'s `json.dumps` (`journal.py:849`) gains
`allow_nan=False` so the serializer cannot emit one even if a value reached it by a path that
skipped the validator. Both sides are then structural rather than resting on one branch.

**Preserve the exact type test, extended to float as `type(value) is float`.** The exactness
is load-bearing for numbers, and the codebase already depends on it. `bool` is an `int`
subclass, so the tuple lists both or `True` would be rejected; an `IntEnum` *is* rejected
today (checked), and `replay/actions.py:173-181`'s `_usable_id` docstring records why that
matters — a value that passes every upstream `isinstance` guard and is then refused by the
journal blames the record for a boundary reply after the in-flight record has landed. `float`
gets the same exact test for the same reason. This is a property of the numeric branches
only: `journal.py:118` admits a `str` by `isinstance`, so a `str` subclass survives
validation as itself and does not come back as itself (checked). This change neither widens
nor narrows that gap.

**The canonical form is what `json.dumps` already writes.** CPython emits a float via `repr`,
the shortest decimal that round-trips exactly since 3.1 — checked on CPython 3.11.15 for
`8.0`, `0.0`, `-0.0`, `0.1`, `1/3`, `1e308`, `5e-324`. The existing
`sort_keys=True, ensure_ascii=False, separators=(",", ":")` encoding is otherwise unchanged
and no canonicalization step is added.

## Consequences

- A reconciliation that reads back a bug with time-tracking fields records its entry and the
  replay continues, on the group-restricted path reachable today and on the ordinary path
  issue #30 would create.
- `handler_output` is carried on a digest-bearing record but is **not** an input to
  `scenario_digest`, which is computed over the scenario package (ADR 0002:36-40). No float
  can enter the digest domain, and the round-trip test's `scenario_digest` assertion guards
  that field against disturbance rather than evidencing the digest.
- `JsonValue` gains `float`. The alias is exported from `bzr_live.scenario`; `handler_output`
  is produced by `replay/engine.py:228` and consumed by `replay/actions.py`, whose guards are
  `isinstance` on dict/list and `type(value) is int` for ids (`actions.py:181`), so none of
  them changes behaviour under the widened alias.
- Authored input is unchanged and ADR 0002 is not superseded; its sentence is now read as
  scoped to its own paragraph. The existing `test_rejects_floats_and_non_finite_numbers`
  (`tests/test_scenario_resources.py:134`) does not on its own hold that line — it asserts
  only that loading fails at field `$` — so this change adds a test asserting the decoder's
  own message for both authored decode paths.
- A future third decode path that forgets `allow_float=True` reintroduces the write-only
  record. The round-trip test is the guard, not the flag's default.
- `_redact_opaque` and `_contains_secret` already fall through to a bare return for a float,
  so redaction stays a no-op on a number and no secret can hide in one.

## Considered & rejected

- **Admit floats in `_validate_json` only.** verified: `_decode_json_bytes(b'{"a":1.5}',
  "journal")` raises `journal:$: floating-point numbers are not supported` at `226b607b` on
  CPython 3.11.15, the interpreter `make test` pins. A write-only record is worse than the
  abort it replaces.
- **Drop the float rejection from `_decode_json_bytes` for every caller.** verified: ADR 0002
  fixes the scenario digest's accepted domain as excluding floats, so this changes what the
  digest covers for a gain no authored fixture needs.
- **Key the policy off the existing `source` tag instead of a flag.** verified: `source` is
  an exact discriminator — `"journal"` from `journal.py:800`, `"scenario.json"` /
  `"resources.json"` from `loader.py:105-106`, `f"events.jsonl:{n}"` from `loader.py:438-439`
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
  `parse_constant`; the record would depend on a CPython extension to be readable.
- **Do nothing and wait for issue #30.** verified: the abort is reachable now on any
  group-restricted bug — issue #44 reproduced it from a control declaring `groups` at create
  time only, a path `main` supported before issue #27 and which issue #27 never touched.
