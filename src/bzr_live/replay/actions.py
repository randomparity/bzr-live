from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from ..provision.executor import custom_field_name
from ..scenario import InvocationMetadata, JsonValue, PlannedEvent
from .context import KEY_ENV, REST_BOUNDARY, ReplayContext, ReplayError

# Bugzilla declares attachments.description TINYTEXT at Bugzilla/DB/Schema.pm:505 -- read
# from this fixture's own image (BUGZILLA_VERSION "5.2+") -- a MySQL 255-BYTE column, so the
# check measures encoded bytes and not code points.
ATTACHMENT_SUMMARY_BYTE_LIMIT = 255

# Grounds for the refusals below, at bzr b80303b7: create_json.rs:150 defaults an omitted
# version to "unspecified"; update.rs:85 and :92 both carry conflicts_with = "dupe_of".
# Kept here rather than in the operator-facing messages, which outlive any line number.
#
# A message naming a *bzr* limitation cites its docs/bzr-findings.md entry as "(finding X)".
# A message whose ground is Bugzilla's own model names Bugzilla and cites no entry, because
# charging bzr for a constraint it did not impose corrupts the register as surely as hiding
# a real gap does. Neither kind tells the author how to route around the boundary.
_CREATE_UNSUPPORTED = {
    "custom_fields": "bzr excludes cf_* from bug create by design (finding G4)",
    "estimated_hours": "bzr bug create --from-json has no estimated_time field, "
                       "though Bugzilla accepts one (finding G1)",
    "remaining_hours": "bzr bug create --from-json has no remaining_time field, "
                       "though Bugzilla accepts one (finding G1)",
    "duplicate_of": "Bugzilla's own Bug.create has no dupe_of field",
}
_UPDATE_UNSUPPORTED = {
    "version": "bzr bug update has no version flag, though Bugzilla accepts one "
               "(finding G2)",
}
_UPDATE_NO_CLEAR = {
    "resolution": "Bugzilla clears the resolution on transition to an open status, "
                  "so declare that status change instead",
    "milestone": "bzr offers --reset-assigned-to but no milestone reset (finding G3)",
    # Bugzilla's, not bzr's: Bug.update types dupe_of as int with no null form, and
    # clear_resolution -- which calls _clear_dup_id -- throws unless the bug is already
    # open (Bugzilla/Bug.pm:2933-2939, WebService/Bug.pm:3935-3942, verified against the
    # fixture image). bzr's dupe_of: Option<u64> mirrors that exactly.
    "duplicate_of": "Bugzilla clears a duplicate through a status transition, not by "
                    "nulling dupe_of, so declare that status change instead",
}
# Both flags carry `conflicts_with = "dupe_of"` at bzr b80303b7
# (src/cli/bug/update.rs:85 and :92), so clap rejects either pairing at parse time.
_UPDATE_DUPE_CONFLICTS = ("status", "resolution")

# Neither field confirms, on two grounds that were never the same -- one shared rationale
# over both is what let a stale premise cover them together until issue #27 split it.
#
# estimated_hours: Bugzilla gates the time-tracking fields on timetrackinggroup (editbugs
# here) and omits them from an otherwise-successful 200 for a caller that has not cleared
# it, so finding D8's unauthenticated read never sees the value and no error status fires
# bzr's alternate-auth retry. This is a floor over the common case, not an absolute: a
# group-restricted bug 401s the whole read, so that retry does fire, authenticates, and
# brings the time fields back with it (measured at 63abb94e). Every bug a scenario here
# declares is anonymously readable, so the floor is what the code assumes.
#
# remaining_hours: Bugzilla decrements remaining_time by logged work, so the declared
# value is not the fixture's final state. Bugzilla's own model, surviving any bzr fix.
#
# check_supported has already refused version and the None forms (resolution, milestone,
# duplicate_of) that would otherwise need a place here too.
_UPDATE_ALWAYS_RETRY = ("estimated_hours", "remaining_hours")

# Declared field -> (bzr bug view key, projection of the declared value for comparison).
# Reference values compare after resolution, per the spec's "Reconciliation" section.
_UPDATE_COMPARE = {
    "summary": ("summary", lambda context, value: value),
    "status": ("status", lambda context, value: value),
    "resolution": ("resolution", lambda context, value: value),
    "assignee": ("assigned_to", lambda context, value: context.actor_email(value)),
    "duplicate_of": ("dupe_of", lambda context, value: context.resolve(value)),
    "milestone": ("target_milestone", lambda context, value: value.name),
}

# Bugzilla list fields compare as sets: the delta bzr applies is order-independent.
_UPDATE_COMPARE_SETS = {
    # Equality, like keywords, not cc's containment: Bugzilla widens the set behind the
    # caller only through a product's mandatory or default bug groups, and every
    # group_control_map row this fixture writes carries CONTROLMAPSHOWN in both control
    # columns -- neither CONTROLMAPMANDATORY nor CONTROLMAPDEFAULT (ADR 0006, ADR 0013).
    "groups": ("groups", lambda context, ref: ref.name),
    "cc": ("cc", lambda context, ref: context.actor_email(ref)),
    "keywords": ("keywords", lambda context, ref: ref.name),
    "depends_on": ("depends_on", lambda context, ref: context.resolve(ref)),
    "blocks": ("blocks", lambda context, ref: context.resolve(ref)),
}


def render_marker(text: str, marker: str) -> str:
    return f"{text}\n\n[{marker}]"


def render_attachment_summary(description: str, marker: str, sha256: str) -> str:
    return f"{description} [{marker}] sha256={sha256}".strip()


@dataclass(frozen=True, slots=True)
class Invocation:
    metadata: InvocationMetadata
    args: tuple[str, ...] = ()
    positionals: tuple[str, ...] = ()
    target_id: int | None = None            # bug id, for the REST boundary
    values: Mapping[str, object] | None = None   # REST body, without the api_key


@dataclass(frozen=True, slots=True)
class Reconciliation:
    next_action: str                 # "advance" | "retry" | "stop"
    output: JsonValue
    resolved_ids: Mapping[str, int]
    detail: str


AMBIGUOUS_HINT = (
    "the fixture cannot be reconciled automatically; reset it "
    "(CONFIRM_RESET=1 make reset) and replay")

# The append reconcilers list a bug's comments or attachments, so no API code means the
# entry is absent: an unreadable bug must raise, not answer "empty". This is the same
# distinction ReplayContext.read_bug draws by keeping 102 out of BUG_ABSENT_CODES.
_NO_ABSENT_CODES: frozenset[int] = frozenset()


def _bzr(operation: str, args, positionals=()) -> Invocation:
    args, positionals = tuple(args), tuple(str(p) for p in positionals)
    return Invocation(
        InvocationMetadata("bzr", operation, args + positionals, (KEY_ENV,)),
        args, positionals)


def _delta(declared: list, observed: list) -> tuple[list, list]:
    """Bugzilla list fields are edited by add/remove, so a declared set is a delta."""
    add = [item for item in declared if item not in observed]
    remove = [item for item in observed if item not in declared]
    return add, remove


def _bug_object(payload: JsonValue) -> dict | None:
    """The one bug object `bzr bug view` returns for a single ID, or None.

    Exactly one shape is accepted. Every read this engine issues names one bug, and
    bzr short-circuits a single ID to `view_single` before any batch handling
    (`src/commands/bug/view.rs:89-92` at b80303b7), so the batch wrapper is
    unreachable here; `BzrClient._payload` has already unwrapped the `data` envelope.
    Accepting a second shape would let a future change in bzr's reply be absorbed
    silently instead of failing loudly, which is the accommodation this repository
    reverted in 04c3ac7. Anything else returns None and the caller refuses.
    """
    if isinstance(payload, dict) and "id" in payload:
        return payload
    return None


def _attachment_object(payload: JsonValue) -> dict | None:
    """The one attachment object `bzr attachment view` returns for a single ID, or None.

    Same reasoning as `_bug_object`: exactly one shape is accepted so a reply-shape
    change surfaces instead of being silently absorbed.
    """
    if isinstance(payload, dict) and "id" in payload:
        return payload
    return None


def _usable_id(value) -> bool:
    """A server id the journal will accept. `True` is not one.

    `isinstance(True, int)` holds in Python, but `CompletedRecord.__post_init__` tests
    `type(value) is int` -- so a bool passes every isinstance guard here and is then
    refused by the journal, blaming the record for a boundary reply after the in-flight
    record has landed and the mutation may already have committed.
    """
    return type(value) is int and value > 0


# Reconciliation finds an append by looking for its bracketed marker in server-visible
# text. Declared text that already carries one would let event A's body satisfy event B's
# reconciliation -- `advance` for a mutation B never made, and for bug.attach the id of
# the colliding entry adopted into the resolution table. Refused as a precondition, where
# every other payload this boundary cannot express faithfully is refused.
_MARKER_SENTINEL = "[bzr-live:"


def _reject_embedded_marker(event: PlannedEvent, field: str, text: str) -> None:
    if _MARKER_SENTINEL in text:
        raise _unsupported(
            event, field,
            f"it contains {_MARKER_SENTINEL!r}, the token this engine appends to mark "
            "its own writes, so reconciliation could not tell the declared text from a "
            "marker it wrote; remove the bracketed token")


def _marker_count(entries, field: str, marker: str) -> tuple[int | None, int | None]:
    """Entries carrying the marker. A None count means the reply did not answer."""
    if entries is None:
        return None, None
    token = f"[{marker}]"
    hits = [entry for entry in entries
            if isinstance(entry, dict) and token in (entry.get(field) or "")]
    if len(hits) != 1:
        return len(hits), None
    return 1, hits[0].get("id")


def _append_result(event: PlannedEvent, count: int, entry_id, output: JsonValue, *,
                   id_key: str | None = None) -> Reconciliation:
    """The append-class verdict: one marker advances, none retries, more than one stops.

    `id_key` names the resolved reference an append that creates something adopts. The
    id is checked here rather than by the caller, because `_marker_count` yields None
    for a matched entry carrying no `id` and an unchecked None reaches
    `CompletedRecord.resolved_ids`, which refuses it -- turning a malformed boundary
    reply into a halt that blames the journal, after the in-flight record has landed.

    A None count is the reply that did not answer. It stops rather than retrying: an
    append that cannot be observed is not an append that did not happen, and this is
    the one recovery class whose contract is that it is never blindly repeated.
    """
    if count is None:
        return Reconciliation(
            "stop", output, {},
            f"event {event.name!r}: the boundary did not answer the query for marker "
            f"{event.reconciliation_marker!r}, so neither a commit nor its absence is "
            f"proven; {AMBIGUOUS_HINT}")
    if count == 1:
        if id_key is None:
            return Reconciliation("advance", output, {}, "")
        if not _usable_id(entry_id):
            return Reconciliation(
                "stop", output, {},
                f"event {event.name!r}: the entry matching marker "
                f"{event.reconciliation_marker!r} carries no usable id; "
                f"{AMBIGUOUS_HINT}")
        return Reconciliation("advance", output, {id_key: entry_id}, "")
    if count == 0:
        return Reconciliation(
            "retry", output, {}, f"event {event.name!r} did not commit")
    return Reconciliation(
        "stop", output, {},
        f"event {event.name!r} matches {count} results for marker "
        f"{event.reconciliation_marker!r}; {AMBIGUOUS_HINT}")


def _entries(payload: JsonValue) -> list | None:
    """The list `comment list` / `attachment list` returns, or None for no answer.

    One shape, for the same reason as `_bug_object`: a wrapper branch here would be
    dead code that silently absorbs a reply-shape change instead of surfacing it.
    A non-list reply is not an empty bug -- `BzrClient.read` answers None on exit 2 --
    so it must not collapse into the empty list that drives the append retry.
    """
    return payload if isinstance(payload, list) else None


def _unsupported(event: PlannedEvent, field: str, limitation: str) -> ReplayError:
    # The register pointer is appended only for a bzr-grounded limitation, which is
    # exactly the set that names an entry. A Bugzilla-grounded refusal has no entry to
    # point at, and sending an operator to look for one would be its own small lie.
    pointer = " See docs/bzr-findings.md." if "(finding " in limitation else ""
    return ReplayError(
        f"event {event.name!r} ({event.action}) declares {field!r}, which the boundary "
        f"cannot apply: {limitation}.{pointer}")


class ActionHandler:
    action = ""
    boundary = "bzr"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        return

    def build(self, context: ReplayContext, event: PlannedEvent) -> Invocation:
        raise NotImplementedError(self.action)

    def resolved_ids(self, event: PlannedEvent, output: JsonValue) -> dict[str, int]:
        return {}

    def reconcile(self, context: ReplayContext, event: PlannedEvent) -> Reconciliation:
        raise NotImplementedError(self.action)


class BugCreateHandler(ActionHandler):
    action = "bug.create"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        values = event.expected_postcondition["values"]
        for field, fix in _CREATE_UNSUPPORTED.items():
            declared = values.get(field)
            if declared:                      # () and None are both "not declared"
                raise _unsupported(event, field, fix)
        if values.get("version") is None:
            raise _unsupported(
                event, "version",
                "bzr silently substitutes the version 'unspecified', which this "
                "fixture's products do not declare (finding G9)")
        # Not an append, but its text lands in the corpus the append reconcilers search:
        # Bugzilla stores the description as comment 0 (Bugzilla/Bug.pm:828 inserts it
        # through Comment->insert_create_data; sub comments numbers from 0), and
        # `bzr comment list` filters nothing out. A create description carrying an
        # append's marker would satisfy that append's reconciliation.
        _reject_embedded_marker(event, "description", values["description"])

    def build(self, context: ReplayContext, event: PlannedEvent) -> Invocation:
        values = event.expected_postcondition["values"]
        document: dict[str, JsonValue] = {
            "alias": values["server_alias"],
            "product": values["product"].name,
            "component": values["component"].name,
            "summary": values["summary"],
            "description": values["description"],
            "version": values["version"].name,
        }
        # Nothing else goes in. bzr wants op_sys/rep_platform on installations that set no
        # defaults; the honest fix is the fixture's checksetup answers (Task 0), not a value
        # the scenario never declared. See AGENTS.md, "Purpose: prove bzr".
        if values["milestone"] is not None:
            document["target_milestone"] = values["milestone"].name
        if values["assignee"] is not None:
            document["assignee"] = context.actor_email(values["assignee"])
        cc = [context.actor_email(ref) for ref in values["cc"]]
        keywords = [ref.name for ref in values["keywords"]]
        groups = [ref.name for ref in values["groups"]]
        for key, collection in (
            ("cc", cc), ("keywords", keywords), ("groups", groups),
            ("blocks", context.resolve_all(values["blocks"])),
            ("depends_on", context.resolve_all(values["depends_on"])),
        ):
            if collection:
                document[key] = collection
        path = context.json_file(f"create-{event.name}", document)
        return _bzr("bug create", ["bug", "create", f"--from-json={path}"])

    def resolved_ids(self, event: PlannedEvent, output: JsonValue) -> dict[str, int]:
        bug = _bug_object(output)
        if bug is None or not _usable_id(bug.get("id")):
            raise ReplayError(
                f"event {event.name!r}: bzr bug create returned no bug id")
        return {f"bug:{event.creates.name}": bug["id"]}

    def reconcile(self, context: ReplayContext, event: PlannedEvent) -> Reconciliation:
        values = event.expected_postcondition["values"]
        payload = context.read_bug(event.actor, [values["server_alias"]])
        if payload is None:
            return Reconciliation(
                "retry", payload, {}, f"event {event.name!r} did not commit")
        bug = _bug_object(payload)
        bug_id = bug.get("id") if bug is not None else None
        if not _usable_id(bug_id):
            return Reconciliation(
                "stop", payload, {},
                f"event {event.name!r}: bzr bug view returned no usable bug id; "
                f"{AMBIGUOUS_HINT}")
        return Reconciliation(
            "advance", payload, {f"bug:{event.creates.name}": bug_id}, "")


class BugUpdateHandler(ActionHandler):
    action = "bug.update"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        values = event.expected_postcondition["values"]
        for field, fix in _UPDATE_UNSUPPORTED.items():
            if field in values:
                raise _unsupported(event, field, fix)
        for field, fix in _UPDATE_NO_CLEAR.items():
            if field in values and values[field] is None:
                raise _unsupported(event, field, fix)
        if values.get("duplicate_of") is not None:
            for field in _UPDATE_DUPE_CONFLICTS:
                if field in values:
                    raise _unsupported(
                        event, field,
                        f"bzr's --{field} carries conflicts_with = \"dupe_of\", "
                        "deliberately (finding G5)")

    def build(self, context: ReplayContext, event: PlannedEvent) -> Invocation:
        values = event.expected_postcondition["values"]
        bug_ref = event.expected_postcondition["target"]
        bug_id = context.resolve(bug_ref)
        observed = _bug_object(
            context.client(event.actor).read(["bug", "view"], positionals=[str(bug_id)]))
        if observed is None:
            raise ReplayError(
                f"event {event.name!r}: cannot read bug {bug_id} to compute the update")
        args = ["bug", "update"]
        for key, flag in (
            ("summary", "--summary"), ("status", "--status"),
            ("resolution", "--resolution"),
        ):
            if key in values:
                args.append(f"{flag}={values[key]}")
        if "assignee" in values:
            args.append(
                "--reset-assigned-to" if values["assignee"] is None
                else f"--assignee={context.actor_email(values['assignee'])}")
        if values.get("duplicate_of") is not None:
            args.append(f"--dupe-of={context.resolve(values['duplicate_of'])}")
        if values.get("milestone") is not None:
            args.append(f"--target-milestone={values['milestone'].name}")
        for key, flag in (
            ("estimated_hours", "--estimated-time"),
            ("remaining_hours", "--remaining-time"),
        ):
            if key in values:
                args.append(f"{flag}={values[key]}")
        for key, add_flag, remove_flag, project in (
            ("groups", "--groups-add", "--groups-remove", lambda r: r.name),
            ("cc", "--cc-add", "--cc-remove", lambda r: context.actor_email(r)),
            ("keywords", "--keywords-add", "--keywords-remove", lambda r: r.name),
            ("depends_on", "--depends-on-add", "--depends-on-remove",
             lambda r: context.resolve(r)),
            ("blocks", "--blocks-add", "--blocks-remove", lambda r: context.resolve(r)),
        ):
            if key not in values:
                continue
            declared = [project(ref) for ref in values[key]]
            add, remove = _delta(declared, list(observed.get(key) or []))
            args += [f"{add_flag}={item}" for item in add]
            args += [f"{remove_flag}={item}" for item in remove]
        return _bzr("bug update", args, [bug_id])

    def reconcile(self, context: ReplayContext, event: PlannedEvent) -> Reconciliation:
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(event.expected_postcondition["target"])
        bug = _bug_object(context.read_bug(event.actor, [str(bug_id)]))
        if bug is None:
            return Reconciliation(
                "retry", bug, {}, f"event {event.name!r}: bug {bug_id} is unreadable")
        for field in values:
            if field in _UPDATE_ALWAYS_RETRY or (field == "assignee" and values[field] is None):
                return Reconciliation(
                    "retry", bug, {},
                    f"event {event.name!r}: declared {field!r} cannot be read back")
            if field in _UPDATE_COMPARE:
                key, project = _UPDATE_COMPARE[field]
                if project(context, values[field]) != bug.get(key):
                    return Reconciliation(
                        "retry", bug, {},
                        f"event {event.name!r}: declared {field!r} differs from the fixture")
            elif field in _UPDATE_COMPARE_SETS:
                key, project = _UPDATE_COMPARE_SETS[field]
                declared = {project(context, ref) for ref in values[field]}
                if declared != set(bug.get(key) or []):
                    return Reconciliation(
                        "retry", bug, {},
                        f"event {event.name!r}: declared {field!r} differs from the fixture")
        return Reconciliation("advance", bug, {}, "")


class BugCommentHandler(ActionHandler):
    action = "bug.comment"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        _reject_embedded_marker(
            event, "body", event.expected_postcondition["values"]["body"])

    def build(self, context: ReplayContext, event: PlannedEvent) -> Invocation:
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(event.expected_postcondition["target"])
        path = context.text_file(
            f"comment-{event.name}",
            render_marker(values["body"], event.reconciliation_marker))
        args = ["comment", "add", f"--body-file={path}"]
        if values["private"]:
            args.append("--private")
        return _bzr("comment add", args, [bug_id])

    def reconcile(self, context: ReplayContext, event: PlannedEvent) -> Reconciliation:
        bug_id = context.resolve(event.expected_postcondition["target"])
        payload = context.client(event.actor).read(
            ["comment", "list"], positionals=[str(bug_id)],
            absent_codes=_NO_ABSENT_CODES)
        count, entry_id = _marker_count(_entries(payload), "text", event.reconciliation_marker)
        return _append_result(event, count, entry_id, payload)


class BugAttachHandler(ActionHandler):
    action = "bug.attach"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        values = event.expected_postcondition["values"]
        _reject_embedded_marker(event, "description", values["description"])
        rendered = render_attachment_summary(
            values["description"], event.reconciliation_marker, values["asset_sha256"])
        size = len(rendered.encode("utf-8"))
        if size > ATTACHMENT_SUMMARY_BYTE_LIMIT:
            raise _unsupported(
                event, "description",
                f"the rendered attachment summary is {size} bytes and Bugzilla's "
                f"attachments.description holds {ATTACHMENT_SUMMARY_BYTE_LIMIT}; "
                "shorten the description")

    def build(self, context: ReplayContext, event: PlannedEvent) -> Invocation:
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(values["bug"])
        path = context.asset_file(values["asset"].name, values["asset_sha256"])
        summary = render_attachment_summary(
            values["description"], event.reconciliation_marker, values["asset_sha256"])
        args = ["attachment", "upload", f"--summary={summary}",
                f"--content-type={values['content_type']}"]
        if values["private"]:
            args.append("--private")
        return _bzr("attachment upload", args, [bug_id, path])

    def resolved_ids(self, event: PlannedEvent, output: JsonValue) -> dict[str, int]:
        if not isinstance(output, dict) or not _usable_id(output.get("id")):
            raise ReplayError(
                f"event {event.name!r}: bzr attachment upload returned no usable "
                "attachment id")
        return {f"attachment:{event.creates.name}": output["id"]}

    def reconcile(self, context: ReplayContext, event: PlannedEvent) -> Reconciliation:
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(values["bug"])
        payload = context.client(event.actor).read(
            ["attachment", "list"], positionals=[str(bug_id)],
            absent_codes=_NO_ABSENT_CODES)
        count, entry_id = _marker_count(
            _entries(payload), "summary", event.reconciliation_marker)
        return _append_result(
            event, count, entry_id, payload,
            id_key=f"attachment:{event.creates.name}")


class BugWorktimeHandler(ActionHandler):
    action = "bug.worktime"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        _reject_embedded_marker(
            event, "comment", event.expected_postcondition["values"]["comment"])

    def build(self, context: ReplayContext, event: PlannedEvent) -> Invocation:
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(values["bug"])
        path = context.text_file(
            f"worktime-{event.name}",
            render_marker(values["comment"], event.reconciliation_marker))
        return _bzr("bug update", [
            "bug", "update", f"--work-time={values['hours']}",
            f"--comment-file={path}"], [bug_id])

    def reconcile(self, context: ReplayContext, event: PlannedEvent) -> Reconciliation:
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(values["bug"])
        payload = context.client(event.actor).read(
            ["comment", "list"], positionals=[str(bug_id)],
            absent_codes=_NO_ABSENT_CODES)
        count, entry_id = _marker_count(_entries(payload), "text", event.reconciliation_marker)
        return _append_result(event, count, entry_id, payload)


class BugCustomFieldHandler(ActionHandler):
    action = "bug.custom-field-set"
    boundary = "bugzilla-rest-custom-field"

    def build(self, context: ReplayContext, event: PlannedEvent) -> Invocation:
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(values["bug"])
        body = {
            custom_field_name(assignment["field"].name):
                list(assignment["value"]) if isinstance(assignment["value"], tuple)
                else assignment["value"]
            for assignment in values["values"]
        }
        return Invocation(
            InvocationMetadata(
                REST_BOUNDARY, f"PUT rest/bug/{bug_id}", tuple(sorted(body)), ()),
            target_id=bug_id, values=body)

    def reconcile(self, context: ReplayContext, event: PlannedEvent) -> Reconciliation:
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(values["bug"])
        bug = _bug_object(context.read_bug(event.actor, [str(bug_id)]))
        if bug is None:
            return Reconciliation(
                "retry", bug, {}, f"event {event.name!r}: bug {bug_id} is unreadable")
        for assignment in values["values"]:
            key = custom_field_name(assignment["field"].name)
            declared = assignment["value"]
            if isinstance(declared, tuple):
                matches = set(declared) == set(bug.get(key) or [])
            else:
                matches = declared == bug.get(key)
            if not matches:
                return Reconciliation(
                    "retry", bug, {},
                    f"event {event.name!r}: declared {key!r} differs from the fixture")
        return Reconciliation("advance", bug, {}, "")


class BugFlagHandler(ActionHandler):
    action = "bug.flag"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        # bzr's parse_single_flag takes the FIRST of + - ? X as the status character, so a
        # hyphenated slug like `needs-info` renders `--flag=needs-info?` and parses as name
        # `needs`. Uppercase X cannot occur in a slug, so the hyphen is the live hazard.
        name = event.expected_postcondition["values"]["flag_type"].name
        if any(character in name for character in "+-?X"):
            raise _unsupported(
                event, "flag_type",
                f"bzr cannot address the flag type name {name!r}: its flag parser takes "
                "the first of + - ? X as the status character, so the name is truncated. "
                "Bugzilla permits such names (finding D1)")

    def build(self, context: ReplayContext, event: PlannedEvent) -> Invocation:
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(values["bug"])
        spec = f"{values['flag_type'].name}{values['status']}"
        if values["requestee"] is not None:
            spec += f"({context.actor_email(values['requestee'])})"
        return _bzr("bug update", ["bug", "update", f"--flag={spec}"], [bug_id])

    def reconcile(self, context: ReplayContext, event: PlannedEvent) -> Reconciliation:
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(values["bug"])
        bug = _bug_object(context.read_bug(event.actor, [str(bug_id)]))
        if bug is None:
            return Reconciliation(
                "retry", bug, {}, f"event {event.name!r}: bug {bug_id} is unreadable")
        flag_name = values["flag_type"].name
        status = values["status"]
        flags = [flag for flag in (bug.get("flags") or []) if isinstance(flag, dict)]
        if status == "X":
            # A clear leaves no entry with this type name behind; comparing for a
            # present X entry would make a flag-clear unable to reconcile at all.
            matches = not any(flag.get("name") == flag_name for flag in flags)
        else:
            requestee_email = (
                context.actor_email(values["requestee"])
                if values["requestee"] is not None else None)
            matches = any(
                flag.get("name") == flag_name and flag.get("status") == status
                and (requestee_email is None or flag.get("requestee") == requestee_email)
                for flag in flags)
        if matches:
            return Reconciliation("advance", bug, {}, "")
        return Reconciliation(
            "retry", bug, {},
            f"event {event.name!r}: declared flag {flag_name!r} differs from the fixture")


class AttachmentUpdateHandler(ActionHandler):
    action = "attachment.update"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        # The same TINYTEXT column bug.attach guards, reached by a second writer. The
        # ceiling belongs to the column, not to the event that first wrote it: nothing
        # between the scenario and the row enforces it, because Bugzilla applies no
        # length validator to attachments.description (Bugzilla/Attachment.pm:578-584,
        # unlike bugs.short_desc at Bugzilla/Bug.pm:2046-2049) and removes
        # STRICT_TRANS_TABLES from the session sql_mode (Bugzilla/DB/MariaDB.pm:87-100),
        # so MariaDB truncates instead of refusing and bzr reports success.
        values = event.expected_postcondition["values"]
        if "description" not in values:
            return
        _reject_embedded_marker(event, "description", values["description"])
        size = len(values["description"].encode("utf-8"))
        if size > ATTACHMENT_SUMMARY_BYTE_LIMIT:
            raise _unsupported(
                event, "description",
                f"the declared attachment description is {size} bytes and Bugzilla's "
                f"attachments.description holds {ATTACHMENT_SUMMARY_BYTE_LIMIT}; "
                "shorten the description")

    def build(self, context: ReplayContext, event: PlannedEvent) -> Invocation:
        values = event.expected_postcondition["values"]
        attachment_id = context.resolve(values["attachment"])
        args = ["attachment", "update",
                "--obsolete" if values["obsolete"] else "--no-obsolete"]
        if "description" in values:
            args.append(f"--summary={values['description']}")
        return _bzr("attachment update", args, [attachment_id])

    def reconcile(self, context: ReplayContext, event: PlannedEvent) -> Reconciliation:
        values = event.expected_postcondition["values"]
        attachment_id = context.resolve(values["attachment"])
        payload = context.client(event.actor).read(
            ["attachment", "view"], positionals=[str(attachment_id)])
        attachment = _attachment_object(payload)
        if attachment is None:
            return Reconciliation(
                "retry", payload, {},
                f"event {event.name!r}: attachment {attachment_id} is unreadable")
        if attachment.get("is_obsolete") != values["obsolete"]:
            return Reconciliation(
                "retry", payload, {},
                f"event {event.name!r}: declared 'obsolete' differs from the fixture")
        if "description" in values and attachment.get("summary") != values["description"]:
            return Reconciliation(
                "retry", payload, {},
                f"event {event.name!r}: declared 'description' differs from the fixture")
        return Reconciliation("advance", payload, {}, "")


HANDLERS: dict[str, ActionHandler] = {
    handler.action: handler()
    for handler in (
        BugCreateHandler, BugUpdateHandler, BugCommentHandler, BugAttachHandler,
        BugWorktimeHandler, BugCustomFieldHandler, BugFlagHandler, AttachmentUpdateHandler,
    )
}
