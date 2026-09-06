# 0014. The journal admits finite JSON numbers in captured handler output

## Status

Accepted (2026-09-05)

## Context

[ADR 0002](0002-versioned-scenario-contract.md) fixed one JSON value domain for the scenario
package: *"The accepted JSON domain excludes floats and non-finite numbers."* That sentence
sits in the paragraph defining the scenario digest, and it is right for what it was written
about — authored fixture content is hand-written and digest-bearing, so a float there is a
value an author typed and a digest input whose decimal spelling nobody agreed on.

The journal's `handler_output` is not authored content. It is whatever the mutation boundary
returned, captured verbatim, and Bugzilla returns `estimated_time` and `remaining_time` as
JSON numbers. Issue #44 measured the result: a completed record carrying
`estimated_time: 8.0` raises `journal:$.handler_output.estimated_time: contains an
unsupported JSON value`, and the replay aborts. Two layers reject it independently:

- `journal.py:121` admits scalars by an exact type test, `type(value) in (bool, int)`. A
  `float` matches no branch and falls to `:132`.
- `_decode_json_bytes` (`loader.py:86-95`) hardcodes `parse_float=_reject_number(...)`, and
  `JournalStore._read_file` (`journal.py:800`) decodes through it. Admitting `float` in the
  validator alone therefore yields a record that writes and then cannot be re-read — a
  strictly worse failure than the abort.

`_decode_json_bytes` is shared: `scenario.json`, `resources.json`, and every `events.jsonl`
line decode through it, where ADR 0002's exclusion still governs and
`tests/test_scenario_resources.py:134` enforces it.

The trigger is narrow today — only a group-restricted bug, where `bzr`'s alternate-auth
retry authenticates, returns the `timetrackinggroup`-gated fields. Issue #30 proposes to
authenticate reads generally, at which point every reconciliation carries them.

## Decision

**Diverge the policy between authored input and captured output, in the one decoder, by an
explicit opt-in.** `_decode_json_bytes` takes a keyword-only `allow_float: bool = False`.
The default preserves ADR 0002 for every existing caller; `JournalStore._read_file` is the
sole caller passing `True`. No second decoder, no global loosening.

**Admit finite floats; keep rejecting non-finite ones, on both layers.** `NaN` and
`Infinity` are not JSON (RFC 8259), so `json.dumps` would write a record no conformant
parser reads back, and Bugzilla does not send them. On read, `parse_constant` keeps
rejecting them whatever `allow_float` says. On write, `_validate_json` rejects a non-finite
float with `"must be a finite number"` — refusing before the record exists rather than
producing an unreadable one.

**Preserve the exact type test, extended to float as `type(value) is float`.** The exactness
is load-bearing. `_validate_json` normalizes `handler_output` into a closed set of
exactly-typed JSON scalars, so a record read from disk always yields `None`, `bool`, `int`,
`float`, or `str` and nothing else. `isinstance` would admit subclasses — an `IntEnum`, a
`float` subclass carrying extra state — that compare equal to their base value while being a
different object than the round trip returns, silently breaking equality-as-identity between
a record and its re-read form. The tuple lists `bool` and `int` separately for the same
reason: `bool` is an `int` subclass, so an exact test would otherwise reject `True`.

**The canonical form is what `json.dumps` already writes.** CPython emits a float via
`repr`, the shortest decimal that round-trips exactly since 3.1, so
`json.loads(json.dumps(x)) == x` holds for every finite float — checked on CPython 3.11.15
for `8.0`, `0.0`, `-0.0`, `0.1`, `1/3`, `1e308`, `5e-324`. The existing
`sort_keys=True, ensure_ascii=False, separators=(",", ":")` encoding is unchanged and no
canonicalization step is added.

## Consequences

- A reconciliation that reads back a bug with time-tracking fields records its entry and the
  replay continues, on the group-restricted path reachable today and on the ordinary path
  issue #30 would create.
- `JsonValue` gains `float`. The alias is exported from `bzr_live.scenario`, so this widens a
  package-level type; inside this repository `handler_output` has no consumer outside
  `journal.py`.
- Authored input is unchanged, and the scenario digest domain ADR 0002 fixed is untouched.
  ADR 0002 is not superseded; its sentence is now read as scoped to its own paragraph.
- The layers agree by construction on finite floats and by two separate mechanisms on
  non-finite ones. A future third decode path that forgets `allow_float=True` reintroduces
  the write-only record, so the round-trip test is the guard, not the flag's default.
- `_redact_opaque` and `_contains_secret` already fall through to a bare return for a float,
  so redaction stays a no-op on a number and no secret can hide in one.

## Considered & rejected

- **Admit floats in `_validate_json` only.** verified: `_decode_json_bytes(b'{"a":1.5}',
  "journal")` raises `journal:$: floating-point numbers are not supported` at `226b607b` on
  CPython 3.11.15, the interpreter `make test` pins. A write-only record is worse than the
  abort it replaces.
- **Drop the float rejection from `_decode_json_bytes` for every caller.** verified: ADR 0002
  fixes the scenario digest's accepted domain as excluding floats and
  `tests/test_scenario_resources.py:134` enforces it, so this changes what the digest covers
  for a gain no authored fixture needs.
- **Give the journal its own decoder beside `_decode_json_bytes`.** judgment: a second copy
  of the duplicate-key, UTF-8, and error-shaping logic, kept in step by hand, to express one
  boolean.
- **Encode numbers as strings in `handler_output`.** verified: `AGENTS.md` forbids silently
  substituting a value the boundary did not return. It also loses the type across the round
  trip.
- **Admit non-finite floats too.** verified: `json.dumps(float("nan"))` emits bare `NaN` on
  CPython 3.11.15, which RFC 8259 does not permit and which `json.loads` accepts only through
  `parse_constant`; the record would depend on a CPython extension to be readable. Bugzilla
  sends neither.
- **Do nothing and wait for issue #30.** verified: the abort is reachable now on any
  group-restricted bug — issue #44 reproduced it from a control declaring `groups` at create
  time only, a path `main` supported before issue #27 and which issue #27 never touched.
