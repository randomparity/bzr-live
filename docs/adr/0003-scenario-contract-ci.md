# ADR 0003: Dedicated scenario-contract CI workflow

## Status

Accepted

## Context

The versioned scenario contract is complete and locally proven by 42 tests, a package build,
and an installed-wheel smoke. PR #10 nevertheless has no commit-bound Actions result because
the repository's only active workflow is path-filtered to the separate container-lifecycle
surface. Merge-gate part 2 requires a non-empty successful run set for the exact delivered
commit without changing the product contract or issue #2's workflow.

The CI job executes pull-request-controlled repository code and third-party action code. Its
trigger, permissions, action identities, and tool version are therefore part of the decision,
not incidental workflow syntax.

## Decision

Add `.github/workflows/scenario-contract.yml` as a dedicated path-filtered workflow for pull
requests and pushes to `main`. It triggers for its own file, Python package and test paths,
package metadata, and the issue #3 ADR/spec/plan paths. A single Ubuntu 24.04 job explicitly
checks out `github.event.pull_request.head.sha` for a pull request or `github.sha` for a push,
then asserts that `git rev-parse HEAD` equals that selected event SHA. It installs uv 0.12.7,
runs the existing Python 3.11 unittest discovery command, builds the package, and executes the
existing smoke against the exact built wheel.

Grant only `contents: read`. Use `pull_request`, never `pull_request_target`; configure and pass
no long-lived repository or environment secrets. GitHub's automatic `GITHUB_TOKEN` remains
available to every action and step with the declared read-only permission; checkout may use it
transiently but must not persist it. Disable setup-uv caching. Pin
`actions/checkout` v7.0.1 to commit
`3d3c42e5aac5ba805825da76410c181273ba90b1` and `astral-sh/setup-uv` v10.0.1
to commit `20cfd1bf945f4377ade1205e4dbc17946fc9a30d`. Install uv 0.12.7 with
the release asset's published x86_64 Linux SHA-256
`788f18abea7c5f55d6216e4f5613fd89d4d59b631efeec117b2b07fe72f1da21`.
GitHub's release API identified the action versions as current stable releases on 2026-08-29,
resolved each tag directly to the stated commit, exposed the uv asset digest, and supplied each
immutable action manifest for inspection before adoption.

## Consequences

- Every relevant pull request and main push gets a commit-bound result covering the package
  test, build, and installed-wheel boundaries. Ubuntu discovers all 42 tests but skips the
  three Darwin-only ACL cases; local Darwin validation remains their proof.
- Product behavior, the lifecycle workflow, and issue #2 files remain unchanged.
- Pull-request-controlled code runs with an automatic credential. Least privilege, no configured
  secrets, and no persisted checkout credentials bound its exposure; they do not make the job
  credential-free.
- Path filtering deliberately avoids spending scenario-contract CI on unrelated changes; a
  future scenario-contract path must be added here when it becomes part of this boundary.
- The workflow depends on GitHub-hosted Ubuntu runners and two immutable third-party action
  commits. Upgrading either action or uv requires a reviewed pin and, for uv, the matching
  published archive checksum.

## Considered & rejected

- **Do nothing and rely on the lifecycle workflow.** verified: `gh run list --commit
  3fbbe7efe3ab13d90dd31551016de2dfbdc16c21` returned no runs in three bounded reads, and
  `gh workflow view 345442114 --yaml` showed filters limited to the issue #2 lifecycle surface.
- **Extend the lifecycle workflow.** judgment: this would couple fast package validation to an
  unrelated 45-minute container lifecycle job and modify a surface explicitly owned by issue #2.
- **Run on every repository change.** judgment: path filtering gives the required commit-bound
  signal without spending runner time on unrelated documentation or lifecycle-only changes.
- **Install uv with an unpinned shell command or mutable action tag.** verified: setup-uv
  v10.0.1 exposes pinned `version`, `python-version`, and cache controls in `action.yml` at
  commit `20cfd1bf945f4377ade1205e4dbc17946fc9a30d`; adopting that immutable source is smaller
  and more reviewable than maintaining an installer pipeline.
