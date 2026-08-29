# ADR 0002: Versioned scenario validation and journal boundary

## Status

Accepted

## Context

Scenario authors need stable symbolic identities and ordered mutations without generated
Bugzilla IDs in committed fixtures. Later provisioning, replay, and checkpoint work must
share one pre-mutation answer to four questions: whether the scenario is valid, what it
references, what exact content it represents, and whether an interrupted event is safe to
continue. The selected `bzr` binary remains responsible for supported Bugzilla mutations;
this repository must not grow a second general Bugzilla client.

The repository is new, so there is no compatibility contract to preserve. The first format
can make strictness, crash recovery, and secret handling structural instead of layering them
onto permissive dictionaries later.

## Decision

Use a standard-library-only Python package as the version 1 contract boundary.
`load_scenario(path)` strictly decodes `scenario.json`, `resources.json`, and each non-empty
line of `events.jsonl`, rejects duplicate JSON keys and every unknown or ill-typed field,
validates kind-specific resources and actions, resolves typed symbolic references, builds a
stable resource dependency plan, and returns an immutable `ValidatedScenario`. No mutation
adapter accepts raw fixture dictionaries.

References use `{"ref":"<kind>:<name>"}`. Resources are provisioned in a stable topological
order. Events retain file order and may reference only resources or symbolic objects created
by earlier events. The fixed version 1 action registry classifies every action as
`unique-create`, `idempotent-set`, or `append`; adding an action requires one validator entry
and, in later replay work, one `bzr` handler.

The scenario digest is SHA-256 over canonical UTF-8 JSON containing the format version,
validated manifest, resources, ordered events, and a sorted table of every declared asset's
path and SHA-256. Canonical bytes use sorted keys, no ASCII escaping, separators `,` and `:`
without whitespace, no trailing newline, and UTF-8 without normalization. The accepted JSON
domain excludes floats and non-finite numbers. Asset paths must be canonical relative POSIX
paths below `assets/`; asset files must be regular, non-symlinked files whose declared
checksums match their bytes. `ValidatedScenario` retains each validated asset as immutable
`bytes`, and later handlers must consume those bytes rather than reopening the path.

Journal state lives in an exact mode-0700 local directory. One non-blocking exclusive lock
on an exact mode-0600 lock file gives each state directory a single writer. Each event has
one exact mode-0600 JSON record and exactly two legal transitions: absent to matching
`in_flight`, then that `in_flight` record to matching `completed`. The first transition uses
an fsynced same-directory temporary file and an atomic no-replace link. After `bzr` returns,
the second transition verifies the existing digest, event, actor, recovery class, and marker,
then atomically replaces it with an fsynced completed record. Both transitions fsync the
containing directory. Every other overwrite, a concurrent writer, unsafe permissions,
symlinks, malformed records, and digest/event mismatches fail closed. The completed record
contains redacted invocation metadata, redacted structured output, exit status, resolved IDs,
and the next safe action. Sensitive key values are replaced recursively; credential
environment values are never accepted as journal metadata.

The package performs no Bugzilla mutation, network request, or subprocess invocation. Later
runner code may execute only from a `ValidatedScenario` and must delegate supported
mutations to `bzr` (apart from the separate, epic-authorized narrow custom-field adapter).

## Consequences

- Malformed fixtures, duplicate identities, bad dependency graphs, wrong-kind references,
  unsupported actions, and changed assets fail before a caller can receive an executable plan.
- Whitespace and JSON object key order do not change a digest; semantic values, ordered event
  position, declared asset metadata, and asset bytes do.
- Version 1 is deliberately closed. New resource kinds, actions, or fields require an
  explicit format-version decision instead of being silently ignored.
- Journal transitions survive process termination and request directory durability against
  power loss on supported local filesystems; later replay work retains responsibility for
  server-side reconciliation.
- Owner-only state is local secret material and is not a portable or sanitized export.
- The standard library keeps the runtime dependency and supply-chain surface empty, at the
  cost of explicit kind-specific validation code.

## Considered & rejected

- **Do nothing or defer the contract to replay implementation.** judgment: the frozen outcome
  requires validation and recovery contracts before any mutation handler; deferral would make
  later handlers accept the undefined inputs this decision exists to close.
- **Validate with JSON Schema alone.** judgment: cross-file typed references, ordered event
  outputs, dependency cycles, asset bytes, canonical hashing, and journal transitions still
  require Python logic, so a second schema authority would drift without replacing the hard
  validation work.
- **Combine JSON Schema with small semantic Python checks.** judgment: strict structural rules
  would then have two owners—the schema for authoring and generated/handwritten Python
  representations for immutable return types—while most risk remains in the semantic Python
  checks; one closed validator registry is smaller for the first version.
- **Use a model/validation framework.** judgment: the closed first-version vocabulary and
  small host runner do not justify a runtime dependency whose coercion and unknown-field
  defaults would themselves need auditing.
- **Allow generic resource and action dictionaries for later handlers.** judgment: this
  postpones contract decisions until mutation time, directly defeating the required
  pre-mutation validation boundary.
- **Journal by appending status lines.** judgment: a multi-record log adds recovery parsing
  and compaction while the required recovery boundary needs only the current in-flight or
  completed state.
- **Hash raw input bytes.** judgment: insignificant JSON whitespace or object-key ordering
  would invalidate resume even though the validated scenario is unchanged; canonical
  semantic JSON plus exact asset bytes preserves the intended boundary.
- **Write journal state through `bzr`.** judgment: `bzr` owns Bugzilla mutations, not the
  runner's local crash-recovery record, so this would blur both boundaries without adding a
  supported Bugzilla capability.
