from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request


class ProvisionError(Exception):
    """Actionable provisioning failure; str(exc) is the operator-facing message."""


# Resource kind -> mutation boundary. Fixed by issue #4's implementation boundaries.
BOUNDARIES: dict[str, str] = {
    "group": "bzr",
    "actor": "bzr",
    "product": "bzr",
    "component": "bzr",
    "version": "bridge",
    "milestone": "bridge",
    "custom-field": "bridge",
    "keyword": "bridge",
    "flag-type": "bridge",
}

# Per-bug custom-field assignment is the sole stock-REST operation (journal boundary
# vocabulary, ADR 0002).
BUG_CUSTOM_FIELD_BOUNDARY = "bugzilla-rest-custom-field"


def compose_project_name(root: str) -> str:
    """Derive the compose project name exactly as scripts/lifecycle does.

    `root` must already be the physical path (os.path.realpath), matching the
    script's `pwd -P`.
    """
    return "bzr-live-" + hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]


_KEY_ENV = "BZR_LIVE_API_KEY"

# docker compose's messages when the exec target does not exist. Matching them maps
# a wrong --project-root to an actionable message instead of a generic failure.
_NO_CONTAINER_MARKERS = ("is not running", "no such service", "no container found")


def _api_error_code(stderr: str) -> int | None:
    for line in reversed(stderr.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            code = error.get("api_code")
            return code if isinstance(code, int) else None
    return None


def _payload(raw: bytes, context: str) -> object:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProvisionError(f"{context}: reply was not JSON") from None
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value


class BzrClient:
    """Stateless bzr subprocess seam. The key rides the environment, never argv."""

    def __init__(self, bzr_path: str, base_url: str, api_key: str,
                 admin_email: str, run=subprocess.run) -> None:
        self._bzr = bzr_path
        self._base_url = base_url
        self._api_key = api_key
        # The pinned Bugzilla has no rest/whoami; bzr's identity fallback
        # (rest/valid_login) needs the login email, passed via --server-email.
        self._admin_email = admin_email
        self._run = run

    def _invoke(self, args: list[str], positionals: list[str] | None):
        argv = [self._bzr, "--json", "--server-url", self._base_url,
                "--server-api-key-env", _KEY_ENV,
                "--server-email", self._admin_email, *args]
        if positionals:
            argv += ["--", *positionals]
        env = dict(os.environ)
        env[_KEY_ENV] = self._api_key
        return self._run(argv, capture_output=True, env=env, shell=False)

    def read(self, args: list[str], positionals: list[str] | None = None):
        """A read exiting 2, or exiting 4 with a not-found API code, is absent.

        Live-verified: a missing object is a server-side API error (exit 4)
        whose structured code rides stderr's last line. 51 (object), 105
        (unknown component), and 106 (unknown/inaccessible product) are the
        not-found codes at the pinned Bugzilla (WebService/Constants.pm).
        """
        done = self._invoke(args, positionals)
        if done.returncode == 0:
            return _payload(done.stdout, f"bzr {' '.join(args)}")
        if done.returncode == 2:
            return None
        stderr = done.stderr.decode("utf-8", "replace")
        if done.returncode == 4 and _api_error_code(stderr) in (51, 105, 106):
            return None
        raise ProvisionError(
            f"bzr boundary failure (exit {done.returncode}) running "
            f"{' '.join(args)}: {stderr.strip()}")

    def write(self, args: list[str], positionals: list[str] | None = None):
        done = self._invoke(args, positionals)
        if done.returncode != 0:
            raise ProvisionError(
                f"bzr boundary failure (exit {done.returncode}) running "
                f"{' '.join(args)}: {done.stderr.decode('utf-8', 'replace').strip()}")
        if done.stdout.strip():
            return _payload(done.stdout, f"bzr {' '.join(args)}")
        return {}

    def whoami(self) -> str:
        payload = self.read(["whoami"])
        if payload is None:
            raise ProvisionError("bzr whoami reported not-found; fixture unreachable")
        for key in ("name", "login", "email"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, str) and value:
                return value
        raise ProvisionError("bzr whoami reply carried no login")


class BridgeClient:
    """Fixed-operation container-local Perl bridge over docker compose exec."""

    OPERATIONS = frozenset({
        "create-version", "create-milestone", "create-custom-field",
        "create-keyword", "create-flag-type", "create-api-key",
        "get-custom-field", "get-keyword", "get-flag-type",
    })

    def __init__(self, argv_prefix: list[str], project: str,
                 run=subprocess.run) -> None:
        self._prefix = list(argv_prefix)
        self._project = project
        self._run = run

    def call(self, operation: str, payload: dict) -> object:
        if operation not in self.OPERATIONS:
            raise ProvisionError(f"bridge operation {operation!r} is not allowlisted")
        done = self._run(
            [*self._prefix, operation],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True, shell=False,
        )
        stderr = done.stderr.decode("utf-8", "replace")
        if any(marker in stderr.lower() for marker in _NO_CONTAINER_MARKERS):
            raise ProvisionError(
                f"bridge cannot reach the fixture: compose project {self._project} has "
                "no running bugzilla container. Pass --project-root <checkout root> "
                "(or run from the checkout) so the derived project matches make up.")
        secret = operation == "create-api-key"
        if done.returncode not in (0, 1):
            detail = "" if secret else f": {stderr.strip()}"
            raise ProvisionError(
                f"bridge boundary failure (exit {done.returncode}) in {operation}{detail}")
        try:
            reply = json.loads(done.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = "" if secret else f": {stderr.strip()}"
            raise ProvisionError(
                f"bridge reply for {operation} was not JSON{detail}") from None
        if not isinstance(reply, dict) or "ok" not in reply:
            raise ProvisionError(f"bridge reply for {operation} was malformed")
        if not reply["ok"]:
            if secret:
                # never echo any part of a create-api-key reply (spec: failure modes)
                raise ProvisionError(
                    "bridge create-api-key failed; check the fixture and rerun")
            raise ProvisionError(
                f"bridge {operation} failed: {reply.get('error', 'unknown')}")
        return reply.get("result")


def assign_bug_custom_fields(base_url: str, api_key: str, bug_id: int,
                             values: dict, opener=urllib.request.urlopen) -> object:
    """The sole stock-REST operation: per-bug custom-field assignment.

    The key rides the JSON body (`api_key` param) — the pinned Bugzilla has no auth
    header, and a query-string key would land in the container's access log.
    """
    if not isinstance(bug_id, int) or isinstance(bug_id, bool):
        raise ProvisionError("bug id must be an integer")
    body = dict(values)
    body["api_key"] = api_key
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/rest/bug/{bug_id}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with opener(request) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # Bugzilla returns error bodies with 4xx/5xx; read the body, never the key.
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise ProvisionError(
            f"REST custom-field assignment failed (HTTP {exc.code}): {detail}") from None
    except urllib.error.URLError as exc:
        raise ProvisionError(
            f"REST custom-field assignment failed: {exc.reason}") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProvisionError("REST custom-field reply was not JSON") from None
    if isinstance(payload, dict) and payload.get("error"):
        raise ProvisionError(
            f"REST custom-field assignment failed: {payload.get('message', 'unknown')}")
    return payload
