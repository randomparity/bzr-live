from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from ..provision.executor import custom_field_name
from ..replay.actions import render_attachment_summary
from ..scenario import PlannedEvent, Reference, ValidatedScenario

# containers/bugzilla/checksetup_answers.txt:31 sets $answer{'insidergroup'} = 'admin',
# so only a member of that group reads or writes a private comment. `editbugs` is not a
# proxy for it: stock Bugzilla grants editbugs by userregexp '.*'
# (Bugzilla/Install.pm:134-138), so every account on this image holds one regardless of
# what the scenario declares -- the premise commit 3f3f035 withdrew.
INSIDER_GROUP = "admin"

# bzr history fields whose old_value/new_value chain head to tail, so an ordering can be
# reconstructed from the values instead of from the reply's order.
CHAIN_FIELDS = frozenset(
    {"status", "resolution", "assigned_to", "target_milestone", "summary"})

# Declared field -> the reason it cannot be asserted. Printed verbatim by the report.
# 'groups' is NOT here: bzr a7f6ab70 (PR #646, closing bzr#641) exposes it in bug view,
# README pins make smoke at 63abb94e, which contains it, and the REST reply does carry a
# groups key. No scenarios/smoke bug declares one, so that assertion has unit coverage
# only -- the live tier has never exercised it.
UNVERIFIABLE_FIELDS: Mapping[str, str] = {
    "remaining_hours":
        "Bugzilla decrements remaining_time by logged work, so the declared value is not "
        "the fixture's final state; PR #23 excludes asserting against it",
    # a7f6ab70 is necessary but not sufficient, which the first live run of this stage is
    # what established. bzr serializes estimated_time, but Bugzilla gates the
    # time-tracking fields on timetrackinggroup -- 'editbugs' on this image -- and finding
    # D8 makes every bzr REST read anonymous, so the field is withheld. Measured at
    # 63abb94e with the insider's own valid key on a freshly replayed fixture: the default
    # transport and --api hybrid both return a bug view with no estimated_time, while
    # `--api xmlrpc` returns 8.0 and `GET /rest/bug/1?Bugzilla_api_key=...` returns 8.
    "estimated_hours":
        "Bugzilla gates estimated_time on timetrackinggroup ('editbugs' here) and finding "
        "D8 leaves bzr's REST reads unauthenticated, so bug view returns no "
        "estimated_time on this verifier's transport; only --api xmlrpc reads it back",
}
WORKTIME_UNVERIFIABLE = (
    "Bugzilla gates time-tracking fields on timetrackinggroup (issue #22), so logged "
    "hours are not reliably readable; the work-time comment is asserted instead")

# Declared update field -> the bzr bug view key and bzr history field name it lands in.
_SCALARS: Mapping[str, tuple[str, str]] = {
    "summary": ("summary", "summary"),
    "status": ("status", "status"),
    "resolution": ("resolution", "resolution"),
    "assignee": ("assigned_to", "assigned_to"),
    "milestone": ("target_milestone", "target_milestone"),
}
# Set-valued declared fields that compare by name or email.
_NAME_SETS = ("cc", "keywords")
# Set-valued declared fields whose members are bug aliases.
_EDGE_SETS = ("depends_on", "blocks")
# The inverse Bugzilla materializes on the other endpoint. dupe_of has no stock inverse:
# `bzr bug view 1` returns no `duplicates` field for the target of a duplicate.
_INVERSE = {"depends_on": "blocks", "blocks": "depends_on"}
_DIRECTION = {"depends_on": "out", "blocks": "in", "dupe_of": "out"}


@dataclass(frozen=True, slots=True)
class ExpectedFlag:
    name: str                       # flag-type alias
    status: str
    requestee: str | None           # actor alias


@dataclass(frozen=True, slots=True)
class ExpectedComment:
    marker: str | None              # None for comment 0, the create description
    author: str                     # actor alias
    private: bool
    text: str | None                # the exact body, for comment 0 only


@dataclass(frozen=True, slots=True)
class ExpectedAttachment:
    alias: str
    bug: str
    marker: str
    author: str                     # actor alias
    summary: str                    # as it should stand after every attachment.update
    content_type: str
    private: bool
    obsolete: bool
    sha256: str


@dataclass(frozen=True, slots=True)
class ExpectedChange:
    actor: str                      # actor alias
    field: str                      # bzr history field name
    # A frozenset for a _NAME_SETS field (the added members, compared as a set against the
    # observed value split on ", "), a string for a scalar, None for a generated id: skip
    # it. The representation follows the field, never the rule that produced the change.
    value: str | frozenset[str] | None
    chain: bool


@dataclass(frozen=True, slots=True)
class ExpectedBug:
    alias: str
    creator: str
    description: str
    scalars: Mapping[str, str]
    names: Mapping[str, frozenset[str]]
    edges: Mapping[str, frozenset[str]]
    duplicate_of: str | None
    custom_fields: Mapping[str, object]
    flags: tuple[ExpectedFlag, ...]
    comments: tuple[ExpectedComment, ...]
    attachments: tuple[ExpectedAttachment, ...]
    history: tuple[ExpectedChange, ...]
    unverifiable: tuple[tuple[str, str], ...]
    unasserted: frozenset[str]      # bzr bug view keys the server owns, not the scenario


@dataclass(frozen=True, slots=True)
class ExpectedScenario:
    bugs: Mapping[str, ExpectedBug]
    insider: str | None             # actor alias
    outsider: str | None            # actor alias
    custom_field_keys: tuple[str, ...]
    actor_emails: Mapping[str, str]


@dataclass
class _Bug:
    """Mutable fold state for one bug. Frozen into an ExpectedBug at the end."""

    alias: str
    creator: str
    description: str
    scalars: dict[str, str] = field(default_factory=dict)
    names: dict[str, set[str]] = field(default_factory=dict)
    edges: dict[str, set[str]] = field(default_factory=dict)
    duplicate_of: str | None = None
    custom_fields: dict[str, object] = field(default_factory=dict)
    flags: list[ExpectedFlag] = field(default_factory=list)
    comments: list[ExpectedComment] = field(default_factory=list)
    attachments: list[ExpectedAttachment] = field(default_factory=list)
    history: list[ExpectedChange] = field(default_factory=list)
    unverifiable: list[tuple[str, str]] = field(default_factory=list)
    unasserted: set[str] = field(default_factory=set)


def _project(value: Reference, emails: Mapping[str, str]) -> str:
    """A declared reference as the string bzr reads back: an email for an actor."""
    return emails[value.name] if value.kind == "actor" else value.name


def _bug_of(event: PlannedEvent, values: Mapping[str, object],
            bugs: dict[str, _Bug],
            attachments: Mapping[str, ExpectedAttachment]) -> _Bug:
    """The bug an event's postcondition targets, by alias.

    `expected_postcondition["target"]` is a bug reference for every action except
    bug.attach and attachment.update, where it is the attachment
    (`src/bzr_live/scenario/journal.py:330`). bug.attach declares its bug in `values`;
    attachment.update reaches it through the attachment registered by the attach event.
    """
    target = event.expected_postcondition["target"]
    if target.kind == "bug":
        return bugs[target.name]
    if event.action == "bug.attach":
        return bugs[values["bug"].name]
    return bugs[attachments[target.name].bug]


def _create(event: PlannedEvent, values: Mapping[str, object], bugs: dict[str, _Bug],
            attachments: dict[str, ExpectedAttachment],
            emails: Mapping[str, str]) -> None:
    bug = _Bug(event.creates.name, event.actor.name, values["description"])
    bug.scalars["summary"] = values["summary"]
    for key, view_key in (("product", "product"), ("component", "component"),
                          ("version", "version"), ("milestone", "target_milestone"),
                          ("assignee", "assigned_to")):
        if values[key] is not None:
            bug.scalars[view_key] = _project(values[key], emails)
    if values["estimated_hours"] is not None:
        _unverifiable(bug, "estimated_hours")
    for key in (*_NAME_SETS, "groups"):
        if values[key]:
            bug.names[key] = {_project(ref, emails) for ref in values[key]}
    bugs[bug.alias] = bug
    for key in _EDGE_SETS:
        if values[key]:
            _link(bug, key, {ref.name for ref in values[key]}, bugs)
    # Bugzilla writes no bugs_activity row for a creation, so create contributes no
    # ExpectedChange -- only the state the later events fold on top of.
    bug.comments.append(ExpectedComment(None, bug.creator, False, bug.description))


def _link(bug: _Bug, key: str, declared: set[str], bugs: dict[str, _Bug]) -> bool:
    """Replace one edge set, maintaining the inverse Bugzilla writes on each endpoint."""
    current = bug.edges.get(key, set())
    for other in current - declared:
        bugs[other].edges.get(_INVERSE[key], set()).discard(bug.alias)
    for other in declared - current:
        bugs[other].edges.setdefault(_INVERSE[key], set()).add(bug.alias)
    bug.edges[key] = declared
    return bool(declared - current)


def _update_scalar(bug: _Bug, actor: str, key: str, raw: object,
                   emails: Mapping[str, str]) -> None:
    view_key, history_field = _SCALARS[key]
    value = _project(raw, emails) if isinstance(raw, Reference) else raw
    bug.scalars[view_key] = value
    bug.history.append(ExpectedChange(actor, history_field, value, True))


def _update_names(bug: _Bug, actor: str, key: str, raw: object,
                  emails: Mapping[str, str]) -> None:
    declared = {_project(ref, emails) for ref in raw}
    added = declared - bug.names.get(key, set())
    bug.names[key] = declared
    # One record per field per event, not one per member: Bugzilla comma-joins the added
    # members into a single bugs_activity row, and nothing establishes the join order.
    if added:
        bug.history.append(ExpectedChange(actor, key, frozenset(added), False))


def _update_edges(bug: _Bug, actor: str, key: str, raw: object,
                  bugs: dict[str, _Bug]) -> None:
    if _link(bug, key, {ref.name for ref in raw}, bugs):
        # The history value is a comma-joined list of generated bug ids, so no assertion
        # can rest on it; check_links proves the edge itself.
        bug.history.append(ExpectedChange(actor, key, None, False))


def _unverifiable(bug: _Bug, key: str) -> None:
    row = (key, UNVERIFIABLE_FIELDS[key])
    if row not in bug.unverifiable:
        bug.unverifiable.append(row)


def _update_other(bug: _Bug, key: str, raw: object,
                  declared: Mapping[str, object]) -> None:
    """The declared update keys that are neither a scalar, a name set, nor an edge set.

    Silently ignoring anything else is safe ONLY because
    `src/bzr_live/replay/actions.py`'s `_UPDATE_UNSUPPORTED` refuses `groups` and
    `version` before any mutation, so no journalled run can carry one. Issue #27 removes
    that refusal; when it does, this fold has to grow an arm for each key it releases, or
    the verifier will quietly stop asserting it.
    """
    if key == "duplicate_of":
        bug.duplicate_of = raw.name
        # Bugzilla drives both itself on a duplicate (finding G5), and writes no dupe_of
        # activity row, so neither is the scenario's to claim.
        if "status" not in declared:
            bug.unasserted.update({"status", "resolution"})
    elif key in ("estimated_hours", "remaining_hours"):
        _unverifiable(bug, key)


def _update(event: PlannedEvent, values: Mapping[str, object], bugs: dict[str, _Bug],
            attachments: dict[str, ExpectedAttachment],
            emails: Mapping[str, str]) -> None:
    bug = _bug_of(event, values, bugs, attachments)
    actor = event.actor.name
    for key, raw in values.items():
        if key in _SCALARS:
            _update_scalar(bug, actor, key, raw, emails)
        elif key in _NAME_SETS:
            _update_names(bug, actor, key, raw, emails)
        elif key in _EDGE_SETS:
            _update_edges(bug, actor, key, raw, bugs)
        else:
            _update_other(bug, key, raw, values)
    if "status" in values and "resolution" not in values:
        # Bugzilla clears the resolution on a transition to an open status.
        bug.scalars.pop("resolution", None)


def _comment(event: PlannedEvent, values: Mapping[str, object], bugs: dict[str, _Bug],
             attachments: dict[str, ExpectedAttachment],
             emails: Mapping[str, str]) -> None:
    bug = _bug_of(event, values, bugs, attachments)
    bug.comments.append(ExpectedComment(
        event.reconciliation_marker, event.actor.name, values["private"], None))


def _worktime(event: PlannedEvent, values: Mapping[str, object], bugs: dict[str, _Bug],
              attachments: dict[str, ExpectedAttachment],
              emails: Mapping[str, str]) -> None:
    bug = _bug_of(event, values, bugs, attachments)
    bug.comments.append(ExpectedComment(
        event.reconciliation_marker, event.actor.name, False, None))
    row = ("worktime", WORKTIME_UNVERIFIABLE)
    if row not in bug.unverifiable:
        bug.unverifiable.append(row)


def _attach(event: PlannedEvent, values: Mapping[str, object], bugs: dict[str, _Bug],
            attachments: dict[str, ExpectedAttachment],
            emails: Mapping[str, str]) -> None:
    bug = _bug_of(event, values, bugs, attachments)
    attachment = ExpectedAttachment(
        alias=event.creates.name,
        bug=bug.alias,
        marker=event.reconciliation_marker,
        author=event.actor.name,
        summary=render_attachment_summary(
            values["description"], event.reconciliation_marker, values["asset_sha256"]),
        content_type=values["content_type"],
        private=values["private"],
        obsolete=False,
        sha256=values["asset_sha256"])
    attachments[attachment.alias] = attachment
    bug.attachments.append(attachment)


def _attachment_update(event: PlannedEvent, values: Mapping[str, object],
                       bugs: dict[str, _Bug],
                       attachments: dict[str, ExpectedAttachment],
                       emails: Mapping[str, str]) -> None:
    current = attachments[values["attachment"].name]
    summary = current.summary
    if "description" in values:
        summary = render_attachment_summary(
            values["description"], current.marker, current.sha256)
    updated = replace(current, obsolete=values["obsolete"], summary=summary)
    attachments[current.alias] = updated
    bug = bugs[current.bug]
    bug.attachments[bug.attachments.index(current)] = updated
    bug.history.append(ExpectedChange(
        event.actor.name, "attachments.isobsolete",
        "1" if updated.obsolete else "0", False))


def _custom(event: PlannedEvent, values: Mapping[str, object], bugs: dict[str, _Bug],
            attachments: dict[str, ExpectedAttachment],
            emails: Mapping[str, str]) -> None:
    bug = _bug_of(event, values, bugs, attachments)
    for assignment in values["values"]:
        name = custom_field_name(assignment["field"].name)
        raw = assignment["value"]
        multiple = isinstance(raw, tuple)
        bug.custom_fields[name] = frozenset(raw) if multiple else raw
        bug.history.append(ExpectedChange(
            event.actor.name, name, ", ".join(sorted(raw)) if multiple else raw, False))


def _flag(event: PlannedEvent, values: Mapping[str, object], bugs: dict[str, _Bug],
          attachments: dict[str, ExpectedAttachment],
          emails: Mapping[str, str]) -> None:
    bug = _bug_of(event, values, bugs, attachments)
    name = values["flag_type"].name
    status = values["status"]
    requestee = values["requestee"]
    spec = f"{name}{status}"
    bug.flags[:] = [flag for flag in bug.flags if flag.name != name]
    if status != "X":
        if requestee is not None:
            spec += f"({emails[requestee.name]})"
            # Bugzilla puts a requestee on the CC list. Modelled so that a later `cc`
            # declaration's delta matches the one BugUpdateHandler.build computes against
            # live server state; PR #23 excludes asserting the postcondition itself, so
            # this emits no ExpectedChange.
            bug.names.setdefault("cc", set()).add(emails[requestee.name])
        bug.flags.append(ExpectedFlag(
            name, status, None if requestee is None else requestee.name))
    bug.history.append(ExpectedChange(event.actor.name, "flagtypes.name", spec, False))


_HANDLERS = {
    "bug.create": _create, "bug.update": _update, "bug.comment": _comment,
    "bug.attach": _attach, "bug.worktime": _worktime,
    "bug.custom-field-set": _custom, "bug.flag": _flag,
    "attachment.update": _attachment_update,
}


def _apply(event: PlannedEvent, bugs: dict[str, _Bug],
           attachments: dict[str, ExpectedAttachment],
           emails: Mapping[str, str]) -> None:
    _HANDLERS[event.action](
        event, event.expected_postcondition["values"], bugs, attachments, emails)


def _freeze(bug: _Bug) -> ExpectedBug:
    return ExpectedBug(
        alias=bug.alias,
        creator=bug.creator,
        description=bug.description,
        scalars=MappingProxyType(dict(bug.scalars)),
        names=MappingProxyType(
            {key: frozenset(value) for key, value in bug.names.items()}),
        edges=MappingProxyType(
            {key: frozenset(value) for key, value in bug.edges.items()}),
        duplicate_of=bug.duplicate_of,
        custom_fields=MappingProxyType(dict(bug.custom_fields)),
        flags=tuple(bug.flags),
        comments=tuple(bug.comments),
        attachments=tuple(bug.attachments),
        history=tuple(bug.history),
        unverifiable=tuple(bug.unverifiable),
        unasserted=frozenset(bug.unasserted))


def _reader(scenario: ValidatedScenario, *, inside: bool) -> str | None:
    for resource in scenario.resources:
        if resource.kind != "actor":
            continue
        groups = {ref.name for ref in resource.data.get("groups") or ()}
        if (INSIDER_GROUP in groups) is inside:
            return resource.name
    return None


def fold(scenario: ValidatedScenario) -> ExpectedScenario:
    """Fold a scenario's events, in declaration order, into its expected end state."""
    emails = {
        resource.name: resource.data["email"]
        for resource in scenario.resources if resource.kind == "actor"}
    bugs: dict[str, _Bug] = {}
    attachments: dict[str, ExpectedAttachment] = {}
    for event in scenario.events:
        _apply(event, bugs, attachments, emails)
    return ExpectedScenario(
        bugs=MappingProxyType({alias: _freeze(bug) for alias, bug in bugs.items()}),
        insider=_reader(scenario, inside=True),
        outsider=_reader(scenario, inside=False),
        custom_field_keys=tuple(sorted(
            custom_field_name(resource.name) for resource in scenario.resources
            if resource.kind == "custom-field")),
        actor_emails=MappingProxyType(emails))


def link_edges(
        bugs: Mapping[str, ExpectedBug]) -> dict[str, frozenset[tuple[str, str, str]]]:
    """alias -> {(other alias, bzr relation, bzr direction)} for the declared graph."""
    out: dict[str, set[tuple[str, str, str]]] = {alias: set() for alias in bugs}
    for alias, bug in bugs.items():
        for relation in _EDGE_SETS:
            for other in bug.edges.get(relation, frozenset()):
                out[alias].add((other, relation, _DIRECTION[relation]))
        if bug.duplicate_of is not None:
            out[alias].add((bug.duplicate_of, "dupe_of", _DIRECTION["dupe_of"]))
    return {alias: frozenset(edges) for alias, edges in out.items()}


def reachable(edges: Mapping[str, frozenset[tuple[str, str, str]]],
              root: str) -> dict[str, int]:
    """Hop distance from `root` to every bug reachable through the declared edges."""
    hops: dict[str, int] = {}
    frontier = [root]
    seen = {root}
    depth = 0
    while frontier:
        depth += 1
        following: list[str] = []
        for alias in frontier:
            for other, _relation, _direction in sorted(edges.get(alias, frozenset())):
                if other in seen:
                    continue
                seen.add(other)
                hops[other] = depth
                following.append(other)
        frontier = following
    return hops
