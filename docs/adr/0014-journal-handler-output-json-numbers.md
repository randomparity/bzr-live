# 0014. The journal admits finite JSON numbers in captured handler output

## Status

Accepted (2026-09-05)

## Context

[ADR 0002](0002-versioned-scenario-contract.md) fixed one JSON value domain for the whole
scenario package: *"The accepted JSON domain excludes floats and non-finite numbers."* That
sentence sits in the paragraph defining the scenario digest, and it is right for what it was
written about. Authored fixture content is hand-written and digest-bearing; a float there is
a value an author typed, a digest input whose decimal spelling nobody agreed on, and never
something the fixture needs.

The journal's `handler_output` is not authored content. It is whatever the mutation boundary
returned, captured verbatim — and Bugzilla returns `estimated_time` and `remaining_time` as
JSON numbers. Issue #44 measured the result at `51b34b6`: a completed record carrying
`estimated_time: 8.0` raises `journal:$.handler_output.estimated_time: contains an
unsupported JSON value`, and the replay aborts. Two independent layers reject it:

- `src/bzr_live/scenario/journal.py:121` admits scalars by an exact type test,
  `if value is None or type(value) in (bool, int)`. A `float` matches no branch and falls to
  `:132`.
- `_decode_json_bytes` (`src/bzr_live/scenario/loader.py:86-95`) hardcodes
  `parse_float=_reject_number(...)`, and `JournalStore._read_file` (`journal.py:800`) decodes
  through it. So admitting `float` in the validator alone yields a record that writes and
  then cannot be re-read — a strictly worse failure than the one being fixed.

`_decode_json_bytes` is shared: `scenario.json`, `resources.json`, and every line of
`events.jsonl` decode through the same function, where ADR 0002's exclusion still governs
and `tests/test_scenario_resources.py:134` enforces it.

The trigger is narrow today. An unauthenticated REST read gets no `timetrackinggroup`-gated
fields, so no float appears; only a group-restricted bug, where `bzr`'s alternate-auth retry
authenticates, returns them. Issue #30 proposes to authenticate reads generally, at which
point every reconciliation carries these fields.

## Decision

**Diverge the JSON number policy between authored scenario input and captured handler
output, in the one decoder, by an explicit opt-in.** `_decode_json_bytes` takes a
keyword-only `allow_float: bool = False`. The default preserves ADR 0002 for every existing
caller; `JournalStore._read_file` is the sole caller that passes `True`. There is no second
decoder and no global loosening.

**Admit finite floats; keep rejecting non-finite ones, on both layers.** `NaN` and
`Infinity` are not JSON (RFC 8259), so `json.dumps` would write a record no conformant
parser can read back, and Bugzilla does not send them. On the read side `parse_constant`
keeps rejecting them whatever `allow_float` says. On the write side `_validate_json` rejects
a non-finite float with `"must be a finite number"` — refusing before the record is written
rather than producing an unreadable one.

**Preserve the exact type test, and extend it to float as `type(value) is float`.** The
exactness is load-bearing and stays that way. `_validate_json` normalizes `handler_output`
into a closed set of exactly-typed JSON scalars, so a record re-read from disk always yields
`None`, `bool`, `int`, `float`, or `str` and nothing else. An `isinstance` test would admit
subclasses — an `IntEnum`, a `float` subclass carrying extra state — that compare equal to
their base value while being a different object than the one the round trip returns, which
silently breaks equality-as-identity for a record compared against its re-read form. The
tuple lists `bool` and `int` separately for the same reason: `bool` is an `int` subclass, so
an exact test would otherwise reject `True`.

**The canonical form is what `json.dumps` already writes.** CPython emits a float via
`repr`, which since 3.1 is the shortest decimal string that round-trips exactly, so
`json.loads(json.dumps(x)) == x` holds for every finite float — checked on CPython 3.11.15
for `8.0`, `0.0`, `-0.0`, `0.1`, `1/3`, `1e308`, and `5e-324` — and the existing
`sort_keys=True, ensure_ascii=False, separators=(",", ":")` encoding is unchanged. No new
canonicalization step is introduced.

## Consequences

- A reconciliation that reads back a bug with time-tracking fields records its journal entry
  and the replay continues. This unblocks the failure on both the group-restricted path
  reachable today and the ordinary path issue #30 would create.
- `JsonValue` gains `float`. The alias is exported from `bzr_live.scenario`, so this widens a
  package-level type, and any consumer that exhaustively matched its members must add a case.
  Inside this repository `handler_output` has no consumer outside `journal.py`.
- Authored scenario input is unchanged: `scenario.json`, `resources.json`, and
  `events.jsonl` still reject floats and non-finite numbers, and the scenario digest domain
  ADR 0002 fixed is untouched. ADR 0002 is not superseded; its sentence is now read as
  scoped to what its paragraph is about.
- The two layers now agree by construction on finite floats and by two separate mechanisms on
  non-finite ones. A future third decode path that forgets `allow_float=True` reintroduces
  the write-only record, so the round-trip test is the guard, not the flag's default.
- `_redact_opaque` and `_contains_secret` already fall through to a bare `return` for a
  float, so redaction stays a no-op on a number and no secret can hide in one.

## Considered & rejected

- **Admit floats in `_validate_json` only.** verified: the record writes and then fails to
  re-read — `_decode_json_bytes(b'{"a":1.5}', "journal")` raises
  `journal:$: floating-point numbers are not supported` at `226b607b` on CPython 3.11.15,
  the interpreter `make test` pins. A write-only record is worse than the abort it replaces.
- **Drop the float rejection from `_decode_json_bytes` for every caller.** verified: ADR 0002
  fixes the scenario digest's accepted domain as excluding floats, and
  `tests/test_scenario_resources.py:134` enforces it for `resources.json`. Loosening it
  globally changes what the digest covers for a gain no authored fixture needs.
- **Give the journal its own decoder beside `_decode_json_bytes`.** judgment: a second copy
  of the duplicate-key, UTF-8, and error-shaping logic, kept in step by hand, to express one
  boolean.
- **Encode numbers as strings in `handler_output`.** verified: `AGENTS.md` forbids silently
  substituting a value the boundary did not return — the fixture exists to capture what
  `bzr` and Bugzilla actually produce. It also loses the type across the round trip.
- **Admit non-finite floats too.** verified: `json.dumps(float("nan"))` emits bare `NaN`,
  which RFC 8259 does not permit and which `json.loads` accepts only through
  `parse_constant`; the record would depend on a CPython extension to be readable at all.
  Bugzilla sends neither.
- **Do nothing and wait for issue #30.** verified: the abort is reachable now on any
  group-restricted bug — issue #44 reproduced it from a control declaring `groups` at create
  time only, a path `main` supported before issue #27 and which issue #27 never touched.
