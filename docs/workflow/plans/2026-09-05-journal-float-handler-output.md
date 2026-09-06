# Journal handler output accepts finite JSON numbers — implementation plan

**Goal.** Let a completed journal record carry Bugzilla's `estimated_time` /
`remaining_time` as finite JSON numbers, written and re-read, without loosening the float
rejection that governs authored scenario input. Decision and rationale:
[ADR 0014](../../adr/0014-journal-handler-output-json-numbers.md). Criteria:
`docs/workflow/specs/2026-09-05-journal-float-handler-output-design.md`.

**Tech stack.** Python 3.11+, standard library only, `unittest`, run through `uv`.

Expected implementation size: 75–90 changed lines (S) — from the file map: ~19 source lines across three files, ~45 in `tests/test_journal.py`, ~7 each in `tests/test_scenario_events.py` and `tests/test_scenario_resources.py`.

## Global Constraints

- Python `>=3.11` (`pyproject.toml`); no runtime dependency may be added, `src/bzr_live/` is
  standard-library only. Branch `feat/journal-float-handler-output-44`, base `main` at
  `226b607b`.
- Guardrails run **bare** — no pipes, no `|| true`: `make check`, then `make test`.
- `tests/test_scenario_resources.py`: append the new test at the end of the class only;
  `test_rejects_floats_and_non_finite_numbers` stays byte-identical and at line 134.
- Do not touch `tests/test_smoke_trap_status.py`, `docs/bzr-findings.md`, or
  `.github/workflows/scenario-contract.yml`, and do not run `make clean` / `make reset` —
  concurrent sessions own them.
- Deferrals carried from the design review: none.

## File map

| File | Change |
|---|---|
| `src/bzr_live/scenario/loader.py` | `_decode_json_bytes` gains keyword-only `allow_float: bool = False`. |
| `src/bzr_live/scenario/journal.py` | `import math`; `_validate_json` finite-`float` branch; `_read_file` passes `allow_float=True`; `_write_temp` gains `allow_nan=False`. |
| `src/bzr_live/scenario/model.py` | `JsonValue` alias gains `float`. |
| `tests/test_journal.py` | `completed()` helper takes `handler_output`; three new tests. |
| `tests/test_scenario_events.py` | One new test: a float in an `events.jsonl` line still fails. |
| `tests/test_scenario_resources.py` | One new test: a float in `resources.json` fails with the decoder's own message. |

## One task — admit finite floats, keep rejecting the rest

**Interfaces.** Borrowed names, all confirmed present at `226b607b`:
`JournalStore.write_in_flight(record, *, known_secrets=()) -> Path`;
`replace_completed(record, *, known_secrets=()) -> Path`, returning `comment.000001.json`
here; `read(event, attempt=None)`; `ScenarioResourceTests.assert_invalid(source, field)`,
which **returns** the message (`tests/test_scenario_resources.py:63-69`);
`ScenarioEventTests.write()` and its `self.events` list of dicts; and `CompletedRecord`,
`InFlightRecord`, `ScenarioValidationError`, `freeze_planned`, `load_scenario`, each already
imported by the test module that uses it below.

**The focused run**, referred to below:

```
uv run --python 3.11 python -m unittest tests.test_journal tests.test_scenario_events tests.test_scenario_resources -v
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
        # Exact type, like the numeric scalars above: `bool` is an `int` subclass, and an
        # `IntEnum` that passed an upstream `isinstance` guard would be refused here after
        # the in-flight record has landed (see `replay/actions.py:_usable_id`). Non-finite
        # values are rejected so the store never writes a record no decoder reads back.
        if not math.isfinite(value):
            raise _journal_error(field, "must be a finite number")
        return value
```

The focused run must **still** fail that test, now with a different error:
`journal:$: floating-point numbers are not supported`, raised from `_decode_json_bytes`.
That is layer two, and observing it here is the proof that the validator fix alone produces
a write-only record. A run that goes green here means the read path was not exercised and
the test is wrong.

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

In `src/bzr_live/scenario/journal.py`, replace `JournalStore._read_file`'s return line, and
add `allow_nan=False` to `_write_temp`'s `json.dumps` (`journal.py:849`) so the serializer
cannot emit a non-finite value even by a path that skipped the validator:

```python
        return _record_from_json(_decode_json_bytes(b"".join(chunks), "journal", allow_float=True))
```

```python
            content = json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
```

In `src/bzr_live/scenario/model.py`, replace the `JsonValue` alias line:

```python
JsonValue: TypeAlias = bool | int | float | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"] | None
```

The focused run must now report `OK`.

### 5. Write the two non-finite journal tests

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

### 6. Write the two authored-input tests

These assert the **decoder's own message**, not just that loading failed: the existing
`test_rejects_floats_and_non_finite_numbers` checks only `source` and field `$`, both of
which an `unknown field` error also satisfies, so it stays green under a float-admitting
decoder. Append to `ScenarioEventTests` in `tests/test_scenario_events.py`:

```python
    def test_rejects_a_float_in_an_event_line(self) -> None:
        self.events[1]["estimated_time"] = 1.5
        self.write()
        with self.assertRaises(ScenarioValidationError) as caught:
            load_scenario(self.root)
        self.assertIn("floating-point numbers are not supported", str(caught.exception))
```

Append to `ScenarioResourceTests` in `tests/test_scenario_resources.py`, at the end of the
class, leaving every existing line at its current number:

```python
    def test_float_rejection_names_the_decoder_not_a_later_field_error(self) -> None:
        (self.root / "resources.json").write_text(
            '{"format_version":1,"resources":[],"bad":1.5}', encoding="utf-8"
        )
        message = self.assert_invalid("resources.json", "$")
        self.assertIn("floating-point numbers are not supported", message)
```

The focused run must report `OK` with all five new test names in the listing.

### 7. Verify the tests bite

Five controlled faults, each reverted immediately after observing red, with the observed
output recorded in the build ledger:

1. `journal.py`: `if not math.isfinite(value):` → `if False:` → non-finite write test red.
2. `journal.py` `_read_file`: drop `allow_float=True` → round-trip test red with
   `floating-point numbers are not supported`.
3. `loader.py`: `parse_constant=_reject_number(source, field)` → `parse_constant=float` →
   the record-file decode test red.
4. `loader.py`: `allow_float` default → `True` → **both** new authored-input tests red.
   `test_rejects_floats_and_non_finite_numbers` is expected to stay **green** under this
   fault; that is exactly why the two new tests exist.
5. `journal.py`: drop `allow_nan=False` from `_write_temp` and, in a scratch check that
   bypasses `_validate_json`, place a `NaN` in a record → the store writes a file its own
   reader refuses.

### 8. Guardrails, bare, then commit

`make check` — expect exit 0. `make test` — expect exit 0 and `OK` (409 tests at `226b607b`,
414 now). Then:

```
git add -A
git commit -m "fix(journal): accept finite JSON numbers in handler output"
```

## Rollback

Reverting the commit restores `226b607b` behaviour exactly. No state or on-disk record needs
migrating; a record written under this change is unreadable by an older build only if it
carries a float, which is the abort this change removes.
