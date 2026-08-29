# 0001: Build Bugzilla from pinned upstream source

## Status

Accepted

## Context

The local live-test substrate must run Bugzilla 5.2 with MariaDB on both Linux
amd64 and arm64, while keeping secrets and mutable state out of the repository.
Bugzilla publishes a 5.2 source branch and a development Compose stack, but does
not publish a first-party multi-architecture Bugzilla 5.2 runtime image. The
upstream stack also embeds development passwords and publishes on every host
interface.

## Decision

Build Bugzilla from the immutable upstream commit
`644c66f45ce0b1b2746a31a061fbd96886278225`. Download its canonical archive from
`https://github.com/bugzilla/bugzilla/archive/644c66f45ce0b1b2746a31a061fbd96886278225.tar.gz`
and require SHA-256
`bf4f79d9e6b230ad01d9b3c4d12a56af7eebd96cc76d5c9231a696a0750e1660`.
Build on the pinned multi-architecture Ubuntu 24.04 image index
`sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517`.
Run MariaDB 10.6 from the pinned multi-architecture image index
`sha256:92e50059ea0a5965a33ef751970eab37d421b91ebbd01ac909039cffe159e574`.
Use Ubuntu-signed packages where their versions satisfy Bugzilla. Fetch DBD
MariaDB 1.24, Template Toolkit 3.106, and PatchReader 0.9.6 directly and verify
respective SHA-256 values
`f977a25b4116a0a95a7c8a894fd37097abe19af9a6a9ed4d800604ec17873fe4`,
`c7474050be80201f1fb55f0a569b9c0ab6c1c3f0cebbd7e601bda9b4046eec85`, and
`b8de37460347bb5474dc01916ccb31dd2fe0cd92242c4a32d730e8eb087c323c`
before any distribution's build machinery executes.

Keep orchestration in one Compose file. Generate a private local `.env` before
startup, bind the web port to `127.0.0.1`, and use separate named volumes for
MariaDB and Bugzilla's mutable data. A single lifecycle script owns readiness,
diagnostics, and confirmed destructive operations; a Makefile provides the
operator entry points.

## Consequences

- Bugzilla source, DBD MariaDB, Template Toolkit, PatchReader, and the two
  base-image identities are pinned, and both image indexes contain Linux amd64
  and arm64 manifests. Ubuntu package indexes remain a time-varying input, so
  this decision does not promise byte-for-byte image reproduction.
- Building Bugzilla installs its Perl and OS dependencies and is slower than
  pulling a prebuilt image.
- Rotating a pin is an explicit repository change that must re-run both-platform
  image verification and lifecycle tests.
- Reset removes the two project volumes but preserves generated credentials;
  cleanup additionally removes the local image and credentials and therefore
  requires a distinct confirmation.

## Considered & rejected

- **Commit the complete Bugzilla source tree.** judgment: vendoring thousands of
  upstream files would obscure this repository's lifecycle code and make source
  updates noisy without improving the immutable pin.
- **Use a third-party prebuilt Bugzilla image.** verified: the upstream 5.2 tree
  at commit `644c66f45ce0b1b2746a31a061fbd96886278225` contains Docker build files
  but no first-party published runtime image reference; an external image would
  add an unnecessary publisher trust boundary.
- **Track the moving `5.2` branch or floating image tags.** judgment: mutable
  inputs contradict the requested pinned substrate and make cross-machine
  failures difficult to reproduce.
- **Expose Bugzilla on all host interfaces as upstream does.** judgment: a local
  development database does not need LAN reachability; loopback is the safer
  default and can still be reached from the host browser.
