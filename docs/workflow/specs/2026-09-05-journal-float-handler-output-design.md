# Journal handler output accepts finite JSON numbers — design

Issue: [#44](https://github.com/randomparity/bzr-live/issues/44)
Decision record: [ADR 0014](../../adr/0014-journal-handler-output-json-numbers.md), which
holds the context, the policy divergence, and the rejected alternatives.
Base branch: `main` (`226b607b`) · Branch: `feat/journal-float-handler-output-44`
Guardrails: `make check`, `make test` (both run bare)

## Goal

A completed journal record whose `handler_output` carries a finite JSON number — Bugzilla's
`estimated_time` / `remaining_time` — is written and re-read without error, so a
reconciliation that reads back a time-tracking-bearing bug records its entry and the replay
continues. Authored scenario input keeps rejecting floats.

## Changes

**C1 — opt-in float decoding, one decoder.** `_decode_json_bytes`
(`src/bzr_live/scenario/loader.py:86`) gains a keyword-only `allow_float: bool = False`.
When true it passes `parse_float=float`; `parse_constant` keeps `_reject_number` in both
modes, so `NaN` / `Infinity` / `-Infinity` stay rejected everywhere.
`JournalStore._read_file` (`journal.py:800`) is the only caller that passes `True`. The
default preserves ADR 0002's domain for `scenario.json` and `resources.json` (via
`_load_json`, `loader.py:106`) and for every `events.jsonl` line (via `_parse_events`,
`loader.py:439`).

**C2 — finite floats admitted on the validation layer.** `_validate_json`
(`journal.py:115-132`) gains a `type(value) is float` branch. A non-finite float raises
`_journal_error(field, "must be a finite number")` — a message distinct from the existing
`contains an unsupported JSON value`, so the two rejections are separable in a test and in a
failure report. The exact type test is preserved rather than relaxed to `isinstance`, and
`float` inherits that property; ADR 0014 records why.

Rejecting non-finite on the write path is what stops the fixture producing an unreadable
record: `json.dumps` would emit bare `NaN`, which the read path then refuses. That is *fail
before mutating*, not defence in depth for its own sake.

**C3 — `JsonValue` gains `float`.** `src/bzr_live/scenario/model.py:22` becomes
`bool | int | float | str | tuple[...] | Mapping[str, ...] | None`. `handler_output` has no
consumer outside `journal.py` in this repository (`rg -n handler_output src/` returns only
`journal.py`).

## Non-goals

- No change to the scenario digest domain, `scenario.json`, `resources.json`, or
  `events.jsonl`. `tests/test_scenario_resources.py:134` stays green unmodified.
- No canonicalization step. `json.dumps` already emits the shortest round-tripping decimal.
- No change to redaction: `_redact_opaque` (`journal.py:494-511`) and `_contains_secret`
  (`journal.py:514-528`) both fall through to a bare return for a float, which is correct —
  a number cannot carry a secret substring.
- No crash-consistency, fsync, or transaction work (`AGENTS.md`: explicitly out of scope).

## Security model

**Boundary.** One existing boundary is widened, none added: the journal record file, read by
`JournalStore._read_file` from an owner-only mode-0700 directory. The value entering it
originates from `bzr`/Bugzilla output captured as `handler_output`.

**Actors.** Per `AGENTS.md` the fixture binds to 127.0.0.1, every datum is fabricated, and
neither a remote nor a hostile local actor is in the model. The realistic actor is an
unexpected response from the fixture's own Bugzilla.

**Controls.** Decode stays bounded by `_reject_number` on `parse_constant` — non-finite
values never enter, in either mode — and by the existing duplicate-key, UTF-8, and
regular-file checks, none of which this change touches. `_validate_json` bounds the admitted
type to exactly `float` and to finite values, and rejects before any write. File modes,
symlink refusal, and the retained-descriptor discipline are unchanged.

**Out of scope.** Numeric magnitude and precision are not bounded: `1e308` and `5e-324` are
accepted, because they round-trip exactly and the fixture has no reason to police what
Bugzilla returns. Denial of service through a large record is not addressed — the reader is
the same process that wrote it, on local disk.

## Acceptance criteria

1. A `CompletedRecord` whose `handler_output` holds `estimated_time: 8.0` and
   `remaining_time: 0.0` is accepted by `__post_init__`, written by
   `JournalStore.replace_completed`, and returned by `JournalStore.read` with
   `handler_output` equal to what was written and each value still exactly `float`. The
   record file on disk holds `"estimated_time":8.0`.
2. `scenario_digest` and the other identity fields survive that round trip, and
   `replace_completed`'s identity comparison against the in-flight record still succeeds.
3. `float("nan")`, `float("inf")`, and `float("-inf")` in `handler_output` raise
   `ScenarioValidationError` naming `$.handler_output` and `must be a finite number`, at
   construction — before any record file is written, leaving the in-flight record intact.
4. A journal record file whose `handler_output` holds a literal `NaN` fails to decode.
5. `resources.json` containing `1.5` or `NaN` still fails to load, with
   `tests/test_scenario_resources.py::ScenarioResourceTests::test_rejects_floats_and_non_finite_numbers`
   unmodified and green.
6. An `events.jsonl` line containing a float still fails to load.
7. `make check` and `make test` are green, run bare.

## Testing

Criteria 1–4 are tested in `tests/test_journal.py`. Criterion 5 is met by leaving
`tests/test_scenario_resources.py` unmodified and green. Criterion 6 adds one test to
`tests/test_scenario_events.py`, which owns the `events.jsonl` decode path — it is what
catches a later mistaken `allow_float=True` on `_parse_events`, which the `resources.json`
test would not.

Each new test is verified to bite: revert one half of the fix, observe red, restore.
