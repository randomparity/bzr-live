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
  reservation, alias verification, automatic runner coordination, exact image-identity
  verification, or issue #4 actor-secret changes.
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
   checkpoint commands use `/tmp/<checkout-project>.lifecycle.lock` regardless of `TMPDIR`.
   Before locking, checkpoint may validate only CLI grammar and path control characters, discover
   required tools, and derive the checkout root, project name, and lock identity. It acquires the
   lock before canonicalizing or traversing store/runner paths, reading the current revision,
   stack fingerprint, `.env`, or fixture state, and retains it through completion. It does not
   change the lock-directory format or add durable checkpoint ownership/recovery records.
5. Save cleanly stops the entire Compose stack before reading either Docker volume. Restore stops
   it before deleting fixture state. No live or cross-domain snapshot promise exists. The caller
   keeps the local `db` and `bugzilla` image generation unchanged between save and restore;
   checkpoint does not fingerprint or verify images, so drift may fail only after destructive
   replacement.
6. A final checkpoint contains exactly `manifest.json`, `mariadb-volume.tar`,
   `bugzilla-volume.tar`, and `runner-state.tar`. It is accepted only when the manifest checkpoint
   name equals both the validated CLI `NAME` and final-directory basename, and by the same
   checkpoint format, checkout revision, stack-input fingerprint, active `.env` credential
   generation, and canonical runner-state path.
7. Bundle staging and final directories are mode 0700; files are mode 0600. Store and runner path
   arguments reject ASCII control characters; spaces, quotes, and leading hyphens remain valid.
   Store and runner roots are owned by the invoking user, canonical, and non-overlapping. Runner
   state may not be the filesystem root, invoking user's home, checkout root, store root, or an
   ancestor of any of them. Restore accepts an absent runner leaf only at the fingerprinted path
   and only when its existing canonical parent is a non-symlink owner-only directory owned by the
   invoking user. Every existing descendant must be owned by the invoking user; directories must
   have owner rwx and may not be links, mount points, or on another filesystem.
8. The manifest and every artifact are fully validated before restore deletes volumes or runner
   state. The owner-owned mode-0700 final root and owner-owned mode-0600 single-link regular files
   must stay on one filesystem, outside mount points, and retain their opened identities.
   Validation covers schema, exact artifact names, byte sizes, SHA-256 checksums, revision, the
   fingerprint that binds `.env`, canonical runner target, and stack inputs, and successful
   end-to-end parsing of all three tar streams. Restore retains and rewinds those exact opened
   objects through destructive replacement and extraction rather than reopening pathnames.
9. Runner-state archive members use normalized relative POSIX paths. Restore rejects absolute
   paths, empty or parent components, backslashes, duplicate members, links, devices, FIFOs,
   sockets, sparse files, unknown types, and directory modes missing owner rwx before deleting
   active state. Validation builds a normalized path trie: every ancestor is an explicit or
   implicit directory, no regular file has descendants, and no path changes type. Save publication
   uses the same validator.
10. Docker-volume archive and extraction run in an ephemeral, network-disabled helper container
    based on the existing pinned MariaDB image. Save mounts only the source volume read-only and
    streams tar to stdout. Restore mounts only the fresh destination volume read-write and streams
    tar through stdin. The helper receives no Docker socket, credentials, checkout, runner state,
    or other host path.
11. Restore always removes and recreates the fixed canonical MariaDB and Bugzilla volumes and the
    runner-state directory before extraction. It never selects dynamic volume names or modifies
    volume pointers in `.env`.
12. The staging-to-final rename commits save. Save then starts the current local stack without
    building or pulling images and health-checks it. A readiness failure after commit returns
    nonzero, states that the checkpoint exists, and directs the operator to keep runners stopped,
    restore the expected local images if needed, and run `scripts/lifecycle up`; it does not delete
    or overwrite the checkpoint. Restore starts the reconstructed stack without building or
    pulling images and health-checks it. Restore failure returns nonzero with the exact retry
    command and does not roll back. Retry commands use POSIX-shell-safe `shlex.join` serialization
    of literal `scripts/checkpoint restore`, the validated name, and canonical absolute directory
    arguments.
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

While staging only, `.checkpoint-staging.json` is canonical UTF-8 JSON with sorted keys, no
insignificant whitespace, a final newline, and exactly `staging_format` integer 1,
`checkpoint_name`, and `final_path` equal to the canonical intended final directory. It is created
mode 0600 immediately after the staging directory, removed before final bundle validation, and is
never part of a published checkpoint.

`checkout_revision` is `git rev-parse HEAD`. `stack_fingerprint` is SHA-256 over a versioned,
length-delimited sequence of tagged input-name bytes and input-value bytes, sorted by input name.
File inputs are the owner-only `.env`, `compose.yaml`, every regular file under `containers/`,
`scripts/lifecycle`, `scripts/checkpoint`, and `src/bzr_live/checkpoint.py`. The canonical
runner-state path and fingerprint format identifier are also inputs. The manifest stores only the
resulting fingerprint, never `.env` bytes, individual secret hashes, or the runner path. This
catches relevant dirty working-tree changes, recreated credentials, and a different runner target
without requiring a clean checkout. A mismatch is incompatible; there is no migration path.

Tar archives are uncompressed. This avoids decompression-bomb behavior and keeps failure/retry
semantics observable. Docker-volume archives preserve the cold volume's numeric ownership, mode,
regular files, directories, and links as required by the exact pinned stack. They are consumed and
preflight-parsed only inside the isolated helper container. Runner archives preserve regular-file
owner permission bits; restored directories are always mode 0700. Links and special files are
unsupported.

## Architecture

`src/bzr_live/checkpoint.py` owns:

- CLI parsing and validation;
- lifecycle-lock acquisition using the existing lock identity;
- manifest encoding, fingerprinting, size checks, and checksums;
- safe runner-state tar creation, validation, and extraction;
- fixed Docker/Compose commands for cold stack stop/start/health;
- isolated helper-container commands for volume archive and extraction;
- safe staging creation, provenance-marker validation, and bounded direct-child cleanup;

`scripts/checkpoint` remains a thin executable that loads the installed/local package and delegates
to `main()`.

Per ADR 0005's bounded qualification of ADR 0001, this is the second operator entry point only for
checkpoint save/restore. Existing lifecycle operations remain owned by `scripts/lifecycle`; both
entry points use the fixed shared lock.

Checkpoint invokes Docker and Compose with fixed argv arrays and passes caller paths only through
validated path arguments or explicit mount specifications. It never constructs a shell command.
It operates on the canonical volume names already owned by Compose/lifecycle; it does not modify
`compose.yaml` or `.env` to point elsewhere.

The current branch's transaction implementation is replaced rather than adapted. Candidate volume,
rollback, reserve, HMAC, alias client, durable recovery, SQL import, and active-pointer code have no
compatibility obligation because they have never merged or shipped.

## Save data flow

1. Before locking, validate only CLI grammar and path control characters, discover required tools,
   and derive the checkout root, project name, and fixed lock identity.
2. Acquire the lifecycle lock. Under the lock, canonicalize and validate store and runner paths,
   ownership, modes, non-overlap, dangerous-root exclusions, mount/filesystem boundaries, the
   absent final name, owner-only `.env`, checkout revision, and fixture health.
3. If `.<NAME>.staging` exists, remove it only when it is an owner-owned mode-0700 directory on the
   store filesystem, is not a link or mount point, has only owner-owned mode-0600 regular direct
   children from the staging marker and final bundle filename set, and contains canonical
   mode-0600 `.checkpoint-staging.json` whose version, checkpoint name, canonical final path,
   checkout root, project, and save operation match this checkout and invocation. Unlink validated
   direct children, then remove the directory. Otherwise fail with the exact path and an
   instruction to inspect and remove it manually.
4. Compute the stack fingerprint including the canonical runner-state path. Report the caller's
   continuous runner-stopped responsibility through successful health, including any post-return
   manual restart interval; checkpoint cannot verify it.
5. Run lifecycle-equivalent `docker compose down --remove-orphans` semantics without `--volumes`
   while retaining the shared lock.
6. Create `.<NAME>.staging` atomically at mode 0700 and immediately write its canonical
   `.checkpoint-staging.json` marker. Stream the canonical MariaDB volume and Bugzilla data volume
   to their mode-0600 uncompressed tar files through the isolated helper based on the pinned
   MariaDB image. Create the runner-state tar with host-side safe traversal. Reject runner
   directories missing owner rwx, links, mount points, filesystem crossings, and foreign-owned
   descendants. On Linux, mount identities from the decoded process mount table must reject
   same-filesystem bind mounts before descendant inspection; inability to inspect that source is
   an actionable failure. Parse all three complete tar streams and validate runner topology before
   continuing.
7. Compute sizes/checksums, write mode-0600 canonical `manifest.json`, remove the staging marker,
   then re-read and validate the exact final bundle file set through the same validator restore
   uses.
8. Rename the absent staging directory to `NAME`. No overwrite is permitted. This publication
   prevents ordinary readers from observing a partial checkpoint but makes no crash-durability
   promise.
9. Start the current local stack with Compose `up --detach --no-build --pull never` semantics and
   run the existing health check, then release the lifecycle lock. If startup or health fails
   after publication, report that the checkpoint exists and fixture restart failed; do not delete
   the checkpoint.

On an ordinary pre-publication error, remove staging only through the same validated direct-child
cleanup and only when this invocation retained proof that it created the exact directory. Never
create marker authority after a staging-creation collision. Restart the current local stack without
building or pulling images, and report both the capture phase and restart result. Process or host
termination may leave staging, a stopped stack, and a stale lock. The next save applies only the
checkout-bound marker cleanup above. If restart fails, the operator keeps runners stopped, restores
the expected local images if necessary, clears only a
verified-stale lock through the existing manual procedure, and runs `scripts/lifecycle up`. No
recovery command or durable recovery record is added.

## Restore data flow

1. Before locking, validate only CLI grammar and path control characters, discover required tools,
   and derive the checkout root, project name, and fixed lock identity.
2. Acquire the lifecycle lock and report the caller's continuous runner-stopped responsibility
   through successful restored-stack health, including any post-return retry interval.
3. Under the lock, canonicalize and validate store and runner paths, ownership, modes, non-overlap,
   dangerous-root exclusions, mount/filesystem boundaries, owner-only `.env`, and checkout
   revision. An existing runner leaf and every descendant must be non-symlink, owned by the
   invoking user, on the runner filesystem, outside mount points, and owner-rwx when a directory.
   An absent leaf is valid only when its fingerprinted canonical path matches and its existing
   parent satisfies those checks.
4. While retaining the lock, validate the checkpoint name against the manifest and directory,
   canonical runner-state path, stack fingerprint, exact final file set, manifest schema,
   root/file ownership and private modes, regular single-link file identity, sizes, checksums, and
   all three complete tar streams. Apply the runner member, topology, ownership, directory-mode,
   mount, and filesystem-boundary checks. Keep the verified root and file descriptors open, repeat
   their authority and identity checks immediately before destructive work, and finish every check
   before stack shutdown or destructive work.
5. Run lifecycle-equivalent `docker compose down --remove-orphans` semantics without `--volumes`
   and without relying on current health, while retaining the shared lock.
6. Remove and recreate the fixed canonical MariaDB and Bugzilla Docker volumes. Delete the already
   validated runner tree bottom-up without changing permissions, then create the exact
   fingerprinted leaf mode 0700 beneath its validated parent.
7. Rewind and stream the same validated volume-archive descriptors into their fresh destinations
   through the isolated helper based on the pinned MariaDB image. Rewind and extract runner state
   from its validated descriptor without following links: create every directory mode 0700 and
   preserve archived owner permission bits only for regular files.
8. Start the current local stack with Compose `up --detach --no-build --pull never` semantics and
   run the existing bounded health check. Release the lock and report the restored checkpoint name
   and revision.

Any failure during or after step 6 returns nonzero and names the failed phase, unchanged checkpoint
path, and POSIX-shell-safe exact restore command to retry. It makes no attempt to preserve or
reconstruct the pre-restore fixture. A retry repeats validation and recreates every target before
extraction, so partial output cannot accumulate across attempts.

## Error behavior

- Usage, name, ownership, overlap, revision, fingerprint, manifest, size, checksum, and runner-tar
  errors fail before stack shutdown or destructive restore.
- Docker/Compose/helper failures include the operation and bounded stderr, never secret values.
- Save distinguishes `checkpoint not published`, `checkpoint published but fixture restart failed`,
  and success.
- Restore distinguishes validation, shutdown, deletion, volume extraction, runner
  cleanup/extraction, startup, and health phases. Every post-delete error includes the
  POSIX-shell-safe retry command.
- On the first `SIGINT` or `SIGTERM`, checkpoint records the signal, launches no further normal
  phase work, forwards the signal to the active child process group, and reaps it before recovery.
  Before stack shutdown it exits without recovery. During save after shutdown, it applies validated
  staging cleanup and attempts stack start/health. During restore after shutdown but before the
  first target deletion, it attempts start/health of the unchanged fixture. After the first target
  deletion, it leaves the stack down and reports the exact restore retry. A second signal or
  uncatchable process/OS termination has no cleanup guarantee.
- If termination leaves the lifecycle lock stale, the operator first verifies no holder remains
  and removes it through the lifecycle command's existing manual procedure before lifecycle
  restart or the exact restore retry.
- Every nonzero save or restore exit after the stack stops states that runners must remain stopped
  until the reported manual restart or repeated restore succeeds and passes health checks.
- Disk exhaustion is an ordinary save/restore failure. No capacity forecast or reserve is promised.

## Threat model

### Boundary inventory

Added boundaries are checkpoint CLI paths and names, local bundle bytes, tar member metadata,
Docker/Compose subprocesses, and writes to owner-local bundle/runner roots and Docker volumes. The
feature does not widen network access; the helper container runs with networking disabled.

### Actor model

The invoking local account, reviewed checkout, Docker daemon, unchanged local `db` and `bugzilla`
image generation, pinned helper image, and checkpoint it created are trusted. Accidental
corruption and accidental unsafe paths are in scope. A hostile local account, hostile Docker
daemon/image, or deliberately malicious bundle is outside the fixture contract.

### Controls

- Closed checkpoint names, manifest-to-directory name equality, and exact manifest schema prevent
  ambiguous destinations and artifacts.
- Ownership, mode, canonical non-overlap, no-overwrite, and marker-bound staging cleanup checks
  govern host paths and recursive deletion authority.
- SHA-256, exact size, and complete tar parsing reject accidental artifact corruption before
  deletion.
- Runner validation rejects traversal, conflicting file/descendant topology, links, sparse data,
  special files, unsafe ownership, restrictive directory modes, mount points, and filesystem
  crossings before deletion; extraction creates only mode-0700 directories beneath a new
  owner-only root.
- The helper container has no network or Docker socket, sees only one source/destination volume, and
  receives archive data through standard I/O.
- Fixed argv arrays prevent shell interpolation; retry display uses POSIX-safe argv serialization.
  Error output is bounded and excludes `.env` values.
- The stack fingerprint binds the complete owner-only `.env` and canonical runner-state path
  without writing their bytes or values into the manifest.
- The fixed absolute lock root serializes supported lifecycle and checkpoint mutations even when
  callers use different `TMPDIR` values.

### Out of scope

Bundle authenticity, encryption, remote transport, multi-user host isolation, hostile local input,
cross-version migration, online consistency, crash-atomic publication, preserving active state on
restore failure, and failures of Docker or storage after reported success are not promised.

## Verification

Focused tests must prove:

- closed name grammar, manifest-name/directory/CLI equality, and immutable no-overwrite behavior;
- owner-only staging/final roots and files, store/runner non-overlap, canonical runner-path
  fingerprinting, rejection of dangerous roots and control characters, and safe absent-leaf
  recreation;
- stale staging cleanup accepts only the matching checkout/project/operation provenance marker and
  allowed regular direct children, while create collisions, cross-checkout markers, unrelated,
  unmarked, linked, mounted, cross-filesystem, or unknown content fail without gaining deletion
  authority and with manual-inspection guidance;
- canonical manifest encoding, revision/fingerprint mismatch, exact file set, size mismatch,
  checksum corruption, root/file owner-mode-link-mount violations, opened-identity replacement,
  and malformed or truncated Docker-volume tar streams;
- a recreated `.env` at the same Git revision fails before stack shutdown or any target deletion;
- runner archive rejection for traversal, duplicates, links, sparse files, special types,
  restrictive directory modes, mounted descendants, filesystem crossings, foreign ownership, and
  both orderings of a regular-file/descendant conflict such as `a` with `a/b`;
- restored runner directories remain mode 0700, including partial extraction output, so a repeated
  restore can remove them without permission mutation;
- no store or runner path stat/open/traversal occurs before lock acquisition;
- save ordering: parse, lock, validate paths and compatibility inputs, health, fingerprint,
  `compose down --remove-orphans` without volumes, archive three domains, parse and validate,
  publish, start without build/pull, health;
- save phase errors preserve existing final names and distinguish restart failure after publication;
- restore validates every bundle/header condition before stack shutdown, and uses
  `compose down --remove-orphans` rather than `compose stop` before deleting volumes;
- restore ordering: parse, lock, validate, down without volumes, recreate all targets, extract all
  domains, start without build/pull, health;
- injected failures during and after each destructive phase retain the bundle and print the exact
  retry command;
- retry rendering round-trips canonical paths containing spaces, single quotes, and leading
  hyphens through POSIX shell parsing;
- injected first signals during shutdown, pre-delete restore, bounded host runner archive
  creation/extraction, each container extraction domain, startup, and health prove that normal
  archive work stops and any active child is reaped before cleanup/restart or retry reporting;
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
