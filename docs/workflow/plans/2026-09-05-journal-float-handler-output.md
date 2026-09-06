# Journal handler output accepts finite JSON numbers — implementation plan

**Goal.** Let a completed journal record carry Bugzilla's `estimated_time` /
`remaining_time` as finite JSON numbers, written and re-read, without loosening the float
rejection that governs authored scenario input.

**Architecture.** Two layers reject a float today and both must move together, or the
result is a record that writes and cannot be re-read. `_validate_json`
(`src/bzr_live/scenario/journal.py`) gains a finite-`float` branch; `_decode_json_bytes`
(`src/bzr_live/scenario/loader.py`) gains a keyword-only `allow_float` opt-in that only
`JournalStore._read_file` passes. Non-finite values stay rejected on both layers.

**Tech stack.** Python 3.11+, standard library only, `unittest`. Run through `uv`.

Spec: `docs/workflow/specs/2026-09-05-journal-float-handler-output-design.md`
Decision record: `docs/adr/0014-journal-handler-output-json-numbers.md`

Expected implementation size: 90–120 changed lines (S) — derived from the file map below: ~14 source lines across three files, ~70 test lines in `tests/test_journal.py`, ~8 in `tests/test_scenario_events.py`.

## Global Constraints

- Python `>=3.11` (`pyproject.toml`). No runtime dependencies may be added; `src/bzr_live/`
  is standard-library only.
- Branch `feat/journal-float-handler-output-44`, base `main` at `226b607b`.
- Guardrails, run **bare** — no pipes, no `|| true`: `make check`, then `make test`.
- Do not touch `tests/test_smoke_trap_status.py`, `docs/bzr-findings.md`, or
  `.github/workflows/scenario-contract.yml` — concurrent work owns them.
- Do not run `make clean` or `make reset`; another session owns containers on this host.
- `tests/test_scenario_resources.py` must not be modified.
- Deferrals carried from the design review: none at authoring time; append any the review
  disposes of here.

## File map

| File | Change |
|---|---|
| `src/bzr_live/scenario/loader.py` | `_decode_json_bytes` gains keyword-only `allow_float: bool = False`. |
| `src/bzr_live/scenario/journal.py` | `import math`; `_validate_json` finite-`float` branch; `_read_file` passes `allow_float=True`. |
| `src/bzr_live/scenario/model.py` | `JsonValue` alias gains `float`. |
| `tests/test_journal.py` | `completed()` helper takes `handler_output`; three new tests. |
| `tests/test_scenario_events.py` | One new test: a float in an `events.jsonl` line still fails. |

## Task 1 — the journal round-trips a finite float

**Interfaces.** Consumes nothing from earlier tasks. Defines, for Task 2:

- `_decode_json_bytes(content: bytes, source: str, field: str = "$", *, allow_float: bool = False) -> object`
- `_validate_json(value: object, field: str) -> JsonValue` — now returns `float` too
- `JsonValue: TypeAlias = bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"] | None`
- `JournalTests.completed(self, attempt: int = 1, next_action: str = "advance", handler_output: object | None = None) -> CompletedRecord`

Existing names this task relies on, confirmed present at `226b607b`:
`JournalStore.write_in_flight(record, *, known_secrets=()) -> Path`,
`JournalStore.replace_completed(record, *, known_secrets=()) -> Path`,
`JournalStore.read(event, attempt=None) -> InFlightRecord | CompletedRecord | None`,
`ScenarioValidationError`, `CompletedRecord`, `InFlightRecord`.

### Step 1.1 — parametrize the test helper

In `tests/test_journal.py`, change `JournalTests.completed` so callers can supply the
handler output. Replace its signature line and its `handler_output=` line with:

```python
    def completed(
        self,
        attempt: int = 1,
        next_action: str = "advance",
        handler_output: object | None = None,
    ) -> CompletedRecord:
```

```python
            handler_output=freeze_planned({"ok": True}) if handler_output is None else handler_output,
```

Leave every other line of the helper as it is.

### Step 1.2 — write the failing round-trip test

Append to `JournalTests` in `tests/test_journal.py`:

```python
    def test_completed_record_round_trips_finite_time_tracking_numbers(self) -> None:
        output = {"estimated_time": 8.0, "remaining_time": 0.0, "id": 42}
        with JournalStore(self.state) as store:
            store.write_in_flight(self.in_flight())
            path = store.replace_completed(self.completed(handler_output=output))
            record = store.read("comment")
        self.assertIsInstance(record, CompletedRecord)
        self.assertEqual(dict(record.handler_output), output)  # type: ignore[union-attr,arg-type]
        for key in ("estimated_time", "remaining_time"):
            self.assertIs(type(record.handler_output[key]), float)  # type: ignore[union-attr,index]
        self.assertIs(type(record.handler_output["id"]), int)  # type: ignore[union-attr,index]
        self.assertEqual(record.scenario_digest, self.digest)  # type: ignore[union-attr]
        self.assertIn('"estimated_time":8.0', path.read_text(encoding="utf-8"))
```

### Step 1.3 — confirm it fails

```
uv run --python 3.11 python -m unittest tests.test_journal -v -k round_trips_finite
```

Expect `FAILED (errors=1)` with
`ScenarioValidationError: journal:$.handler_output.estimated_time: contains an unsupported JSON value`.

### Step 1.4 — admit finite floats in the validator

In `src/bzr_live/scenario/journal.py`, add `import math` to the standard-library import
block, alphabetically between `import json` and `import os`.

Then, in `_validate_json`, insert a branch immediately after the existing
`if value is None or type(value) in (bool, int):` block:

```python
    if type(value) is float:
        # Exact type, like the scalars above: a subclass would not survive the JSON round
        # trip as itself. Non-finite values are rejected here so the store never writes a
        # record that `json.dumps` spells `NaN` and no decoder will read back (ADR 0014).
        if not math.isfinite(value):
            raise _journal_error(field, "must be a finite number")
        return value
```

### Step 1.5 — confirm the write path passes and the read path still fails

```
uv run --python 3.11 python -m unittest tests.test_journal -v -k round_trips_finite
```

Expect `FAILED (errors=1)` still, but now with a different error:
`ScenarioValidationError: journal:$: floating-point numbers are not supported`, raised from
`_decode_json_bytes`. This is layer two, and observing it here is the proof that the
validator fix alone produces a write-only record.

### Step 1.6 — opt the journal read path into float decoding

In `src/bzr_live/scenario/loader.py`, replace the `_decode_json_bytes` signature and its
`json.loads` call:

```python
def _decode_json_bytes(
    content: bytes, source: str, field: str = "$", *, allow_float: bool = False
) -> object:
```

```python
        raw = json.loads(
            text,
            object_pairs_hook=_Pairs,
            # Authored scenario input excludes floats (ADR 0002); captured journal handler
            # output admits finite ones (ADR 0014). Non-finite values are rejected in both
            # modes, because `parse_constant` is not conditional.
            parse_float=float if allow_float else _reject_number(source, field),
            parse_constant=_reject_number(source, field),
        )
```

In `src/bzr_live/scenario/journal.py`, `JournalStore._read_file`, replace the return line:

```python
        return _record_from_json(_decode_json_bytes(b"".join(chunks), "journal", allow_float=True))
```

In `src/bzr_live/scenario/model.py`, replace the `JsonValue` alias line:

```python
JsonValue: TypeAlias = bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"] | None
```

### Step 1.7 — confirm it passes

```
uv run --python 3.11 python -m unittest tests.test_journal -v -k round_trips_finite
```

Expect `OK` with 1 test run.

### Step 1.8 — commit

```
git add -A && git commit -m "fix(journal): accept finite JSON numbers in handler output"
```

**Acceptance.** A completed record carrying `estimated_time: 8.0` and `remaining_time: 0.0`
writes and re-reads; the re-read values are exactly `float`, the `id` stays exactly `int`,
`scenario_digest` is unchanged, and the on-disk record holds `"estimated_time":8.0`.

## Task 2 — non-finite values stay rejected on both layers, and authored input is unchanged

**Interfaces.** Consumes every name Task 1 defined. Defines nothing for later tasks.

Existing names this task relies on, confirmed present at `226b607b`:
`JournalStore._filename` yields `comment.000001.json`, so `replace_completed` returns that
path; `ScenarioEventTests.write()` and `ScenarioEventTests.events` in
`tests/test_scenario_events.py`; `load_scenario(path)` exported from `bzr_live.scenario`.

### Step 2.1 — write the non-finite write-path test

Append to `JournalTests` in `tests/test_journal.py`:

```python
    def test_completed_record_rejects_non_finite_numbers_before_writing(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ScenarioValidationError) as caught:
                    self.completed(handler_output={"estimated_time": value})
                self.assertIn("$.handler_output.estimated_time", str(caught.exception))
                self.assertIn("must be a finite number", str(caught.exception))
        with JournalStore(self.state) as store:
            store.write_in_flight(self.in_flight())
            with self.assertRaises(ScenarioValidationError):
                store.replace_completed(self.completed(handler_output={"t": float("nan")}))
            self.assertIsInstance(store.read("comment"), InFlightRecord)
```

### Step 2.2 — write the non-finite read-path test

Append to `JournalTests` in `tests/test_journal.py`:

```python
    def test_record_file_holding_a_non_finite_number_fails_to_decode(self) -> None:
        with JournalStore(self.state) as store:
            store.write_in_flight(self.in_flight())
            path = store.replace_completed(self.completed(handler_output={"estimated_time": 8.0}))
        content = path.read_text(encoding="utf-8")
        self.assertIn('"estimated_time":8.0', content)
        path.write_text(content.replace('"estimated_time":8.0', '"estimated_time":NaN'), encoding="utf-8")
        path.chmod(0o600)
        with JournalStore(self.state) as store:
            with self.assertRaises(ScenarioValidationError) as caught:
                store.read("comment")
        self.assertIn("floating-point numbers are not supported", str(caught.exception))
```

### Step 2.3 — write the authored-input test

Append to `ScenarioEventTests` in `tests/test_scenario_events.py`:

```python
    def test_rejects_a_float_in_an_event_line(self) -> None:
        self.events[1]["estimated_time"] = 1.5
        self.write()
        with self.assertRaises(ScenarioValidationError) as caught:
            load_scenario(self.root)
        self.assertIn("floating-point numbers are not supported", str(caught.exception))
```

### Step 2.4 — confirm all three pass

```
uv run --python 3.11 python -m unittest tests.test_journal tests.test_scenario_events -v
```

Expect `OK`, with the three new test names in the listing.

### Step 2.5 — verify the tests bite

Three controlled faults, each reverted immediately after observing red:

1. In `journal.py`, change `if not math.isfinite(value):` to `if False:`. Expect
   `test_completed_record_rejects_non_finite_numbers_before_writing` to fail. Revert.
2. In `journal.py` `_read_file`, drop `allow_float=True`. Expect
   `test_completed_record_round_trips_finite_time_tracking_numbers` to fail with
   `floating-point numbers are not supported`. Revert.
3. In `loader.py`, change `parse_constant=_reject_number(source, field)` to
   `parse_constant=float`. Expect `test_record_file_holding_a_non_finite_number_fails_to_decode`
   to fail. Revert.
4. In `loader.py`, change the `allow_float` default to `True`. Expect
   `test_rejects_a_float_in_an_event_line` and
   `test_rejects_floats_and_non_finite_numbers` (in `tests/test_scenario_resources.py`) to
   fail — this is the fault that proves the default is what preserves ADR 0002. Revert.

Record the observed red output for each in the build ledger.

### Step 2.6 — run the guardrails bare

```
make check
```
Expect exit 0, no output beyond the tools' own.

```
make test
```
Expect exit 0 and `OK` with the suite total (409 tests at `226b607b`, now 413).

### Step 2.7 — commit

```
git add -A && git commit -m "test(journal): cover finite and non-finite JSON numbers"
```

**Acceptance.** Non-finite values are refused on the write path with
`must be a finite number` and on the read path with
`floating-point numbers are not supported`; a float in an `events.jsonl` line still fails to
load; `tests/test_scenario_resources.py` is unmodified and green; `make check` and
`make test` are green run bare.

## Rollback

Every change is additive to a validator branch, a keyword-only parameter with a
policy-preserving default, and a type alias. Reverting the two commits restores `226b607b`
behaviour exactly; no state, file format, or on-disk record needs migrating, and a record
written under this change is only unreadable by an older build if it actually carries a
float — which is the abort this change removes.
