#!/usr/bin/env bash
# Operator-run live proof for issue #4: two provisioning runs against a fresh
# fixture — first all created, second all unchanged — plus bzr custom-field
# readback. Requires: make up already healthy (start from CONFIRM_RESET=1 make
# reset for a fresh fixture), and BZR_LIVE_BZR pointing at a bzr binary carrying
# the component default_assigned_to fix.
#
# Runs on bash 3.2 -- macOS /bin/bash, the `bash` the Makefile's smoke recipes
# resolve to on the development host. So: no bash 4+ syntax here, and no version
# precondition either. That bash also discards a fatal `set -u` error's status
# before the EXIT trap runs, which is why cleanup carries a completion sentinel
# rather than only the status it was handed (issue #29).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"  # uv resolves the project from cwd
BZR=${BZR_LIVE_BZR:?set BZR_LIVE_BZR to the bzr binary to validate with}
SCENARIO="$ROOT/tests/fixtures/provision-scenario"
STATE=$(mktemp -d "${TMPDIR:-/tmp}/bzr-live-provision-smoke.XXXXXX")
COMPLETED=0
cleanup() {
  local status=$?
  rm -rf -- "$STATE"
  if [ "$status" -eq 0 ] && [ "$COMPLETED" -ne 1 ]; then
    echo "smoke failed: provision smoke exited before finishing; see the error above" >&2
    status=1
  fi
  exit "$status"
}
trap cleanup EXIT
chmod 700 "$STATE"

# The fixture's port lives in the checkout's .env; fall back to it when the shell
# does not export BZ_PORT, so a customized port still reaches every host-side call.
if [[ -z ${BZ_PORT:-} && -f "$ROOT/.env" ]]; then
  BZ_PORT=$(grep -E '^BZ_PORT=' "$ROOT/.env" | tail -1 | cut -d= -f2)
fi
BASE_URL="http://127.0.0.1:${BZ_PORT:-8080}/"
if [[ -z ${BZ_ADMIN_EMAIL:-} && -f "$ROOT/.env" ]]; then
  BZ_ADMIN_EMAIL=$(grep -E '^BZ_ADMIN_EMAIL=' "$ROOT/.env" | tail -1 | cut -d= -f2)
fi
ADMIN_EMAIL=${BZ_ADMIN_EMAIL:-admin@bugzilla.test}

run() {
  uv run --python 3.11 python -m bzr_live.provision "$SCENARIO" \
    --state-root "$STATE/state" --bzr "$BZR" --project-root "$ROOT" \
    --base-url "$BASE_URL"
}

echo "provision smoke: bridge syntax check inside the image"
PROJECT="bzr-live-$(printf '%s' "$ROOT" | openssl dgst -sha256 -r | cut -c1-12)"
docker compose --project-name "$PROJECT" --project-directory "$ROOT" \
  --file "$ROOT/compose.yaml" exec -T bugzilla \
  perl -c /usr/local/bin/bzr-live-bridge

echo "provision smoke: first run (expect all created)"
first=$(run)
echo "$first"
if printf '%s\n' "$first" | grep -q '^unchanged '; then
  echo "smoke failed: first run reported unchanged resources" >&2
  exit 1
fi

echo "provision smoke: second run (expect all unchanged)"
second=$(run)
echo "$second"
if printf '%s\n' "$second" | grep -q '^created '; then
  echo "smoke failed: second run created resources" >&2
  exit 1
fi

echo "provision smoke: custom-field readback through bzr"
key=$(cat "$STATE/state/admin.key")
values=$(BZR_LIVE_API_KEY=$key "$BZR" --json \
  --server-url "$BASE_URL" \
  --server-api-key-env BZR_LIVE_API_KEY \
  --server-email "$ADMIN_EMAIL" \
  field list cf_q4_risk)
grep -q 'low' <<<"$values" || {
  echo "smoke failed: cf_q4_risk legal values not observable through bzr" >&2
  echo "$values" >&2
  exit 1
}
# The text field's definition must also reach bzr (criterion 5). Live boundary:
# Bugzilla omits `values` for freetext fields and bzr 0.8.3-dev's field model
# requires it, so field list exits 8 (deserialize) while the error body itself
# carries the server's definition — proof the definition is exposed to bzr's
# transport. Accept success, or exactly that limitation: exit 8 (bzr's
# deserialize code; usage/API/network/auth errors exit 2/4/5/9) naming our
# field. Anything else fails the criterion.
notes_out=$(BZR_LIVE_API_KEY=$key "$BZR" --json \
  --server-url "$BASE_URL" \
  --server-api-key-env BZR_LIVE_API_KEY \
  --server-email "$ADMIN_EMAIL" \
  field list cf_q4_notes 2>&1) && notes_ok=0 || notes_ok=$?
if [[ $notes_ok -ne 0 ]] \
    && { [[ $notes_ok -ne 8 ]] || ! grep -q 'cf_q4_notes' <<<"$notes_out"; }; then
  echo "smoke failed: cf_q4_notes definition not observable through bzr" >&2
  echo "$notes_out" >&2
  exit 1
fi

COMPLETED=1
echo "provision smoke: OK"
