"""The operator smoke scripts must never exit 0 without finishing (issue #29).

`make replay-smoke` reported success while executing none of its assertions. Two
things had to be true at once: a fatal error early in the body, and an exit status
that came back 0 anyway. This module is about the second one, because it is the half
that made the first invisible and would hide the next fatal error identically.

The mechanism, measured on this host rather than assumed:

- Under `set -u`, bash 3.2 treats a bad array subscript or an unbound variable as
  fatal. Bash 5.x exits 1 on the same fault.
- With **no** `EXIT` trap, bash 3.2 also exits 1. Install any `EXIT` trap and the
  status becomes 0 -- and `$?` is *already* 0 when the trap body starts, so a trap
  that captures `$?` and re-exits with it cannot recover the failure. Bash 5.x does
  not lose the status this way.

So the guarantee costs two things, and both are asserted here: a cleanup that
preserves the status it was handed, and a completion sentinel, which is the only
thing left that can tell "finished" from "aborted" once bash 3.2 has thrown the
status away.

The two injection modes exercise those separately, on the real scripts, without
editing them:

- **command failure** -- a stub earlier on `PATH` exits with a distinctive status.
  This is the case bash reports honestly on every version, and the assertion is that
  the cleanup hands that exact status back rather than replacing it with its own.
- **fatal expansion** -- `BASH_ENV` defines a function shadowing the same command,
  which expands an unset variable under `set -u`. This is the issue's own fault
  class, reproduced in the script's own shell after its trap is installed, and on
  bash 3.2 an unfixed script exits 0 here.

Both modes stub the first external command each script runs once its trap is live,
so the marker file is proof the fault landed inside the trap's window; a script that
died at one of the `${VAR:?}` preconditions above it would leave no marker and prove
nothing.

Every discovered `bash` is exercised. A fix verified only under Homebrew's bash 5.x
fixes nothing on a host whose `/bin/bash` is 3.2 -- and `/bin/bash` is what the
Makefile's plain `bash tests/replay_smoke.sh` resolves to there.

`tests/smoke_scenario.sh` carries the same trap and is deliberately absent: it is
owned by issue #25, in flight at the time of writing. Add its row once that lands.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Distinguishable from 0 and from 1, so the command-failure mode separates "the
# cleanup preserved the failing status" from "the cleanup re-exited with a literal".
STUB_STATUS = 3

# One row per in-scope script: the command to shadow -- the first external command
# the script runs after installing its trap -- and whatever the script needs to reach
# it. `BZ_PORT` is supplied so no run depends on a generated `.env` in the checkout.
SMOKE_SCRIPTS = (
    ("replay_smoke.sh", "uv", {"BZR_LIVE_BZR": "/bin/true", "BZ_PORT": "8080"}),
    ("provision_smoke.sh", "docker", {"BZR_LIVE_BZR": "/bin/true", "BZ_PORT": "8080"}),
    ("checkpoint_smoke.sh", "make", {}),
)


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

    def test_a_command_failure_keeps_its_own_status(self):
        for script, command, extra_env in SMOKE_SCRIPTS:
            for interpreter in discovered_bash_interpreters():
                with self.subTest(script=script, bash=interpreter):
                    completed, context = self._inject(
                        script, command, extra_env, interpreter, fatal=False)
                    self.assertEqual(
                        completed.returncode, STUB_STATUS,
                        f"the cleanup did not hand back the failing status.\n{context}")

    def test_a_fatal_expansion_error_does_not_report_success(self):
        for script, command, extra_env in SMOKE_SCRIPTS:
            for interpreter in discovered_bash_interpreters():
                with self.subTest(script=script, bash=interpreter):
                    completed, context = self._inject(
                        script, command, extra_env, interpreter, fatal=True)
                    self.assertNotEqual(
                        completed.returncode, 0,
                        f"the script aborted mid-body and reported success.\n{context}")

    def _inject(self, script, command, extra_env, interpreter, fatal):
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

            env = dict(os.environ)
            env.update(extra_env)
            env["TMPDIR"] = str(script_tmp)
            if fatal:
                # Sourced before the script, so the function is defined by the time
                # `set -u` is in force and the expansion inside it becomes fatal.
                bash_env = sandbox / "inject.sh"
                bash_env.write_text(
                    f"{command}() {{\n"
                    f"  printf 'ran\\n' >>'{marker}'\n"
                    f"  printf '%s\\n' \"$SMOKE_TRAP_TEST_UNSET_VARIABLE\"\n"
                    f"}}\n")
                env["BASH_ENV"] = str(bash_env)
            else:
                stub_dir = sandbox / "bin"
                stub_dir.mkdir()
                stub = stub_dir / command
                stub.write_text(
                    "#!/bin/sh\n"
                    f"printf 'ran\\n' >>'{marker}'\n"
                    f"exit {STUB_STATUS}\n")
                stub.chmod(0o755)
                env["PATH"] = os.pathsep.join([str(stub_dir), env["PATH"]])

            completed = subprocess.run(
                [interpreter, str(ROOT / "tests" / script)],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
                check=False)
            context = (f"{script} under {interpreter} "
                       f"({'fatal expansion' if fatal else 'command failure'}), "
                       f"exit {completed.returncode}\n"
                       f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")

            self.assertTrue(
                marker.exists(),
                f"the injected {command!r} never ran, so the script failed before its "
                f"trap was installed and this case proves nothing.\n{context}")
            self.assertEqual(
                sorted(entry.name for entry in script_tmp.iterdir()), [],
                f"cleanup left its temporary directory behind.\n{context}")
            return completed, context


if __name__ == "__main__":
    unittest.main()
