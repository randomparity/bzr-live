# Journal handler output accepts finite JSON numbers — design

Issue [#44](https://github.com/randomparity/bzr-live/issues/44) · branch
`feat/journal-float-handler-output-44`, cut at `main` `226b607b`, merged up to `1c518f26` ·
guardrails `make check` and `make test`, run bare.
[ADR 0014](../../adr/0014-journal-handler-output-json-numbers.md) holds the problem, the
decision, and the rejected alternatives; the plan holds the file-level changes. This spec
carries the threat model and the criteria the work is accepted against.

## Goal

A completed journal record whose `handler_output` carries a finite JSON number — Bugzilla's
`estimated_time` / `remaining_time` — is written and re-read without error, so a
reconciliation that reads back a time-tracking-bearing bug records its entry and the replay
continues. Authored scenario input keeps rejecting floats.

## Threat model

**Boundary:** one widened, none added — the journal record file, read by
`JournalStore._read_file` from an owner-only mode-0700 directory. **Actor:** per `AGENTS.md`
the fixture binds to 127.0.0.1 and every datum is fabricated, so the only realistic actor is
an unexpected response from the fixture's own Bugzilla. **Controls:** the widening is one
document-scoped decoder flag, so the exact `int` guards ADR 0014 enumerates carry every
non-`handler_output` field; non-finite values are refused by `parse_constant` and
`math.isfinite` between them, both reachable and both tested; file modes, symlink refusal,
and the retained-descriptor discipline are untouched. **Out of scope:** magnitude and
precision are
deliberately unbounded — `1e308` and `5e-324` round-trip exactly and the fixture has no
reason to police what Bugzilla returns.

## Acceptance criteria

1. A `CompletedRecord` whose `handler_output` holds `estimated_time: 8.0` and
   `remaining_time: 0.0` is accepted by `__post_init__`, written by `replace_completed`, and
   returned by `JournalStore.read` with `handler_output` equal to what was written, each
   value still exactly `float`, a sibling `int` still exactly `int`, and the record file on
   disk holding `"estimated_time":8.0`.
2. `scenario_digest` survives that round trip — a guard on the field, not evidence about the
   digest, which `handler_output` is not an input to (ADR 0014 Consequences).
3. `float("nan")`, `float("inf")`, `float("-inf")` in `handler_output` raise
   `ScenarioValidationError` naming `$.handler_output` and `must be a finite number`, both at
   construction and inside `replace_completed`, which re-validates at `journal.py:902` — so
   the in-flight record survives a completion the store refuses.
4. A record file fails to decode when `handler_output` holds a literal `NaN` (refused by
   `parse_constant`) **and** when it holds `1e400` (valid JSON, decodes to `inf`, refused by
   `math.isfinite` — `parse_constant` is never called for it). A float in `attempt` fails the
   same way, through the exact `int` guard.
5. Loading a `resources.json` or an `events.jsonl` line containing a float fails with the
   decoder's own message, `floating-point numbers are not supported`. Asserting the message
   is the point: `ScenarioResourceTests::test_rejects_floats_and_non_finite_numbers` checks
   only that the load fails at field `$`, so it stays green under a float-admitting decoder.
   It stays unmodified; the new tests are what bite.
6. `make check` and `make test` green, run bare.

Criteria 1–4 are tested in `tests/test_journal.py`, 5 by one new test in each of
`tests/test_scenario_resources.py` and `tests/test_scenario_events.py`. Each new test is
verified to bite: revert one half of the fix, observe red, restore.
