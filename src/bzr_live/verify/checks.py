from __future__ import annotations

from collections.abc import Mapping, Sequence

from . import Finding
from .expected import ExpectedBug, ExpectedFlag


def _show(value: object) -> str:
    """One side of a detail string. Absence is named, never rendered as None."""
    if value is None:
        return "(absent)"
    if isinstance(value, (frozenset, set, list, tuple)):
        return ", ".join(sorted(str(item) for item in value)) or "(none)"
    return str(value)


def _detail(declared: object, observed: object) -> str:
    return f"declared {_show(declared)}, observed {_show(observed)}"


def _alias(bug_id: object, alias_of: Mapping[int, str]) -> str:
    """A server id as the alias the scenario names it by.

    An id the scenario never named still renders symbolically enough to read: the report
    must never print a bare number a reader would have to resolve against the journal.
    """
    named = alias_of.get(bug_id) if isinstance(bug_id, int) else None
    return named or f"bug id {bug_id} (not named by this scenario)"


def _same(declared: str, observed: object) -> bool:
    """A declared scalar against the value `bzr bug view` returns for it.

    Two of the built-ins are not text on the wire at the revision README pins (63abb94e).
    `component` and `version` deserialize through `deserialize_optional_string_list` and
    serialize back as arrays (bzr src/types/bug.rs:50-51, 225-226), and `estimated_time`
    is an f64 (src/types/bug.rs:68), so a scenario's "8" arrives as 8.0. Unwrapping a
    one-element array and comparing numbers as numbers compares the value the scenario
    declared; a genuinely different value still diverges.
    """
    if isinstance(observed, list) and len(observed) == 1:
        observed = observed[0]
    if declared == observed:
        return True
    try:
        return float(declared) == float(observed)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _unverifiable(bug: ExpectedBug) -> list[Finding]:
    return [Finding("unverifiable", bug.alias, field, reason)
            for field, reason in bug.unverifiable]


def _scalars(bug: ExpectedBug, observed: Mapping[str, object]) -> list[Finding]:
    return [Finding("divergence", bug.alias, key, _detail(value, observed.get(key)))
            for key, value in bug.scalars.items()
            if key not in bug.unasserted and not _same(value, observed.get(key))]


def _names(bug: ExpectedBug, observed: Mapping[str, object]) -> list[Finding]:
    findings = []
    for key, declared in bug.names.items():
        seen = {str(item) for item in observed.get(key) or ()}
        # cc is the one set compared by containment. PR #23 named a flag requestee
        # landing on the CC list as a postcondition that is not the server's final
        # state and excluded asserting against it, and pay-retry-loop's whole observed
        # CC set is server-derived -- equality there would assert the exclusion itself.
        diverged = not declared <= seen if key == "cc" else declared != seen
        if diverged:
            findings.append(
                Finding("divergence", bug.alias, key, _detail(declared, seen)))
    return findings


def _edges(bug: ExpectedBug, observed: Mapping[str, object],
           alias_of: Mapping[int, str]) -> list[Finding]:
    findings = []
    for key, declared in bug.edges.items():
        seen = {_alias(item, alias_of) for item in observed.get(key) or ()}
        if declared != seen:
            findings.append(
                Finding("divergence", bug.alias, key, _detail(declared, seen)))
    return findings


def _duplicate(bug: ExpectedBug, observed: Mapping[str, object],
               alias_of: Mapping[int, str]) -> list[Finding]:
    if bug.duplicate_of is None:
        return []
    raw = observed.get("dupe_of")
    seen = None if raw is None else _alias(raw, alias_of)
    if seen == bug.duplicate_of:
        return []
    return [Finding("divergence", bug.alias, "dupe_of",
                    _detail(bug.duplicate_of, seen))]


def _custom_fields(bug: ExpectedBug, observed: Mapping[str, object]) -> list[Finding]:
    findings = []
    for key, declared in bug.custom_fields.items():
        if key not in observed:
            # The --fields request named this key, so its absence is the fixture
            # disagreeing with the scenario, not a value bzr cannot read back.
            findings.append(
                Finding("divergence", bug.alias, key, _detail(declared, None)))
            continue
        raw = observed[key]
        seen = {str(item) for item in raw or ()} if isinstance(
            declared, frozenset) else raw
        if seen != declared:
            findings.append(
                Finding("divergence", bug.alias, key, _detail(declared, seen)))
    return findings


def _flag_spec(name: object, status: object, requestee: object) -> str:
    """A flag as the `name?(requestee)` syntax both sides of the fixture speak."""
    return f"{name}{status}" + (f"({requestee})" if requestee else "")


def _matches(flag: ExpectedFlag, named: Sequence[Mapping[str, object]],
             requestee: str | None) -> bool:
    if flag.status == "X":
        # A clear leaves no entry with this type name behind; requiring a present X
        # entry would make a declared clear impossible to satisfy. Mirrors the replay
        # reconciler (src/bzr_live/replay/actions.py:618-621).
        return not named
    return any(
        entry.get("status") == flag.status
        and (requestee is None or entry.get("requestee") == requestee)
        for entry in named)


def _flags(bug: ExpectedBug, observed: Mapping[str, object],
           emails: Mapping[str, str]) -> list[Finding]:
    entries = [entry for entry in observed.get("flags") or ()
               if isinstance(entry, dict)]
    findings = []
    for flag in bug.flags:
        # ExpectedFlag.requestee is the actor alias, the one declared value the fold
        # leaves unprojected; bzr returns the requestee's login
        # (bzr src/types/flag.rs:85 at 63abb94e).
        requestee = None if flag.requestee is None else emails[flag.requestee]
        named = [entry for entry in entries if entry.get("name") == flag.name]
        if _matches(flag, named, requestee):
            continue
        seen = ", ".join(sorted(
            _flag_spec(entry.get("name"), entry.get("status"), entry.get("requestee"))
            for entry in named)) or None
        findings.append(Finding(
            "divergence", bug.alias, "flags",
            _detail(_flag_spec(flag.name, flag.status, requestee), seen)))
    return findings


def check_fields(bug: ExpectedBug, observed: dict, alias_of: Mapping[int, str],
                 emails: Mapping[str, str]) -> list[Finding]:
    """Every declared field, custom field and flag on one bug, against one `bug view`.

    `observed` is the reply to a read of `VIEW_FIELDS + custom_field_keys`, so a declared
    cf_* name was requested by name and its absence is a divergence. `alias_of` inverts
    the journal's resolved ids so no detail carries a bare server id; `emails` projects a
    declared flag requestee, the one declared value the fold leaves as an actor alias.
    """
    return [
        *_unverifiable(bug),
        *_scalars(bug, observed),
        *_names(bug, observed),
        *_edges(bug, observed, alias_of),
        *_duplicate(bug, observed, alias_of),
        *_custom_fields(bug, observed),
        *_flags(bug, observed, emails),
    ]
