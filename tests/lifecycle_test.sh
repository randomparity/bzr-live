#!/usr/bin/env bash
# Shell lifecycle contract tests, run by `make test`.
#
# Runs on bash 3.2 -- macOS /bin/bash, which is what the Makefile's plain
# `bash tests/lifecycle_test.sh` resolves to on the development host. So: no bash 4+
# syntax here. That bash also discards a fatal `set -u` error's status before the EXIT
# trap runs, which is why cleanup carries a completion sentinel rather than only the
# status it was handed (issue #32, applying the pattern issue #29 measured).
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
LIFECYCLE="$REPO_ROOT/scripts/lifecycle"
REAL_OPENSSL=$(command -v openssl)
TEST_TMP=$(mktemp -d "${TMPDIR:-/tmp}/bzr-live-test.XXXXXX")
COMPLETED=0
cleanup() {
  local status=$?
  rm -rf -- "$TEST_TMP"
  if [ "$status" -eq 0 ] && [ "$COMPLETED" -ne 1 ]; then
    printf 'FAIL: lifecycle test exited before finishing; see the error above\n' >&2
    status=1
  fi
  exit "$status"
}
trap cleanup EXIT

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_contains() {
  local file=$1 expected=$2
  grep -F -- "$expected" "$file" >/dev/null ||
    fail "expected $file to contain: $expected"
}

assert_not_contains() {
  local file=$1 unexpected=$2
  if grep -F -- "$unexpected" "$file" >/dev/null; then
    fail "expected $file not to contain: $unexpected"
  fi
}

assert_mode_600() {
  local file=$1 mode
  mode=$(stat -c '%a' "$file" 2>/dev/null || stat -f '%Lp' "$file")
  [[ $mode == 600 ]] || fail "expected mode 600 for $file, got $mode"
}

make_stubs() {
  local bin=$1
  mkdir -p "$bin"
  cat >"$bin/docker" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$STUB_LOG"
if [[ -n ${STUB_HOLD_READY_FILE:-} && " $* " == *" --project-name "* ]]; then
  printf 'ready\n' >"$STUB_HOLD_READY_FILE"
  while [[ ! -e $STUB_HOLD_RELEASE_FILE ]]; do sleep 0.01; done
fi
if [[ ${1:-} == compose && ${2:-} == version ]]; then
  printf '2.40.0\n'
  exit 0
fi
if [[ ${1:-} == compose && ${2:-} == up && ${3:-} == --help ]]; then
  printf '%s\n' '      --wait' '      --wait-timeout int'
  exit 0
fi
if [[ ${1:-} == volume && ${2:-} == ls ]]; then
  if [[ ${STUB_VOLUME_INSPECT_ERROR:-0} == 1 ]]; then
    printf 'daemon unavailable\n' >&2
    exit 2
  fi
  volume_name=${*: -1}
  volume_name=${volume_name#name=^}
  [[ ${STUB_EXISTING_VOLUME:-0} == 1 ]] && printf '%s\n' "${volume_name%\$}"
  exit 0
fi
if [[ " $* " == *" port bugzilla 80 "* ]]; then
  printf '127.0.0.1:8080\n'
fi
if [[ -n ${STUB_FAIL_MATCH:-} && " $* " == *" $STUB_FAIL_MATCH "* ]]; then
  exit 23
fi
exit 0
STUB
  cat >"$bin/openssl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
if [[ ${1:-} == rand ]]; then
  count=0
  [[ -f $STUB_OPENSSL_COUNT ]] && read -r count <"$STUB_OPENSSL_COUNT"
  count=$((count + 1))
  printf '%s\n' "$count" >"$STUB_OPENSSL_COUNT"
  if [[ ${STUB_OPENSSL_FAIL_AT:-0} == "$count" ]]; then
    exit 19
  fi
  printf '%048x\n' "$count"
  exit 0
fi
exec "$REAL_OPENSSL" "$@"
STUB
  cat >"$bin/curl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$STUB_LOG"
[[ ${STUB_CURL_FAIL:-0} != 1 ]]
STUB
  chmod +x "$bin/docker" "$bin/openssl" "$bin/curl"
}

new_root() {
  local name=$1 root
  root="$TEST_TMP/$name"
  mkdir -p "$root"
  cp "$REPO_ROOT/compose.yaml" "$root/compose.yaml"
  printf '%s\n' "$root"
}

run_lifecycle() {
  local root=$1 command=$2 output=$3
  shift 3
  env \
    PATH="$STUB_BIN:$PATH" \
    REAL_OPENSSL="$REAL_OPENSSL" \
    STUB_LOG="$STUB_LOG" \
    STUB_OPENSSL_COUNT="$STUB_OPENSSL_COUNT" \
    BZ_LIVE_ROOT="$root" \
    "$@" \
    "$LIFECYCLE" "$command" >"$output" 2>&1
}

[[ -x $LIFECYCLE ]] || fail "scripts/lifecycle is missing or not executable"
[[ -f $REPO_ROOT/compose.yaml ]] || fail "compose.yaml is missing"

STUB_BIN="$TEST_TMP/bin"
STUB_LOG="$TEST_TMP/docker.log"
STUB_OPENSSL_COUNT="$TEST_TMP/openssl.count"
: >"$STUB_LOG"
make_stubs "$STUB_BIN"

root=$(new_root init)
out="$TEST_TMP/init.out"
run_lifecycle "$root" init "$out"
[[ -f $root/.env ]] || fail 'init did not create .env'
assert_mode_600 "$root/.env"
for key in BZ_ADMIN_PASSWORD BZ_DB_PASSWORD MARIADB_ROOT_PASSWORD; do
  value=$(sed -n "s/^${key}=//p" "$root/.env")
  [[ $value =~ ^[0-9a-f]{48}$ ]] || fail "$key is not 48 lowercase hex characters"
  assert_not_contains "$out" "$value"
done
[[ $(sed -n 's/^BZ_ALLOW_UNSAFE_UTF8_CONVERSION=//p' "$root/.env") == 0 ]] ||
  fail 'unsafe UTF-8 conversion must default to 0'
before=$(shasum -a 256 "$root/.env")
run_lifecycle "$root" init "$out"
after=$(shasum -a 256 "$root/.env")
[[ $before == "$after" ]] || fail 'init overwrote existing .env'

root=$(new_root retained-volume)
if run_lifecycle "$root" init "$TEST_TMP/retained.out" STUB_EXISTING_VOLUME=1; then
  fail 'init accepted an existing data volume without .env'
fi
assert_contains "$TEST_TMP/retained.out" 'init failed'
assert_contains "$TEST_TMP/retained.out" 'restore .env or run confirmed cleanup'

root=$(new_root volume-inspection-failure)
if run_lifecycle "$root" init "$TEST_TMP/volume-inspection-failure.out" STUB_VOLUME_INSPECT_ERROR=1; then
  fail 'init generated credentials after volume inspection failed'
fi
[[ ! -e $root/.env ]] || fail 'init left .env after volume inspection failed'
assert_contains "$TEST_TMP/volume-inspection-failure.out" 'cannot inspect Docker volumes'

rm -f "$STUB_OPENSSL_COUNT"
root=$(new_root failed-secret)
if run_lifecycle "$root" init "$TEST_TMP/secret-fail.out" STUB_OPENSSL_FAIL_AT=2; then
  fail 'init succeeded after secret generation failed'
fi
if compgen -G "$root/.env.tmp.*" >/dev/null; then
  fail 'secret temporary file survived failed generation'
fi
assert_contains "$TEST_TMP/secret-fail.out" 'init failed'

root=$(new_root down)
printf 'BZ_ADMIN_PASSWORD=a\nBZ_DB_PASSWORD=b\nMARIADB_ROOT_PASSWORD=c\n' >"$root/.env"
: >"$STUB_LOG"
run_lifecycle "$root" down "$TEST_TMP/down.out"
assert_contains "$STUB_LOG" 'down --remove-orphans'
assert_not_contains "$STUB_LOG" '--volumes'

root=$(new_root reset)
printf 'BZ_ADMIN_PASSWORD=a\nBZ_DB_PASSWORD=b\nMARIADB_ROOT_PASSWORD=c\n' >"$root/.env"
if run_lifecycle "$root" reset "$TEST_TMP/reset-refuse.out"; then
  fail 'reset succeeded without CONFIRM_RESET=1'
fi
assert_contains "$TEST_TMP/reset-refuse.out" 'CONFIRM_RESET=1'
: >"$STUB_LOG"
run_lifecycle "$root" reset "$TEST_TMP/reset.out" CONFIRM_RESET=1
assert_contains "$STUB_LOG" 'down --volumes --remove-orphans'
[[ -f $root/.env ]] || fail 'reset removed .env'

root=$(new_root clean)
printf 'BZ_ADMIN_PASSWORD=a\nBZ_DB_PASSWORD=b\nMARIADB_ROOT_PASSWORD=c\n' >"$root/.env"
if run_lifecycle "$root" clean "$TEST_TMP/clean-refuse.out"; then
  fail 'clean succeeded without CONFIRM_CLEAN=1'
fi
: >"$STUB_LOG"
if run_lifecycle "$root" clean "$TEST_TMP/clean-fail.out" CONFIRM_CLEAN=1 STUB_FAIL_MATCH=down; then
  fail 'clean succeeded when Compose teardown failed'
fi
[[ -f $root/.env ]] || fail 'clean removed .env after failed teardown'
: >"$STUB_LOG"
run_lifecycle "$root" clean "$TEST_TMP/clean.out" CONFIRM_CLEAN=1
assert_contains "$STUB_LOG" 'down --volumes --remove-orphans --rmi local'
[[ ! -e $root/.env ]] || fail 'clean kept .env after successful teardown'

root=$(new_root clean-without-env)
: >"$STUB_LOG"
run_lifecycle "$root" clean "$TEST_TMP/clean-no-env.out" CONFIRM_CLEAN=1
assert_contains "$STUB_LOG" 'down --volumes --remove-orphans --rmi local'

root=$(new_root lock)
canonical_root=$(cd "$root" && pwd -P)
project_hash=$(printf '%s' "$canonical_root" | "$REAL_OPENSSL" dgst -sha256 -r | sed 's/ .*//')
lock_path="/tmp/bzr-live-${project_hash:0:12}.lifecycle.lock"
mkdir "$lock_path"
if run_lifecycle "$root" init "$TEST_TMP/lock.out"; then
  fail 'init reclaimed an ownerless lock'
fi
assert_contains "$TEST_TMP/lock.out" "$lock_path"
assert_contains "$TEST_TMP/lock.out" 'verify no lifecycle process is running'
rmdir "$lock_path"

root=$(new_root lock-identity)
canonical_root=$(cd "$root" && pwd -P)
project_hash=$(printf '%s' "$canonical_root" | "$REAL_OPENSSL" dgst -sha256 -r | sed 's/ .*//')
fixed_lock_path="/tmp/bzr-live-${project_hash:0:12}.lifecycle.lock"
identity_cwd_a="$TEST_TMP/identity-cwd-a"
identity_cwd_b="$TEST_TMP/identity-cwd-b"
identity_tmp_a="$TEST_TMP/identity-tmp-a"
identity_tmp_b="$TEST_TMP/identity-tmp-b"
mkdir -p "$fixed_lock_path" "$identity_cwd_a" "$identity_cwd_b" "$identity_tmp_a" "$identity_tmp_b"
for identity_case in a b; do
  identity_cwd_var="identity_cwd_$identity_case"
  identity_tmp_var="identity_tmp_$identity_case"
  identity_out="$TEST_TMP/lock-identity-$identity_case.out"
  if (
    cd "${!identity_cwd_var}"
    run_lifecycle "$root" init "$identity_out" TMPDIR="${!identity_tmp_var}"
  ); then
    rmdir "$fixed_lock_path"
    fail "lifecycle ignored the fixed lock for identity case $identity_case"
  fi
done
rmdir "$fixed_lock_path"
for identity_case in a b; do
  identity_out="$TEST_TMP/lock-identity-$identity_case.out"
  assert_contains "$identity_out" "lifecycle lock is held"
  assert_contains "$identity_out" "at $fixed_lock_path"
done

root=$(new_root checkpoint-holds-lock)
canonical_root=$(cd "$root" && pwd -P)
project_hash=$(printf '%s' "$canonical_root" | "$REAL_OPENSSL" dgst -sha256 -r | sed 's/ .*//')
fixed_lock_path="/tmp/bzr-live-${project_hash:0:12}.lifecycle.lock"
different_tmp="$TEST_TMP/checkpoint-contention-tmp"
checkpoint_ready_file="$TEST_TMP/checkpoint-ready"
checkpoint_release_file="$TEST_TMP/checkpoint-release"
checkpoint_git_bin="$TEST_TMP/checkpoint-git-bin"
checkpoint_store="$TEST_TMP/checkpoint-store"
checkpoint_runner="$TEST_TMP/checkpoint-runner"
mkdir "$different_tmp" "$checkpoint_git_bin" "$checkpoint_store" "$checkpoint_runner"
chmod 700 "$checkpoint_store" "$checkpoint_runner"
checkpoint_store=$(cd "$checkpoint_store" && pwd -P)
checkpoint_runner=$(cd "$checkpoint_runner" && pwd -P)
cat >"$checkpoint_git_bin/git" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf 'ready\n' >"$CHECKPOINT_READY_FILE"
while [[ ! -e $CHECKPOINT_RELEASE_FILE ]]; do sleep 0.01; done
printf 'forced checkpoint holder failure\n' >&2
exit 71
STUB
chmod +x "$checkpoint_git_bin/git"
env \
  PATH="$checkpoint_git_bin:$STUB_BIN:$PATH" \
  BZ_LIVE_ROOT="$root" \
  CHECKPOINT_READY_FILE="$checkpoint_ready_file" \
  CHECKPOINT_RELEASE_FILE="$checkpoint_release_file" \
  "$REPO_ROOT/scripts/checkpoint" save lock-test \
    --store "$checkpoint_store" \
    --runner-state "$checkpoint_runner" \
    >"$TEST_TMP/checkpoint-holder.out" 2>&1 &
checkpoint_holder_pid=$!
for ((attempt = 0; attempt < 1000; attempt++)); do
  [[ -e $checkpoint_ready_file ]] && break
  kill -0 "$checkpoint_holder_pid" 2>/dev/null || break
  sleep 0.01
done
if [[ ! -e $checkpoint_ready_file ]]; then
  cat "$TEST_TMP/checkpoint-holder.out" >&2
  fail 'checkpoint exited before holding the lifecycle lock'
fi
if run_lifecycle \
  "$root" init "$TEST_TMP/checkpoint-blocks-lifecycle.out" TMPDIR="$different_tmp"; then
  checkpoint_blocks_lifecycle_status=0
else
  checkpoint_blocks_lifecycle_status=$?
fi
: >"$checkpoint_release_file"
if wait "$checkpoint_holder_pid"; then
  fail 'checkpoint holder unexpectedly completed against the stub checkout'
fi
[[ $checkpoint_blocks_lifecycle_status != 0 ]] ||
  fail 'lifecycle did not contend with the checkpoint lock'
assert_contains "$TEST_TMP/checkpoint-blocks-lifecycle.out" "at $fixed_lock_path"
assert_contains "$TEST_TMP/checkpoint-blocks-lifecycle.out" \
  "verify no lifecycle process is running, then remove $fixed_lock_path manually"

root=$(new_root lifecycle-holds-lock)
canonical_root=$(cd "$root" && pwd -P)
project_hash=$(printf '%s' "$canonical_root" | "$REAL_OPENSSL" dgst -sha256 -r | sed 's/ .*//')
fixed_lock_path="/tmp/bzr-live-${project_hash:0:12}.lifecycle.lock"
different_tmp="$TEST_TMP/lifecycle-contention-tmp"
lifecycle_ready_file="$TEST_TMP/lifecycle-ready"
lifecycle_release_file="$TEST_TMP/lifecycle-release"
mkdir "$different_tmp"
run_lifecycle \
  "$root" down "$TEST_TMP/lifecycle-holder.out" \
  TMPDIR="$different_tmp" \
  STUB_HOLD_READY_FILE="$lifecycle_ready_file" \
  STUB_HOLD_RELEASE_FILE="$lifecycle_release_file" &
lifecycle_holder_pid=$!
for ((attempt = 0; attempt < 1000; attempt++)); do
  [[ -e $lifecycle_ready_file ]] && break
  kill -0 "$lifecycle_holder_pid" 2>/dev/null || break
  sleep 0.01
done
if [[ ! -e $lifecycle_ready_file ]]; then
  kill "$lifecycle_holder_pid" 2>/dev/null || true
  wait "$lifecycle_holder_pid" 2>/dev/null || true
  cat "$TEST_TMP/lifecycle-holder.out" >&2
  fail 'lifecycle exited before holding its lock'
fi
if env \
  PATH="$STUB_BIN:$PATH" \
  BZ_LIVE_ROOT="$root" \
  "$REPO_ROOT/scripts/checkpoint" save lock-test \
    --store "$TEST_TMP/store" \
    --runner-state "$TEST_TMP/runner" \
    >"$TEST_TMP/lifecycle-blocks-checkpoint.out" 2>&1; then
  lifecycle_blocks_checkpoint_status=0
else
  lifecycle_blocks_checkpoint_status=$?
fi
: >"$lifecycle_release_file"
wait "$lifecycle_holder_pid"
[[ $lifecycle_blocks_checkpoint_status != 0 ]] ||
  fail 'checkpoint did not contend with the lifecycle lock'
assert_contains "$TEST_TMP/lifecycle-blocks-checkpoint.out" \
  "save: lifecycle lock is held (live PID "
assert_contains "$TEST_TMP/lifecycle-blocks-checkpoint.out" "at $fixed_lock_path"
assert_contains "$TEST_TMP/lifecycle-blocks-checkpoint.out" \
  "verify no lifecycle process is running, then remove $fixed_lock_path manually"

root_a=$(new_root project-a)
root_b=$(new_root project-b)
: >"$STUB_LOG"
run_lifecycle "$root_a" clean "$TEST_TMP/project-a.out" CONFIRM_CLEAN=1
project_a=$(sed -n 's/.*--project-name \([^ ]*\).*/\1/p' "$STUB_LOG" | tail -1)
: >"$STUB_LOG"
run_lifecycle "$root_b" clean "$TEST_TMP/project-b.out" CONFIRM_CLEAN=1
project_b=$(sed -n 's/.*--project-name \([^ ]*\).*/\1/p' "$STUB_LOG" | tail -1)
[[ -n $project_a && -n $project_b && $project_a != "$project_b" ]] ||
  fail 'different roots did not produce distinct project identities'

root=$(new_root doctor)
printf 'BZ_ADMIN_PASSWORD=a\nBZ_DB_PASSWORD=b\nMARIADB_ROOT_PASSWORD=c\n' >"$root/.env"
: >"$STUB_LOG"
run_lifecycle "$root" doctor "$TEST_TMP/doctor.out"
assert_contains "$STUB_LOG" 'config --quiet'
assert_contains "$STUB_LOG" 'port bugzilla 80'
assert_contains "$STUB_LOG" 'http://127.0.0.1:8080/'

# Assert literal Compose interpolation syntax rather than shell expansion.
# shellcheck disable=SC2016
assert_contains "$REPO_ROOT/compose.yaml" '127.0.0.1:${BZ_PORT:-8080}:80'
assert_contains "$REPO_ROOT/compose.yaml" 'mariadb-data:/var/lib/mysql'
assert_contains "$REPO_ROOT/compose.yaml" 'bugzilla-data:/var/www/html/data'
assert_contains "$REPO_ROOT/compose.yaml" 'condition: service_healthy'
assert_contains "$REPO_ROOT/compose.yaml" 'sha256:92e50059ea0a5965a33ef751970eab37d421b91ebbd01ac909039cffe159e574'
assert_contains "$REPO_ROOT/containers/bugzilla/Dockerfile" 'sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517'
assert_contains "$REPO_ROOT/containers/bugzilla/Dockerfile" 'bf4f79d9e6b230ad01d9b3c4d12a56af7eebd96cc76d5c9231a696a0750e1660'
assert_contains "$REPO_ROOT/containers/bugzilla/Dockerfile" 'f977a25b4116a0a95a7c8a894fd37097abe19af9a6a9ed4d800604ec17873fe4'
assert_contains "$REPO_ROOT/containers/bugzilla/Dockerfile" 'c7474050be80201f1fb55f0a569b9c0ab6c1c3f0cebbd7e601bda9b4046eec85'
assert_contains "$REPO_ROOT/containers/bugzilla/Dockerfile" 'b8de37460347bb5474dc01916ccb31dd2fe0cd92242c4a32d730e8eb087c323c'
assert_not_contains "$REPO_ROOT/containers/bugzilla/Dockerfile" 'cpan -T install'

COMPLETED=1
printf 'PASS: lifecycle contract\n'
