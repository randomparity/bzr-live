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
#
# Runs on bash 3.2 -- macOS /bin/bash, the `bash` the Makefile's smoke recipes resolve to
# on the development host. So: no bash 4+ syntax here, and no version precondition either.
# That bash also discards a fatal `set -u` error's status before the EXIT trap runs, which
# is why cleanup carries a completion sentinel rather than only the status it was handed
# (issue #29).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"  # uv resolves the project from cwd
BZR=${BZR_LIVE_BZR:?set BZR_LIVE_BZR to the bzr binary to validate with}
SCENARIO="$ROOT/scenarios/smoke"
STATE=$(mktemp -d "${TMPDIR:-/tmp}/bzr-live-smoke-scenario.XXXXXX")
# Cleanup removes the state root on every path, so the actor keys provisioning mints never
# outlive the run. The sentinel is what makes that safe on bash 3.2: measured under
# `set -euo pipefail`, a fatal expansion error (an unbound variable under `set -u`) exits 1
# with NO trap and 0 with ANY trap there, including a status-preserving
# `cleanup(){ local s=$?; ...; exit "$s"; }`, because $? is already 0 at handler entry. So
# cleanup does not try to recover that status -- it derives one from an independent fact,
# whether control ever reached COMPLETED=1 (ADR 0010 decision 8, issue #29).
COMPLETED=0
cleanup() {
  local status=$?
  rm -rf -- "$STATE"
  if [ "$status" -eq 0 ] && [ "$COMPLETED" -ne 1 ]; then
    echo "smoke scenario: exited before finishing; see the error above" >&2
    status=1
  fi
  exit "$status"
}
trap cleanup EXIT
# Canonicalize before anything uses it: scripts/checkpoint requires every path argument to
# equal its own resolve() (src/bzr_live/checkpoint.py:155-167), and on macOS TMPDIR sits
# under the /var -> /private/var symlink, so the mktemp spelling is refused with
# "paths: store must be canonical". tests/checkpoint_smoke.sh:5-6 does the same.
STATE=$(cd "$STATE" && pwd -P)
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

# --- checkpoint round trip over the verified fixture --------------------------------
# Issue #25 asks the live path to prove that a saved checkpoint restores the state the
# scenario declares. These stages run in the replay's own state root because `verify` and
# `resume` read the journal and actor keys it holds. The store is a sibling of that root:
# scripts/checkpoint refuses a store containing the runner state or the reverse
# (src/bzr_live/checkpoint.py:334-335).
STORE="$STATE/store"
# 0700 explicitly, not at the umask: the store holds a checkpoint archive of the fixture
# database, api-key rows included, and scripts/checkpoint requires only owner rwx
# (checkpoint.py:331), so it would accept whatever the invoking shell's umask produced.
mkdir -m 700 "$STORE"
echo "smoke scenario: saving checkpoint 'smoke' over the verified fixture"
scripts/checkpoint save smoke --store "$STORE" --runner-state "$STATE/state"

# The mutation target is read from the scenario rather than pasted, for the same reason the
# counts above are: the first bug.create's SERVER alias, the actor that files it, and that
# actor's address. The server alias is not the declared one -- loader.py:711-716 drops
# `alias` from the postcondition and substitutes `bzr-live-<hash>`, which is what the bug
# actually carries; `verify` still reports its findings under the declared name. Bugzilla
# validates an API key against its own address, so key and address must belong to one actor
# (tests/replay_smoke.sh:70-73). Assign first and split second -- `read ... <<<"$(...)"`
# would take its status from `read`, and a scenario that failed to load would leave three
# empty values behind.
PROBE=$(uv run --python 3.11 python -c '
import sys
from bzr_live.scenario import load_scenario
s = load_scenario(sys.argv[1])
create = next(e for e in s.events if e.action == "bug.create")
emails = {r.name: r.data["email"] for r in s.resources if r.kind == "actor"}
print(create.expected_postcondition["values"]["server_alias"], create.actor.name,
      emails[create.actor.name])
' "$SCENARIO")
read -r PROBE_ALIAS PROBE_ACTOR PROBE_EMAIL <<<"$PROBE"
PROBE_KEY=$(cat "$STATE/state/actor-keys/$PROBE_ACTOR.key")

# `bug view` reads an alias but `bug update` cannot: at the pinned revision UpdateArgs
# declares `pub ids: Vec<u64>` (src/cli/bug/update.rs:79) where ViewArgs declares
# `Vec<String>` and documents "Bug ID(s) or alias(es)" (view.rs:60-62). That asymmetry is
# finding G10 in docs/bzr-findings.md; the id is resolved here rather than worked around.
# `bzr --json` wraps its result in a schema envelope; unwrap "data" exactly as
# BzrClient._payload does (src/bzr_live/provision/adapters.py:76-77), including its
# tolerance of a reply that carries no envelope.
probe_view() {
  BZR_LIVE_API_KEY=$PROBE_KEY "$BZR" --json \
    --server-url "$BASE_URL" \
    --server-api-key-env BZR_LIVE_API_KEY \
    --server-email "$PROBE_EMAIL" \
    bug view -- "$PROBE_ALIAS"
}
probe_field() {
  uv run --python 3.11 python -c \
    "import json, sys; d = json.load(sys.stdin); print(d.get('data', d)[sys.argv[1]])" "$1"
}
PROBE_ID=$(probe_view | probe_field id)

# A summary the scenario does not declare, written only after the scenario's own
# verification has passed and reverted by the restore below. `summary` is the field because
# every declared summary is one of the scalars compared against `bug view`
# (src/bzr_live/verify/checks.py:70-73); ADR 0010 decision 6 records why, and which
# alternatives verify would not have caught.
PROBE_SUMMARY="checkpoint probe: not the summary $PROBE_ALIAS declares"
echo "smoke scenario: mutating $PROBE_ALIAS (bug $PROBE_ID) as $PROBE_ACTOR"
BZR_LIVE_API_KEY=$PROBE_KEY "$BZR" --json \
  --server-url "$BASE_URL" \
  --server-api-key-env BZR_LIVE_API_KEY \
  --server-email "$PROBE_EMAIL" \
  bug update "--summary=$PROBE_SUMMARY" -- "$PROBE_ID"

# Read it back before restoring. Without this, a mutation that never reached the server and
# a restore that reverted it are the same observation.
OBSERVED_SUMMARY=$(probe_view | probe_field summary)
if [[ "$OBSERVED_SUMMARY" != "$PROBE_SUMMARY" ]]; then
  echo "smoke scenario: the mutation did not reach $PROBE_ALIAS; observed" \
    "'$OBSERVED_SUMMARY'" >&2
  exit 1
fi

echo "smoke scenario: restoring checkpoint 'smoke'"
scripts/checkpoint restore smoke --store "$STORE" --runner-state "$STATE/state"

RESTORE_START=$(date +%s%N)
echo "smoke scenario: re-verifying the restored fixture"
uv run --python 3.11 python -m bzr_live.replay verify "$SCENARIO" \
  --state-root "$STATE/state" --bzr "$BZR" --base-url "$BASE_URL"
RESTORE_ELAPSED=$(elapsed_since "$RESTORE_START")
echo "smoke scenario: re-verified the restored fixture in ${RESTORE_ELAPSED}s"

# Every event's journal record is complete, so resume adopts each one's resolved ids and
# reports it already complete without sending a mutation
# (src/bzr_live/replay/engine.py:145-148). It asserts that the restored journal and the
# restored fixture still describe one run.
echo "smoke scenario: resuming the restored journal"
uv run --python 3.11 python -m bzr_live.replay resume "$SCENARIO" \
  --state-root "$STATE/state" --bzr "$BZR" --base-url "$BASE_URL"

COMPLETED=1
echo "smoke scenario: OK"
