# 0005: Save cold fixture checkpoints as volume archives

## Status

Proposed

## Context

Issue #5 needs reusable pristine and named states for the local Bugzilla test fixture. A
complete state contains the MariaDB volume, Bugzilla's mutable-data volume, and an opaque
runner-state directory containing journals and actor credentials.

The original design treated restore as a production backup transaction: it retained two
volume generations, authenticated portable bundles, promoted candidates through an atomic
pointer, and recovered interrupted operations. The operator clarified that this is a temporal
test fixture. Save may stop every writer, restore may destructively replace the active fixture,
and a failed restore is recovered by rerunning it from the unchanged checkpoint. A checkpoint
only needs to work with the same checkout and stack revision.

## Decision

Use cold, immutable, same-revision checkpoint bundles.

A bundle is an owner-only directory containing:

- `manifest.json`;
- `mariadb-volume.tar`;
- `bugzilla-volume.tar`;
- `runner-state.tar`.

The manifest records format version 1, checkpoint name, creation time, checkout revision, a
fingerprint of the relevant stack and checkpoint inputs, and each artifact's byte size and
SHA-256 checksum. Checksums detect accidental corruption; they do not authenticate provenance.
Checkpoints are trusted local fixture artifacts and are not portable backups.

Save acquires the existing lifecycle lock after the caller has stopped runner activity. It
validates the destination, cleanly stops the complete Compose stack, archives both cold Docker
volumes and the opaque runner-state directory, writes and verifies the manifest in an
owner-only sibling staging directory, renames staging to the absent final name, then restarts
and health-checks the original stack. Existing names are never overwritten. A partial staging
directory is not a checkpoint and may be removed by a later save.

Restore validates the complete manifest, revision/fingerprint, artifact sizes, checksums, and
paths before destructive work. It then acquires the lifecycle lock, stops the stack, deletes
and recreates the canonical Docker volumes and runner-state directory, extracts all three
archives, starts the stack, and runs the existing health check. Each retry starts by recreating
the targets, so an interrupted or failed restore is recoverable by rerunning the same command.
The active fixture may be unusable between a failed restore and a successful retry; the source
checkpoint remains unchanged.

Docker-volume archive and extraction run in a network-disabled ephemeral container based on an
existing pinned stack image. Only the source or destination volume is writable as required;
archive bytes stream through standard input or output. Runner-state extraction is host-side and
rejects absolute paths, parent traversal, links, devices, and other special files.

Bundle and runner paths must be owned by the invoking user, owner-only, and non-overlapping.
The design trusts the invoking account, reviewed checkout, Docker daemon, existing stack images,
and locally produced bundle. It does not add HMAC keys, encryption, multi-user isolation, or a
hostile-bundle promise.

Compose continues to use its fixed canonical volumes. Checkpoint shares the existing lifecycle
lock identity but does not add active-volume pointers, transaction phases, durable recovery
owners, candidate generations, rollback state, capacity reserves, or a recovery command.

## Consequences

- Saving causes fixture downtime while every state domain is cold.
- Restoring is simple, deterministic, destructive, and retryable from the immutable bundle.
- Raw volume archives are coupled to the recorded checkout, container images, and storage
  formats; no migration or cross-version restore is promised.
- Normal validation rejects corrupt or incompatible input before deleting active state.
- Disk exhaustion, process termination, or startup failure after deletion can leave the fixture
  unusable until restore is rerun.
- Owner-only permissions and safe host extraction protect local secrets without claiming backup
  authenticity or encryption.
- The implementation and test matrix lose the production transaction, crash-recovery, capacity,
  candidate-promotion, and alias-commit machinery.

## Considered & rejected

- **Keep the transactional candidate-volume design.** judgment: preserving a running fixture
  through restore failure or host termination is a production availability guarantee beyond the
  explicitly approved retryable destructive-restore contract.
- **Use a logical MariaDB dump.** judgment: logical import adds SQL parsing, sandbox, allocation,
  and import-failure surfaces to provide portability that same-revision checkpoints explicitly do
  not require.
- **Authenticate bundles with a private HMAC key.** judgment: checkpoints are trusted local
  artifacts; checksums cover the requested accidental-corruption signal without introducing key
  lifecycle or recovery coupling.
- **Copy live volumes without stopping writers.** judgment: the fixture may stop completely, so
  online cross-domain snapshot coordination adds risk without user value.
- **Do nothing.** verified: issue #5 states that developers need named complete checkpoints to
  preserve and restore coherent intermediate fixture state.
