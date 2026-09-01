#!/usr/bin/env bash
# Operator-run live proof for issue #4: two provisioning runs against a fresh
# fixture — first all created, second all unchanged — plus bzr custom-field
# readback. Requires: make up already healthy (start from CONFIRM_RESET=1 make
# reset for a fresh fixture), and BZR_LIVE_BZR pointing at a bzr binary carrying
# the component default_assigned_to fix.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"  # uv resolves the project from cwd
BZR=${BZR_LIVE_BZR:?set BZR_LIVE_BZR to the bzr binary to validate with}
SCENARIO="$ROOT/tests/fixtures/provision-scenario"
STATE=$(mktemp -d "${TMPDIR:-/tmp}/bzr-live-provision-smoke.XXXXXX")
trap 'rm -rf "$STATE"' EXIT
chmod 700 "$STATE"

# The fixture's port lives in the checkout's .env; fall back to it when the shell
# does not export BZ_PORT, so a customized port still reaches every host-side call.
if [[ -z ${BZ_PORT:-} && -f "$ROOT/.env" ]]; then
  BZ_PORT=$(grep -E '^BZ_PORT=' "$ROOT/.env" | tail -1 | cut -d= -f2)
fi
BASE_URL="http://127.0.0.1:${BZ_PORT:-8080}/"

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
  field list cf_q4_risk)
grep -q 'low' <<<"$values" || {
  echo "smoke failed: cf_q4_risk legal values not observable through bzr" >&2
  echo "$values" >&2
  exit 1
}
# The text field's definition must also be observable through bzr (criterion 5):
# field list on a freetext field should succeed (its value list may be empty).
BZR_LIVE_API_KEY=$key "$BZR" --json \
  --server-url "$BASE_URL" \
  --server-api-key-env BZR_LIVE_API_KEY \
  field list cf_q4_notes >/dev/null || {
  echo "smoke failed: cf_q4_notes definition not observable through bzr" >&2
  exit 1
}

echo "provision smoke: OK"
