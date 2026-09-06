# Journal handler output accepts finite JSON numbers — implementation plan

**Goal.** Let a completed journal record carry Bugzilla's `estimated_time` /
`remaining_time` as finite JSON numbers, written and re-read, without loosening the float
rejection that governs authored scenario input. Decision and rationale:
[ADR 0014](../../adr/0014-journal-handler-output-json-numbers.md). Criteria:
`docs/workflow/specs/2026-09-05-journal-float-handler-output-design.md`.

**Tech stack.** Python 3.11+, standard library only, `unittest`, run through `uv`.

Expected implementation size: 74–88 changed lines (S) — from the file map: ~18 source lines across three files, ~45 in `tests/test_journal.py`, ~7 each in `tests/test_scenario_events.py` and `tests/test_scenario_resources.py`.

## Global Constraints

- Python `>=3.11` (`pyproject.toml`); no runtime dependency may be added, `src/bzr_live/` is
  standard-library only. Branch `feat/journal-float-handler-output-44`, cut at `main`
  `226b607b` and merged up to `1c518f26`, which is the base every count below is measured
  against. The `226b607b` citations elsewhere mark where a claim was verified, not the base.
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
| `src/bzr_live/scenario/loader.py` | `_decode_json_bytes` gains keyword-only `allow_float: bool = False`; `_reject_number` gains a `message` parameter so `parse_constant` gets its own wording, since the journal path does support floats. |
| `src/bzr_live/scenario/journal.py` | `import math`; `_validate_json` finite-`float` branch; `_read_file` passes `allow_float=True`. |
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

In `src/bzr_live/scenario/loader.py`, give `_reject_number` a `message` parameter so one
factory serves both hooks, then replace `_decode_json_bytes`'s signature and the two
`parse_*` lines of its `json.loads` call:

```python
def _reject_number(source: str, field: str, message: str) -> Callable[[str], object]:
    def reject(_: str) -> object:
        raise _error(source, field, message)

    return reject
```

```python
def _decode_json_bytes(
    content: bytes, source: str, field: str = "$", *, allow_float: bool = False
) -> object:
```

```python
            # Authored scenario input excludes floats (ADR 0002); captured journal handler
            # output admits finite ones (ADR 0014). `parse_constant` is unconditional -- its
            # message says "non-finite" rather than "floating-point" because it also fires on
            # the journal path, where floats are supported. An overflow literal like 1e400
            # reaches parse_float instead, and is caught downstream by `_validate_json`.
            parse_float=(
                float if allow_float
                else _reject_number(source, field, "floating-point numbers are not supported")
            ),
            parse_constant=_reject_number(source, field, "non-finite numbers are not supported"),
```

In `src/bzr_live/scenario/journal.py`, replace `JournalStore._read_file`'s return line. Leave
`_write_temp` alone — ADR 0014 records why no serializer backstop is added.

```python
        return _record_from_json(_decode_json_bytes(b"".join(chunks), "journal", allow_float=True))
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
        # Reach `replace_completed`'s own re-validation. Passing
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
        # Each case names the mechanism that must refuse it. Asserting the message, not just
        # the exception, is what makes the subtests discriminating: all three would raise
        # `ScenarioValidationError` even if only `math.isfinite` were left.
        cases = (
            ('"estimated_time":8.0', '"estimated_time":NaN',
             "journal:$: non-finite numbers are not supported"),
            ('"estimated_time":8.0', '"estimated_time":1e400',
             "journal:$.handler_output.estimated_time: must be a finite number"),
            # `allow_float=True` is document-scoped, so the per-field guards below are what
            # keep a float out of the rest of the record: four exact `int` tests, plus
            # `freeze_planned`'s `type(value) in (bool, int, str)` for postcondition leaves.
            ('"attempt":1', '"attempt":1.0', "journal:$.attempt: must be a positive integer"),
            ('"exit_status":0', '"exit_status":0.0', "journal:$.exit_status: must be an integer"),
            ('"journal_version":1', '"journal_version":1.0',
             "journal:$.journal_version: unsupported journal version"),
            ('"bug:race":42', '"bug:race":42.0',
             "journal:$.resolved_ids.bug:race: must be a positive integer"),
            ('"private":false', '"private":1.0',
             "journal:$.expected_postcondition.values.private: contains an unsupported value"),
        )
        for index, (original, replacement, expected) in enumerate(cases):
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
                    with self.assertRaises(ScenarioValidationError) as caught:
                        store.read("comment")
                self.assertEqual(str(caught.exception), expected)
```

The message assertions are not decoration, and the build proved it: an earlier draft asserted
only `assertRaises(ScenarioValidationError)`, and fault 3 below did **not** turn it red —
with `parse_constant` disabled, the `NaN` token decodes to `nan` and `math.isfinite` refuses
it a step later, so the test passed while the mechanism it was written for was gone. That is
the same defect shape as the pre-existing `test_rejects_floats_and_non_finite_numbers`: a
check that cannot fail for what it verifies.

`NaN` is refused by `parse_constant`; `1e400` is valid JSON that decodes to `inf` and is
refused only by `_validate_json`'s `math.isfinite` branch, via `_record_from_json`; the
`attempt` case is refused by `_validate_common`'s exact `int` guard, which `allow_float=True`
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
   the `NaN` subtest red on its message assertion, the `1e400` subtest green. **Observed:**
   red only after the subtests were made to assert the message; see step 5.
4. `loader.py`: `allow_float` default → `True` → **both** new authored-input tests red.
   `test_rejects_floats_and_non_finite_numbers` is expected to stay **green** under this
   fault; that is exactly why the two new tests exist.

Every guard this change adds has a fault above. That is the test: a guard with no reachable
fault would be a guard nothing can prove, which is why ADR 0014 rejects the serializer
backstop rather than shipping one untested.

### 8. Guardrails, bare, then commit

`make check` — expect exit 0. `make test` — expect exit 0 and `OK`. The baseline is **412
tests**, measured bare on this branch's merged head with both guardrails green; the five new
tests take it to **417**. The shift from 409 is not a regression — issues #38 and #40 added
three tests to `main` after this branch was cut. Then:

```
git add -A
git commit -m "fix(journal): accept finite JSON numbers in handler output"
```

## Rollback

Reverting the commit restores `226b607b` behaviour exactly. No state or on-disk record needs
migrating; a record written under this change is unreadable by an older build only if it
carries a float, which is the abort this change removes.
