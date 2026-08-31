#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/bzr-live-checkpoint-smoke.XXXXXX")
TEMP_ROOT=$(cd "$TEMP_ROOT" && pwd -P)
STORE="$TEMP_ROOT/store"
CORRUPT_STORE="$TEMP_ROOT/corrupt-store"
RUNNER_STATE="$TEMP_ROOT/runner"
RUNNER_MARKER="$RUNNER_STATE/checkpoint-smoke-marker"
BUGZILLA_MARKER=/var/www/html/data/checkpoint-smoke-marker

cleanup() {
  local status=$?
  rm -rf -- "$TEMP_ROOT"
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

fail() {
  printf 'checkpoint smoke failed: %s\n' "$1" >&2
  exit 1
}

cd "$ROOT"
mkdir "$STORE" "$CORRUPT_STORE" "$RUNNER_STATE"
project_hash=$(printf '%s' "$ROOT" | openssl dgst -sha256 -r)
project_hash=${project_hash%% *}
[[ $project_hash =~ ^[0-9a-fA-F]{64}$ ]] || fail 'checkout hash was malformed'
COMPOSE=(
  docker compose
  --project-name "bzr-live-${project_hash:0:12}"
  --project-directory "$ROOT"
  --file "$ROOT/compose.yaml"
)

db_client() {
  # These variables expand only inside the database container.
  # shellcheck disable=SC2016
  "${COMPOSE[@]}" exec -T db sh -eu -c '
    export MYSQL_PWD=$MARIADB_ROOT_PASSWORD
    exec mariadb --batch --skip-column-names --host=127.0.0.1 --user=root
  '
}

set_db_marker() {
  local marker=$1 sql
  case "$marker" in
    original|changed) ;;
    *) fail "unsupported MariaDB marker: $marker" ;;
  esac
  sql="
    CREATE DATABASE IF NOT EXISTS checkpoint_smoke;
    CREATE TABLE IF NOT EXISTS checkpoint_smoke.marker (
      id INTEGER PRIMARY KEY,
      value VARCHAR(16) NOT NULL
    );
    INSERT INTO checkpoint_smoke.marker (id, value) VALUES (1, '$marker')
      ON DUPLICATE KEY UPDATE value = VALUES(value);
  "
  db_client <<<"$sql"
}

set_bugzilla_marker() {
  local marker=$1
  # The marker arguments expand only inside the Bugzilla container.
  # shellcheck disable=SC2016
  "${COMPOSE[@]}" exec -T bugzilla sh -eu -c \
    'printf "%s\n" "$1" > "$2"' checkpoint-smoke "$marker" "$BUGZILLA_MARKER"
}

set_runner_marker() {
  printf '%s\n' "$1" >"$RUNNER_MARKER"
}

set_all_markers() {
  set_db_marker "$1"
  set_bugzilla_marker "$1"
  set_runner_marker "$1"
}

assert_original_markers() {
  local actual
  actual=$(db_client <<<'SELECT value FROM checkpoint_smoke.marker WHERE id = 1;')
  [[ $actual == original ]] || fail "MariaDB marker is '$actual', expected 'original'"
  # The marker path expands only inside the Bugzilla container.
  # shellcheck disable=SC2016
  "${COMPOSE[@]}" exec -T bugzilla sh -eu -c \
    'test "$(cat "$1")" = original' checkpoint-smoke "$BUGZILLA_MARKER" ||
    fail "Bugzilla volume marker is not 'original'"
  [[ -f $RUNNER_MARKER ]] || fail 'runner marker is missing'
  actual=$(<"$RUNNER_MARKER")
  [[ $actual == original ]] || fail "runner marker is '$actual', expected 'original'"
}

make up
make doctor
set_all_markers original

scripts/checkpoint save smoke --store "$STORE" --runner-state "$RUNNER_STATE"
make doctor

set_all_markers changed
scripts/checkpoint restore smoke --store "$STORE" --runner-state "$RUNNER_STATE"
assert_original_markers
make doctor

set_all_markers changed
scripts/checkpoint restore smoke --store "$STORE" --runner-state "$RUNNER_STATE"
assert_original_markers
make doctor

cp -R "$STORE/smoke" "$CORRUPT_STORE/smoke"
python3 - "$CORRUPT_STORE/smoke/mariadb-volume.tar" <<'PY'
from pathlib import Path
import sys

archive = Path(sys.argv[1])
with archive.open("r+b") as stream:
    stream.seek(0, 2)
    size = stream.tell()
    if size < 1:
        raise SystemExit("checkpoint smoke failed: MariaDB archive is empty")
    stream.seek(size // 2)
    byte = stream.read(1)
    stream.seek(-1, 1)
    stream.write(bytes((byte[0] ^ 1,)))
PY

if corruption_output=$(scripts/checkpoint restore smoke \
  --store "$CORRUPT_STORE" --runner-state "$RUNNER_STATE" 2>&1); then
  fail 'corrupt checkpoint restore unexpectedly succeeded'
fi
[[ $corruption_output == *'mariadb-volume.tar checksum does not match manifest'* ]] || {
  printf '%s\n' "$corruption_output" >&2
  fail 'corrupt checkpoint was not rejected by checksum validation'
}
actual=$(db_client <<<'SELECT value FROM checkpoint_smoke.marker WHERE id = 1;')
[[ $actual == original ]] ||
  fail "corrupt restore changed MariaDB marker to '$actual' before rejection"
make doctor

printf 'Checkpoint smoke passed: save, repeated restore, corruption preflight, and health.\n'
