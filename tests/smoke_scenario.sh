#!/usr/bin/env bash
# Operator-run live proof for issue #19: provisions and replays scenarios/smoke/ --
# 20 bugs, 48 events, two products -- against the running fixture using the selected
# bzr binary, and reports the observed replay duration. It proves that provisioning,
# actor switching, symbolic references and every supported action compose on a real
# Bugzilla; it asserts nothing about semantic invariants, which is issue #20's job.
#
# Requires: a healthy `make up` (start from CONFIRM_RESET=1 make reset for a fresh
# fixture -- Bugzilla reads containers/bugzilla/checksetup_answers.txt only at install,
# and a pre-existing fixture may already hold conflicting resource definitions), and
# BZR_LIVE_BZR pointing at a bzr binary. This script never edits docs/bzr-findings.md;
# the operator reads its output and transcribes any finding by hand.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"  # uv resolves the project from cwd
BZR=${BZR_LIVE_BZR:?set BZR_LIVE_BZR to the bzr binary to validate with}
SCENARIO="$ROOT/scenarios/smoke"
STATE=$(mktemp -d "${TMPDIR:-/tmp}/bzr-live-smoke-scenario.XXXXXX")
trap 'rm -rf "$STATE"' EXIT
chmod 700 "$STATE"

# The fixture's port lives in the checkout's .env; fall back to it when the shell does
# not export BZ_PORT, so a customized port still reaches every host-side call.
# `|| true` is load-bearing: under `set -euo pipefail` a .env carrying no BZ_PORT= line
# makes grep exit 1, pipefail propagates it, and the assignment kills the script before
# the ${BZ_PORT:-8080} default below is ever reached -- turning a documented fallback
# into a bare non-zero exit. `-f2-` keeps a value containing '='; the numeric guard then
# rejects anything that would build a malformed URL.
if [[ -z ${BZ_PORT:-} && -f "$ROOT/.env" ]]; then
  BZ_PORT=$(grep -E '^BZ_PORT=' "$ROOT/.env" | tail -1 | cut -d= -f2- || true)
  [[ ${BZ_PORT:-} =~ ^[0-9]+$ ]] || BZ_PORT=
fi
BASE_URL="http://127.0.0.1:${BZ_PORT:-8080}/"

# Every refusal the scenario is written against is pinned to a bzr revision
# (src/bzr_live/replay/actions.py:15-16). BZR_LIVE_BZR is operator-selected and may be a
# different one, so a run whose output does not name the revision cannot tell a live
# finding from a stale one. This is the first line for that reason.
echo "smoke scenario: bzr under test: $("$BZR" --version)"

# Read the counts from the loaded scenario rather than hardcoding them, so no reported
# figure can drift from the fixture. The scenario path goes in as argv rather than being
# interpolated into the Python source, so a path containing a quote cannot alter it.
read -r EVENT_COUNT RESOURCE_COUNT ACTOR_COUNT <<<"$(uv run --python 3.11 python -c '
import sys
from bzr_live.scenario import load_scenario
s = load_scenario(sys.argv[1])
print(len(s.events), len(s.resources),
      sum(1 for r in s.resources if r.kind == "actor"))
' "$SCENARIO")"

echo "smoke scenario: provisioning $RESOURCE_COUNT resources ($ACTOR_COUNT actors)"
uv run --python 3.11 python -m bzr_live.provision "$SCENARIO" \
  --state-root "$STATE/state" --bzr "$BZR" --project-root "$ROOT" \
  --base-url "$BASE_URL"

# Time the replay alone. bash's SECONDS counts from shell start and has one-second
# granularity, so it would fold provisioning into a figure labelled "replayed" and can
# report 0s as though that were a measurement. `date +%s%N` is available on both targets
# (BSD date on Darwin, GNU date on the CI runner) but is not a documented prerequisite,
# so fall back to whole seconds when it does not yield digits.
REPLAY_START=$(date +%s%N)
echo "smoke scenario: replaying $EVENT_COUNT events as $ACTOR_COUNT actors"
uv run --python 3.11 python -m bzr_live.replay replay "$SCENARIO" \
  --state-root "$STATE/state" --bzr "$BZR" --base-url "$BASE_URL"
REPLAY_END=$(date +%s%N)

if [[ $REPLAY_START =~ ^[0-9]+$ && $REPLAY_END =~ ^[0-9]+$ ]]; then
  ELAPSED_NS=$((REPLAY_END - REPLAY_START))
  ELAPSED=$(printf '%d.%02d' \
    $((ELAPSED_NS / 1000000000)) $((ELAPSED_NS % 1000000000 / 10000000)))
else
  ELAPSED="$(($(date +%s) - ${REPLAY_START%%[!0-9]*})) (whole seconds; date lacks %N)"
fi
echo "smoke scenario: replayed $EVENT_COUNT events in ${ELAPSED}s (replay only,"
echo "  excluding provisioning and make up)"

echo "smoke scenario: OK"
