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
| [G10](#g10) | gap | `bug update` addresses bugs by numeric id only, where `bug view` accepts aliases too | — |
| [D6](#d6) | defect (fixed upstream) | `component view` reports `default_assignee: null` for a component Bugzilla says has one | fixed by `5fb99362` |
| [D7](#d7) | defect | `bug history` attributes a `comment_id` to a change that carried no comment | hold: recording only, filing declined |
| [D8](#d8) | defect | The auth probe concludes header auth works when it does not, so REST reads run effectively unauthenticated | [bzr#713](https://github.com/randomparity/bzr/issues/713) |
| [D9](#d9) | defect | On Bugzilla >= 5.1 the auto-detected `rest` mode never takes the XML-RPC path `bzr` documents as the only one returning a full comment thread or attachment `data` | [bzr#714](https://github.com/randomparity/bzr/issues/714) |
| [G11](#g11) | gap | No command makes a bug group settable on a product, because Bugzilla's WebService does not expose group controls at all | — |
| [D11](#d11) | defect | The alternate-auth retry is judged by HTTP status alone, so a policy refusal that is also 401 is discarded and surfaces as `410 "You must log in"` | [bzr#715](https://github.com/randomparity/bzr/issues/715) |

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

**What still depends on the defect — nothing, for `groups`.** `src/bzr_live/verify/`
asserts `groups` and `estimated_time` rather than waiving them, because it runs at the
supported revision. `src/bzr_live/replay/actions.py` **has now been revisited**, under
issue #27: the refusal is gone and a declared `groups` set on `bug.update` is executed as
an add/remove delta and confirmed by set comparison. The two time fields stay
never-confirming, but no longer on this entry's grounds — `estimated_hours` under
[D8](#d8), `remaining_hours` under Bugzilla's own decrement of `remaining_time` by logged
work. See ADR 0006.

**The `groups` half is now observed, not read from source.** Measured at `63abb94e`
against a freshly installed fixture provisioned from `scenarios/smoke`, as `admin-ops`:
`bug update --groups-add=restricted` exits 0, `bug view --fields=id,groups` returns
`{"id":1,"groups":["restricted"]}` on the **default** transport, and `bug_group_map`
carries the row; `--groups-remove=restricted` exits 0 and both revert. A group-restricted
read draws HTTP 401 and `bzr`'s alternate-auth retry recovers it, which is the mechanism —
`bug_access_denied` maps to `STATUS_NOT_AUTHORIZED`
(`Bugzilla/WebService/Constants.pm:270`).

**The fix is sufficient for `groups` and not for the time fields**, which is the
correction issue #20's first live run forced. `bzr` does serialize `estimated_time` from
`a7f6ab70` on; Bugzilla withholds it, gating the time-tracking fields on
`timetrackinggroup` and omitting them from an otherwise-**successful 200** for a caller
that has not cleared it. No error status, so nothing fires the retry that rescues
`groups`. The one exception is worth stating because this repository can now reach it: on
a *group-restricted* bug the 401 is raised for the whole read, the retry authenticates,
and the time fields return with everything else — so the `estimated_hours` waiver is a
floor over anonymously-readable bugs rather than an absolute.

Related but distinct from that epic's own entries: entry 10 (bzr#623) covers
`groups: []` being unexpressible on *create*, and [bzr#621](https://github.com/randomparity/bzr/issues/621)
covers the `platform` naming on read and write. Neither covers the read-side omission of
these three fields.

**What the fixture does.** Executes a declared `groups` set on `bug.update` as an
add/remove delta against observed state and confirms it by set comparison, exactly as it
does `cc` and `keywords`. Treats `estimated_hours` and `remaining_hours` as fields that
never confirm and therefore always re-apply — on two distinct grounds, neither of them
this entry: Bugzilla withholds `estimated_time` from an unauthenticated read ([D8](#d8)),
and decrements `remaining_time` by logged work so the declared value is never the final
state. See ADR 0006.

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

## G10

**`bug update` addresses bugs by numeric id only, where `bug view` accepts aliases too.**
*Read from source, at `63abb94e`.*

`UpdateArgs` declares `pub ids: Vec<u64>` (`src/cli/bug/update.rs:79`), so an alias fails
clap's own parse before any request is built. `ViewArgs` declares `pub ids: Vec<String>` with
the doc comment "Bug ID(s) or alias(es). Aliases and numeric IDs may be mixed."
(`src/cli/bug/view.rs:60-62`). Bugzilla's `Bug.update` itself accepts either form in `ids`,
so the narrowing is `bzr`'s.

It is a **gap** rather than a defect: `bug update --help` documents the argument as
`[IDS]... Bug ID(s)`, so the CLI does not claim alias support and then drop it — the way
[D4](#d4) does. Nothing is silently substituted; the command refuses. It is recorded because
the asymmetry is invisible from either command's help alone: a caller who learns from
`bug view` that aliases work has no reason to expect `bug update` to differ.

**What the fixture does.** The replay engine never met this, because
`BugUpdateHandler.build` (`src/bzr_live/replay/actions.py:368-370`) already resolves the
target to a numeric id from the journal. `tests/smoke_scenario.sh`'s checkpoint probe reads
the id back from a `bug view` addressed by the declared alias and then mutates by that id,
rather than addressing `bug update` by alias.

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
`asset_sha256`.

`bug view`, `bug history` and `bug links` are byte-identical across the two modes on this
fixture. `comment list` is byte-identical **only on a thread carrying no private comment**
— bug 1's five-entry thread is, byte for byte. On a thread that does carry one the two
modes differ, and that is [D8](#d8) compounding this entry rather than a second observation
of it: reading bug 7 as the insider `admin-ops@example.test` with that actor's own valid
key, the default `rest` mode omits the declared private comment, while `--api hybrid`
returns it with `is_private: true`. So the divergence is not confined to `attachment list`;
it is confined to the two reads whose doc comments name XML-RPC as their only correct path,
which is exactly the pair this entry is about.

**Defect, not design choice.** The version mapping is deliberate and reasonable on its own;
what makes this a defect is that the two call sites document XML-RPC as the *only* correct
path for their data and then dispatch through a gate that silently excludes it. A caller
gets a reply that is well-formed, successful, and quietly missing the field it asked about.
`bzr`'s own `ATTACHMENT_FIELDS` doc comment states the consequence independently: "`data` is
only populated by `attachment download`; selecting it on `attachment list` yields empty
objects."

**What the fixture does.** The verifier passes `--api hybrid` on its `comment list` and
`attachment list` reads and on no others (`src/bzr_live/verify/observed.py`), under an
operator decision taken on 2026-09-02. This is not a workaround and not a substitution:
`--api hybrid` is `bzr`'s own documented setting for this case. Its `--api` help text says
that under `hybrid` "comments and attachments use XML-RPC first to preserve private-data
behavior", and that the auto-detected default is "`hybrid` for 5.0.x, `rest` for >= 5.1".
The flag configures the client for the data being read; it injects no value the scenario
did not declare and swaps no command for another. Without it the chartered
comment-visibility and attachment-checksum criteria are both unreachable and would have to
be reported `unverifiable` against this entry.

The three reads that keep the default do so because they are byte-identical across the two
modes, as measured above — the records should not be read as saying every read changed.

**The package and the flag are both necessary and neither is sufficient.**
`containers/bugzilla/Dockerfile` installs `libxmlrpc-lite-perl` for this same pair of
reads: `--api hybrid` makes `bzr` *attempt* XML-RPC, and the package makes the fixture
*answer* it. With the flag and no package, `dispatch_xmlrpc_first`'s hybrid arm falls back
to REST on the transport failure and the data is lost again — which is what
`check_comment_transport` (`src/bzr_live/verify/runner.py`) refuses on, naming the package.

**Upstream.** [bzr#714](https://github.com/randomparity/bzr/issues/714), filed 2026-09-06 on
the operator's authorization. The issue puts the contradiction as this entry does: the two call
sites document XML-RPC as their only complete source, and the dispatch gate excludes it on every
Bugzilla the fixture targets.

## D7

**`bug history` attributes a `comment_id` to a change that carried no comment.**
*Observed against the running fixture, 2026-09-02, with `bzr 0.8.3-dev (63abb94e)` — the
revision `README.md` proves `make smoke` at. Originally observed at `0.8.2 (ae39fbd8)`,
which is below that floor, and re-verified here because a citation taken at an untested
revision does not support the claim.*

`flatten_history` (`src/commands/bug/history.rs:68-71`) documents that its comment
correlation "can miss (→ null) but never produces a wrong id", correlating on exact `who`
plus a canonical timestamp key. It does produce a wrong id.

Observed on `scenarios/smoke/` bug 12 (`inv-tax-mismatch`), `bzr --json bug history 12`:

```
2026-09-02T14:20:03Z | triager@example.test | cf_subsystem | '' -> 'invoicing'   | comment_id: 31
2026-09-02T14:20:03Z | triager@example.test | cf_risk      | '---' -> 'medium'   | comment_id: 31
```

Comment 31 is the `worktime-inv-tax` comment, posted by the same actor in the same second
through a different call — confirmed by `bzr --json comment list 12`, where id 31 at
`2026-09-02T14:20:03Z` by `triager@example.test` carries the
`[bzr-live:smoke:worktime-inv-tax]` marker. The custom-field write is a stock-REST `PUT`
that posts no comment at all, so the correct `comment_id` for both rows is null. The three
earlier records on the same bug correlate correctly to null.

The correlation key is `who` plus a second-resolution timestamp, which is not unique: any
two changes by one actor in the same second collide, and how often that happens is a
property of how fast the replay ran rather than of the data.

**Class: defect.** The doc comment states a guarantee the implementation does not provide.

**Upstream.** Not filed. The operator declined filing this on `randomparity/bzr` on
2026-09-02 and authorized recording only.

**What the fixture does.** Nothing: the verifier never asserts `comment_id`, which is why
this is a recorded finding rather than a blocker.

## D8

**`bzr`'s auth probe concludes header auth works when it does not, so its REST reads run
effectively unauthenticated.**
*Observed against the running fixture, 2026-09-02, with `bzr 0.8.3-dev (63abb94e)`.
Originally observed at `0.8.2 (ae39fbd8)`, below `README.md`'s floor, and re-verified here.*

`bzr` probes authentication, finds `rest/whoami` unavailable, falls back to
`rest/valid_login`, is rejected, then probes `rest/bug` and concludes the rejection was
wrong. Verbatim from `RUST_LOG=debug` at `63abb94e`:

```
DEBUG bzr::client::auth::whoami: rest/whoami not available on this server
 INFO bzr::client::auth: falling back to rest/valid_login for older Bugzilla
DEBUG bzr::client::auth::valid_login: valid_login returned false method=header
DEBUG bzr::client::auth::valid_login: header auth probe on rest/bug succeeded
 INFO bzr::client::auth: header auth works on API endpoints despite valid_login rejecting
      it; preferring header
 INFO bzr::client::auth: detected server settings method=header api_mode=rest version="5.2+"
DEBUG bzr::client: created Bugzilla client auth_method=Some(Header) api_mode=rest
```

`valid_login` was right and the probe was wrong. Against this fixture, with a valid API key
for `admin-ops@example.test`:

- `GET /rest/valid_login?login=...` with an `X-BUGZILLA-API-KEY` header returns
  `{"result":false}`;
- the same call with `Bugzilla_api_key` as a **query parameter** returns `{"result":true}`.

The probe cannot tell, because the 200 it reads from `rest/bug` is exactly what an anonymous
caller gets — an unauthenticated read of a public bug succeeds. The success signal does not
discriminate between "authenticated" and "readable anyway".

**Observed consequence, demonstrated rather than inferred.** Reading bug 7's thread as the
insider `admin-ops@example.test`, with that actor's own valid key:

| Path | Declared private comment |
|---|---|
| `bzr comment list 7` (default: header auth, REST) | **absent** |
| `GET /rest/bug/7/comment?Bugzilla_api_key=...` | present, `is_private: true` |
| `bzr --api hybrid comment list 7` (XML-RPC) | present, `is_private: true` |

So the comment is on the server and visible to a properly authenticated insider, and `bzr`'s
preferred transport is the one that does not see it. Writes are unaffected *while the retry
succeeds*: they draw a 401, which triggers `bzr`'s alternate-auth retry, which is why the replay
works at all. Where the server refuses the retried write on policy grounds the retry is also a
401, and `bzr` discards it and reports the first attempt's error instead — see [D11](#d11).

**Second observed consequence: `bug view` withholds the time-tracking fields.** Found by the
first live run of the verify stage (`make smoke`, 2026-09-02, `bzr 0.8.3-dev (63abb94e)`),
which reported `cart-double-charge: estimated_time: declared 8, observed (absent)`. Bugzilla
gates `estimated_time` and `remaining_time` on the `timetrackinggroup` parameter, which this
image sets to `editbugs` (read back from the container). `admin-ops` holds `editbugs`, but
the read is anonymous, so the fields are withheld. With that actor's own valid key:

| Path | `estimated_time` |
|---|---|
| `bzr bug view 1 --fields=id,estimated_time` (default) | **absent** |
| `bzr --api hybrid bug view 1 --fields=id,estimated_time` | **absent** |
| `bzr --api xmlrpc bug view 1 --fields=id,estimated_time` | `8.0` |
| `GET /rest/bug/1?Bugzilla_api_key=...&include_fields=estimated_time` | `8` |

`--api hybrid` does not help here and is not expected to: `bzr --help` says that under
`hybrid` "bug search/view is generally REST-first with targeted XML-RPC fallback", so
`bug view` stays on REST. This is why the verifier waives `estimated_hours` — see
`UNVERIFIABLE_FIELDS` in `src/bzr_live/verify/expected.py` — rather than reporting a
divergence against a server that holds the declared value. Moving `bug view` to
`--api xmlrpc` would read it; that is a transport change the operator has not authorized
and one that would invalidate the REST reply shapes ADR 0008 verified, so it is reported
here and not taken.

**Class: defect** — the probe's success signal does not discriminate, and it overrides a
server response that was correct.

**Upstream.** [bzr#713](https://github.com/randomparity/bzr/issues/713), filed 2026-09-06 on
the operator's authorization, superseding the earlier recording-only ruling. The issue names the
mechanism this entry establishes: `verify_header_auth_via_rest`
(`src/client/auth/valid_login.rs:195-228` at `v0.9.0`) probes `rest/bug?limit=1` and treats any
2xx as proof header auth works, but that endpoint answers 200 anonymously, so the probe cannot
fail for the condition it verifies and overrides a correct negative from `rest/valid_login`.

**What the fixture does.** It refutes the premise that a positive read observes the state the
issuing actor would see, so the design states that boundary rather than assuming it. Note
that an earlier draft of this entry claimed the one identity-dependent check — `comment
list` — "travels over XML-RPC, which does authenticate, so no check today reads less than it
should". That mitigation is **false**, and [D9](#d9) is why: on this fixture `bzr` selects
`api_mode=rest` from the server version, so `comment list` never takes the XML-RPC arm
unless `--api hybrid` is passed. D8 and D9 compound — one makes REST reads anonymous, the
other makes REST the only transport — and together they are what put the comment-visibility
criterion out of reach at the default transport.

The `bug view` consequence above is D8 acting alone: `bug view` is REST under both `rest`
and `hybrid`, so D9's transport gate does not enter into it.

## G11

**No `bzr` command makes a bug group settable on a product.** *Observed against a running
fixture; source read at the documented floor `63abb94e` and re-read at `v0.9.0`.*

Restricting a bug to a group requires a `group_control_map` row making that group mandatory
or available for the bug's product — `Product::group_is_settable` reads `isactive`,
`isbuggroup`, `groups_mandatory` and `groups_available`, and the latter two select on
`membercontrol`/`othercontrol` (`Bugzilla/Product.pm:659-736`, `:740-748` at the pinned
Bugzilla SHA `644c66f4`). Without such a row every group name is refused alike.

`bzr` has no surface that writes one. `product update` accepts `--description`,
`--default-milestone` and `--is-open` and nothing else (`src/cli/product.rs:118-133`).
`bzr group` has six subcommands — `add-user`, `remove-user`, `list-users`, `view`, `create`,
`update` — none product-facing, and `group update` accepts only `--description` and
`--is-active` (`src/cli/group.rs:20-161`). Both files are byte-identical at `63abb94e` and
at `v0.9.0`, so the gap is unchanged across the revision move issue #35 owns.

The group `bzr` creates is otherwise fit for the purpose, which is what narrows the gap to
the mapping alone: `group create` sends `is_active: input.is_active.unwrap_or(true)`
(`src/commands/group/create.rs:63-70`) and Bugzilla's `WebService::Group::create` forces
`isbuggroup => 1` (`Bugzilla/WebService/Group.pm:46`), so two of the four conditions
`group_is_settable` tests are already met. Only the two sourced from `group_control_map`
are out of reach.

**Class: gap, not defect.** `bzr` cannot expose what the server API does not have: the whole
of `Bugzilla/WebService/` at `644c66f4` contains no reference to `group_control` or
`set_group_controls`, so group controls are reachable only through the Perl object layer,
never over REST or XML-RPC. A `bzr` command for this would have nothing to call. Product
group controls are therefore an administrative surface in the same family as versions,
milestones and flag types — all of which this fixture already reaches through the
container-local admin bridge rather than through `bzr` (ADR 0004).

**Upstream.** Not filed. It is a server-API limitation surfacing through `bzr`, so there is
no `bzr`-side defect to file.

**What the fixture does.** Sets the mapping through the container-local admin bridge — the
epic's authorized surface for exactly this case, setup `bzr` does not offer — using
`Bugzilla::Product::set_group_controls` and `update()` rather than upstream's raw `INSERT`,
per `containers/bugzilla/bridge.pl:5` ("Bugzilla object layer only — never raw SQL"). The
mapping is declared in the scenario contract as a `products` list on the `group` resource,
not applied out of band. See [ADR 0013](adr/0013-scenario-declared-bug-group-product-controls.md).

Measured before and after on a freshly installed fixture, as `admin-ops`, over the raw REST
`PUT /rest/bug/4` that issue #34 used. A system group (`editbugs`, `isbuggroup = 0`) and a
freshly created bug group with no mapping (`isbuggroup = 1`, zero `group_control_map` rows)
both return error 120, "you are not allowed to restrict bugs to this group in the 'checkout'
product". The provisioned group `restricted`, differing only in carrying the mapping, returns
`"changes":{"groups":{"removed":"","added":"restricted"}}`. Through `bzr` itself the same
contrast holds, except that the two refusals surface as `code 410 "You must log in"` rather
than as error 120: the server's stated cause is replaced by an authentication message on a
request that was authenticated, which is why the unmasked measurement above is taken over raw
REST. That masking is its own finding, recorded as [D11](#d11).

A bug created with the group declared up front behaves the same way: `bug create --product
checkout --component cart --groups restricted` succeeds and reads back `groups:
['restricted']`, while the same create naming an unmapped group is refused.

## D11

**The alternate-auth retry is judged by HTTP status alone, so a refusal that is authenticated
but not permitted is discarded and reported as a login failure.** *Observed against a running
fixture with `bzr 0.8.3-dev (63abb94e)` while proving issue #27, both halves on one fixture at
the floor; recorded on [bzr-live#39](https://github.com/randomparity/bzr-live/issues/39),
2026-09-06. Source read at `63abb94e`, the floor `README.md` documents, and byte-identical at
`bzr` `f28f4570`.*

Restricting a bug to a group the product does not allow is refused by Bugzilla with a specific,
honest error. `bzr` reports it as a login prompt on a request that was authenticated:

| Path | Reported |
|---|---|
| `bzr bug update <id> --groups-add=editbugs` | `api_code 410`, "You must log in" |
| the same write, raw REST with query-parameter auth | `code 120`, "you are not allowed to restrict bugs to this group in the 'checkout' product" |

The bug id is elided because the handed-over evidence did not preserve it — it is required, not
optional, since `bug update` takes numeric ids only ([G10](#g10)). The paired raw-REST half of
the same measurement was taken as `admin-ops` over `PUT /rest/bug/4`; see [G11](#g11), which
records that side in full.

**Which leg is observed, and which is read.** The two reported errors in the table above were
both measured. That `bzr`'s *own* retry received the 120-carrying body and discarded it is
**read from source** at `63abb94e` and inferred from the raw-REST half — no `bzr` transcript of
the fallback was captured, so this entry quotes none. The inference rests on the retry
authenticating the same way the raw-REST half does, which [D8](#d8) establishes independently.
Confirming it outright would take one `RUST_LOG=debug` run showing the "auth fallback also
failed, returning original 401" line. That run has not been made.

**Mechanism.** Two unrelated Bugzilla faults share one HTTP status, and `bzr`'s fallback reads
only that status.

Bugzilla maps `group_restriction_not_allowed` and `group_invalid_removal` both to wire code
**120** (`Bugzilla/WebService/Constants.pm:144-145`), and `login_required` to **410** (`:173`).
It then maps both 120 (`:276`) and 410 (`:282`) to `STATUS_NOT_AUTHORIZED`, which is HTTP
**401** (`:258`). Line numbers at the pinned Bugzilla SHA `644c66f4`
(`containers/bugzilla/Dockerfile:4`). So "you are not logged in" and "you are logged in and may
not do this" are indistinguishable by status.

On the `bzr` side, `send_raw` sends the request with header auth, and on a 401 calls
`retry_with_alternate_auth` (`src/client/transport.rs:121-135`). That retry re-sends with
query-parameter auth — which on this fixture is the method that actually authenticates, per
[D8](#d8) — and then tests the outcome with `alternate_auth_failed(retried.status())`
(`:141-164`). That predicate is `status == UNAUTHORIZED || status == FORBIDDEN` (`:165-167`):
**it inspects the status and never the body.** So a retry that authenticated fine and was then
refused on policy grounds reads as "auth failed again": the retried response is dropped whole,
error 120 with it, and `send_raw` returns the *original* 401 — whose body still carries the
header attempt's stale `410 "You must log in"`.

**Class: defect**, and a `bzr`-side one. Bugzilla answered with a specific error naming the real
constraint; `bzr` discarded that answer and substituted a message about authentication for a
request it had itself authenticated. The collision at 401 is Bugzilla's, but the decision to
judge the retry by status alone — and to drop the body that disambiguates it — is `bzr`'s.
Contrast [G11](#g11), which is classed a gap because the server API genuinely lacks the surface;
here the server supplied the information and the client threw it away.

Checked against `bzr`'s own records first, as the preamble to this file requires. `bzr`'s
**Accepted** ADR 0015, "A server error is never masked by an empty result" (2026-08-04, `bzr`
issue #504), settles the principle in the opposite direction: "A server error is surfaced
whenever it is the only thing the server told us. `bzr` does not re-implement Bugzilla's
disclosure policy." Its own Context names, as the second of two triggers, a retry path that
"dropped the original error" — structurally the same defect as this one — and its Decision
requires that fallback to preserve it. So this is a departure from an accepted upstream
decision rather than a judgement call, which is what settles the class.

This compounds [D8](#d8). D8 is why the *first* attempt draws a 401 at all — header auth does
not authenticate against this fixture — so the fallback runs on every authenticated write, and
this masking is reachable on any of them that the server refuses on policy grounds, not just on
group writes. Any Bugzilla fault mapping to `STATUS_NOT_AUTHORIZED` is masked the same way;
`Constants.pm:270-284` lists fifteen such codes.

**Upstream.** [bzr#715](https://github.com/randomparity/bzr/issues/715), filed 2026-09-06 on
the operator's authorization. The search recorded above found nothing covering this behaviour,
so it was unreported rather than a duplicate. The issue carries the evidence split this entry
draws — both reported errors measured, the discarded-body leg read from source and named as
inferred — and leads on the departure from `bzr` ADR 0015, which is what makes it a defect
rather than a judgement call.

**What the fixture does.** Nothing: there is no client-side substitution to make, and inventing
one would destroy the evidence. The fixture takes its unmasked group-control measurements over
raw REST instead — see [G11](#g11) — and this entry is why that detour exists rather than being
an unexplained preference. It also explains a diagnostic cost already paid: issue #34 was hard
to diagnose precisely because the honest server error was replaced by a login prompt pointing at
the wrong cause.

The identifier `D10` is deliberately unused. This behaviour circulated in issues and working
notes under that name for several days without ever being written here, and numbering it `D10`
now would make those citations look retroactively correct; bzr-live#39 records that history.
