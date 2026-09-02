#!/usr/bin/env bash
# Operator-run live proof for issue #6: provisions and replays the replay fixture,
# then asserts the three facts the unit suite cannot reach (an honest create with no
# op_sys/rep_platform, the alias round-trip, and server-side alias uniqueness), and
# probes docs/bzr-findings.md's D1 against the running server. Requires: make up
# already healthy (start from CONFIRM_RESET=1 make reset for a fresh fixture --
# Bugzilla reads Task 0's checksetup answers only at install), and BZR_LIVE_BZR
# pointing at a bzr binary. This script never edits docs/bzr-findings.md; the
# operator reads its "findings probe:" lines and transcribes any promotion by hand.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"  # uv resolves the project from cwd
BZR=${BZR_LIVE_BZR:?set BZR_LIVE_BZR to the bzr binary to validate with}
SCENARIO="$ROOT/tests/fixtures/replay-scenario"
STATE=$(mktemp -d "${TMPDIR:-/tmp}/bzr-live-replay-smoke.XXXXXX")
trap 'rm -rf "$STATE"' EXIT
chmod 700 "$STATE"

# The fixture's port lives in the checkout's .env; fall back to it when the shell
# does not export BZ_PORT, so a customized port still reaches every host-side call.
if [[ -z ${BZ_PORT:-} && -f "$ROOT/.env" ]]; then
  BZ_PORT=$(grep -E '^BZ_PORT=' "$ROOT/.env" | tail -1 | cut -d= -f2)
fi
BASE_URL="http://127.0.0.1:${BZ_PORT:-8080}/"

echo "replay smoke: provisioning scenario resources"
uv run --python 3.11 python -m bzr_live.provision "$SCENARIO" \
  --state-root "$STATE/state" --bzr "$BZR" --project-root "$ROOT" \
  --base-url "$BASE_URL"

echo "replay smoke: replaying events (expect: the first bug.create to succeed with no"
echo "op_sys/rep_platform declared, proving Task 0's checksetup defaults)"
uv run --python 3.11 python -m bzr_live.replay replay "$SCENARIO" \
  --state-root "$STATE/state" --bzr "$BZR" --base-url "$BASE_URL"

# Read the expected server_alias, scenario name and create event name out of the
# loaded scenario rather than recomputing the digest or pasting a literal -- the
# fixture's digest has changed during this build and could again.
read -r ALIAS SCENARIO_NAME CREATE_EVENT <<<"$(uv run --python 3.11 python -c "
from bzr_live.scenario import load_scenario
s = load_scenario('$SCENARIO')
create = next(e for e in s.events if e.action == 'bug.create')
print(create.expected_postcondition['values']['server_alias'], s.name, create.name)
")"

JOURNAL_RECORD="$STATE/state/journal/$SCENARIO_NAME/$CREATE_EVENT.000001.json"
CREATE_BUG_ID=$(uv run --python 3.11 python -c "
import json
with open('$JOURNAL_RECORD') as f:
    record = json.load(f)
print(record['resolved_ids']['bug:checkout-race'])
")

TRIAGER_KEY=$(cat "$STATE/state/actor-keys/triager.key")
REPORTER_KEY=$(cat "$STATE/state/actor-keys/reporter.key")

# Each key must be paired with its own actor's login: Bugzilla validates the API key
# against the email, so an actor key sent with the admin address fails authentication
# outright (exit 9, "rest/valid_login did not confirm your credentials"). Read the
# addresses out of the scenario for the same reason the alias is read from it.
read -r TRIAGER_EMAIL REPORTER_EMAIL <<<"$(uv run --python 3.11 python -c "
from bzr_live.scenario import load_scenario
s = load_scenario('$SCENARIO')
actors = {r.name: r.data['email'] for r in s.resources if r.kind == 'actor'}
print(actors['triager'], actors['reporter'])
")"

echo "replay smoke: alias round-trip (finding D4)"
view=$(BZR_LIVE_API_KEY=$TRIAGER_KEY "$BZR" --json \
  --server-url "$BASE_URL" \
  --server-api-key-env BZR_LIVE_API_KEY \
  --server-email "$TRIAGER_EMAIL" \
  bug view -- "$ALIAS")
# bzr --json wraps its result in a schema envelope; unwrap "data" exactly as
# BzrClient._payload does (src/bzr_live/provision/adapters.py:76-77), including its
# tolerance of a reply that carries no envelope.
VIEWED_ID=$(printf '%s\n' "$view" | uv run --python 3.11 python -c \
  "import json, sys; d = json.load(sys.stdin); print(d.get('data', d)['id'])")
if [[ "$VIEWED_ID" != "$CREATE_BUG_ID" ]]; then
  echo "smoke failed: alias $ALIAS resolved to bug $VIEWED_ID, not the created bug $CREATE_BUG_ID" >&2
  echo "$view" >&2
  exit 1
fi
echo "replay smoke: alias $ALIAS round-trips to bug $CREATE_BUG_ID"

echo "replay smoke: alias uniqueness (finding D4)"
DUP_JSON="$STATE/duplicate-create.json"
cat > "$DUP_JSON" <<JSON
{"alias": "$ALIAS", "product": "checkout", "component": "cart",
 "summary": "Duplicate alias probe",
 "description": "Probing Bugzilla's alias uniqueness constraint.", "version": "v1"}
JSON
set +e
dup_out=$(BZR_LIVE_API_KEY=$REPORTER_KEY "$BZR" --json \
  --server-url "$BASE_URL" \
  --server-api-key-env BZR_LIVE_API_KEY \
  --server-email "$REPORTER_EMAIL" \
  bug create "--from-json=$DUP_JSON" 2>&1)
dup_status=$?
set -e
if [[ $dup_status -eq 0 ]]; then
  echo "smoke failed: a second bug.create declaring alias $ALIAS succeeded" >&2
  echo "$dup_out" >&2
  exit 1
fi
echo "replay smoke: a second create declaring alias $ALIAS failed as expected (exit $dup_status)"

echo "replay smoke: attachment summary TINYTEXT ceiling against the running server"
SUMMARY_256=$(printf 'x%.0s' {1..256})
set +e
attach_out=$(BZR_LIVE_API_KEY=$TRIAGER_KEY "$BZR" --json \
  --server-url "$BASE_URL" \
  --server-api-key-env BZR_LIVE_API_KEY \
  --server-email "$TRIAGER_EMAIL" \
  attachment upload "--summary=$SUMMARY_256" --content-type=text/plain \
  -- "$CREATE_BUG_ID" "$SCENARIO/assets/notes.txt" 2>&1)
attach_status=$?
set -e
echo "tinytext probe: 256-byte attachment summary exit $attach_status: $attach_out"

echo "replay smoke: findings probes (this script writes nothing to"
echo "docs/bzr-findings.md -- transcribe any promotion from *read* to *observed* by hand)"
echo "findings probe: not probed -- D3 G1 G2 G3 G4 G5 G6 G9 (source-read entries whose"
echo "  live probe syntax is unverified here; e.g. G2's --version would collide with"
echo "  bzr's own global --version flag rather than exercise bug update's)"
set +e
flag_out=$(BZR_LIVE_API_KEY=$TRIAGER_KEY "$BZR" --json \
  --server-url "$BASE_URL" \
  --server-api-key-env BZR_LIVE_API_KEY \
  --server-email "$TRIAGER_EMAIL" \
  bug update "--flag=needs-info?" -- "$CREATE_BUG_ID" 2>&1)
flag_status=$?
set -e
echo "findings probe: D1 hyphenated flag type (--flag=needs-info?) exit $flag_status: $flag_out"

echo "replay smoke: OK"
