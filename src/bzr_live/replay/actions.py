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
    "groups": "bzr bug view does not return groups, so no delta can be computed and no "
              "result confirmed (finding D3)",
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
        if bug is None or not isinstance(bug.get("id"), int) or bug["id"] <= 0:
            raise ReplayError(
                f"event {event.name!r}: bzr bug create returned no bug id")
        return {f"bug:{event.creates.name}": bug["id"]}


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


class BugCommentHandler(ActionHandler):
    action = "bug.comment"

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


class BugAttachHandler(ActionHandler):
    action = "bug.attach"

    @staticmethod
    def check_supported(event: PlannedEvent) -> None:
        values = event.expected_postcondition["values"]
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
        if not isinstance(output, dict) or not isinstance(output.get("id"), int):
            raise ReplayError(
                f"event {event.name!r}: bzr attachment upload returned no attachment id")
        return {f"attachment:{event.creates.name}": output["id"]}


class BugWorktimeHandler(ActionHandler):
    action = "bug.worktime"

    def build(self, context: ReplayContext, event: PlannedEvent) -> Invocation:
        values = event.expected_postcondition["values"]
        bug_id = context.resolve(values["bug"])
        path = context.text_file(
            f"worktime-{event.name}",
            render_marker(values["comment"], event.reconciliation_marker))
        return _bzr("bug update", [
            "bug", "update", f"--work-time={values['hours']}",
            f"--comment-file={path}"], [bug_id])


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


class AttachmentUpdateHandler(ActionHandler):
    action = "attachment.update"

    def build(self, context: ReplayContext, event: PlannedEvent) -> Invocation:
        values = event.expected_postcondition["values"]
        attachment_id = context.resolve(values["attachment"])
        args = ["attachment", "update",
                "--obsolete" if values["obsolete"] else "--no-obsolete"]
        if "description" in values:
            args.append(f"--summary={values['description']}")
        return _bzr("attachment update", args, [attachment_id])


HANDLERS: dict[str, ActionHandler] = {
    handler.action: handler()
    for handler in (
        BugCreateHandler, BugUpdateHandler, BugCommentHandler, BugAttachHandler,
        BugWorktimeHandler, BugCustomFieldHandler, BugFlagHandler, AttachmentUpdateHandler,
    )
}
