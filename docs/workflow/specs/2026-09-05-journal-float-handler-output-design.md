# Journal handler output accepts finite JSON numbers — design

Issue [#44](https://github.com/randomparity/bzr-live/issues/44) · branch
`feat/journal-float-handler-output-44` off `main` (`226b607b`) · guardrails `make check` and
`make test`, run bare.
[ADR 0014](../../adr/0014-journal-handler-output-json-numbers.md) holds the problem, the
decision, and the rejected alternatives; the plan holds the file-level changes. This spec
adds the criteria the work is accepted against.

## Goal

A completed journal record whose `handler_output` carries a finite JSON number — Bugzilla's
`estimated_time` / `remaining_time` — is written and re-read without error, so a
reconciliation that reads back a time-tracking-bearing bug records its entry and the replay
continues. Authored scenario input keeps rejecting floats.

## Security model

The change parses input the fixture did not author, across one existing boundary that is
widened and none that are added: the journal record file, read by `JournalStore._read_file`
from an owner-only mode-0700 directory, carrying `bzr`/Bugzilla output. Per `AGENTS.md` the
fixture binds to 127.0.0.1 and every datum is fabricated, so the realistic actor is an
unexpected response from the fixture's own Bugzilla, not a remote or hostile local one.
Non-finite values are refused structurally on both sides (`parse_constant` on read,
`allow_nan=False` plus `_validate_json` on write), redaction is a no-op on a number
(ADR 0014 Consequences), and file modes, symlink refusal, and the retained-descriptor
discipline are untouched. Magnitude and precision are deliberately not bounded — `1e308` and
`5e-324` round-trip exactly and the fixture has no reason to police what Bugzilla returns.

## Acceptance criteria

1. A `CompletedRecord` whose `handler_output` holds `estimated_time: 8.0` and
   `remaining_time: 0.0` is accepted by `__post_init__`, written by `replace_completed`, and
   returned by `JournalStore.read` with `handler_output` equal to what was written, each
   value still exactly `float`, a sibling `int` still exactly `int`, and the record file on
   disk holding `"estimated_time":8.0`.
2. `scenario_digest` survives that round trip — a guard on the field, not evidence about the
   digest, which `handler_output` is not an input to (ADR 0014 Consequences).
3. `float("nan")`, `float("inf")`, `float("-inf")` in `handler_output` raise
   `ScenarioValidationError` naming `$.handler_output` and `must be a finite number`, at
   construction — before any record file is written, leaving the in-flight record intact.
4. A journal record file whose `handler_output` holds a literal `NaN` fails to decode, and
   `_write_temp` cannot emit one (`allow_nan=False`).
5. Loading a `resources.json` or an `events.jsonl` line containing a float fails with the
   decoder's own message, `floating-point numbers are not supported`. Asserting the message
   is the point: `ScenarioResourceTests::test_rejects_floats_and_non_finite_numbers` checks
   only that loading fails at field `$`, which an `unknown field` error also satisfies, so it
   stays green under a float-admitting decoder and cannot hold this line by itself. It stays
   unmodified and green; the new tests are what bite.
6. `make check` and `make test` green, run bare.

Criteria 1–4 are tested in `tests/test_journal.py`, 5 by one new test in each of
`tests/test_scenario_resources.py` and `tests/test_scenario_events.py`. Each new test is
verified to bite: revert one half of the fix, observe red, restore.
