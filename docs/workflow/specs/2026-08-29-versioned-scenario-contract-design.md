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
the returned validated plan and delegate Bugzilla mutations to `bzr`, with only the parent
epic's separately owned narrow custom-field REST adapter as an explicit exception.

## Requirements

1. Load exactly one scenario directory containing `scenario.json`, `resources.json`, and
   `events.jsonl`, plus declared regular files below optional `assets/`.
2. Require integer `format_version: 1` in the manifest, resource catalog, and every event.
3. Reject malformed JSON, duplicate object keys, non-finite or floating-point JSON numbers,
   unknown fields at every contract object, duplicate names within an identity namespace,
   unsupported resource/action kinds, invalid values, unsafe or changed assets, missing
   references, wrong-kind references, invalid dependencies, and event references to
   outputs not yet created.
4. Return immutable typed objects and a stable resource topological plan only after all
   files, references, event outputs, and assets validate.
5. Compute one lowercase SHA-256 digest from every validated document and every declared
   asset byte. JSON whitespace and object-key order are non-semantic; event line order and
   asset bytes are semantic.
6. Define recovery metadata for every supported action before execution: action class,
   expected postcondition, and deterministic reconciliation marker.
7. Store local state only in exact mode-0700 directories and exact mode-0600 regular files.
   Atomically install each numbered in-flight attempt before mutation, atomically replace it
   with a completed attempt after `bzr` returns, and retain completed attempts across a
   guarded retry.
8. Never persist credential environment values or rewrite journal identity/recovery fields.
   Reject known secrets in structural metadata and expected postconditions before mutation.
   Scrub sensitive-key values and known-secret substrings only from opaque invocation
   arguments and handler output before completion is persisted.
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

RecoveryClass = Literal["unique-create", "idempotent-set", "append"]
JsonValue = bool | int | str | tuple["JsonValue", ...] | Mapping[str, "JsonValue"] | None
PlannedValue = (
    bool | int | str | Reference
    | tuple["PlannedValue", ...] | Mapping[str, "PlannedValue"] | None
)

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
    data: Mapping[str, PlannedValue]
    dependencies: tuple[Reference, ...]

@dataclass(frozen=True)
class PlannedEvent:
    name: str
    actor: Reference
    action: str
    action_class: RecoveryClass
    payload: Mapping[str, PlannedValue]
    dependencies: tuple[Reference, ...]
    reconciliation_marker: str
    expected_postcondition: Mapping[str, PlannedValue]
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

`PlannedValue` is the immutable recursive runtime domain and includes typed `Reference`
objects. `JsonValue` is the same closed domain without references. Canonical digest and journal
serialization recursively convert every `Reference(kind, name)` to exactly
`{"ref":"<kind>:<name>"}`; deserialization performs the inverse only in schema-declared
reference fields. Returned mappings are read-only and returned sequences are tuples.
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
initial `assets/`. The loader opens the scenario directory and walks every asset path
component with descriptor-relative `os.open`, `O_NOFOLLOW`, and `O_DIRECTORY` on ancestors;
the final descriptor also uses `O_NOFOLLOW` and must identify a regular file. It reads those
bytes once, verifies that the declared 64-character lowercase SHA-256 matches, and retains
the immutable snapshot in `Asset.content`. Event validation and every later attachment
handler use that snapshot, closing both ancestor-symlink traversal and check-to-use races.

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
| `keyword` | `kind`, `name`, `description` | none | none |
| `flag-type` | `kind`, `name`, `description`, `target` | `products` (default `[]`), `components` (default `[]`) | products are `product`; components are `component` |

Emails must contain one non-edge `@` and no ASCII whitespace. `field_type` is one of `text`,
`single-select`, or `multi-select`. Select fields require a non-empty unique list of non-empty
string values; text fields require no values. A flag type target is `bug` or `attachment`.
Flag product/component lists are duplicate-free; an empty list means all values at that
scope. When both lists are non-empty, every listed component's owning product must also be
listed. All reference lists reject duplicate references.

Dependencies are every typed reference in the kind-specific reference fields. All must
resolve to declared resources of the required kind. The closed version 1 kind graph is
acyclic by construction. A stable Kahn topological sort preserves input order among currently
ready resources, and no partial plan is returned after any dependency error.

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

`PlannedEvent.dependencies` is complete and stable. It starts with the event actor, then adds
every typed reference in the validated payload—including assets and earlier event outputs—in
the field order shown by the action table (required fields first, then optional fields) and
the nested-field order stated below. Repeated identities collapse at their first occurrence.
The identity produced by the current event is recorded only in `creates`, never as its own
dependency. Tests assert the exact tuple for every reference-bearing field of every action.

The fixed version 1 actions are:

| Action | Class | Required payload keys | Optional payload keys | Creates |
|---|---|---|---|---|
| `bug.create` | `unique-create` | `alias`, `product`, `component`, `summary` | `description`, `version`, `milestone`, `assignee`, `cc`, `groups`, `depends_on`, `blocks`, `duplicate_of`, `keywords`, `estimated_hours`, `remaining_hours`, `custom_fields` | `bug:<alias>` |
| `bug.update` | `idempotent-set` | `bug`, `set` | none | none |
| `bug.comment` | `append` | `bug`, `body` | `private` | none |
| `bug.attach` | `append` | `alias`, `bug`, `asset`, `description`, `content_type` | `private` | `attachment:<alias>` |
| `bug.worktime` | `append` | `bug`, `hours`, `comment` | none | none |
| `bug.custom-field-set` | `idempotent-set` | `bug`, `values` | none | none |
| `bug.flag` | `idempotent-set` | `bug`, `flag_type`, `status` | `requestee` | none |
| `attachment.update` | `idempotent-set` | `attachment`, `obsolete` | `description` | none |

`bug.create` references must have the kinds implied by their key. Its component, version, and
milestone (when present) must resolve to resources owned by its selected product. The
validator records that product and component against the created bug identity. `cc`, `groups`,
`depends_on`, and `blocks` are duplicate-free lists. `keywords` is a duplicate-free list of
`keyword` references. Optional hours use the decimal-string grammar above. `custom_fields`
and `bug.custom-field-set.values` are non-empty lists of objects containing exactly `field`
and `value`; `field` references `custom-field`, and field names are unique in the assignment.
Text fields require a string. Single-select fields require one string present in the
resource's declared `values`; every member assigned to a multi-select field must occur in its
declared `values`, and the assignment list is duplicate-free.

`bug.update.set` is non-empty and accepts only `summary`, `status`, `resolution`, `assignee`,
`cc`, `groups`, `depends_on`, `blocks`, `duplicate_of`, `version`, `milestone`, `keywords`,
`estimated_hours`, and `remaining_hours`. Each reference field has its named kind; list and
hours rules match `bug.create`, including `keyword` references. A version or milestone must
belong to the target bug's recorded product. Null is accepted only for `resolution`,
`assignee`, `duplicate_of`, `version`, and `milestone`, where it explicitly clears the field.

Comment/work-time body text is non-empty. Attachment `content_type` must match
`[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+`: exactly one slash, non-empty
type/subtype, and no whitespace or control characters. A `bug.flag` `flag_type` references a
declared `flag-type` whose target is `bug`; if its product or component scopes are non-empty,
the target bug's recorded product/component must occur in them. Flag status is one of `?`,
`+`, `-`, or `X`; requestee is an actor and is allowed only with `?`. `obsolete` and
`private` are strict booleans. `private` defaults false.

Every event receives the deterministic reconciliation marker
`bzr-live:<scenario-name>:<event-name>`. Append handlers must include it in the semantic
mutation: comment and work-time bodies, and attachment descriptions together with the
validated asset checksum.

Every `expected_postcondition` has exactly `action`, `target`, `values`, and `marker`.
`action` and `marker` equal the planned event. `target` is the created `bug` or `attachment`
reference for create/attach and the payload's target reference for every other action.
`values` is constructed exactly as follows:

| Action | `values` mapping |
|---|---|
| `bug.create` | the normalized payload without `alias`, plus `server_alias: "bzr-live-<31hex>"`, where `<31hex>` is the first 31 lowercase hexadecimal characters of SHA-256 over UTF-8 bytes `v1\\0<scenario-name>\\0<alias>`; the result is exactly 40 ASCII characters; absent lists become `[]`, absent nullable references/hours become `null`, absent description becomes `""`, and absent custom fields become `[]` |
| `bug.update` | the normalized `set` mapping with only explicitly supplied keys; explicit nulls remain null |
| `bug.comment` | `body` and default-expanded `private` |
| `bug.attach` | `bug`, `asset`, the validated `asset_sha256`, `description`, `content_type`, and default-expanded `private` |
| `bug.worktime` | `bug`, canonical decimal-string `hours`, and `comment` |
| `bug.custom-field-set` | `bug` and normalized `values`, preserving declared field order |
| `bug.flag` | `bug`, `flag_type`, `status`, and `requestee` defaulted to null |
| `attachment.update` | `attachment`, `obsolete`, and `description` only when explicitly supplied |

These mappings and nested sequences are immutable. A later handler must compare the exact
normalized values, must not weaken the postcondition, and must not invent recovery metadata
at mutation time.

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
is ordered. Within normalized `scenario.json`, asset declarations are sorted by canonical
path. The envelope's asset table is the same sorted name/path/checksum projection, and each
checksum was computed from the exact immutable bytes retained in `Asset.content`. Reordering
asset declarations alone therefore leaves the digest unchanged. Serialize as UTF-8 JSON with
lexicographically sorted keys, no ASCII escaping, and separators `,` and `:` with no added
whitespace, then hash those bytes with SHA-256. Floats are absent by contract, eliminating
platform-dependent number rendering.

The digest includes the version in both the envelope and normalized documents. Any accepted
field value, resource/event order, event action, declared asset identity/path/checksum, or
asset byte change affects the digest. JSON formatting, object-key order, and asset declaration
order do not.

## Journal contract

The package exports frozen records and one store:

```python
@dataclass(frozen=True)
class InvocationMetadata:
    mutation_boundary: Literal["bzr", "bugzilla-rest-custom-field"]
    operation: str
    arguments: tuple[str, ...]
    environment_names: tuple[str, ...]

@dataclass(frozen=True)
class InFlightRecord:
    scenario_digest: str
    event: str
    attempt: int
    actor: Reference
    action_class: RecoveryClass
    expected_postcondition: Mapping[str, PlannedValue]
    reconciliation_marker: str

@dataclass(frozen=True)
class CompletedRecord:
    scenario_digest: str
    event: str
    attempt: int
    actor: Reference
    action_class: RecoveryClass
    expected_postcondition: Mapping[str, PlannedValue]
    reconciliation_marker: str
    invocation: InvocationMetadata
    handler_output: JsonValue
    exit_status: int
    resolved_ids: Mapping[str, int]
    next_safe_action: Literal["advance", "reconcile", "retry", "stop"]

class JournalStore:
    def __init__(self, state_dir: str | Path) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> JournalStore: ...
    def __exit__(self, *exc_info: object) -> None: ...
    def write_in_flight(
        self, record: InFlightRecord, *, known_secrets: Collection[str] = ()
    ) -> Path: ...
    def replace_completed(
        self, record: CompletedRecord, *, known_secrets: Collection[str] = ()
    ) -> Path: ...
    def read(
        self, event: str, attempt: int | None = None
    ) -> InFlightRecord | CompletedRecord | None: ...
```

Record JSON adds `journal_version: 1` and `phase: "in_flight" | "completed"`. Actors and
postcondition references serialize as typed reference objects. Digests are lowercase SHA-256;
event names use the slug grammar; attempts are positive integers; exit statuses are integers
excluding booleans; resolved ID keys are typed references and values are positive integers.
Both constructors and store reads/writes reject an action class outside `RecoveryClass`;
`next_safe_action` is closed to the four values above.

`JournalStore` creates only an absent state directory with mode 0700. It opens that directory
once with `O_DIRECTORY | O_NOFOLLOW`, verifies its exact mode and owner from `fstat`, and
retains the descriptor for the store's lifetime. Non-directories, symlinks, wrong ownership,
and any mode other than 0700 fail. Every later lock, temporary-file, link, replace, unlink,
read, and directory-fsync operation is relative to that descriptor, so replacing a pathname
ancestor cannot move work away from the locked directory. The store opens an exact mode-0600
`.lock` regular file relative to the descriptor with `O_NOFOLLOW` and takes a non-blocking
exclusive `flock`; a concurrent store fails closed. `close()` releases both descriptors, and
context-manager use is supported. On macOS, creation clears inherited access ACLs before
verification; retained directories, locks, temporary files, and attempt files reject any
nontrivial access ACL in addition to wrong ownership or mode bits. Attempt files are
`<event>.<attempt-as-six-digits>.json`. Reads reject non-regular files, symlinks, permission
bits other than 0600, malformed/unknown fields, unsupported journal versions, and gaps or
conflicting attempts. With no attempt argument, `read` returns the latest attempt.

A transition serializes canonical JSON plus one newline to a mode-0600 descriptor-relative
temporary regular file, flushes and `fsync`s it. `write_in_flight` permits attempt 1 only
when no attempt exists. It permits attempt $n+1$ only when the latest record is completed
attempt $n$ with `next_safe_action: "retry"` and the new digest, event, actor, action class,
expected postcondition, and marker equal that completed record; all prior attempt files
remain immutable. It uses descriptor-relative `os.link` to install the fully written inode
without replacing an existing name, then unlinks the temporary name. `replace_completed`
reads the same attempt's valid in-flight record and requires matching attempt, scenario
digest, event, actor, action class, expected postcondition, and marker, then calls
descriptor-relative `os.replace`. Both transitions `fsync` the retained directory descriptor
after directory changes. Temporary files are removed after any pre-install failure. Every
other overwrite or mismatch is rejected. The exclusive store lock prevents two compliant
writers from passing a read-before-replace check concurrently.

Invocation metadata is structurally allowlisted to mutation boundary, operation, argument
strings, and credential environment _variable names_; environment values have no input field.
The boundary is `bzr` except for `bug.custom-field-set`, whose later handler may use only the
epic-authorized `bugzilla-rest-custom-field` adapter. Protocol identity and recovery fields
are never redacted: scenario digest, event, attempt, actor, action class, expected
postcondition, marker, mutation boundary, operation, environment names, exit status, resolved
IDs, and next safe action remain byte-stable. `write_in_flight` rejects any non-empty known
secret found recursively in its structural fields or expected postcondition, before the
caller may mutate. `replace_completed` rejects a secret in completion-only structural fields
and leaves the in-flight record intact.

Redaction copies only `invocation.arguments` and `handler_output`. Within those opaque values,
key normalization first replaces every non-alphanumeric run with `_`, then inserts `_` at
both a lowercase-letter-or-digit to uppercase-letter boundary and an uppercase acronym to a
following title-cased word boundary, strips edge underscores, and lowercases. Thus `API_KEY`
becomes `api_key`, `AUTHORIZATION` becomes `authorization`, `refreshToken` becomes
`refresh_token`, and `APIToken` becomes `api_token`. Split the normalized value on
underscores. A key is sensitive when any segment is `token`, `password`, `secret`, `cookie`,
`credential`, `authorization`, or `apikey`, or when adjacent segments are `api`, `key`; its
value becomes JSON null. This covers `API_KEY`, `AUTHORIZATION`, `clientAPIKey`,
`proxyAuthorization`, `APIToken`, `APISecret`, `HTTPAuthorization`, `access_token`,
`client-secret`, and `set-cookie` with an empty `known_secrets` collection. In all other
opaque strings, every non-empty known-secret substring is removed, longest secrets first,
until none remains in that value. The collection is neither retained nor serialized.
The invariant applies to parsed opaque payload values,
not coincidental bytes in JSON syntax or protocol identities. Tests cover all compound keys
above and secrets such as `redacted`, `<`, and `OPAQUE`, require no opaque output string to
contain them, and require identities to remain unchanged.

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
- **Added: caller result to local journal.** A local runner controls allowlisted invocation
  metadata, structured handler output, resolved IDs, and next-action classification; these
  may contain credentials or server-returned sensitive data.
- **Not widened: Bugzilla mutation boundary.** This package performs no mutation. Later
  handlers use `bzr`, apart from the already-authorized narrow custom-field adapter, and this
  change grants neither path new callers or credentials.

### Actors and trust

The untrusted party is a local scenario author or a modified checkout consumed by an
operator or CI job. The local operator account is trusted to select the state directory and
run the later executor, but other local users are not trusted to read its secrets. The later
runner is trusted to supply the complete known-secret collection and never place credential
environment values in metadata; structural-field rejection and opaque-payload redaction
enforce that contract before each atomic transition.

### Controls

- Strict duplicate-aware decoding, closed keys/kinds/actions, exact value checks, and complete
  dependency/reference resolution prevent malformed control data reaching a handler.
- Canonical slug identities and immutable returned mappings prevent alias confusion and
  post-validation mutation.
- Canonical relative asset paths, no symlinks, containment checks, regular-file checks, and
  verified hashes prevent traversal and digesting a different target through a link.
- Input size is bounded by available local disk/memory; this first local-development contract
  does not claim hostile multi-tenant denial-of-service resistance.
- Exact directory/file modes, no symlink following, same-directory atomic replacement, and
  fsync control disclosure and torn state on supported local filesystems.
- Structural-field secret rejection keeps protocol identity byte-stable; sensitive-key and
  known-secret redaction is limited to opaque arguments/output, and credential environment
  values have no journal field. Errors identify fields but never echo sensitive values.
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

- a minimal valid scenario returns immutable typed resources/events whose nested planned
  values contain `Reference` objects, plus a stable plan;
- duplicate JSON keys, unknown fields, invalid versions/types, malformed resource/action
  shapes, duplicate resource/event/asset/output names, unresolved/forward/self references,
  wrong-kind and cross-product references, undeclared keywords/flag types, out-of-scope flag
  targets, out-of-catalog select values, missing actors/assets, invalid custom-field values,
  and invalid dependencies fail with source and field context;
- unsafe, missing, ancestor/final symlinked, non-regular, and checksum-mismatched assets fail;
- equivalent JSON formatting/key order and reordered asset declarations have the same digest,
  while every manifest/resource/event field, resource/event order, asset field, and asset byte
  mutation changes it;
- every supported action exposes the exact declared recovery class, ordered complete
  dependency tuple, marker, created identity, and action-specific postcondition mapping;
  every created bug server alias is deterministic and exactly 40 ASCII characters;
- media types `/`, `/plain`, `text/`, values with extra slashes, whitespace, or controls fail;
- state ownership/permissions are exact, replacing the state path after construction cannot
  redirect descriptor-relative work, in-flight install refuses overwrite, constructors/store
  reads/writes reject unsupported recovery classes, completion requires a matching attempt
  and expected postcondition, retry requires the latest completed attempt to authorize
  identical recovery metadata, completed history remains immutable, concurrent stores fail,
  and reads reject unsafe files;
- controlled failure before replacement leaves the valid in-flight record, controlled failure
  after replacement leaves the completed record, and temporary files are cleaned;
- known secrets in structural fields or expected postconditions are rejected without
  installing/replacing a record; exceptions retain field context but contain neither the
  supplied secret nor the sensitive value; protocol identities remain byte-stable; parsed
  opaque output values null compound credential keys including `API_KEY`, `AUTHORIZATION`,
  `clientAPIKey`, `proxyAuthorization`, `access_token`, `refreshToken`, `client-secret`, and
  `set-cookie`, and contain none of the known secrets—including collision cases `redacted`,
  `<`, and `act`—while non-sensitive values remain;
- source inspection plus import behavior confirms the package never imports or invokes a
  mutation/network subprocess surface.

The focused guardrail is:

```bash
uv run --python 3.11 python -m unittest discover -s tests -v
```

It must exit 0 with every test passing. Packaging is checked with `uv build`, which must exit
0 and produce source and wheel artifacts without adding them to the commit.
