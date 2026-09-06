"""Semantic verification of a replayed scenario against live server state."""

from dataclasses import dataclass


class VerifyError(Exception):
    """Actionable verification failure; str(exc) is the operator-facing message."""


class ReadNotFound(VerifyError):
    """A read the server answered with "no such object".

    Separated from its base so a caller explaining *why* an object was not found cannot
    also catch the other failures `ServerReader` raises through the same type -- an
    unrecognised reply shape above all, which says nothing about whether the object
    exists. Callers that only need "the verification failed" keep catching `VerifyError`
    and are unaffected.
    """


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


__all__ = ["Finding", "ReadNotFound", "VerifyError"]
