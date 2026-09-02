# 0009. Response-loss fault injection at the boundary seam

## Status

Accepted (2026-09-02)

## Context

ADR 0006 decided how an interrupted replay run recovers: the engine writes an in-flight
record before the mutation, reconciles against the fixture when the boundary does not
answer usefully, and writes exactly one completed record carrying `advance`, `retry`, or
`stop`. Its first consequence is stated as a guarantee — "an interrupted run resumes to
exactly one semantic result per event or refuses with a reset/replay instruction. An
append is never repeated."

Nothing in the repository interrupts a run. The unit suite drives `ReplayContext` with
`_FakeRun` (`tests/test_replay.py:306-322`), a queue of canned replies that ignores what
was sent, and `tests/replay_smoke.sh` needs a running Docker fixture and a `bzr` binary,
so it runs in neither `make test` nor either of CI's two hard gates. The guarantee is
therefore asserted and unproven, for every action the scenario contract supports.

Issue #24 asks for the proof, for five actions specifically — `bug.create`, `bug.update`,
`bug.comment`, `bug.attach`, `bug.worktime` — and constrains how: the shipped replay path
must be the code under test, and recovery must run through `resume`.

Two facts shape the answer. First, `src/bzr_live/replay/engine.py:187-196` already catches
a boundary error around the mutation and settles it *in-run*, so a fault the engine can
catch never reaches `resume`. Second, `src/bzr_live/scenario/journal.py:28-38` maps the
eight actions onto three recovery classes, and `bug.comment`, `bug.attach`, and
`bug.worktime` share `append` while carrying three different `reconcile` bodies
(`src/bzr_live/replay/actions.py:459-465`, `:504-514`, `:535-542`): one matches a
different reply field and adopts an id, and one resolves its bug id from a different
postcondition key.

## Decision

**The fault is injected at `ReplayContext`'s existing `run` seam, and no file under
`src/` changes.** `ReplayContext.__init__` already takes `run=subprocess.run`
(`src/bzr_live/replay/context.py:31-33`). A test wraps that callable; the engine,
handlers, and journal that execute are the shipped ones.

**The fault is a killed runner, not a caught error.** The wrapper lets the boundary apply
the mutation, then raises an exception deriving from `BaseException`, which unwinds out of
`replay()` past the engine's `except (ProvisionError, ReplayError)` arm. What survives is
exactly what a `SIGKILL` leaves: the in-flight record on disk, and nothing else. Recovery
is then driven by a second `ReplayEngine` over the same journal directory, with its own
`JournalStore` and its own workspace directory — the two things `__main__.py` creates per
process (`src/bzr_live/replay/__main__.py:35-41`).

**The boundary is a stateful in-memory double, not a reply queue.** A double that applies
what it is sent is what makes "the server holds exactly one entry carrying this marker"
an assertion that can fail; what it models and refuses is the spec's to fix.

**All five actions are covered, not the three classes they collapse into**, and
`bug.update` is covered twice — once where the declared fields read back and once where
they do not. The spec's case table is the record of both.

## Consequences

- The double is a second model of the boundary and can drift from the real one, and that
  drift is **accepted rather than mitigated**. Nothing automatic re-checks it: the only
  live comparison is `tests/replay_smoke.sh`, which this record's own Context establishes
  gates nothing — it needs a healthy fixture and a `bzr` binary and runs when an operator
  chooses to run it. So a bzr or Bugzilla change that invalidated the double would leave
  these tests green. That is the bargain a disposable fixture gets: the double proves
  *engine recovery*, it is kept small enough that an operator can read it against
  `actions.py` in one sitting, and it claims nothing about bzr's behaviour. This record
  gives the residual no owner **on purpose** — an automatic live comparison would be a
  second CI job against a real Bugzilla, which `AGENTS.md`'s "Project scope" declines for
  this repository. Not a deferral awaiting a tracker entry: a decision not to hold the
  line automatically.
- Because the double implements only what the five actions reach, a later test that
  exercises `bug.flag`, `attachment.update`, or the REST custom-field boundary through it
  fails with "does not model" rather than passing on a default. That is the intended cost
  of not modelling the whole API.
- The killed runner is an exception unwinding a live process, not a real `SIGKILL`. What
  this proves is that the journal's on-disk state after `write_in_flight` is sufficient
  for `resume` to recover. It does not prove anything about a mutation lost between
  `write_in_flight` returning and the filesystem flushing; `AGENTS.md` scopes crash
  consistency out, so that residual is accepted rather than closed.
- `bug.update` resolving two ways is now pinned rather than incidental. A change that made
  the unreadable-field case reconcile as `advance` would be silently wrong — it would
  adopt a postcondition nothing confirmed — and one of these two tests is what catches it.
- No production file changes, so nothing here narrows what the engine may do. These tests
  are the only thing standing between a future engine edit and a silently lost guarantee.

## Considered & rejected

- **Add a fault hook to the engine — an injection flag, a hookable method, or a test-only
  `ReplayEngine` subclass.** verified: issue #24's "Proposed approach" requires injecting
  at the existing seam so the shipped replay path is the one under test; a recovery path
  reachable only from a test proves that path and not the shipped one.
- **Drive the fault through the existing `_FakeRun`.** verified: `_FakeRun.__call__`
  (`tests/test_replay.py:315-322`) pops a queued reply and never reads `argv`, so its state
  after a run is the queue the test wrote. "Exactly one comment carries this marker" would
  be an assertion about the fixture data, not about the fixture, and could not fail on a
  duplicated append.
- **Model the loss as a `ProvisionError` from the mutation and stop there.** verified:
  `src/bzr_live/replay/engine.py:190-193` catches it, calls `_settle`, and returns
  "reconciled" when the mutation committed — the run continues and `resume` is never
  reached, which is the one thing issue #24's fourth criterion requires.
- **Prove it live in `tests/replay_smoke.sh` against the Docker fixture.** judgment: the
  smoke needs a healthy fixture and a `bzr` binary, so it gates no PR; the guarantee would
  stay unproven on every change that could break it. Interrupting a real subprocess
  mid-mutation is also not reproducible enough to assert on.
- **Cover the three recovery classes instead of the five actions.** verified:
  `src/bzr_live/scenario/journal.py:28-38` puts `bug.comment`, `bug.attach`, and
  `bug.worktime` in one `append` class, but each has its own `reconcile`
  (`src/bzr_live/replay/actions.py:459-465`, `:504-514`, `:535-542`). `bug.attach` matches
  a different reply field (`summary` on `attachment list`) and adopts an id;
  `bug.worktime` shares `bug.comment`'s field but resolves its bug id from a different
  postcondition key (`values["bug"]` rather than `expected_postcondition["target"]`). One
  append case leaves both unexercised.
- **Model the rest of the Bugzilla API in the double — flags, attachment updates, the REST
  custom-field boundary, the `TINYTEXT` truncation ADR 0006 observed.** judgment: fidelity
  no test in this change consumes, and untested branches in a test double are the place a
  wrong model hides longest.
- **Add a `make fault-injection` target.** verified: `make test` runs
  `uv run --python 3.11 python -m unittest discover -s tests -v` (`Makefile:34-36`), whose
  default pattern already collects `tests/test_fault_injection.py`. A target would add an
  edit to a file two concurrent branches share, for nothing.
- **Do nothing: leave the guarantee to ADR 0006's prose.** judgment: it is the guarantee
  the whole journal exists to provide, and the epic's own issue #7 names it as the thing to
  prove; an unproven invariant in recovery code is the kind that stays true until the day
  it does not.
