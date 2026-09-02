# Response-loss recovery for the five replay actions — design

Issue: [#24](https://github.com/randomparity/bzr-live/issues/24) (part of #7).
Decision record: [ADR 0009](../../adr/0009-response-loss-fault-injection.md).
Governing prior decision: [ADR 0006](../../adr/0006-actor-scoped-event-replay.md).

## Goal

Prove, inside `make test`, that a response lost *after* the fixture has applied a mutation
recovers through `resume` to exactly one semantic result — or refuses with a reset/replay
instruction — for `bug.create`, `bug.update`, `bug.comment`, `bug.attach`, and
`bug.worktime`.

## Why this is not already proven

ADR 0006's first consequence states the guarantee. The repository has no way to interrupt
a run:

- `tests/test_replay.py`'s `_FakeRun` is a queue of canned replies that never reads what
  was sent, so it holds no fixture state to assert against.
- `tests/replay_smoke.sh` needs a healthy Docker fixture and a `bzr` binary; it gates
  neither `make test` nor either CI job.
- No fault-injection seam is referenced anywhere in `src/` or `tests/`.

The three existing tests that reach reconciliation
(`test_an_exit_zero_reply_with_no_usable_id_reconciles`,
`test_a_failing_mutation_aborts_the_run_and_journals_the_retry`,
`test_resume_adopts_a_committed_in_flight_create`) cover `bug.create` only, and the first
two settle in-run rather than through `resume`.

## Architecture

One new test module, `tests/test_fault_injection.py`, holding three things:

1. **`FakeBugzilla`** — a stateful in-memory stand-in for the fixture, driven through
   `bzr`'s own argv. It is passed as `ReplayContext(run=...)`, the seam that already
   exists.
2. **`_ResponseLoss`** — a wrapper around `FakeBugzilla` that lets one named mutation
   commit and then raises `_RunnerKilled`, a `BaseException` subclass.
3. **The recovery tests** — each one runs a replay that dies mid-event, then a second,
   independent `resume` over the same journal.

No file under `src/` changes. `Makefile` is not touched: `make test` already discovers
`tests/test_*.py`.

## Data flow

```
process 1:  ReplayEngine.replay()
              -> _execute: write_in_flight(...)          [journal: in-flight record]
              -> _context.invoke -> _ResponseLoss.__call__
                                      -> FakeBugzilla applies the mutation   [server: committed]
                                      -> raise _RunnerKilled                 [runner: gone]
            (nothing further is written; the JournalStore and workspace are closed)

process 2:  ReplayEngine.resume()
              -> _check_local_preconditions -> reads the in-flight record
              -> _advance -> _settle -> HANDLERS[action].reconcile(FakeBugzilla)
              -> write_completed(next_action)            [journal: completed record]
```

`_RunnerKilled` derives from `BaseException` so the engine's
`except (ProvisionError, ReplayError)` arm cannot absorb it. A fault any handler could
catch would exercise the in-run reconciliation path, which is not the path issue #24 names.

Each "process" gets its own `JournalStore` (the store takes an exclusive `flock`, so the
first must be closed before the second opens) and its own workspace directory — the two
per-run resources `src/bzr_live/replay/__main__.py:33-41` creates. Sharing the workspace
would collide on `ReplayContext._write_private`'s `O_EXCL` open, because the file counter
restarts at 1 in a fresh context.

## `FakeBugzilla`

### What it models

Only the commands the five actions' handlers issue, with the reply shapes those handlers
read:

| Command | Effect | Reply |
|---|---|---|
| `bug create --from-json=<path>` | reads the document, assigns the next bug id, stores the alias, stores the description as the bug's first comment | the stored bug dict |
| `bug view -- <alias-or-id>` | none | the stored bug dict; exit 4 + `api_code` 100 (alias) or 101 (id) when absent |
| `bug update <flags> -- <id>` | applies `--status` and `--target-milestone`; appends the text of `--comment-file`; accepts and discards `--work-time`, `--estimated-time`, `--remaining-time` | the updated bug dict |
| `comment add --body-file=<path> -- <id>` | appends a comment carrying the file's text | `{"id": <comment id>}` |
| `comment list -- <id>` | none | `[{"id": …, "text": …}, …]` |
| `attachment upload --summary=… --content-type=… -- <id> <path>` | appends an attachment carrying the summary | `{"id": <attachment id>}` |
| `attachment list -- <id>` | none | `[{"id": …, "summary": …, "is_obsolete": …}, …]` |

Three behaviours are modelled because a reconciliation result depends on them:

- **The description is the bug's first comment.** Bugzilla stores it as comment 0 and
  `bzr comment list` returns it (`src/bzr_live/replay/actions.py:286-291`), so it is part
  of the corpus the append reconcilers search.
- **`--estimated-time` and `--remaining-time` are applied but never serialized by
  `bug view`.** That is finding D3 (ADR 0006), and it is what makes the fixture's
  `update-triage` reconcile as `retry` rather than `advance`.
- **A second create declaring an existing alias fails.** ADR 0006 records the constraint as
  observed live against the fixture. The double fails the test outright rather than
  answering exit 4 as the real server does: the only way to reach it here is a successful
  create being repeated, which is exactly the defect these tests hunt.

Every applied mutation is appended to `FakeBugzilla.mutations` as
`(operation, target)`. That list is how a test asserts a mutation was sent once and not
twice.

### What it deliberately does not model

`bug.flag`, `attachment.update`, `attachment view`, the REST custom-field boundary, the
silent `TINYTEXT` truncation of `attachments.description`, comment and attachment privacy
(`--private`), and every `bug update` flag the scoped events do not send (`--summary`,
`--resolution`, `--assignee`, `--dupe-of`, `--reset-assigned-to`, and the
`--*-add`/`--*-remove` list deltas). An unmodelled command, flag, **or switch** raises
`AssertionError` naming it, so a future test that reaches one fails loudly instead of
passing on a default reply. Switches need saying separately because they carry no `=`: a
refusal that inspected only `name=value` arguments would accept `--private` and
`--reset-assigned-to` silently, which is the default reply the refusal exists to prevent.
Branches no test exercises are where a wrong model hides longest, so the double stays at
exactly what the seven cases reach.

The double proves engine recovery. It proves nothing about `bzr` or Bugzilla; that
remains `tests/replay_smoke.sh`'s job.

## Cases

Every case runs the same two-process shape above. `<prefix>` is whatever earlier events
the target needs in order to resolve its references, and those execute normally.

| # | Action | Scenario | Faulted operation | Expected `resume` outcome |
|---|---|---|---|---|
| 1 | `bug.create` | `[create]` | `bug create` | `resumed`; one bug on the server; `bug create` sent once; completed record `advance`, `exit_status` -1, `resolved_ids` naming the created bug |
| 2 | `bug.update` (reads back) | `[create, update-status]` | `bug update` | `resumed`; `bug update` sent once; the declared status and milestone hold |
| 3 | `bug.update` (does not read back) | `[create, update-triage]` | `bug update` | `executed`; attempt 1 recorded `retry`, attempt 2 `advance`; `bug update` sent twice; one semantic result — the declared set holds and no append was created |
| 4 | `bug.comment` | `[create, comment-triage]` | `comment add` | `resumed`; exactly one comment carries the marker, before and after |
| 5 | `bug.attach` | `[create, attach-notes]` | `attachment upload` | `resumed`; exactly one attachment carries the marker; the completed record adopts its id |
| 6 | `bug.worktime` | `[create, worktime-triage]` | `bug update` | `resumed`; exactly one comment carries the marker |
| 7 | refusal arm | `[create, comment-triage]` | `comment add` | `resume` raises; the message names the marker, "did not answer", and `CONFIRM_RESET=1 make reset`; the comment is not re-sent |

Case 2 needs a `bug.update` event whose every declared field reads back, and the fixture
has none — `update-triage` declares `remaining_hours` deliberately. The case therefore
hand-builds a `PlannedEvent` declaring `status` and `milestone` only and splices it into
the scenario in place of `update-triage`, the way `tests/test_replay.py:484-505` already
hand-builds one for the same reason.

Case 3 is the honest reading of ADR 0006's "one extra idempotent invocation, never a
duplicate": two mutations, one semantic result. Asserting the mutation count is 2 *and*
that the server holds no second comment or attachment is what distinguishes it from a
duplicated append.

Case 7 reaches the refusal arm through a boundary whose `comment list` answers exit 2 —
`BzrClient.read` maps that to `None`, `_entries` refuses to read it as an empty list, and
`_append_result` returns `stop` (`src/bzr_live/replay/actions.py:206-212`). This is the
one arm where a response loss leaves the fixture unreconcilable, and it is reached through
`resume` like every other case.

## Error handling

- An unmodelled command, an unknown flag on a modelled command, or a missing referenced
  file raises `AssertionError` naming what was reached. Silence would let a test pass on a
  mutation that never happened.
- `_ResponseLoss` records whether it fired. Every case asserts it did, so a predicate that
  stops matching (a renamed flag, a changed operation) fails the test rather than turning
  it into a plain replay that trivially passes.
- Case 1's post-fault assertion that the journal holds an `InFlightRecord` and *not* a
  `CompletedRecord` is what proves the fault landed where the design says it does — after
  `write_in_flight` and before any completed write.

## Testing

`tests/test_fault_injection.py` also carries a fidelity test for the double itself: a
create, a comment, and an attachment round-trip through it, and `_ResponseLoss` is shown
to let the mutation land before raising. Without it, a double that silently dropped every
mutation would make all seven "no duplicate" assertions pass vacuously.

Guardrails: `make check` (`compileall` covers `tests/`) and `make test`. `make smoke` and
`make replay-smoke` need a running fixture and are unaffected by this change.

## Not in scope

- Any change under `src/`. Criterion 5 forbids it, and nothing here needs it.
- `src/bzr_live/verify/`, owned by concurrent issue #20.
- The three actions outside issue #24's list (`bug.flag`, `bug.custom-field-set`,
  `attachment.update`). They are `idempotent-set` and `idempotent-set`-like and the
  `bug.update` cases cover that class's two resolutions; extending the double to reach
  them is work issue #24 did not ask for.
- Crash-consistency machinery. `AGENTS.md` scopes it out, and the residual it leaves is
  recorded in ADR 0009's consequences.
- A `Makefile` target. `make test` already discovers the module.

## Security relevance

Not security-relevant. The change adds no entry point, handles no secret, parses no input
it did not produce (the double parses argv the test itself caused the engine to build),
builds no command, query, path, or URL from a non-literal, widens no permission grant, and
adds no dependency. `src/bzr_live/replay/context.py`'s existing secret handling is
unchanged and untouched. No threat model section is therefore written; the branch diff is
re-judged against the same triggers before shipping.
