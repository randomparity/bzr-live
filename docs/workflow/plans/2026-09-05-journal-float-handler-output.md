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

In `tests/test_journal.py`, add a fourth parameter `handler_output: object | None = None` to
`JournalTests.completed`'s signature, and replace its `handler_output=` line with the
expression below. Leave every other line of the helper alone.

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
            # output admits finite ones (ADR 0014). `parse_constant` is unconditional, so the
            # bare NaN/Infinity tokens are refused in both modes -- but an overflow literal
            # like 1e400 reaches parse_float, not parse_constant, and is caught downstream by
            # `_validate_json`'s math.isfinite branch.
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
        # Reach `replace_completed`'s own re-validation (journal.py:902). Passing
        # `self.completed(...)` would raise while the argument is evaluated, so the store
        # would never be entered and the assertion would prove nothing.
        smuggled = self.completed()
        object.__setattr__(smuggled, "handler_output", MappingProxyType({"t": float("nan")}))
        with JournalStore(self.state) as store:
            store.write_in_flight(self.in_flight())
            with self.assertRaises(ScenarioValidationError) as caught:
                store.replace_completed(smuggled)
            self.assertIn("must be a finite number", str(caught.exception))
            self.assertIsInstance(store.read("comment"), InFlightRecord)

    def test_record_file_holding_an_unreadable_number_fails_to_decode(self) -> None:
        cases = (
            ('"estimated_time":8.0', '"estimated_time":NaN'),
            ('"estimated_time":8.0', '"estimated_time":1e400'),
            ('"attempt":1', '"attempt":1.0'),
        )
        for index, (original, replacement) in enumerate(cases):
            with self.subTest(replacement=replacement):
                state = Path(self._temporary.name) / f"state{index}"
                with JournalStore(state) as store:
                    store.write_in_flight(self.in_flight())
                    path = store.replace_completed(
                        self.completed(handler_output={"estimated_time": 8.0})
                    )
                content = path.read_text(encoding="utf-8")
                self.assertIn(original, content)
                path.write_text(content.replace(original, replacement), encoding="utf-8")
                path.chmod(0o600)
                with JournalStore(state) as store:
                    with self.assertRaises(ScenarioValidationError):
                        store.read("comment")
```

`NaN` is refused by `parse_constant`; `1e400` is valid JSON that decodes to `inf` and is
refused only by `_validate_json`'s `math.isfinite` branch, via `_record_from_json`; the
`attempt` case is refused by the exact `int` guard at `journal.py:312`, which `allow_float=True`
now leaves as that field's sole float defence. `tests/test_journal.py` already imports
`MappingProxyType` and `Path`.

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

Four controlled faults, each reverted immediately after observing red, with the observed
output recorded in the build ledger:

1. `journal.py`: `if not math.isfinite(value):` → `if False:` → **both** the non-finite write
   test and the `1e400` subtest of the decode test red. The `NaN` subtest stays green — that
   asymmetry is the point of the two cases.
2. `journal.py` `_read_file`: drop `allow_float=True` → round-trip test red with
   `floating-point numbers are not supported`.
3. `loader.py`: `parse_constant=_reject_number(source, field)` → `parse_constant=float` →
   the `NaN` subtest red, the `1e400` subtest green.
4. `loader.py`: `allow_float` default → `True` → **both** new authored-input tests red.
   `test_rejects_floats_and_non_finite_numbers` is expected to stay **green** under this
   fault; that is exactly why the two new tests exist.

`allow_nan=False` gets no fault of its own: it is unreachable while `_validate_json` runs on
every construction path, which is why ADR 0014 records it as a structural backstop rather
than a tested guarantee.

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
