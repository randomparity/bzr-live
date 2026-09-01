# bzr findings

Limitations and defects in `bzr` that this fixture has surfaced. Recording them is the
point of the fixture (`AGENTS.md`, "Purpose: prove `bzr`"), so a gap belongs here rather
than being routed around in the runner.

Every entry names the `bzr` source that establishes it, at a commit, and says whether the
behaviour was **observed** against a running fixture or **read** from source. An entry that
has only been read is marked so; `make replay-smoke` is what promotes it.

Citations are against `randomparity/bzr` at `b80303b7` unless stated otherwise.

| ID | Class | Summary | Upstream |
|---|---|---|---|
| [D1](#d1) | defect | A flag type name containing `-` is unparseable by `--flag` | *pending* |
| [D2](#d2) | defect | An absent bug exits 4, not 2, though `bzr` has a not-found exit code and maps the codes elsewhere | *pending* |
| [D3](#d3) | defect | `bug view` omits `groups`, `estimated_time` and `remaining_time` that Bugzilla returns | *pending* |
| [D4](#d4) | defect (unverified) | `bug create --from-json` `alias` silently no-ops where aliases are disabled | *pending* |
| [G1](#g1) | gap | `bug create --from-json` has no `estimated_time` / `remaining_time` | — |
| [G2](#g2) | gap | `bug update` has no `--version` | — |
| [G3](#g3) | gap | No `--reset-target-milestone`, though `--reset-assigned-to` exists | — |
| [G4](#g4) | design choice | `cf_*` fields are excluded from create and update | — |
| [G5](#g5) | design choice | `--dupe-of` conflicts with `--status` and `--resolution` | — |
| [G6](#g6) | gap | `--permissive` is rejected for a single bug ID | — |

---

## D1

**A flag type name containing `-`, `+`, `?` or `X` cannot be addressed.** *Read from source.*

`parse_single_flag` locates the status character with
`s.find(['+', '-', '?', 'X'])` — the **first** occurrence anywhere in the argument
(`src/commands/runtime/input/flags.rs`). A flag type named `needs-info` therefore renders
as `--flag=needs-info?` and parses as name `needs`, status "deny", trailing `info?`,
rejected with `invalid flag 'needs-info?': requestee must be in parentheses` (exit 7).

Bugzilla permits hyphens in flag type names, and this repository's resource slugs match
`[a-z][a-z0-9-]{0,62}` (`src/bzr_live/scenario/loader.py`), so a hyphenated flag type is
both legal upstream and legal in a scenario. Affects `bug update --flag`,
`attachment upload --flag` and `attachment update --flag` alike.

A name is unambiguous if the status character is taken from the **end** rather than the
start, since the status is always the last character or immediately precedes `(`.

**What the fixture does.** Refuses the payload as a precondition, before any mutation,
naming this entry. `tests/replay_smoke.sh` probes the live behaviour and records the exit
code and message here.

## D2

**An absent bug exits 4 with an API code, though `bzr` reserves exit 2 for not-found and
already maps these codes elsewhere.** *Observed, in `bzr`'s own functional suite.*

`bzr --json bug view 999999999` exits **4** with `api_code` 101, asserted against a live
Bugzilla at `tests/functional/phases/08e-bugs-restricted-access.sh:289-292`. The alias form
answers `api_code` 100.

`bzr` defines `EXIT_CODE_NOT_FOUND = 2` and produces `BzrError::NotFound` only when a
request succeeds and `data.bugs` comes back empty (`src/client/resources/bug.rs`). A 404
carrying a Bugzilla error body becomes `BzrError::Api { code }` → exit 4
(`src/client/response.rs`, `src/error.rs`). The taxonomy to do better already exists in the
same tree: `src/error.rs` names 100/101/102 as the `Bug.get` per-resource codes, and
`src/client/response.rs:549-550` maps `100 if !numeric => NotFoundAlias` and
`101 if numeric => NotFoundId` — but only on the adjacency path, not on `bug view`.

The consequence for a caller is that "this bug does not exist" and "the server rejected the
request" are the same exit code, distinguishable only by parsing `api_code` out of stderr.
Note that 102 (Access Denied) must stay distinct from both: an invisible bug is not an
absent one.

**What the fixture does.** Passes an explicit `absent_codes={100, 101}` to its own
`BzrClient.read`, leaving 102 to raise. See ADR 0006.

## D3

**`bug view` does not serialize `groups`, `estimated_time` or `remaining_time`.**
*Read from source.*

The `Bug` serializer (`src/types/bug.rs`) emits `id`, `summary`, `status`, `resolution`,
`dupe_of`, `deadline`, `product`, `component`, `version`, `assigned_to`, `priority`,
`severity`, `creation_time`, `last_change_time`, `creator`, `url`, `whiteboard`, `keywords`,
`blocks`, `depends_on`, `cc`, `op_sys`, `rep_platform`, `target_milestone`, `flags` and any
`cf_*`. Bugzilla's `Bug.get` returns `groups`, `estimated_time` and `remaining_time` as
well.

All three are writable through `bzr` — `groups` on create and via `--groups-add/-remove`,
the two time fields via `--estimated-time`/`--remaining-time` — so each is a **set-only**
field: no caller can read back what it wrote, and no caller can compute an add/remove delta
for `groups` from server state.

**What the fixture does.** Refuses a declared `groups` set on `bug.update`, because a delta
cannot be computed and the result cannot be confirmed; treats `estimated_hours` and
`remaining_hours` as fields that can never confirm and therefore always re-apply. See
ADR 0006.

## D4

**A declared `alias` may be silently discarded rather than reported.** *Unverified here.*

`bzr`'s own functional suite records, in the header comment of
`tests/functional/phases/08c-bugs-create-fields.sh`, that "`--alias` is intentionally not
asserted: bug aliases are disabled on these default-config containers, so the field silently
no-ops." A declared field that is accepted, ignored, and reported as success is
indistinguishable from one that was applied.

This fixture enables aliases (`containers/bugzilla/checksetup_answers.txt` sets
`$answer{'usebugaliases'} = 1`), so the round-trip is expected to work here — but that is an
inference from a parameter, not an observation, and the failure mode if it is wrong is a
duplicate bug on resume rather than a refusal.

**What the fixture does.** `tests/replay_smoke.sh` asserts the round-trip against the live
fixture. If it fails, ADR 0006 records the fallback: reconcile creates by a namespaced
marker in the description.

## G1

**`bug create --from-json` has no `estimated_time` or `remaining_time`.** *Read from source.*

`JsonCreateBug` (`src/commands/bug/create_json.rs`) sets `#[serde(deny_unknown_fields)]` and
declares neither, so a document carrying them is rejected outright. Bugzilla's `Bug.create`
accepts both, and `bzr bug update` offers `--estimated-time` and `--remaining-time`, so the
capability exists everywhere except the one path that would set it at creation time.

**What the fixture does.** Refuses the payload as a precondition, naming this entry.

## G2

**`bug update` has no `--version`.** *Read from source.*

`UpdateArgs` (`src/cli/bug/update.rs`) declares `--target-milestone` but no version flag,
while Bugzilla's `Bug.update` accepts `version`. A bug's version can be set at creation and
never changed thereafter.

**What the fixture does.** Refuses a declared `version` on `bug.update`, naming this entry.

## G3

**No `--reset-target-milestone`, though the reset pattern exists.** *Read from source.*

`UpdateArgs` offers `--reset-assigned-to` and `--reset-qa-contact` but no equivalent for the
target milestone, so a milestone can be set and changed but not cleared.

**What the fixture does.** Refuses a declared null `milestone` on `bug.update`, naming this
entry.

## G4

**`cf_*` fields are excluded from create and update by design.** *Read from source.*

`JsonCreateBug`'s doc comment states that `deny_unknown_fields` "keeps undesigned `cf_*`
custom-field writes (issue #283) out of this path". This is a deliberate choice upstream,
not a defect, and it is the one gap epic #1 already authorizes a workaround for: a single
stock-REST route for per-bug custom-field assignment.

**What the fixture does.** Routes `bug.custom-field-set` through that authorized REST call,
and refuses `custom_fields` inside a `bug.create` payload so each event stays one observable
mutation.

## G5

**`--dupe-of` conflicts with `--status` and `--resolution`.** *Read from source.*

Both flags carry `#[arg(long, conflicts_with = "dupe_of")]` (`src/cli/bug/update.rs`), and
`--dupe-of`'s own documentation explains why: "Bugzilla handles the status/resolution
transition to RESOLVED/DUPLICATE." The conflict is deliberate and correct; it is recorded
here because it constrains what a single scenario event can express, not because it is
wrong.

**What the fixture does.** Refuses either pairing as a precondition, naming this entry.

## G6

**`--permissive` is rejected for a single bug ID.** *Read from source.*

`bug view` errors with "--permissive only meaningful with multiple IDs"
(`src/commands/bug/view.rs`), and a single ID short-circuits to `view_single` before the
permissive mode is even computed. So the one mode that turns a per-bug failure into a
structured row with a `faultCode` — Bugzilla's own `permissive` parameter — is unavailable
for the single-bug existence check a caller most often wants.

Combined with [D2](#d2), there is no way to ask "does this bug exist?" and get a non-error
answer for one bug.

**What the fixture does.** Uses [D2](#d2)'s `absent_codes` mapping instead.
