from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import replace

from ..provision.keys import KeyStore
from ..replay.context import ReplayContext
from ..scenario import CompletedRecord, JournalStore, Reference, ValidatedScenario
from . import Finding, VerifyError
from .checks import (
    check_attachments,
    check_comments,
    check_fields,
    check_history,
    check_links,
    check_visibility,
)
from .expected import (
    INSIDER_GROUP,
    ExpectedBug,
    ExpectedScenario,
    fold,
    link_edges,
    reachable,
)
from .observed import LINKS_MAX_NODES, VIEW_FIELDS, ServerReader


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
    marker therefore says the read path cannot reach it. `ServerReader.comments` asks for
    `--api hybrid`, so bzr does attempt XML-RPC Bug.comments
    (src/client/resources/comment.rs:62); what remains is the fixture failing to answer it
    and bzr falling back to REST, which returns the public comments alone. Reporting that
    as a divergence would claim a bzr defect against a correctly replayed fixture.
    """
    text = "".join(str(entry.get("text", "")) for entry in comments)
    for comment in bug.comments:
        if comment.private and comment.marker and f"[{comment.marker}]" not in text:
            raise VerifyError(
                f"bug {alias!r}: the fixture cannot serve a full comment thread: the "
                f"private comment [{comment.marker}] is journalled as written but absent "
                "from the insider read, which asked for --api hybrid. XML-RPC needs "
                "XMLRPC::Lite, which libsoap-lite-perl does not bring in "
                "(Bugzilla/Install/Requirements.pm:303-310 requires them separately). "
                "Check that containers/bugzilla/Dockerfile installs libxmlrpc-lite-perl "
                "and that the running image was built from it (make up)")


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
        # One wrapping site, so every precondition refusal and every unreadable bug names
        # the scenario path without threading it through each message.
        try:
            check_reader_keys(self._expected, self._keys)
            resolved = resolve_ids(self._scenario, self._store)
            check_link_bound(self._expected)
            self._context.adopt(resolved)
            findings, executed = self._read_all(resolved)
        except VerifyError as exc:
            raise VerifyError(f"{self._scenario_dir}: {exc}") from exc
        findings = check_roles(self._expected) + findings
        for finding in findings:
            self._out(f"verify: {self._scenario_dir}: {finding.subject}: "
                      f"{finding.check}: {finding.detail}")
        kinds = Counter(finding.kind for finding in findings)
        self._out(f"verify: {executed} checks, {kinds['divergence']} divergences, "
                  f"{kinds['unverifiable']} unverifiable")
        return 1 if kinds["divergence"] else 0

    def _reader(self, alias: str | None) -> ServerReader | None:
        if alias is None:
            return None
        return ServerReader(self._context, Reference("actor", alias))

    def _read_all(self, resolved: Mapping[str, int]) -> tuple[list[Finding], int]:
        # An id the scenario never named still renders symbolically, so the inverse map
        # covers only the bugs; attachment identities are located by marker, never by id.
        alias_of = {value: key.split(":", 1)[1] for key, value in resolved.items()
                    if key.startswith("bug:")}
        # The insider sees everything the scenario declares; with no insider declared the
        # outsider is the widest reader there is, and check_roles has already reported
        # what that costs. check_reader_keys has proven whichever one this picks has a key.
        reader = self._reader(self._expected.insider or self._expected.outsider)
        if reader is None:
            raise VerifyError("the scenario declares no actor, so nothing can read it "
                              "back; declare at least one actor resource")
        # The outsider reader exists only to prove a private comment is withheld. It
        # costs nothing to build -- the per-bug guard below is what decides whether a
        # read is issued as it -- so a scenario declaring no private comment never pays.
        outsider = self._reader(self._expected.outsider)
        edges = link_edges(self._expected.bugs)
        findings: list[Finding] = []
        executed = 0
        for alias, bug in self._expected.bugs.items():
            bug_id = resolved.get(f"bug:{alias}")
            if bug_id is None:
                # resolve_ids has already refused an incomplete journal, so this is a
                # journal edited by hand rather than an unfinished replay. Say so as a
                # verify failure, not as a KeyError traceback out of the CLI.
                raise VerifyError(
                    f"the journal under this state root resolved no server id for bug "
                    f"{alias!r}; replay the scenario here before verifying it")
            found, ran = self._one_bug(
                bug, bug_id, reader, outsider, alias_of, edges[alias],
                reachable(edges, alias))
            findings += found
            executed += ran
        return findings, executed

    def _comment_findings(self, bug: ExpectedBug, comments: list,
                          emails: Mapping[str, str]) -> list[Finding]:
        """check_comments against the thread the reader was entitled to see.

        With no insider declared, the widest reader is the outsider, who cannot see a
        private comment by construction. Asserting one against their reply would report
        the scenario's own configuration as a divergence, and `check_comment_transport`
        would send the operator to the image's XML-RPC package for a thread the reader
        was never entitled to read. `check_roles` has already reported the missing role,
        once for the scenario rather than once per bug.
        """
        if self._expected.insider is None:
            bug = replace(bug, comments=tuple(
                comment for comment in bug.comments if not comment.private))
        else:
            check_comment_transport(bug.alias, bug, comments)
        return check_comments(bug, comments, emails)

    def _one_bug(self, bug: ExpectedBug, bug_id: int, reader: ServerReader,
                 outsider: ServerReader | None, alias_of: Mapping[int, str],
                 edges: frozenset[tuple[str, str, str]],
                 hops: Mapping[str, int]) -> tuple[list[Finding], int]:
        """The six check families on one bug, and how many of them ran.

        Every skip is decided from the fold, never from a reply, so a family is never
        dropped on the strength of what the server happened to return -- and the count
        this returns is the summary's claim that an executed family asserted something.
        """
        emails = self._expected.actor_emails
        observed = reader.bug(bug_id, (*VIEW_FIELDS, *self._expected.custom_field_keys))
        findings = check_fields(bug, observed, alias_of, emails)
        executed = 1
        if bug.history:
            findings += check_history(bug, reader.history(bug_id), observed, emails)
            executed += 1
        # At an eccentricity of 1 or 0 the direct read already covers the neighbourhood.
        # The skip takes declared_hops with it: a populated one beside an empty walk
        # reports every node absent.
        depth = max(hops.values(), default=0)
        recursive = depth >= 2
        findings += check_links(
            bug.alias, edges, hops if recursive else {}, reader.links(bug_id),
            reader.links(bug_id, depth=depth) if recursive else [], alias_of)
        executed += 1
        findings += self._comment_findings(bug, reader.comments(bug_id), emails)
        executed += 1
        if outsider is not None and any(c.private for c in bug.comments):
            findings += check_visibility(bug, outsider.comments(bug_id))
            executed += 1
        if bug.attachments:
            findings += check_attachments(bug, reader.attachments(bug_id), emails)
            executed += 1
        return findings, executed
