# 0005: Save cold fixture checkpoints as volume archives

## Status

Proposed

## Context

Issue #5 needs reusable pristine and named states for the local Bugzilla test fixture. A complete
archived state contains the MariaDB volume, Bugzilla's mutable-data volume, and an opaque
runner-state directory containing journals and actor credentials. The active owner-only `.env`
credential generation is a compatibility prerequisite: Bugzilla cannot open a restored raw
MariaDB volume with a different database password.

The original design treated restore as a production backup transaction: it retained two
volume generations, authenticated portable bundles, promoted candidates through an atomic
pointer, and recovered interrupted operations. The operator clarified that this is a temporal
test fixture. Save may stop every writer, restore may destructively replace the active fixture,
and a failed restore is recovered by rerunning it from the unchanged checkpoint. A checkpoint
only needs to work with the same checkout, stack revision, and `.env` credential generation.

## Decision

Use cold, immutable, same-revision checkpoint bundles.
The operator interface is:

- `scripts/checkpoint save NAME --store DIRECTORY --runner-state DIRECTORY`;
- `scripts/checkpoint restore NAME --store DIRECTORY --runner-state DIRECTORY`.

`NAME` matches `[a-z0-9][a-z0-9_-]{0,63}`. `pristine` is the conventional reserved name;
invoking `save pristine` is the explicit act that creates the post-install baseline.


A bundle is an owner-only directory containing:

- `manifest.json`;
- `mariadb-volume.tar`;
- `bugzilla-volume.tar`;
- `runner-state.tar`.

The manifest records format version 1, checkpoint name, creation time, checkout revision, a
fingerprint of the relevant stack and checkpoint inputs, and each artifact's byte size and
SHA-256 checksum. The fingerprint binds the owner-only `.env` bytes and canonical runner-state
path without storing either input in the bundle. A different credential generation or runner
target is incompatible before destructive restore. Checksums detect accidental corruption; they
do not authenticate provenance. Checkpoints are trusted local fixture artifacts, not portable
backups.

Save requires the caller to keep runner activity stopped from before invocation through a
successful stack health check. It acquires the existing lifecycle lock before reading the current
revision, stack fingerprint, `.env`, or fixture state; validates the destination and existing
fixture health; cleanly stops the complete Compose stack; archives both cold Docker volumes and
the opaque runner-state directory; and validates the runner archive's members with the same rules
restore uses. It writes and verifies the manifest in an owner-only sibling staging directory, then
renames staging to the absent final name. That rename commits the checkpoint; existing names are
never overwritten.

Once stack shutdown begins, every save exit attempts to restart the current local stack without
building or pulling images and runs the existing health check. A capture failure before commit
removes staging where practical and reports that no checkpoint was published. A restart failure
returns nonzero and directs the operator to keep runners stopped, restore the expected local
images if necessary, and run `scripts/lifecycle up`. A post-commit failure also states that the
checkpoint exists and never removes it. A partial staging directory is not a checkpoint and may be
removed by a later save.

Restore acquires the lifecycle lock before reading the current revision, stack fingerprint,
`.env`, runner path, or fixture state. While retaining the lock, it validates the complete
manifest, revision/fingerprint, artifact sizes, checksums, runner archive members, and paths before
destructive work. It then stops the stack, deletes and recreates the canonical Docker volumes and
runner-state directory, extracts all three archives, starts the current local stack without
building or pulling images, and runs the existing health check. Each retry starts by recreating
the targets, so an interrupted or failed restore is recoverable by rerunning the same command. If
abrupt termination leaves the existing lifecycle lock stale, the operator must first verify that
no holder remains and remove it using the lifecycle command's existing manual procedure. Every
nonzero exit after the stack stops directs the operator to keep runners stopped until expected
local images are restored if necessary, a retry completes, and the restored stack passes health
checks. The active fixture may be unusable between a failed restore and a successful retry; the
source checkpoint remains unchanged.

Docker-volume archive and extraction run in a network-disabled ephemeral container based on the
existing pinned MariaDB image. Only the source or destination volume is writable as required;
archive bytes stream through standard input or output. Runner-state extraction is host-side and
rejects absolute paths, parent traversal, links, devices, and other special files.

Bundle and runner paths must be owned by the invoking user, owner-only, canonical, and
non-overlapping. The canonical runner-state path is fingerprinted at save and must match at
restore. It may not be the filesystem root, invoking user's home, checkout root, store root, or an
ancestor of any of them. On restore the leaf may be absent after a prior failed attempt; only the
fingerprinted path remains authoritative, and its existing canonical parent must be a non-symlink
directory owned by the invoking user and owner-only before the exact leaf is recreated mode 0700.
An existing leaf and every descendant must be non-symlink and owned by the invoking user before
replacement. Cleanup temporarily adds owner rwx permission to owner-owned directories as needed
for bottom-up deletion. Extraction creates directories mode 0700 until all children are written,
then applies archived owner permission bits from the leaves upward. Restrictive archived directory
modes therefore cannot prevent cleanup on a later retry.
The design trusts the invoking account, reviewed checkout, Docker daemon, existing stack images,
and locally produced bundle. It does not add HMAC keys, encryption, multi-user isolation, or a
hostile-bundle promise.

Compose continues to use its fixed canonical volumes. Lifecycle and checkpoint commands use the
single fixed absolute lock path `/tmp/<checkout-project>.lifecycle.lock`; `TMPDIR` does not alter
its identity. Checkpoint does not add active-volume pointers, transaction phases, durable recovery
owners, candidate generations, rollback state, capacity reserves, or a recovery command.

## Consequences

- Saving causes fixture downtime while every state domain is cold.
- The caller owns continuous runner quiescence through successful stack health, including manual
  restart or restore-retry intervals after a nonzero exit. Checkpoint does not coordinate runner
  processes; violating that precondition can produce incoherent state or lose intervening writes.
- Restoring is simple, deterministic, destructive, and retryable from the immutable bundle.
- Raw volume archives are coupled to the recorded checkout, local service images, and storage
  formats; no migration or cross-version restore is promised.
- Restore requires the exact `.env` credential generation fingerprinted at save time. Recreated
  credentials at the same Git revision are incompatible and fail before active state changes.
- The caller must keep the local `db` and `bugzilla` image generation unchanged between save and
  restore. Checkpoint does not fingerprint or verify images. Drift or removal can therefore fail
  only after destructive replacement; recovery is to restore compatible local images and rerun
  restore from the unchanged checkpoint.
- Restore may recursively replace only the fingerprinted canonical runner-state path after
  rejecting broad roots, unsafe ownership, and links. Permission normalization makes restrictive
  owner-owned directories removable on every retry.
- Normal validation rejects corrupt or fingerprint-incompatible input before deleting active state.
- Disk exhaustion, process termination, image drift, or startup failure after deletion can leave
  the fixture unusable until restore is rerun.
- Every save failure after shutdown begins attempts to restore fixture readiness. Failure of that
  attempt keeps runner quiescence active until compatible local images are restored and lifecycle
  restart succeeds.
- Rename is save's commit point. A later restart or health failure returns nonzero but leaves the
  immutable checkpoint valid.
- Abrupt termination may leave the existing lifecycle lock stale. Retry then requires its existing
  verified manual-clear procedure before rerunning restore.
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
- **Coordinate runner processes or acquire their advisory locks.** judgment: the operator
  explicitly assigned continuous runner quiescence to the caller and excluded automatic runner
  coordination. Checkpoint treats runner state as opaque and does not own its writers; starting a
  writer before checkpoint recovery finishes violates the command precondition.
- **Archive and replace `.env` during restore.** judgment: fingerprinting the active credential
  generation is sufficient for same-fixture compatibility. Copying secret configuration into
  every bundle and replacing active credentials would expand secret lifecycle and rollback
  behavior without making the temporal fixture more useful.
- **Bind exact service image IDs and rebind mutable Compose tags.** judgment: the operator selected
  an unchanged-local-images precondition instead. Image fingerprints, tag repair, staged service
  creation, and post-create identity verification add recovery machinery disproportionate to a
  same-checkout temporal fixture that already accepts destructive retry.
- **Do nothing.** verified: issue #5 states that developers need named complete checkpoints to
  preserve and restore coherent intermediate fixture state.
