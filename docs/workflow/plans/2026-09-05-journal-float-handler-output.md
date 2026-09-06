# Journal handler output accepts finite JSON numbers — implementation plan

**Goal.** Let a completed journal record carry Bugzilla's `estimated_time` /
`remaining_time` as finite JSON numbers, written and re-read, without loosening the float
rejection that governs authored scenario input.

**Architecture.** Two layers reject a float today and must move together, or the result is a
record that writes and cannot be re-read. `_validate_json` (`src/bzr_live/scenario/journal.py`)
gains a finite-`float` branch; `_decode_json_bytes` (`src/bzr_live/scenario/loader.py`) gains
a keyword-only `allow_float` opt-in that only `JournalStore._read_file` passes. Non-finite
values stay rejected on both layers.

**Tech stack.** Python 3.11+, standard library only, `unittest`, run through `uv`.

Spec: `docs/workflow/specs/2026-09-05-journal-float-handler-output-design.md` ·
ADR: `docs/adr/0014-journal-handler-output-json-numbers.md`

Expected implementation size: 70–95 changed lines (S) — from the file map below: ~18 source lines across three files, ~51 in `tests/test_journal.py`, ~7 in `tests/test_scenario_events.py`.

## Global Constraints

- Python `>=3.11` (`pyproject.toml`). No runtime dependency may be added; `src/bzr_live/` is
  standard-library only.
- Branch `feat/journal-float-handler-output-44`, base `main` at `226b607b`.
- Guardrails run **bare** — no pipes, no `|| true`: `make check`, then `make test`.
- Do not modify `tests/test_scenario_resources.py`. Do not touch
  `tests/test_smoke_trap_status.py`, `docs/bzr-findings.md`, or
  `.github/workflows/scenario-contract.yml` — concurrent work owns them. Do not run
  `make clean` or `make reset`; another session owns containers on this host.
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

## One task — admit finite floats, keep rejecting the rest

One task, because the validator branch, the decoder opt-in, and the four tests are one
deliverable no reviewer would accept in halves: the validator fix alone *is* the write-only
record the change exists to avoid.

**Interfaces.** Defines
`_decode_json_bytes(content: bytes, source: str, field: str = "$", *, allow_float: bool = False) -> object`;
`JsonValue: TypeAlias = bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"] | None`;
`JournalTests.completed(self, attempt: int = 1, next_action: str = "advance", handler_output: object | None = None) -> CompletedRecord`.
Relies on these, confirmed present at `226b607b`: `JournalStore.write_in_flight(record, *,
known_secrets=()) -> Path`; `JournalStore.replace_completed(record, *, known_secrets=()) ->
Path`, returning `comment.000001.json` for these fixtures; `JournalStore.read(event,
attempt=None)`; `CompletedRecord`, `InFlightRecord`, `ScenarioValidationError`,
`freeze_planned`, already imported by `tests/test_journal.py`; `ScenarioEventTests.write()`
and its `self.events` list of dicts, plus `load_scenario(path)` and
`ScenarioValidationError`, already imported by `tests/test_scenario_events.py`.

**The one command**, referred to below as *the focused run*:

```
uv run --python 3.11 python -m unittest tests.test_journal tests.test_scenario_events -v
```

### 1. Parametrize the test helper

In `tests/test_journal.py`, replace `JournalTests.completed`'s signature line and its
`handler_output=` line, leaving every other line of the helper alone:

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

### 2. Write the failing round-trip test, and confirm it fails

Append to `JournalTests`:

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

The focused run must report this test failing with
`journal:$.handler_output.estimated_time: contains an unsupported JSON value`.

### 3. Admit finite floats in the validator, and watch the error change

In `src/bzr_live/scenario/journal.py`, add `import math` to the standard-library import
block, between `import json` and `import os`. Then insert a branch in `_validate_json`,
immediately after the existing `if value is None or type(value) in (bool, int):` block:

```python
    if type(value) is float:
        # Exact type, like the scalars above: a subclass would not survive the JSON round
        # trip as itself. Non-finite values are rejected here so the store never writes a
        # record that `json.dumps` spells `NaN` and no decoder will read back (ADR 0014).
        if not math.isfinite(value):
            raise _journal_error(field, "must be a finite number")
        return value
```

The focused run must still fail that test, now with a **different** error:
`journal:$: floating-point numbers are not supported`, raised from `_decode_json_bytes`.
That is layer two, and observing it here is the proof that the validator fix alone produces
a write-only record. A run that goes green at this step means the read path was not
exercised, and the test is wrong.

### 4. Opt the journal read path into float decoding

In `src/bzr_live/scenario/loader.py`, replace `_decode_json_bytes`'s signature and the two
`parse_*` lines of its `json.loads` call:

```python
def _decode_json_bytes(
    content: bytes, source: str, field: str = "$", *, allow_float: bool = False
) -> object:
```

```python
            # Authored scenario input excludes floats (ADR 0002); captured journal handler
            # output admits finite ones (ADR 0014). Non-finite values are rejected in both
            # modes, because `parse_constant` is not conditional.
            parse_float=float if allow_float else _reject_number(source, field),
            parse_constant=_reject_number(source, field),
```

In `src/bzr_live/scenario/journal.py`, replace `JournalStore._read_file`'s return line:

```python
        return _record_from_json(_decode_json_bytes(b"".join(chunks), "journal", allow_float=True))
```

In `src/bzr_live/scenario/model.py`, replace the `JsonValue` alias line:

```python
JsonValue: TypeAlias = bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"] | None
```

The focused run must now report `OK`.

### 5. Write the two non-finite tests

Append both to `JournalTests` in `tests/test_journal.py`:

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

### 6. Write the authored-input test

Append to `ScenarioEventTests` in `tests/test_scenario_events.py`:

```python
    def test_rejects_a_float_in_an_event_line(self) -> None:
        self.events[1]["estimated_time"] = 1.5
        self.write()
        with self.assertRaises(ScenarioValidationError) as caught:
            load_scenario(self.root)
        self.assertIn("floating-point numbers are not supported", str(caught.exception))
```

The focused run must report `OK` with all four new test names in the listing.

### 7. Verify the tests bite

Four controlled faults, each reverted immediately after observing red, with the observed
output recorded in the build ledger:

1. `journal.py`: `if not math.isfinite(value):` → `if False:`. Expect
   `test_completed_record_rejects_non_finite_numbers_before_writing` red.
2. `journal.py` `_read_file`: drop `allow_float=True`. Expect
   `test_completed_record_round_trips_finite_time_tracking_numbers` red with
   `floating-point numbers are not supported`.
3. `loader.py`: `parse_constant=_reject_number(source, field)` → `parse_constant=float`.
   Expect `test_record_file_holding_a_non_finite_number_fails_to_decode` red.
4. `loader.py`: `allow_float` default → `True`. Expect `test_rejects_a_float_in_an_event_line`
   and `ScenarioResourceTests::test_rejects_floats_and_non_finite_numbers` red — the fault
   that proves the default is what preserves ADR 0002.

### 8. Guardrails, bare, then commit

`make check` — expect exit 0. `make test` — expect exit 0 and `OK` (409 tests at `226b607b`,
413 now). Then:

```
git add -A
git commit -m "fix(journal): accept finite JSON numbers in handler output"
```

**Acceptance.** A completed record carrying `estimated_time: 8.0` and `remaining_time: 0.0`
writes and re-reads, the values exactly `float`, a sibling `id` exactly `int`,
`scenario_digest` unchanged, the on-disk record holding `"estimated_time":8.0`. Non-finite is
refused on write with `must be a finite number` and on read with `floating-point numbers are
not supported`. A float in an `events.jsonl` line still fails to load,
`tests/test_scenario_resources.py` is unmodified and green, and both guardrails are green run
bare.

## Rollback

Every change is a validator branch, a keyword-only parameter with a policy-preserving
default, and a type alias. Reverting the commit restores `226b607b` behaviour exactly; no
state or on-disk record needs migrating, and a record written under this change is
unreadable by an older build only if it actually carries a float — which is the abort this
change removes.
