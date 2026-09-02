# bzr findings

Limitations and defects in `bzr` that this fixture has surfaced. Recording them is the
point of the fixture (`AGENTS.md`, "Purpose: prove `bzr`"), so a gap belongs here rather
than being routed around in the runner.

Every entry names the `bzr` source that establishes it, at a commit, and says whether the
behaviour was **observed** against a running fixture or **read** from source. An entry that
has only been read is marked so. `make replay-smoke` is what produces the evidence to promote
it: the script prints each probe's observed exit code and message, and a maintainer transcribes
the result here. Today it probes D1 only and prints the remaining source-read entries by name
as unprobed, because a probe whose syntax nobody has validated produces misleading evidence —
so the absence of a probe line for an entry means it was not exercised, not that it passed.
The script does not edit this file — the class of an entry is a judgement call, as D2 becoming
G7 and the withdrawal of G8 both show.

Citations are against `randomparity/bzr` at `b80303b7` unless stated otherwise. Before
filing anything upstream, check it against `bzr`'s own `docs/adr/` and its open issues:
two entries here started as defects and turned out to be an accepted decision (G7) and an
already-filed one (D5).

| ID | Class | Summary | Upstream |
|---|---|---|---|
| [D1](#d1) | defect | A flag type name containing `-` is unparseable by `--flag` | [bzr#640](https://github.com/randomparity/bzr/issues/640) |
| [G7](#g7) | design choice | An absent bug exits 4 with an API code, not 2 — `bzr` ADR 0015 forbids masking a server error | n/a |
| [D3](#d3) | defect, **fixed** | `bug view` omits `groups`, `estimated_time` and `remaining_time` that Bugzilla returns | [bzr#641](https://github.com/randomparity/bzr/issues/641), closed by `a7f6ab70` |
| [D4](#d4) | defect (unverified) | `bug create --from-json` `alias` silently no-ops where aliases are disabled | hold: unverified |
| [D5](#d5) | defect | `rep_platform` is the wrong wire name; the field is `platform` | [bzr#621](https://github.com/randomparity/bzr/issues/621) |
| [G1](#g1) | gap | `bug create --from-json` has no `estimated_time` / `remaining_time` | — |
| [G2](#g2) | gap | `bug update` has no `--version` | — |
| [G3](#g3) | gap | No `--reset-target-milestone`, though `--reset-assigned-to` exists | — |
| [G4](#g4) | design choice | `cf_*` fields are excluded from create and update | — |
| [G5](#g5) | design choice | `--dupe-of` conflicts with `--status` and `--resolution` | — |
| [G6](#g6) | gap | `--permissive` is rejected for a single bug ID | — |
| [G9](#g9) | design choice | `bug create --from-json` silently defaults an omitted `version` to `unspecified` | — |
| [D6](#d6) | defect (fixed upstream) | `component view` reports `default_assignee: null` for a component Bugzilla says has one | fixed by `5fb99362` |
| [D9](#d9) | defect | On Bugzilla >= 5.1 the auto-detected `rest` mode never takes the XML-RPC path `bzr` documents as the only one returning a full comment thread or attachment `data` | not filed |

---

## D1

**A flag type name containing `-`, `+`, `?` or `X` cannot be addressed.** *Observed against
the running fixture by `make replay-smoke` (Bugzilla 5.2+, bzr `b80303b7`): `--flag=needs-info?`
exits 7 with `invalid flag 'needs-info?': requestee must be in parentheses`, the message this
entry predicted from source verbatim.*

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

**Upstream.** [bzr#640](https://github.com/randomparity/bzr/issues/640), with the
end-anchored parse as the suggested fix.

**What the fixture does.** Refuses the payload as a precondition, before any mutation,
naming this entry. `tests/replay_smoke.sh` probes the live behaviour and prints the exit code
and message for transcription here.

## G7

**An absent bug exits 4 with an API code rather than 2.** *Observed, in `bzr`'s own
functional suite. Deliberate — see below.*

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

**This is by design, and the fixture was wrong to call it a defect.** `bzr`'s accepted
ADR 0015, "A server error is never masked by an empty result" (issue #504), settles it: `bzr`
relays what the server said and never substitutes not-found for a stated error. Exit 2 is
reachable only when a request *succeeds* and the `bugs` array is empty. Reporting
`api_code` 100 as exit 4 is that decision working correctly, and the adjacency path maps
100/101 to not-found because a per-row result in a batch is not a masking situation.

Recorded here because it still constrains a caller — "does not exist" and "the server
rejected this" share an exit code, separable only by reading `api_code` off stderr, and
[G6](#g6) removes the obvious alternative. No issue filed.

**What the fixture does.** Passes an explicit `absent_codes={100, 101}` to its own
`BzrClient.read`, leaving 102 to raise. See ADR 0006.

## D3

**`bug view` did not serialize `groups`, `estimated_time` or `remaining_time`.**
*Read from source. Fixed upstream in `a7f6ab70`; the description below is of the defect as
found, on `0.8.2`.*

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

**Upstream. Fixed.** [bzr#641](https://github.com/randomparity/bzr/issues/641), filed as
part of the conformance epic [bzr#616], is closed by `a7f6ab70`
(*fix(bug): expose group and time fields in bug views*, PR #646, branch
`feat/bug-view-read-fields-641`). It adds `Groups`, `EstimatedTime` and `RemainingTime` to
`BugField` (`src/types/bug/fields.rs`) and to the `Bug` serializer, so all three are
readable through `bug view --fields` from that commit on. `a7f6ab70` is an ancestor of
`63abb94e`, the revision `README.md` proves `make smoke` at, so the defect does not
reproduce at any revision this repository supports. It still reproduces on the `0.8.2`
release, which is what the homebrew binary is.

**What still depends on the defect.** `src/bzr_live/verify/` asserts `groups` and
`estimated_time` rather than waiving them, because it runs at the supported revision.
`src/bzr_live/replay/actions.py` has **not** been revisited: it still refuses a declared
`groups` set on `bug.update` and still treats the two time fields as never-confirming, per
ADR 0006. Narrowing that is follow-up work, not part of issue #20.

Related but distinct from that epic's own entries: entry 10 (bzr#623) covers
`groups: []` being unexpressible on *create*, and [bzr#621](https://github.com/randomparity/bzr/issues/621)
covers the `platform` naming on read and write. Neither covers the read-side omission of
these three fields.

**What the fixture does.** Refuses a declared `groups` set on `bug.update`, because a delta
cannot be computed and the result cannot be confirmed; treats `estimated_hours` and
`remaining_hours` as fields that can never confirm and therefore always re-apply. See
ADR 0006.

## D5

**`rep_platform` is the wrong wire name; Bugzilla calls the field `platform`.**
*Already filed upstream: [bzr#621](https://github.com/randomparity/bzr/issues/621), open,
part of the conformance epic [bzr#616](https://github.com/randomparity/bzr/issues/616).*

`bzr` names the hardware field `rep_platform` throughout, while Bugzilla emits and accepts
`platform`. On the read path a non-empty `include_fields` list containing `rep_platform`
does not merely fail to match — it suppresses the `platform` key the server would otherwise
return. `JsonCreateBug` (`src/commands/bug/create_json.rs`) carries the same `rep_platform`
name on the create path.

**Why this matters here.** An earlier draft of ADR 0006 had replay inject a fixed
`op_sys`/`rep_platform` pair into every create document, to satisfy an installation that
declares no defaults. That would have sent a field name bzr already has an open defect
about, to make a run appear to succeed. The fixture now sets `defaultplatform` and
`defaultopsys` in `containers/bugzilla/checksetup_answers.txt` instead, and the create
document carries only what the scenario declared.

[bzr#616]: https://github.com/randomparity/bzr/issues/616

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

Combined with [G7](#g7), there is no way to ask "does this bug exist?" and get a non-error
answer for one bug.

**What the fixture does.** Uses [G7](#g7)'s `absent_codes` mapping instead.

## G9

**`bug create --from-json` silently defaults an omitted `version` to `"unspecified"`.**
*Read from source. Deliberate.*

`JsonCreateBug::into_params` (`:131`) renders
`version: self.version.unwrap_or_else(|| "unspecified".to_string())`
(`src/commands/bug/create_json.rs:150`), and the struct's own doc comment declares the
behaviour: "`component`, and `summary` are required; `version` defaults to `\"unspecified\"`"
(`:129-130`). It is documented, so it is a design choice rather than a defect, and no issue
is filed.

It is recorded anyway, because it is the one place where **bzr itself** does what `AGENTS.md`
forbids this fixture from doing: it sends the server a value the caller never declared. A
Bugzilla product that declares no `unspecified` version — which the fixture's provisioned
products do not — will reject the create, and a product that happens to declare one will
accept a bug whose version no scenario ever stated.

**What the fixture does.** Refuses a declared null `version` on `bug.create` as a
precondition, naming this entry. An omitted `version` is a scenario-contract question rather
than a bzr one and is settled by the loader.

## D6

**`component view` reports `default_assignee: null` for a component Bugzilla says has one.**
*Observed against a running fixture.*

`Component` (`src/types/component.rs` at `ae39fbd8`) declares
`#[serde(default)] pub default_assignee: Option<String>`. Bugzilla's `Product.get` returns the
field as `default_assigned_to`, so serde finds no matching key and leaves the value `None`.
`bzr component view` reads the component out of `get_product`'s nested `components`
(`src/commands/component/view.rs`), so the omission reaches every caller of that command.

The write path is unaffected: `component create --default-assignee=` stores the value
correctly. Observed on the smoke fixture — `bzr component view -- checkout cart` returned
`"default_assignee": null` while stock REST `GET /rest/product/checkout` returned
`"default_assigned_to": "triager@example.test"` for the same component, in the same run.

So this is a **read-side wire-name mismatch**, the same shape as [D5](#d5) and closely related
to [D3](#d3): a value that can be written but not read back, which makes a declared component
default assignee unverifiable through `bzr` alone.

**Upstream.** Already fixed. `5fb99362` ("fix(component): accept Bugzilla default assignee
key") adds `alias = "default_assigned_to"` to the field; the alias is present at `b9d779ef`
and absent at `ae39fbd8`. Nothing to file.

**What the fixture does.** Nothing — the scenario keeps declaring `default_assignee` on all
four components, because the field is honest and the defect is the finding. The provisioner
refuses the readback with
`component:cart differs in declared field 'default_assignee': declared '…', observed None`,
which is correct behaviour: it cannot confirm what it wrote. `5fb99362` is where this defect
stops; it is not a proven floor for `make smoke`, because that revision still declares output
schema `0.6.1` while `src/bzr_live/replay/actions.py` is written against `b80303b7`, which
declares `2.0.0`. `README.md` states the distinction. This entry is why
`tests/smoke_scenario.sh` prints
the `bzr` revision as its first line: the same scenario passes or fails on that revision
alone, and an entry recorded against the wrong one would be a false report.

## D9

**On Bugzilla 5.1 and above, `bzr`'s auto-detected `rest` mode never takes the XML-RPC path
its own source documents as the only one that returns a full comment thread or an
attachment body.**
*Read from source and confirmed live at `bzr 0.8.3-dev (63abb94e)` against this fixture
(Bugzilla `5.2+`), 2026-09-02.*

`get_comments_since` (`src/client/resources/comment.rs:62`) and `get_attachments`
(`attachment.rs:151`) both call `dispatch_xmlrpc_first`, and both carry a doc comment saying
XML-RPC is preferred because "Bugzilla 5.0.x REST silently filters private comments under
API-key auth (issue #125), and the truncation is not reliably detectable from the REST
response — XML-RPC is the only path that returns the full thread", with the same wording for
attachments and issue #133.

But `dispatch_xmlrpc_first` (`src/client/mod.rs:262-280`) does not consult `xmlrpc.cgi` at
all. It branches on the detected `api_mode`:

```rust
match self.api_mode {
    ApiMode::Rest => rest().await,
    ApiMode::XmlRpc => xmlrpc().await,
    ApiMode::Hybrid => match xmlrpc().await { /* fall back to REST on transport failure */ },
}
```

and `version_to_api_mode` (`src/client/version.rs:119-140`) maps `< 5.0` to `xmlrpc`,
`>= 5.0 < 5.1` to `hybrid`, and **`>= 5.1` to `rest`**. This fixture answers `5.2+`, so the
mode is `rest` and the XML-RPC arm is unreachable — the preference those doc comments
describe is disabled on exactly the versions where the REST reply is still lossy.

**Observed.** `bzr --json attachment list 1` returns an entry whose keys are `id`, `bug_id`,
`file_name`, `summary`, `content_type`, `creator`, `creation_time`, `last_change_time`,
`size`, `is_obsolete`, `is_private`, `is_patch`, `flags` — and **no `data`**, because the
REST arm requests `exclude_fields=data` (`attachment.rs:163`). The same command with
`--api hybrid` returns the identical key set **plus `data`**, whose base64 body decodes to
622 bytes matching the attachment's reported `size` and hashing to the declared
`asset_sha256`. `comment list`, `bug view`, `bug history` and `bug links` are byte-identical
across the two modes on this fixture, so the divergence is confined to `attachment list`.

**Defect, not design choice.** The version mapping is deliberate and reasonable on its own;
what makes this a defect is that the two call sites document XML-RPC as the *only* correct
path for their data and then dispatch through a gate that silently excludes it. A caller
gets a reply that is well-formed, successful, and quietly missing the field it asked about.
`bzr`'s own `ATTACHMENT_FIELDS` doc comment states the consequence independently: "`data` is
only populated by `attachment download`; selecting it on `attachment list` yields empty
objects."

**What the fixture does.** Nothing yet — the transport the verifier should use is an open
decision for the operator, recorded on issue #20. `--api hybrid` is a supported `bzr` flag
and is the narrowest thing that makes the chartered comment-visibility and
attachment-checksum criteria reachable; the alternative is to report both `unverifiable`
against this entry. Filing upstream is not authorized.
