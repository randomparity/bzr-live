"""Semantic verification of a replayed scenario against live server state."""

from dataclasses import dataclass


class VerifyError(Exception):
    """Actionable verification failure; str(exc) is the operator-facing message."""


@dataclass(frozen=True, slots=True)
class Finding:
    """One divergence or unverifiable claim, as the report prints it.

    `kind` is "divergence" (the fixture disagrees with the scenario) or "unverifiable"
    (bzr cannot read the declared value back). `subject` is a symbolic alias, never a
    server id, so a report is readable without the journal beside it.
    """

    kind: str
    subject: str
    check: str
    detail: str


__all__ = ["Finding", "VerifyError"]
