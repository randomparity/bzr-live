from __future__ import annotations

from ..scenario import PlannedEvent
from .context import ReplayError

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


class BugCommentHandler(ActionHandler):
    action = "bug.comment"


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


class BugWorktimeHandler(ActionHandler):
    action = "bug.worktime"


class BugCustomFieldHandler(ActionHandler):
    action = "bug.custom-field-set"
    boundary = "bugzilla-rest-custom-field"


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


class AttachmentUpdateHandler(ActionHandler):
    action = "attachment.update"


HANDLERS: dict[str, ActionHandler] = {
    handler.action: handler()
    for handler in (
        BugCreateHandler, BugUpdateHandler, BugCommentHandler, BugAttachHandler,
        BugWorktimeHandler, BugCustomFieldHandler, BugFlagHandler, AttachmentUpdateHandler,
    )
}
