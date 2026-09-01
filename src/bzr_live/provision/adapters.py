from __future__ import annotations

import hashlib


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
