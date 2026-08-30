# Complete cold fixture checkpoint save and restore design

## Scope charter

- **Interaction:** interactive.
- **Scope identity:** GitHub issue #5, `randomparity/bzr-live#5`.
- **Outcome:** developers can save and destructively restore complete named states of the local
  Bugzilla temporal test fixture.
- **Completion criteria:** save and restore round-trip the cold MariaDB volume, cold Bugzilla
  mutable-data volume, and opaque runner-state directory; a different active `.env` credential
  generation, incompatible bundle, or corrupt bundle fails before destructive restore; an
  interrupted restore is retryable from the unchanged bundle; a real fixture smoke proves save,
  mutation, restore, and repeat restore.
- **Provenance:** issue #5 requests pristine and named complete checkpoints. During interactive
  review, the operator classified the stack as a temporal test fixture; selected retryable
  destructive restore, complete fixture contents, same-checkout-revision and active-credential
  compatibility, complete save quiescence, and cold raw-volume archives.
- **Exclusions:** no production backup or availability guarantee; no online snapshot; no
  cross-version or cross-host portability; no logical SQL dump; no candidate/rollback volumes;
  no active-volume pointers; no transaction/recovery state machine; no HMAC, encryption, capacity
  reservation, alias verification, or automatic runner coordination; no issue #4 actor-secret
- **Surface:** ADR 0005; this specification; `src/bzr_live/checkpoint.py`;
  `scripts/checkpoint`; the lifecycle lock root in `scripts/lifecycle`; focused checkpoint and
  lifecycle-lock tests; live checkpoint smoke; lifecycle CI. Existing fixed Compose volumes and
  all other lifecycle behavior remain authoritative.
- **Ambiguities:** none after the operator decisions above.

[ADR 0005](../../adr/0005-cold-fixture-checkpoints.md) governs the bundle and failure
contract.

## Requirements

1. `scripts/checkpoint save NAME --store DIRECTORY --runner-state DIRECTORY` creates an immutable
   named checkpoint. `NAME` matches `[a-z0-9][a-z0-9_-]{0,63}`; `pristine` is reserved for the
   explicit post-install baseline. Save refuses to overwrite an existing final directory.
2. `scripts/checkpoint restore NAME --store DIRECTORY --runner-state DIRECTORY` restores the
   complete fixture destructively. Restore may leave the active fixture unusable on failure; the
   source checkpoint remains unchanged, and rerunning the same command is the supported recovery.
3. The caller keeps runner activity stopped from before either command starts through a successful
   stack health check. After a nonzero exit following stack stop, the caller keeps runners stopped
   through the reported manual restart or restore-retry interval. Checkpoint does not inspect,
   signal, lock, or recover runner processes. Usage and every relevant error report this continuous
   precondition.
4. Save and restore share the existing checkout-scoped lifecycle lock identity. Lifecycle and
   checkpoint commands use `/tmp/<checkout-project>.lifecycle.lock` regardless of `TMPDIR`. They
   acquire it before reading the current revision, stack fingerprint, `.env`, service image IDs, or
   fixture state and retain it through completion. They do not change the lock-directory format or
   add durable checkpoint ownership/recovery records.
5. Save cleanly stops the entire Compose stack before reading either Docker volume. Restore stops
   it before deleting fixture state. No live or cross-domain snapshot promise exists.
6. A checkpoint contains exactly `manifest.json`, `mariadb-volume.tar`,
   `bugzilla-volume.tar`, and `runner-state.tar`. It is accepted only by the same checkpoint
   format, checkout revision, stack-input fingerprint, active `.env` credential generation,
   canonical runner-state path, and exact local Docker image IDs recorded from the healthy `db`
   and `bugzilla` services.
7. Bundle staging and final directories are mode 0700; files are mode 0600. Store and runner roots
   are owned by the invoking user, canonical, and non-overlapping. Runner state may not be the
   filesystem root, invoking user's home, checkout root, store root, or an ancestor of any of them.
   Restore accepts an absent runner leaf only at the fingerprinted path and only when its existing
   canonical parent is a non-symlink owner-only directory owned by the invoking user.
8. The manifest and every artifact are fully validated before restore deletes volumes or runner
   state. Validation covers schema, exact artifact names, byte sizes, SHA-256 checksums, revision,
   and the fingerprint that binds `.env`, canonical runner target, service image IDs, and stack
   inputs.
9. Runner-state archive members use normalized relative POSIX paths. Restore rejects absolute
   paths, empty or parent components, backslashes, duplicate members, links, devices, FIFOs,
   sockets, sparse files, and unknown types before deleting active state.
10. Docker-volume archive and extraction run in an ephemeral, network-disabled helper container
    using the validated `db` image ID. Save mounts only the source volume read-only and streams tar
    to stdout. Restore mounts only the fresh destination volume read-write and streams tar through
    stdin. The helper receives no Docker socket, credentials, checkout, runner state, or other host
    path.
11. Restore always removes and recreates the fixed canonical MariaDB and Bugzilla volumes and the
    runner-state directory before extraction. It never selects dynamic volume names or modifies
    volume pointers in `.env`.
12. The staging-to-final rename commits save. Save then creates services with builds and pulls
    disabled, verifies each created container's image ID against the values fingerprinted before
    shutdown, starts the unchanged stack, and health-checks it. An identity or readiness failure
    after commit returns nonzero, states that the checkpoint exists, reports both recorded image
    IDs and exact identity-preserving recovery commands, and prints the exact restore command. It
    never directs recovery through the rebuilding lifecycle `up` path or deletes the checkpoint.
    Restore applies the same no-build, no-pull create/verify/start/health sequence. Restore failure
    returns nonzero with the exact retry command and does not roll back.
13. Named checkpoints round-trip representative database, Bugzilla mutable-data, and runner-state
    markers. Repeating restore from the same checkpoint succeeds.

## Bundle format

`manifest.json` is canonical UTF-8 JSON with sorted keys, no insignificant whitespace, and a final
newline. Format version 1 contains:

```json
{
  "artifacts": {
    "bugzilla-volume.tar": {"sha256": "<64 lowercase hex>", "size": 0},
    "mariadb-volume.tar": {"sha256": "<64 lowercase hex>", "size": 0},
    "runner-state.tar": {"sha256": "<64 lowercase hex>", "size": 0}
  },
  "checkpoint_format": 1,
  "checkpoint_name": "pristine",
  "checkout_revision": "<40 lowercase hex>",
  "created_at": "<RFC 3339 UTC timestamp>",
  "stack_fingerprint": "<64 lowercase hex>"
}
```

Artifact sizes are non-negative JSON integers and must equal the regular file sizes. Checksums are
computed over the exact uncompressed tar bytes. Checkpoint does not accept extra manifest keys,
extra files, or missing files.

`checkout_revision` is `git rev-parse HEAD`. `stack_fingerprint` is SHA-256 over a versioned,
length-delimited sequence of tagged input-name bytes and input-value bytes, sorted by input name.
File inputs are the owner-only `.env`, `compose.yaml`, every regular file under `containers/`,
`scripts/lifecycle`, `scripts/checkpoint`, and `src/bzr_live/checkpoint.py`. Other inputs are the
canonical runner-state path and exact local Docker image IDs used by the healthy `db` and
`bugzilla` service containers at save time. The fingerprint format identifier is also an input.
The manifest stores only the resulting fingerprint, never `.env` bytes, individual secret hashes,
runner paths, or image IDs. This catches relevant dirty working-tree changes, recreated
credentials, a different runner target, rebuilt service images, and removed service images without
requiring a clean checkout. A mismatch or unavailable recorded image is incompatible; restore
never builds or pulls an image before this gate.
Tar archives are uncompressed. This avoids decompression-bomb behavior and keeps failure/retry
semantics observable. Docker-volume archives preserve the cold volume's numeric ownership, mode,
regular files, directories, and links as required by the exact pinned stack. They are consumed only
inside the isolated helper container. Runner archives preserve directories, regular files, and
owner permission bits only; links and special files are unsupported.

## Architecture

`src/bzr_live/checkpoint.py` owns:

- CLI parsing and validation;
- lifecycle-lock acquisition using the existing lock identity;
- manifest encoding, fingerprinting, size checks, and checksums;
- safe runner-state tar creation, validation, and extraction;
- fixed Docker/Compose commands for cold stack stop/start/health;
- no-build/no-pull Compose service creation and created-container image-ID verification before
  service start;
- isolated helper-container commands for volume archive and extraction;
- deterministic staging cleanup and actionable phase errors.

`scripts/checkpoint` remains a thin executable that loads the installed/local package and delegates
to `main()`.

Checkpoint invokes Docker and Compose with fixed argv arrays and passes caller paths only through
validated path arguments or explicit mount specifications. It never constructs a shell command.
It operates on the canonical volume names already owned by Compose/lifecycle; it does not modify
`compose.yaml` or `.env` to point elsewhere.

The current branch's transaction implementation is replaced rather than adapted. Candidate volume,
rollback, reserve, HMAC, alias client, durable recovery, SQL import, and active-pointer code have no
compatibility obligation because they have never merged or shipped.

## Save data flow

1. Parse and validate root, name, store, and canonical runner-state path; ownership; modes;
   non-overlap; dangerous-root exclusions; and required tools. Require the final checkpoint name
   to be absent.
2. Acquire the existing lifecycle lock. Under the lock, read and validate the owner-only `.env`
   and checkout revision; revalidate the absent final name; remove only the deterministic
   owner-only `.<NAME>.staging` directory from an interrupted prior save; and require the existing
   fixture health check to pass.
3. Resolve the exact image IDs used by the healthy `db` and `bugzilla` containers and require the
   current Compose image references to resolve to those same IDs before shutdown, then compute the
   stack fingerprint including the canonical runner-state path. Report the caller's continuous
   runner-stopped responsibility through successful health, including any post-return manual
   restart interval; checkpoint cannot verify it.
4. Cleanly stop the complete Compose stack without deleting volumes.
5. Create `.<NAME>.staging` mode 0700. Stream the canonical MariaDB volume and Bugzilla data volume
   to their mode-0600 uncompressed tar files through the isolated helper using the recorded `db`
   image ID. Create the runner-state tar with host-side safe traversal.
6. Compute sizes/checksums, write mode-0600 canonical `manifest.json`, then re-read and validate the
   complete staging bundle through the same validator restore uses.
7. Rename the absent staging directory to `NAME`. No overwrite is permitted. This publication
   prevents ordinary readers from observing a partial checkpoint but makes no crash-durability
   promise.
8. With builds and pulls disabled, create the Compose services, inspect each created container's
   image ID, and refuse to start if either differs from the pre-shutdown value. Start the verified
   services and run the existing health check, then release the lifecycle lock. If identity,
   startup, or health fails after publication, report that the checkpoint exists, both recorded
   IDs, the exact identity-preserving no-build/no-pull create/verify/start/health commands, and the
   exact checkpoint restore command; do not delete the checkpoint or direct the operator through
   the rebuilding lifecycle `up` path.

On an ordinary pre-publication error, remove staging where practical, run the same no-build,
no-pull create/image-verify/start/health sequence, and report both the capture phase and restart
result. If restart fails, report both recorded IDs and the exact commands that retry the same
identity-preserving sequence; never direct recovery through lifecycle `up`. Process or host
termination may leave staging and a stopped stack. The next save removes only its matching staging
directory. No recovery command or durable recovery record is added.

## Restore data flow

1. Parse and validate root, name, store, and canonical runner-state path; ownership; modes;
   non-overlap; dangerous-root exclusions; and required tools. An existing runner leaf must be an
   owner-only non-symlink directory owned by the invoking user. An absent leaf is valid only when
   its fingerprinted canonical path matches and its existing parent satisfies those same checks.
2. Acquire the lifecycle lock and report the caller's continuous runner-stopped responsibility
   through successful restored-stack health, including any post-return retry interval.
3. While retaining the lock, read and validate the owner-only `.env`, checkout revision, canonical
   runner-state path, and current Compose image references. Require those references to resolve to
   the IDs represented by the bundle fingerprint, then validate the final bundle's exact files,
   manifest schema, regular-file sizes, checksums, and all runner archive headers. Finish every
   check before destructive work. Restore does not build or pull images.
4. Stop the complete Compose stack without relying on its current health.
5. Remove and recreate the fixed canonical MariaDB and Bugzilla Docker volumes. Remove an existing
   validated runner leaf if present, then create the exact fingerprinted leaf mode 0700 beneath its
   already validated parent.
6. Stream each volume archive into its fresh destination through the isolated helper using the
   validated `db` image ID. Extract runner state into its fresh directory without following links
   and apply owner permission bits after writing contents.
7. With builds and pulls disabled, create the Compose services. Inspect each created container's
   image ID against the preflight values before starting either service. On a match, start the
   services and run the existing bounded health check. Release the lock and report the restored
   checkpoint name and revision.

Any failure during or after step 5 returns nonzero and names the failed phase, unchanged checkpoint
path, and exact restore command to retry. It makes no attempt to preserve or reconstruct the
pre-restore fixture. A retry repeats validation and recreates every target before extraction, so
partial output cannot accumulate across attempts.

## Error behavior

- Usage, name, ownership, overlap, revision, fingerprint, manifest, size, checksum, and runner-tar
  errors fail before destructive restore.
- Docker/Compose/helper failures include the operation and bounded stderr, never secret values.
- Save distinguishes `checkpoint not published`, `checkpoint published but fixture restart failed`,
  and success.
- Restore distinguishes validation, stop/delete, volume extraction, runner extraction, service
  creation, image identity, startup, and health phases. Every post-delete error includes the retry
  command.
- `SIGINT` and `SIGTERM` follow ordinary best-effort cleanup/restart paths, but abrupt process or OS
  termination has no stronger guarantee. If abrupt termination leaves the lifecycle lock stale,
  the operator first verifies no holder remains and removes it through the lifecycle command's
  existing manual procedure, then reruns the exact restore command.
- Every nonzero save or restore exit after the stack stops states that runners must remain stopped
  until the reported manual restart or repeated restore succeeds and passes health checks.
- Disk exhaustion is an ordinary save/restore failure. No capacity forecast or reserve is promised.

## Threat model

### Boundary inventory

Added boundaries are checkpoint CLI paths and names, local bundle bytes, tar member metadata,
Docker/Compose subprocesses, and writes to owner-local bundle/runner roots and Docker volumes. The
feature does not widen network access; the helper container runs with networking disabled.

### Actor model

The invoking local account, reviewed checkout, Docker daemon, pinned stack images, and checkpoint
it created are trusted. Accidental corruption and accidental unsafe paths are in scope. A hostile
local account, hostile Docker daemon/image, or deliberately malicious bundle is outside the fixture
contract.

### Controls

- Closed checkpoint names and exact manifest schema prevent ambiguous destinations and artifacts.
- Ownership, mode, canonical non-overlap, and no-overwrite checks govern host paths.
- SHA-256 and exact size checks reject accidental artifact corruption before deletion.
- Runner tar validation rejects traversal, links, sparse data, and special files before deletion;
  extraction creates only beneath a new owner-only root.
- The helper container has no network or Docker socket, sees only one source/destination volume, and
  receives archive data through standard I/O.
- Fixed argv arrays prevent shell interpolation. Error output is bounded and excludes `.env` values.
- The stack fingerprint binds the complete owner-only `.env` and exact local service image IDs
  without writing their bytes or individual values into the manifest.
- The fixed absolute lock root serializes supported lifecycle and checkpoint mutations even when
  callers use different `TMPDIR` values.

### Out of scope

Bundle authenticity, encryption, remote transport, multi-user host isolation, hostile local input,
cross-version migration, online consistency, crash-atomic publication, preserving active state on
restore failure, and failures of Docker or storage after reported success are not promised.

## Verification

Focused tests must prove:

- closed name grammar and immutable no-overwrite behavior;
- owner-only staging/final files, store/runner non-overlap, canonical runner-path fingerprinting,
  rejection of filesystem/home/checkout/store roots and their ancestors, and safe absent-leaf
  recreation from an owner-only non-symlink parent;
- canonical manifest encoding, revision/fingerprint mismatch, exact file set, size mismatch, and
  checksum corruption;
- a recreated `.env` at the same Git revision fails before any volume or runner deletion;
- a rebuilt, retagged, or missing `db` or `bugzilla` image fails before any volume or runner
  deletion, including a healthy running container whose Compose reference was retagged before save;
- a tag change after preflight is detected from the created container IDs before either service
  starts;
- runner archive rejection for traversal, duplicates, links, sparse files, and special types;
- save ordering: validate paths, lock, read compatibility inputs, health, resolve service images,
  fingerprint, stop, archive three domains, validate, publish, create, verify images, start, health;
- save phase errors preserve existing final names and distinguish restart failure after publication;
- restore acquires the lifecycle lock before reading compatibility inputs and validates every
  bundle/header condition before volume or runner deletion;
- restore ordering: lock, validate, stop, recreate all targets, extract all domains, create, verify
  images, start, health;
- injected failures during and after each destructive phase retain the bundle and print the exact
  retry command;
- a second restore begins from fresh targets rather than partial prior output;
- Docker/helper commands use fixed argv, disabled networking, minimal mounts, and no shell;
- checkpoint and existing lifecycle commands contend on the same fixed lock when launched from
  different working directories with different `TMPDIR` values.

The live smoke must:

1. Start a healthy fixture and create distinct markers in MariaDB, Bugzilla mutable data, and runner
   state.
2. Save a named checkpoint and verify the stack restarts healthy.
3. Mutate or delete all three markers.
4. Restore the checkpoint and verify all original markers plus fixture health.
5. Restore the same checkpoint again and verify the same result.
6. Corrupt a copied bundle and prove restore rejects it before issuing volume deletion.

The normal package tests, shell checks, `actionlint`, installed-package smoke, and lifecycle CI remain
required. Tests for HMAC, reserves, SQL import, candidate promotion, rollback, recovery ownership,
active pointers, aliases, and crash durability are deleted with those guarantees.

## Durable workflow context

- Branch: `design/cold-fixture-checkpoints-5`.
- Base branch: `main` at `124295f703bbb0fa54d4b897e10891b8b7a7b487` when redesign began.
- ADR: `docs/adr/0005-cold-fixture-checkpoints.md`.
- Guardrails known at redesign: `make test`, `make check`,
  `actionlint .github/workflows/container-lifecycle.yml`, and live checkpoint smoke.
- The operator approved the durable ADR and specification on 2026-08-30. A replacement
  implementation plan is the next gate; no implementation begins before that plan is reviewed.
