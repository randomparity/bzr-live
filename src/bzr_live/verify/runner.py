from __future__ import annotations

from collections.abc import Callable

from ..provision.keys import KeyStore
from ..replay.context import ReplayContext
from ..scenario import CompletedRecord, JournalStore, ValidatedScenario
from . import Finding, VerifyError
from .expected import (
    INSIDER_GROUP,
    ExpectedBug,
    ExpectedScenario,
    fold,
    link_edges,
    reachable,
)
from .observed import LINKS_MAX_NODES


def check_reader_keys(expected: ExpectedScenario, keys: KeyStore) -> None:
    """Every reader role the scenario declares must have a key, before any read.

    Left to `ReplayContext.client`, this surfaces as a `ReplayError` naming provisioning,
    raised from inside the first read -- after the insider's reads have already gone out
    for the outsider's case. The spec promises a refusal before any read, so the check is
    explicit and its message names the verify precondition.
    """
    for role, alias in (("insider", expected.insider), ("outsider", expected.outsider)):
        if alias is not None and keys.actor_key(alias) is None:
            raise VerifyError(
                f"the {role} reader {alias!r} has no API key under this state root; "
                "provision the scenario here before verifying it")


def check_roles(expected: ExpectedScenario) -> list[Finding]:
    """A role no declared actor fills is unverifiable, never a refusal.

    A scenario is free to declare only insiders, so the checks needing the missing role
    are reported with the reason instead of failing the run.
    """
    reasons = {
        "insider": (
            f"the scenario declares no actor in the {INSIDER_GROUP!r} group, so no read "
            "is issued as one and a declared private comment cannot be read back"),
        "outsider": (
            f"the scenario declares no actor outside the {INSIDER_GROUP!r} group, so no "
            "read can prove a private comment is withheld"),
    }
    return [
        Finding("unverifiable", role, "roles", reasons[role])
        for role, alias in (
            ("insider", expected.insider), ("outsider", expected.outsider))
        if alias is None]


def resolve_ids(scenario: ValidatedScenario, store: JournalStore) -> dict[str, int]:
    """Every server identity, taken only from completed journal records."""
    resolved: dict[str, int] = {}
    for event in scenario.events:
        record = store.read(event.name)
        if record is None or not isinstance(record, CompletedRecord):
            raise VerifyError(
                f"event {event.name!r} has no completed journal record under this state "
                "root; replay the scenario here before verifying it")
        if record.scenario_digest != scenario.digest:
            raise VerifyError(
                f"event {event.name!r} was journalled under scenario digest "
                f"{record.scenario_digest} but this scenario hashes to "
                f"{scenario.digest}; verify the scenario the journal was written for")
        if record.next_safe_action != "advance":
            raise VerifyError(
                f"event {event.name!r} is journalled with next safe action "
                f"{record.next_safe_action!r}, so the replay did not complete; resume "
                "it before verifying")
        resolved.update(record.resolved_ids)
    return resolved


def check_link_bound(expected: ExpectedScenario) -> None:
    edges = link_edges(expected.bugs)
    for alias in expected.bugs:
        count = len(reachable(edges, alias))
        if count > LINKS_MAX_NODES:
            raise VerifyError(
                f"bug {alias!r} reaches {count} bugs, above bzr's LINKS_MAX_NODES of "
                f"{LINKS_MAX_NODES}; a recursive walk would truncate and the "
                "verification would be incomplete")


def check_comment_transport(alias: str, bug: ExpectedBug, comments: list) -> None:
    """A declared private comment the insider cannot see is a fixture gap, not a divergence.

    `resolve_ids` has already established that the bug.comment event holds a completed
    record, so the comment is on the server. An insider read that does not carry its
    marker therefore says the read path cannot reach it. bzr reads a thread over XML-RPC
    Bug.comments (src/client/resources/comment.rs:62) and falls back to REST, which
    returns the public comments alone -- so the cause is the image, and reporting it as a
    divergence would claim a bzr defect against a correctly replayed fixture.
    """
    text = "".join(str(entry.get("text", "")) for entry in comments)
    for comment in bug.comments:
        if comment.private and comment.marker and f"[{comment.marker}]" not in text:
            raise VerifyError(
                f"bug {alias!r}: the fixture cannot serve a full comment thread: the "
                f"private comment [{comment.marker}] is journalled as written but absent "
                "from the insider read. bzr reads a thread over XML-RPC Bug.comments and "
                "falls back to REST, which returns the public comments alone; this image "
                "has libsoap-lite-perl without XMLRPC::Lite "
                "(Bugzilla/Install/Requirements.pm:303-310 requires them separately). "
                "Add libxmlrpc-lite-perl to containers/bugzilla/Dockerfile and rerun "
                "make up")


class Verifier:
    """One replayed scenario checked against the fixture its journal was written for."""

    def __init__(self, scenario: ValidatedScenario, context: ReplayContext,
                 store: JournalStore, scenario_dir: str, keys: KeyStore, *,
                 out: Callable[[str], None] = print) -> None:
        self._scenario = scenario
        self._context = context
        self._store = store
        self._scenario_dir = scenario_dir
        self._keys = keys
        self._out = out
        self._expected = fold(scenario)

    def run(self) -> int:
        # One wrapping site, so every precondition refusal names the scenario path
        # without threading it through each message.
        try:
            check_reader_keys(self._expected, self._keys)
            resolved = resolve_ids(self._scenario, self._store)
            check_link_bound(self._expected)
        except VerifyError as exc:
            raise VerifyError(f"{self._scenario_dir}: {exc}") from exc
        self._context.adopt(resolved)
        findings = check_roles(self._expected)
        for finding in findings:
            self._out(f"verify: {self._scenario_dir}: {finding.subject}: "
                      f"{finding.check}: {finding.detail}")
        return 1 if any(finding.kind == "divergence" for finding in findings) else 0
