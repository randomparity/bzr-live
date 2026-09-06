"""The operator smoke scripts must never exit 0 without finishing (issue #29).

`make replay-smoke` reported success while executing none of its assertions. Two
things had to be true at once: a fatal error early in the body, and an exit status
that came back 0 anyway. This module is about the second one, because it is the half
that made the first invisible and would hide the next fatal error identically.

The mechanism, measured on the development host rather than assumed:

- Under `set -u`, bash 3.2 treats a bad array subscript or an unbound variable as
  fatal. Bash 5.x exits 1 on the same fault.
- With **no** `EXIT` trap, bash 3.2 also exits 1. Install any `EXIT` trap and the
  status becomes 0 -- and `$?` is *already* 0 when the trap body starts, so a trap
  that captures `$?` and re-exits with it cannot recover the failure. Bash 5.x does
  not lose the status this way.

So the guarantee costs two things: a cleanup that preserves the status it was handed,
and a completion sentinel, which is the only thing left that can tell "finished" from
"aborted" once bash 3.2 has thrown the status away.

Three injection modes exercise that on the real scripts, without editing them. Each
shadows the first external command the script runs once its trap is live, so the
marker file is proof the fault landed inside the trap's window; a script that died at
one of the `${VAR:?}` preconditions above it would leave no marker and prove nothing.

- **command failure** -- a stub earlier on `PATH` exits with a distinctive status.
  Bash reports this one honestly on every version, so it asserts the narrower thing:
  that cleanup hands that exact status back rather than replacing it with its own.
- **fatal expansion** -- `BASH_ENV` defines a function shadowing the same command,
  which expands an unset variable under `set -u`. This is the issue's own fault
  class, reproduced in the script's own shell. Note what it does **not** buy: an
  unfixed script fails it only under bash 3.2, so on a host with no bash below 4.4 --
  including this repository's Linux CI runners -- it passes against unfixed scripts
  and documents the mechanism rather than guarding it.
- **silent exit** -- the shadowing function exits 0 mid-body. That is the sentinel's
  actual contract, stated without reference to how bash lost the status, so it is the
  mode that bites on every interpreter and is what keeps the sentinel from being
  deleted somewhere the fatal-expansion mode is inert.

`test_no_script_indexes_an_array_from_the_end` covers the other half of the fix the
same way. No behavioural test reaches the changed indexing: it sits far below the
injection point, behind a live server and a populated journal. A syntax assertion is
what is left, and it enforces the decision the script headers record -- these stay
runnable on bash 3.2, so no bash 4+ syntax -- on every host.

Every discovered `bash` is exercised. A fix verified only under Homebrew's bash 5.x
fixes nothing on a host whose `/bin/bash` is 3.2 -- and `/bin/bash` is what the
Makefile's plain `bash tests/replay_smoke.sh` resolves to there.

Issue #25 added `tests/smoke_scenario.sh`, the live scenario proof `make smoke` runs.
It reaches its injection point the way `replay_smoke.sh` does, so its row needs
nothing structural -- only `BZR_LIVE_BZR` and `BZ_PORT`, because it refuses without
the first and would otherwise read the second from a generated `.env`. It carries the
same measured limitation `scripts/lifecycle` does, in a different shape: its first
`uv` sits inside a `COUNTS=$(...)` command substitution, and a fatal expansion error
in a subshell reaches the assignment as a real non-zero status, so `set -e` fires with
a status the trap can preserve. That mode therefore asserts status only here and does
not exercise the masking. Measured by hand at the script's first *top-level* `uv`,
under `/bin/bash` 3.2.57: a status-preserving cleanup with no sentinel exits **0**,
this script exits **1** and names the failure, and dropping the trap instead exits 1
but leaves the state root -- with the actor API keys in it -- behind. The silent-exit
mode is what guards that here, and it bites on every interpreter.

Issue #32 added the two scripts outside `tests/` that carried the same defect:
`scripts/lifecycle`, which `make up`, `make down`, `make reset`, `make clean` and
`make doctor` all invoke, and `tests/lifecycle_test.sh`, which `make test` runs. They
need three things the smoke rows did not, all of them per-row rather than structural:
a path, because a row is otherwise read relative to `tests/`; a subcommand, because
`scripts/lifecycle` rejects an empty argument vector before its trap is installed; and
an expected command-failure status, because `scripts/lifecycle` routes every failed
command through `die`, which exits 1 with an actionable message rather than passing the
failing status through. `containers/bugzilla/entrypoint.sh` carries a plain trap too
and stays out: it runs inside the container on bash 5.x, where the class does not
arise, and it clears its own trap before the exec.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Resolved via PATH rather than hardcoded: `true` lives at /usr/bin/true on macOS and
# /bin/true on Linux, and the rows below need only a value that is non-empty and, for
# smoke_scenario.sh, actually executable -- it execs "$BZR" --version for real before
# ever reaching its shadowed command (issue #38). Every POSIX host ships `true` on
# PATH, so a `None` here means the host itself cannot run these scripts. Raised rather
# than asserted: `python -O` strips a bare `assert`, which would silently hand every
# SMOKE_SCRIPTS row a `None` `BZR_LIVE_BZR` instead of failing here with the reason.
TRUE_BINARY = shutil.which("true")
if TRUE_BINARY is None:
    raise RuntimeError("no `true` executable found on PATH")

# Distinguishable from 0 and from 1, so the command-failure mode separates "the
# cleanup preserved the failing status" from "the cleanup re-exited with a literal".
STUB_STATUS = 3

# Every command these scripts reach out with. All of them are stubbed on every run,
# not just the one being injected: each script's body is made of `make`, `docker` and
# `uv` calls against the operator's live fixture, and the rows below rest on an
# unenforced invariant -- that the injected command is the first one reached. Should
# an edit break that invariant, stubbing the whole set turns a `make test` that
# reaches the running fixture into a marker assertion that says so.
SHADOWED_COMMANDS = ("uv", "docker", "make")

# One row per in-scope script: the command to inject the fault through -- the first
# external command the script runs after installing its trap -- and whatever the
# script needs to reach it. `BZ_PORT` is supplied so no run depends on a generated
# `.env` in the checkout. A row names a script under `tests/`, or carries a
# repo-relative path when the script lives elsewhere.
SMOKE_SCRIPTS = (
    ("replay_smoke.sh", "uv", {"BZR_LIVE_BZR": TRUE_BINARY, "BZ_PORT": "8080"}),
    ("provision_smoke.sh", "docker", {"BZR_LIVE_BZR": TRUE_BINARY, "BZ_PORT": "8080"}),
    ("checkpoint_smoke.sh", "make", {}),
    ("smoke_scenario.sh", "uv", {"BZR_LIVE_BZR": TRUE_BINARY, "BZ_PORT": "8080"}),
    ("lifecycle_test.sh", "mkdir", {}),
    ("scripts/lifecycle", "docker", {}),
)

# The argument vector a script needs to reach its injection point. `scripts/lifecycle`
# rejects an empty one at `:12-18` with exit 64, above the trap it installs at `:50`, so
# a row without arguments would prove nothing; `doctor` is the subcommand that takes no
# lock and writes nothing.
SCRIPT_ARGUMENTS = {"scripts/lifecycle": ("doctor",)}

# The sentinel's own stderr message, asserted in the silent-exit mode -- the one that
# bites on every interpreter -- so that deleting a cleanup's `printf` while keeping its
# `status=1` goes red rather than staying green. `scripts/lifecycle` is absent
# deliberately: the first external command it runs after installing its trap is
# `docker compose version >/dev/null 2>&1`, so a fault there reaches the trap with
# stderr still pointed at /dev/null. Its message is correct at any fault site the script
# does not itself silence, and unobservable from this injection point.
SENTINEL_MESSAGE = {
    "replay_smoke.sh": "replay smoke exited before finishing",
    "provision_smoke.sh": "provision smoke exited before finishing",
    "checkpoint_smoke.sh": "checkpoint smoke failed: exited before finishing",
    "lifecycle_test.sh": "lifecycle test exited before finishing",
    "smoke_scenario.sh": "smoke scenario: exited before finishing",
}

# The status the command-failure mode expects, where the script deliberately replaces
# the failing one. `scripts/lifecycle` funnels every failed command through `die`, which
# exits 1 after naming the operation and the next action -- the contract its callers
# document, so preserving it is what this mode asserts there.
EXPECTED_COMMAND_FAILURE_STATUS = {"scripts/lifecycle": 1}


def script_path(script):
    """The checkout path of a `SMOKE_SCRIPTS` row, which may carry its own directory."""
    return ROOT / script if "/" in script else ROOT / "tests" / script


# `${NAME[-1]}` and friends: valid from bash 4.3, fatal under `set -u` on bash 3.2.
FROM_THE_END = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\[\s*-\s*\d+\s*\]")


def discovered_bash_interpreters():
    """Every distinct `bash` on this host, by real path, under a name a caller uses.

    `#!/usr/bin/env bash` and the Makefile's `bash <script>` both resolve through
    `PATH`, so `shutil.which` is the one that matters most. `/bin/bash` is named
    explicitly because on macOS it is both the `PATH` winner and the old one, and the
    Homebrew prefixes because that is where a newer bash lives when the host has one.
    """
    candidates = ["/bin/bash", shutil.which("bash"), "/usr/local/bin/bash",
                  "/opt/homebrew/bin/bash"]
    by_real_path: dict[str, str] = {}
    for candidate in candidates:
        if candidate is None:
            continue
        real = os.path.realpath(candidate)
        if os.path.isfile(real) and os.access(real, os.X_OK):
            by_real_path.setdefault(real, candidate)
    return sorted(by_real_path.values())


class SmokeTrapStatusTest(unittest.TestCase):
    def test_a_bash_interpreter_was_discovered(self):
        """Guard: an empty interpreter list would make every case below vacuous."""
        self.assertTrue(discovered_bash_interpreters(), "no bash interpreter found")

    def test_a_configured_bzr_live_bzr_stub_exists_and_is_executable(self):
        """Guard: a row's `BZR_LIVE_BZR` need only be non-empty for its script's
        `BZR=${BZR_LIVE_BZR:?...}` precondition, but `smoke_scenario.sh` execs it for
        real at its first line of output (`"$BZR" --version`), before ever reaching
        its shadowed command. A stub that does not exist on this host does not fail
        that exec -- `set -euo pipefail` does not see it, because the exec sits inside
        a command substitution nested in another command's arguments -- but it prints
        a "No such file or directory" line to stderr that has nothing to do with the
        fault under test (issue #38)."""
        for script, _command, extra_env in SMOKE_SCRIPTS:
            stub = extra_env.get("BZR_LIVE_BZR")
            if stub is None:
                continue
            with self.subTest(script=script):
                self.assertTrue(
                    os.path.isfile(stub) and os.access(stub, os.X_OK),
                    f"{script}'s BZR_LIVE_BZR stub {stub!r} does not exist or is "
                    f"not executable on this host")

    def test_no_script_indexes_an_array_from_the_end(self):
        for script, _command, _env in SMOKE_SCRIPTS:
            with self.subTest(script=script):
                source = script_path(script).read_text()
                found = FROM_THE_END.findall(source)
                self.assertEqual(
                    found, [],
                    f"{script} indexes an array from the end ({found}), which needs "
                    f"bash 4.3; under `set -u` bash 3.2 aborts there. Use "
                    f"${{NAME[${{#NAME[@]}}-1]}}, per the script's header.")

    def test_a_command_failure_keeps_its_own_status(self):
        for script, command, extra_env in SMOKE_SCRIPTS:
            for interpreter in discovered_bash_interpreters():
                with self.subTest(script=script, bash=interpreter):
                    completed, context = self._inject(
                        script, command, extra_env, interpreter, "command-failure")
                    self.assertEqual(
                        completed.returncode,
                        EXPECTED_COMMAND_FAILURE_STATUS.get(script, STUB_STATUS),
                        f"the cleanup did not hand back the failing status.\n{context}")

    def test_a_fatal_expansion_error_does_not_report_success(self):
        for script, command, extra_env in SMOKE_SCRIPTS:
            for interpreter in discovered_bash_interpreters():
                with self.subTest(script=script, bash=interpreter):
                    completed, context = self._inject(
                        script, command, extra_env, interpreter, "fatal-expansion")
                    self.assertNotEqual(
                        completed.returncode, 0,
                        f"the script aborted mid-body and reported success.\n{context}")

    def test_a_silent_exit_mid_body_does_not_report_success(self):
        for script, command, extra_env in SMOKE_SCRIPTS:
            for interpreter in discovered_bash_interpreters():
                with self.subTest(script=script, bash=interpreter):
                    completed, context = self._inject(
                        script, command, extra_env, interpreter, "silent-exit")
                    self.assertNotEqual(
                        completed.returncode, 0,
                        f"the script stopped before its last assertion and still "
                        f"reported success.\n{context}")
                    expected_message = SENTINEL_MESSAGE.get(script)
                    if expected_message is not None:
                        self.assertIn(
                            expected_message, completed.stderr,
                            f"the sentinel changed the status without naming the "
                            f"failure.\n{context}")

    def _inject(self, script, command, extra_env, interpreter, mode):
        """Run `script` under `interpreter` with `command` replaced by a failing one.

        Returns the completed process and a context string for assertion messages,
        having already checked the two things every case needs to be meaningful: the
        injection point was reached, and cleanup still removed the temporary state.
        """
        with tempfile.TemporaryDirectory() as sandbox_name:
            sandbox = Path(sandbox_name)
            marker = sandbox / "injection-ran"
            script_tmp = sandbox / "tmp"
            script_tmp.mkdir()
            stub_dir = sandbox / "bin"
            stub_dir.mkdir()

            # Every shadowed command fails; only the injected one records that it ran,
            # so reaching a different one fails the marker assertion rather than the
            # operator's fixture. A row whose injection point is outside that set adds
            # it for its own runs only, because stubbing a general-purpose command for
            # every row would move where the other scripts fail.
            shadowed_commands = SHADOWED_COMMANDS
            if command not in shadowed_commands:
                shadowed_commands += (command,)
            for shadowed in shadowed_commands:
                if shadowed == command and mode == "command-failure":
                    body = f"printf 'ran\\n' >>'{marker}'\nexit {STUB_STATUS}\n"
                else:
                    body = f"exit {STUB_STATUS}\n"
                stub = stub_dir / shadowed
                stub.write_text(f"#!/bin/sh\n{body}")
                stub.chmod(0o755)

            env = dict(os.environ)
            env.update(extra_env)
            env["TMPDIR"] = str(script_tmp)
            env["PATH"] = os.pathsep.join([str(stub_dir), env["PATH"]])
            if mode != "command-failure":
                # Sourced before the script, so the function is defined by the time
                # `set -u` is in force and the expansion inside it becomes fatal. A
                # function outranks the PATH stub of the same name.
                if mode == "fatal-expansion":
                    fault = 'printf \'%s\\n\' "$SMOKE_TRAP_TEST_UNSET_VARIABLE"'
                else:
                    fault = "exit 0"
                bash_env = sandbox / "inject.sh"
                bash_env.write_text(
                    f"{command}() {{\n"
                    f"  printf 'ran\\n' >>'{marker}'\n"
                    f"  {fault}\n"
                    f"}}\n")
                env["BASH_ENV"] = str(bash_env)

            completed = subprocess.run(
                [interpreter, str(script_path(script)), *SCRIPT_ARGUMENTS.get(script, ())],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
                check=False)
            context = (f"{script} under {interpreter} ({mode}), "
                       f"exit {completed.returncode}\n"
                       f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")

            self.assertTrue(
                marker.exists(),
                f"the injected {command!r} never ran, so the script failed or reached "
                f"another shadowed command before it, and this case proves "
                f"nothing.\n{context}")
            self.assertEqual(
                sorted(entry.name for entry in script_tmp.iterdir()), [],
                f"cleanup left its temporary directory behind.\n{context}")
            return completed, context


if __name__ == "__main__":
    unittest.main()
