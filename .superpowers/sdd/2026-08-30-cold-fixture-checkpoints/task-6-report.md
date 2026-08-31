# Task 6 report: bounded checkpoint parsing failures

## Review

Starting HEAD: `8f06e901153016fc9f73ce532c5b3a57509ecebf`.

The review retained three narrow changes:

- `read_manifest` now normalizes `ValueError` and `RecursionError` from Python's JSON decoder into `CheckpointError`. `ValueError` includes the former `JSONDecodeError` arm and the bounded-integer conversion failure raised by current Python; `RecursionError` covers excessively nested JSON. Existing `CheckpointError` diagnostics, including duplicate-key rejection, still pass through unchanged.
- `_wait_timeout` now accepts only one to six decimal digits and the range 1 through 999999 before converting to `int`. This preserves all previously valid operational timeout values while preventing an unbounded environment value from reaching integer conversion.
- The unused `capture_stderr` option was removed from `run_child` and the test recorder. No production caller supplied it, its default was always true, and `run_child` continues to capture and bound subprocess diagnostics exactly as before.

`tests/test_checkpoint.py` now covers oversized and deeply nested manifests, invalid and unbounded `BZ_WAIT_TIMEOUT` values, and the accepted upper timeout bound. No further correction was necessary.

## Focused verification

Command:

```text
uv run --python 3.11 python -m unittest tests.test_checkpoint -v
```

Result: exit 0; `Ran 59 tests in 0.185s`; `OK`.

Generated untracked `__pycache__` directories were removed after verification.

## Commit

Commit subject: `fix: bound checkpoint parsing failures`.

Commit identity: this report, `src/bzr_live/checkpoint.py`, and `tests/test_checkpoint.py` are the files in the single commit at `HEAD` with that subject. A literal SHA cannot be embedded in a file that is itself part of the same content-addressed commit; the exact SHA is therefore the output of `git rev-parse HEAD` after this commit.
