# Versioned Scenario Contract Design

**Issue:** [#3](https://github.com/randomparity/bzr-live/issues/3)  
**Decision:** [ADR 0002](../../adr/0002-versioned-scenario-contract.md)  
**Branch:** `feat/versioned-scenario-contract-3`  
**Base branch:** `main`  
**Guardrails:** none existed at design time; this change establishes the focused command
`uv run --python 3.11 python -m unittest discover -s tests -v`.

## Goal and scope

Define the first strict, immutable scenario input and local event-journal contract. A caller
must be unable to obtain an executable resource/event plan from malformed inputs. The
contract binds every declared input and asset to one canonical digest, and local event state
must always be either one durable in-flight record or one durable completed record with
owner-only permissions and sensitive values redacted.

This change does not start containers, provision Bugzilla, invoke `bzr`, replay events,
manage checkpoints, or edit `README.md`. Those surfaces belong to issue #2 and later epic
issues. The library deliberately has no subprocess or network boundary; later code must use
the returned validated plan and keep `bzr` as the supported Bugzilla mutation boundary.

## Requirements

1. Load exactly one scenario directory containing `scenario.json`, `resources.json`, and
   `events.jsonl`, plus declared regular files below optional `assets/`.
2. Require integer `format_version: 1` in the manifest, resource catalog, and every event.
3. Reject malformed JSON, duplicate object keys, non-finite or floating-point JSON numbers,
   unknown fields at every contract object, duplicate names within an identity namespace,
   unsupported resource/action kinds, invalid values, unsafe or changed assets, missing
   references, wrong-kind references, invalid resource cycles, and event references to
   outputs not yet created.
4. Return immutable typed objects and a stable resource topological plan only after all
   files, references, event outputs, and assets validate.
5. Compute one lowercase SHA-256 digest from every validated document and every declared
   asset byte. JSON whitespace and object-key order are non-semantic; event line order and
   asset bytes are semantic.
6. Define recovery metadata for every supported action before execution: action class,
   expected postcondition, and deterministic reconciliation marker.
7. Store local state only in exact mode-0700 directories and exact mode-0600 regular files.
   Atomically install in-flight intent before mutation and atomically replace that intent
   with a completed record after the caller receives a `bzr` result.
8. Never persist credential environment values. Recursively replace values whose object key
   is sensitive in invocation metadata or structured output.
9. Use Python 3.11 or later, Setuptools 84.0.0 as the pinned build backend, and no runtime or
   test dependencies outside the standard library.

## Package boundary

The `bzr_live.scenario` package exports:

```python
class ScenarioValidationError(ValueError):
    source: str
    field: str

@dataclass(frozen=True)
class Reference:
    kind: str
    name: str

@dataclass(frozen=True)
class Asset:
    name: str
    path: str
    sha256: str
    content: bytes

@dataclass(frozen=True)
class PlannedResource:
    kind: str
    name: str
    data: Mapping[str, JsonValue]
    dependencies: tuple[Reference, ...]

@dataclass(frozen=True)
class PlannedEvent:
    name: str
    actor: Reference
    action: str
    action_class: Literal["unique-create", "idempotent-set", "append"]
    payload: Mapping[str, JsonValue]
    dependencies: tuple[Reference, ...]
    reconciliation_marker: str
    expected_postcondition: Mapping[str, JsonValue]
    creates: Reference | None

@dataclass(frozen=True)
class ValidatedScenario:
    name: str
    description: str
    format_version: int
    resources: tuple[PlannedResource, ...]
    resource_plan: tuple[PlannedResource, ...]
    events: tuple[PlannedEvent, ...]
    assets: Mapping[str, Asset]
    digest: str

load_scenario(path: str | Path) -> ValidatedScenario
```

`JsonValue` is the closed recursive set `None | bool | int | str | tuple[JsonValue, ...] |
Mapping[str, JsonValue]`. Returned mappings are read-only and returned sequences are tuples.
Validated assets retain the exact immutable `bytes` that produced their checksum; later
handlers must consume `Asset.content` and must not reopen the author-controlled path. The
implementation may use private mutable dictionaries while decoding but none escape.
Validation errors render as `<source>:<field>: <message>`, retaining the file and JSON path or
JSONL line that an author must fix.

A future executor may accept `ValidatedScenario`; no API accepts a scenario path or raw event
mapping and then mutates. This type boundary is the pre-mutation gate.

## File contract

All objects reject keys not listed below. A name is an ASCII slug matching
`[a-z][a-z0-9-]{0,62}`. Human text must be a non-empty string after stripping and may retain
its original Unicode content. Booleans are never accepted where integers are required
(Python's `bool` subtype must not weaken this rule). JSON floats and `NaN`/`Infinity` are
rejected during decoding. Decimal work values are canonical non-negative strings matching
`0|[1-9][0-9]*(\.[0-9]{1,2})?`.

### `scenario.json`

```json
{
  "format_version": 1,
  "name": "smoke",
  "description": "Small replay and recovery proof",
  "assets": [
    {
      "name": "trace-log",
      "path": "assets/trace.log",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ]
}
```

Allowed top-level keys are exactly `format_version`, `name`, `description`, and `assets`.
`description` is optional and defaults to the empty string. `assets` is optional and defaults
to an empty list. Asset object keys are exactly `name`, `path`, and `sha256`.

Asset names are unique. Asset paths are unique canonical relative POSIX paths: no empty
segments, `.`, `..`, backslashes, absolute paths, drive prefixes, or segment outside the
initial `assets/`. Each path must resolve beneath the scenario directory without traversing
a symlink. The target must be one regular file. The loader opens it without following
symlinks, reads its bytes once, verifies that the declared 64-character lowercase SHA-256
matches, and retains those immutable bytes in `Asset.content`. Event validation and every
later attachment handler use that snapshot, closing the check-to-use gap created by reopening
the path after validation.

### `resources.json`

```json
{
  "format_version": 1,
  "resources": [
    {"kind": "group", "name": "triage", "description": "Triage group"},
    {
      "kind": "actor",
      "name": "ada",
      "email": "ada@example.test",
      "display_name": "Ada",
      "groups": [{"ref": "group:triage"}]
    },
    {"kind": "product", "name": "checkout", "description": "Checkout"},
    {
      "kind": "component",
      "name": "payments",
      "product": {"ref": "product:checkout"},
      "description": "Payments",
      "default_assignee": {"ref": "actor:ada"}
    }
  ]
}
```

Allowed catalog keys are exactly `format_version` and `resources`. `resources` is required.
Resource identity is `<kind>:<name>`; duplicates are rejected. The first-version kinds and
fields are:

| Kind | Required fields | Optional fields | Reference requirements |
|---|---|---|---|
| `group` | `kind`, `name`, `description` | none | none |
| `actor` | `kind`, `name`, `email`, `display_name` | `groups` (default `[]`) | every group is `group` |
| `product` | `kind`, `name`, `description` | none | none |
| `component` | `kind`, `name`, `product`, `description` | `default_assignee` | product is `product`; assignee is `actor` |
| `version` | `kind`, `name`, `product` | none | product is `product` |
| `milestone` | `kind`, `name`, `product` | none | product is `product` |
| `custom-field` | `kind`, `name`, `field_type` | `values` (default `[]`) | none |

Emails must contain one non-edge `@` and no ASCII whitespace. `field_type` is one of `text`,
`single-select`, or `multi-select`. Select fields require a non-empty unique list of non-empty
string values; text fields require no values. Reference lists reject duplicate references.

Dependencies are every typed reference in the kind-specific reference fields. All must
resolve to declared resources of the required kind. A stable Kahn topological sort preserves
input order among currently ready resources. A cycle is rejected with the identities left in
the cycle; no partial plan is returned.

### Typed references

A reference object contains exactly one key, `ref`, whose value is
`<kind>:<name>`. The kind and name must use the same closed kind and slug grammar as their
namespace. Validation is field-sensitive: an actor field rejects `group:triage` even if the
group exists. Asset references use `asset:<name>`. Event-created bugs and attachments use
`bug:<alias>` and `attachment:<alias>`.

### `events.jsonl`

Blank lines are allowed and ignored. Every non-empty UTF-8 line is exactly one JSON object
with these base keys:

```json
{
  "format_version": 1,
  "name": "create-checkout-race",
  "actor": {"ref": "actor:ada"},
  "action": "bug.create",
  "payload": {}
}
```

Event names are unique and the actor must reference a declared `actor`. Events retain file
order. References may resolve to declared resources/assets or outputs created by earlier
events. A self-reference, forward reference, duplicate created identity, or reference to a
wrong kind is rejected. No topological reorder of events is performed.

The fixed version 1 actions are:

| Action | Class | Required payload keys | Optional payload keys | Creates |
|---|---|---|---|---|
| `bug.create` | `unique-create` | `alias`, `product`, `component`, `summary` | `description`, `version`, `milestone`, `assignee`, `cc`, `groups`, `depends_on`, `blocks`, `duplicate_of`, `keywords`, `estimated_hours`, `remaining_hours`, `custom_fields` | `bug:<alias>` |
| `bug.update` | `idempotent-set` | `bug`, `set` | none | none |
| `bug.comment` | `append` | `bug`, `body` | `private` | none |
| `bug.attach` | `append` | `alias`, `bug`, `asset`, `description`, `content_type` | `private` | `attachment:<alias>` |
| `bug.worktime` | `append` | `bug`, `hours`, `comment` | none | none |
| `bug.custom-field-set` | `idempotent-set` | `bug`, `values` | none | none |
| `bug.flag` | `idempotent-set` | `bug`, `name`, `status` | `requestee` | none |
| `attachment.update` | `idempotent-set` | `attachment`, `obsolete` | `description` | none |

`bug.create` references must have the kinds implied by their key. `cc`, `groups`,
`depends_on`, and `blocks` are duplicate-free lists. `keywords` is a duplicate-free list of
non-empty strings. Optional hours use the decimal-string grammar above. `custom_fields` and
`bug.custom-field-set.values` are non-empty lists of objects containing exactly `field` and
`value`; `field` references `custom-field`, names are unique in the list, and values are
strings or duplicate-free lists of strings consistent with the declared field type.

`bug.update.set` is non-empty and accepts only `summary`, `status`, `resolution`, `assignee`,
`cc`, `groups`, `depends_on`, `blocks`, `duplicate_of`, `version`, `milestone`, `keywords`,
`estimated_hours`, and `remaining_hours`. Each reference field has its named kind; list and
hours rules match `bug.create`. Null is accepted only for `resolution`, `assignee`,
`duplicate_of`, `version`, and `milestone`, where it explicitly clears the field.

Comment/work-time body text is non-empty. Attachment `content_type` is a non-empty string
containing one `/`. Flag status is one of `?`, `+`, `-`, or `X`; requestee is an actor and is
allowed only with `?`. `obsolete` and `private` are strict booleans. `private` defaults false.

Every event receives the deterministic reconciliation marker
`bzr-live:<scenario-name>:<event-name>`. Append handlers must include it in the semantic
mutation: comment and work-time bodies, and attachment descriptions together with the
validated asset checksum. The expected postcondition is a read-only canonical mapping built
from the target reference and validated values. A later handler must not weaken it or invent
recovery metadata at mutation time.

## Canonical digest

After full validation, construct this semantic envelope:

```json
{
  "format_version": 1,
  "inputs": {
    "scenario.json": {},
    "resources.json": {},
    "events.jsonl": []
  },
  "assets": [
    {"name": "trace-log", "path": "assets/trace.log", "sha256": "..."}
  ]
}
```

The three input values are their validated, default-expanded semantic forms; the event value
is ordered. Asset entries are sorted by path, and their checksums were computed from exact
bytes. Serialize as UTF-8 JSON with lexicographically sorted keys, no ASCII escaping, and
separators `,` and `:` with no added whitespace, then hash those bytes with SHA-256. Floats
are absent by contract, eliminating platform-dependent number rendering.

The digest includes the version in both the envelope and normalized documents. Any accepted
field value, resource/event order, event action, declared asset identity/path/checksum, or
asset byte change affects the digest. JSON formatting and object-key order do not.

## Journal contract

The package exports frozen records and one store:

```python
@dataclass(frozen=True)
class InFlightRecord:
    scenario_digest: str
    event: str
    actor: Reference
    action_class: str
    expected_postcondition: Mapping[str, JsonValue]
    reconciliation_marker: str

@dataclass(frozen=True)
class CompletedRecord:
    scenario_digest: str
    event: str
    actor: Reference
    action_class: str
    reconciliation_marker: str
    invocation: Mapping[str, JsonValue]
    bzr_output: JsonValue
    exit_status: int
    resolved_ids: Mapping[str, int]
    next_safe_action: Literal["advance", "reconcile", "retry", "stop"]

class JournalStore:
    def __init__(self, state_dir: str | Path) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> JournalStore: ...
    def __exit__(self, *exc_info: object) -> None: ...
    def write_in_flight(self, record: InFlightRecord) -> Path: ...
    def replace_completed(self, record: CompletedRecord) -> Path: ...
    def read(self, event: str) -> InFlightRecord | CompletedRecord | None: ...
```

Record JSON adds `journal_version: 1` and `phase: "in_flight" | "completed"`. Actors serialize
as typed references. Digests are lowercase SHA-256; event names use the slug grammar; exit
statuses are integers excluding booleans; resolved ID keys are typed references and values
are positive integers. `next_safe_action` is closed to the four values above.

`JournalStore` creates only an absent state directory with mode 0700. It rejects existing
state directories whose permission bits are not exactly 0700, and rejects non-directories or
symlinks. It opens an exact mode-0600 `.lock` regular file without following symlinks and
takes a non-blocking exclusive `flock` for the store's lifetime; a concurrent store fails
closed. `close()` releases the descriptor, and context-manager use is supported. Event files
are `<event>.json`, which is safe because event names are slugs. Reads reject non-regular
files, symlinks, any permission bits other than 0600, malformed/unknown record fields, and
unsupported journal versions.

A transition serializes canonical JSON plus one newline to a mode-0600 same-directory
temporary regular file, flushes and `fsync`s it. For absent to `in_flight`, `os.link` installs
the fully written inode at the event path without replacing an existing name, then the
temporary name is unlinked. For `in_flight` to `completed`, the store first reads a valid
in-flight record and requires matching scenario digest, event, actor, action class, and
marker, then calls `os.replace`. Both transitions `fsync` the containing directory after the
directory changes. Temporary files are removed after any pre-install failure. A completed
record, an existing in-flight install, and every mismatch are rejected. The exclusive store
lock prevents two compliant writers from passing the read-before-replace check concurrently.

Before serialization, invocation and `bzr_output` are copied through recursive redaction.
Keys compare case-insensitively after replacing `-` with `_`; `api_key`, `apikey`, `token`,
`password`, `secret`, `authorization`, and `cookie` values become `"<redacted>"` at any depth.
The invocation mapping may record the executable, arguments, and credential environment
_variable names_, but must not receive environment values. Tests use recognizable secret
values and require that none occur in serialized bytes.

The journal makes local write transitions atomic across runner process termination. File and
directory `fsync` request persistence but do not promise survival of an operating-system
crash or power loss. Determining whether an in-flight server mutation committed remains the
later replay handler's responsibility; the record contains the exact evidence that handler
needs and never guesses a result.

## Errors and mutation ordering

Loading proceeds in this order: verify directory and three required regular non-symlinked
files; strictly parse all documents; validate manifest and assets including bytes; validate
all resources and build the resource plan; validate every event and ordered output table;
construct immutable objects; compute the digest; return. Every failure raises
`ScenarioValidationError` before returning.

The package has no callback, hook, subprocess, network client, or partial-plan iterator.
Therefore a caller cannot begin mutation during validation. Later code must finish
`load_scenario`, persist the in-flight record, and only then invoke `bzr`.

## Threat model

### Boundary inventory

- **Added: scenario directory to trusted plan.** A local scenario author controls JSON/JSONL,
  names, references, text, asset metadata, asset paths, symlinks, and asset bytes.
- **Added: caller result to local journal.** A local runner controls invocation metadata,
  structured `bzr` output, resolved IDs, and next-action classification; these may contain
  credentials or server-returned sensitive data.
- **Not widened: Bugzilla mutation boundary.** This package performs no mutation and does not
  change who can call `bzr` or Bugzilla.

### Actors and trust

The untrusted party is a local scenario author or a modified checkout consumed by an
operator or CI job. The local operator account is trusted to select the state directory and
run the later executor, but other local users are not trusted to read its secrets. The later
runner is trusted to pass structured metadata and to keep credential values out of its
invocation mapping; redaction is defense in depth for key-labelled values.

### Controls

- Strict duplicate-aware decoding, closed keys/kinds/actions, exact value checks, complete
  reference resolution, and a cycle check prevent malformed control data reaching a handler.
- Canonical slug identities and immutable returned mappings prevent alias confusion and
  post-validation mutation.
- Canonical relative asset paths, no symlinks, containment checks, regular-file checks, and
  verified hashes prevent traversal and digesting a different target through a link.
- Input size is bounded by available local disk/memory; this first local-development contract
  does not claim hostile multi-tenant denial-of-service resistance.
- Exact directory/file modes, no symlink following, same-directory atomic replacement, and
  fsync control disclosure and torn state on supported local filesystems.
- Recursive sensitive-key redaction and the prohibition on credential environment values
  prevent known credential shapes from reaching disk. Errors identify fields but never echo
  sensitive values.
- No subprocess/network capability preserves the existing `bzr` mutation boundary.

### Explicitly out of scope

- A malicious local operator who owns the state directory can read or change it; owner-only
  permissions are not encryption or tamper authentication.
- Filesystems that do not provide ordinary local `fsync`/atomic-rename guarantees are not
  supported for state; checkpoint portability belongs to issue #5.
- Free-form bug comments or attachment content may intentionally contain private scenario
  data. This issue prevents credential metadata leakage; it does not sanitize fixture content.
- Server-side reconciliation, API authorization, credential acquisition, and the epic's
  narrow custom-field REST adapter belong to later issues.

## Verification

Focused tests must prove:

- a minimal valid scenario returns immutable typed resources/events and a stable plan;
- duplicate JSON keys, unknown fields, invalid versions/types, malformed resource/action
  shapes, duplicate resource/event/asset/output names, unresolved/forward/self references,
  wrong-kind references, missing actors/assets, invalid custom-field values, and resource
  cycles fail with source and field context;
- unsafe, missing, symlinked, non-regular, and checksum-mismatched assets fail;
- equivalent JSON formatting/key order has the same digest, while every manifest/resource/
  event field, event order, asset declaration, and asset byte mutation changes it;
- all supported actions expose the declared recovery class, marker, postcondition, and output;
- state permissions are exact, in-flight install refuses overwrite, completion requires a
  matching in-flight record, completed records cannot be replaced, and reads reject unsafe
  files;
- controlled failure before replacement leaves the valid in-flight record, controlled failure
  after replacement leaves the completed record, and temporary files are cleaned;
- nested sensitive values are absent from completed-record bytes and non-sensitive values
  remain;
- source inspection plus import behavior confirms the package never imports or invokes a
  mutation/network subprocess surface.

The focused guardrail is:

```bash
uv run --python 3.11 python -m unittest discover -s tests -v
```

It must exit 0 with every test passing. Packaging is checked with `uv build`, which must exit
0 and produce source and wheel artifacts without adding them to the commit.
