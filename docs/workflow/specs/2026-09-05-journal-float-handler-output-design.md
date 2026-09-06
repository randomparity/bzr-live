# Journal handler output accepts finite JSON numbers — design

Issue [#44](https://github.com/randomparity/bzr-live/issues/44) ·
[ADR 0014](../../adr/0014-journal-handler-output-json-numbers.md) holds the context, the
policy divergence, and the rejected alternatives · branch
`feat/journal-float-handler-output-44` off `main` (`226b607b`) · guardrails `make check` and
`make test`, run bare.

## Goal

A completed journal record whose `handler_output` carries a finite JSON number — Bugzilla's
`estimated_time` / `remaining_time` — is written and re-read without error, so a
reconciliation that reads back a time-tracking-bearing bug records its entry and the replay
continues. Authored scenario input keeps rejecting floats.

## Changes

- **C1.** `_decode_json_bytes` (`loader.py:86`) gains keyword-only `allow_float: bool =
  False`; when true it passes `parse_float=float`. `parse_constant` keeps `_reject_number`
  in both modes. `JournalStore._read_file` (`journal.py:800`) is the only caller passing
  `True`, so `_load_json` (`loader.py:106`) and `_parse_events` (`loader.py:439`) keep ADR
  0002's domain by the default.
- **C2.** `_validate_json` (`journal.py:115-132`) gains a `type(value) is float` branch;
  non-finite raises `must be a finite number`, a message distinct from the existing
  `contains an unsupported JSON value` so the two rejections are separable in a test.
  Rejecting non-finite on the write path is what stops the fixture producing a record
  `json.dumps` spells `NaN` and the read path then refuses — fail before mutating.
- **C3.** `JsonValue` (`model.py:22`) gains `float`. `rg -n handler_output src/` returns only
  `journal.py`, so nothing else consumes it here.

**Non-goals.** No change to the scenario digest domain or to `scenario.json`,
`resources.json`, `events.jsonl` — `tests/test_scenario_resources.py:134` stays green
unmodified. No canonicalization step; `json.dumps` already emits the shortest
round-tripping decimal. No redaction change: `_redact_opaque` (`journal.py:494-511`) and
`_contains_secret` (`journal.py:514-528`) fall through to a bare return for a float, which is
correct — a number cannot carry a secret substring. No crash-consistency or fsync work
(`AGENTS.md`: out of scope).

## Security model

**Boundary.** One existing boundary is widened and none added: the journal record file, read
by `JournalStore._read_file` from an owner-only mode-0700 directory, carrying `bzr`/Bugzilla
output captured as `handler_output`.

**Actors.** Per `AGENTS.md` the fixture binds to 127.0.0.1 and every datum is fabricated;
neither a remote nor a hostile local actor is in the model. The realistic actor is an
unexpected response from the fixture's own Bugzilla.

**Controls.** Decode stays bounded by `_reject_number` on `parse_constant` — non-finite
values never enter, in either mode — and by the existing duplicate-key, UTF-8, and
regular-file checks, none of which this change touches. `_validate_json` bounds the admitted
type to exactly `float` and to finite values, and rejects before any write. File modes,
symlink refusal, and the retained-descriptor discipline are unchanged.

**Out of scope.** Magnitude and precision are not bounded: `1e308` and `5e-324` round-trip
exactly and the fixture has no reason to police what Bugzilla returns. Denial of service
through a large record is not addressed — the reader is the process that wrote it, on local
disk.

## Acceptance criteria

1. A `CompletedRecord` whose `handler_output` holds `estimated_time: 8.0` and
   `remaining_time: 0.0` is accepted by `__post_init__`, written by `replace_completed`, and
   returned by `JournalStore.read` with `handler_output` equal to what was written, each
   value still exactly `float`, a sibling `int` still exactly `int`, and the record file on
   disk holding `"estimated_time":8.0`.
2. `scenario_digest` survives that round trip and `replace_completed`'s identity comparison
   against the in-flight record still succeeds.
3. `float("nan")`, `float("inf")`, `float("-inf")` in `handler_output` raise
   `ScenarioValidationError` naming `$.handler_output` and `must be a finite number`, at
   construction — before any record file is written, leaving the in-flight record intact.
4. A journal record file whose `handler_output` holds a literal `NaN` fails to decode.
5. `resources.json` containing `1.5` or `NaN` still fails to load, with
   `ScenarioResourceTests::test_rejects_floats_and_non_finite_numbers` unmodified and green.
6. An `events.jsonl` line containing a float still fails to load.
7. `make check` and `make test` green, run bare.

## Testing

Criteria 1–4 go in `tests/test_journal.py`. Criterion 5 is met by leaving
`tests/test_scenario_resources.py` unmodified and green. Criterion 6 adds one test to
`tests/test_scenario_events.py`, which owns the `events.jsonl` decode path — it is what
catches a later mistaken `allow_float=True` on `_parse_events`, which the `resources.json`
test would not. Each new test is verified to bite: revert one half of the fix, observe red,
restore.
