#!/usr/bin/env bash
# Operator-run live proof for issues #19 and #20: provisions and replays scenarios/smoke/
# -- 20 bugs, 47 events, two products -- against the running fixture using the selected
# bzr binary, then verifies the replayed state against the scenario, reporting both
# durations. It proves that provisioning, actor switching, symbolic references and every
# supported action compose on a real Bugzilla, and that the state they leave behind is the
# one the scenario declares.
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
# into a bare non-zero exit. `-f2-` keeps a value containing '='.
if [[ -z ${BZ_PORT:-} && -f "$ROOT/.env" ]]; then
  BZ_PORT=$(grep -E '^BZ_PORT=' "$ROOT/.env" | tail -1 | cut -d= -f2- || true)
fi
# Guard both sources, not just .env: an exported BZ_PORT reaches the same string. An empty
# value means "not set" and takes the default below; anything else must be digits, because
# BASE_URL is passed to provision and replay as --base-url and every actor API key is sent
# there. 'BZ_PORT=8080@example.invalid' would render http://127.0.0.1:8080@example.invalid/,
# whose real host is the trailing authority rather than the loopback address it appears to
# name. Refuse rather than substitute a default, so a typo is not silently a different run.
if [[ -n ${BZ_PORT:-} && ! ${BZ_PORT} =~ ^[0-9]+$ ]]; then
  echo "smoke scenario: BZ_PORT must be digits, got '${BZ_PORT}'" >&2
  exit 1
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
# Assign first and split second: `read ... <<<"$(...)"` takes its status from `read`, and
# an empty substitution still supplies one newline for it to consume successfully, so a
# scenario that fails to load would set three empty counts and print
# "provisioning  resources ( actors)" before continuing. A plain assignment propagates the
# substitution's status, so `set -e` stops here with the loader's own
# `<file>:<line>:<json-path>: <message>` as the last thing on the terminal.
COUNTS=$(uv run --python 3.11 python -c '
import sys
from bzr_live.scenario import load_scenario
s = load_scenario(sys.argv[1])
print(len(s.events), len(s.resources),
      sum(1 for r in s.resources if r.kind == "actor"))
' "$SCENARIO")
read -r EVENT_COUNT RESOURCE_COUNT ACTOR_COUNT <<<"$COUNTS"

echo "smoke scenario: provisioning $RESOURCE_COUNT resources ($ACTOR_COUNT actors)"
uv run --python 3.11 python -m bzr_live.provision "$SCENARIO" \
  --state-root "$STATE/state" --bzr "$BZR" --project-root "$ROOT" \
  --base-url "$BASE_URL"

# Wall time since a `date +%s%N` mark. bash's SECONDS counts from shell start and has
# one-second granularity, so it would fold provisioning into a figure labelled "replayed"
# and can report 0s as though that were a measurement. `date +%s%N` is available on both
# targets (BSD date on Darwin, GNU date on the CI runner) but is not a documented
# prerequisite, so fall back to whole seconds when it does not yield digits.
elapsed_since() {
  local start=$1 end ns
  end=$(date +%s%N)
  if [[ $start =~ ^[0-9]+$ && $end =~ ^[0-9]+$ ]]; then
    ns=$((end - start))
    printf '%d.%02d' $((ns / 1000000000)) $((ns % 1000000000 / 10000000))
  else
    printf '%d (whole seconds; date lacks %%N)' \
      "$(($(date +%s) - ${start%%[!0-9]*}))"
  fi
}

REPLAY_START=$(date +%s%N)
echo "smoke scenario: replaying $EVENT_COUNT events as $ACTOR_COUNT actors"
uv run --python 3.11 python -m bzr_live.replay replay "$SCENARIO" \
  --state-root "$STATE/state" --bzr "$BZR" --base-url "$BASE_URL"
ELAPSED=$(elapsed_since "$REPLAY_START")
echo "smoke scenario: replayed $EVENT_COUNT events in ${ELAPSED}s (replay only,"
echo "  excluding provisioning and make up)"

# The verifier runs in the same state root as the replay: it reads the journal that run
# wrote for every server identity, and the actor API keys provisioning minted. No pipe and
# no `|| true` -- a divergence must fail the script under `set -e`, which is the point of
# the stage.
VERIFY_START=$(date +%s%N)
echo "smoke scenario: verifying the replayed scenario"
uv run --python 3.11 python -m bzr_live.replay verify "$SCENARIO" \
  --state-root "$STATE/state" --bzr "$BZR" --base-url "$BASE_URL"
VERIFY_ELAPSED=$(elapsed_since "$VERIFY_START")
echo "smoke scenario: verified in ${VERIFY_ELAPSED}s"

echo "smoke scenario: OK"
