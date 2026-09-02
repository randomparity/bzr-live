from __future__ import annotations

import base64
import binascii
import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from itertools import permutations

from . import Finding
from .expected import (
    CHAIN_FIELDS,
    INSIDER_GROUP,
    ExpectedAttachment,
    ExpectedBug,
    ExpectedChange,
    ExpectedComment,
    ExpectedFlag,
)


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


# Two independent bounds on a factorial enumeration. _MAX_BUCKET caps one bucket, so the
# largest single enumeration is 7! = 5,040. _SEARCH_BUDGET caps the whole search, across
# every bucket and every branch, so it is reached first on any input with more than one
# non-trivial bucket -- two buckets of 7 would cost up to 5,040 x 5,040. The bounds do not
# agree and are not meant to: the bucket cap rejects an input up front, the budget stops a
# search already under way, and an exhausted budget always answers "oversized".
_MAX_BUCKET = 7
_SEARCH_BUDGET = 10_000


def chain_order(records: list[dict],
                final_value: str | None) -> tuple[list[dict] | None, str | None]:
    """Order one field's history records by linking old_value to new_value.

    Records are bucketed by `when` and the buckets ordered by `when`, but the search is
    global: it permutes each bucket internally and requires the whole ordering to link
    head to tail and to end at `final_value`, the field's current value from `bug view`.
    Committing bucket by bucket does not work -- on the live reply for bug 9 the
    14:19:48Z bucket admits both orderings on its own, and only the :49Z record rules one
    out. `when` is a sort key and a search bound; it is never compared to anything,
    because Bugzilla's `ORDER BY bug_when` leaves ties unordered and the reply's own order
    therefore cannot be trusted.

    Candidate orderings are deduplicated by their (who, old_value, new_value) sequence, so
    two records identical in all three are interchangeable rather than two answers.

    Returns (ordering, None) on success, else (None, reason) where reason is "unlinked"
    (no ordering links -- a divergence), "ambiguous" (more than one does), or "oversized"
    (the search exceeded its bounds). The caller fails the run only on "unlinked".
    """
    if not records:
        # No records is not a broken chain. The caller already skips a field the fold
        # never declares a change to, and this is the second guard on the same case:
        # every bug's summary is seeded at create, which writes no history at all.
        return [], None
    buckets = [bucket for _when, bucket in sorted(_by_when(records).items())]
    if any(len(bucket) > _MAX_BUCKET for bucket in buckets):
        return None, "oversized"
    solutions: list[list[dict]] = []
    budget = _SEARCH_BUDGET

    def walk(index: int, carried: str | None, acc: list[dict]) -> None:
        nonlocal budget
        if len(solutions) > 1 or budget <= 0:
            return
        if index == len(buckets):
            if final_value is None or carried == final_value:
                solutions.append(acc)
            return
        seen: set[tuple] = set()
        for permutation in permutations(buckets[index]):
            budget -= 1
            if budget <= 0:
                return
            key = tuple(
                (r["who"], r["old_value"], r["new_value"]) for r in permutation)
            if key in seen:
                continue
            seen.add(key)
            if _links(permutation, carried):
                walk(index + 1, permutation[-1]["new_value"], acc + list(permutation))

    walk(0, None, [])
    # Order matters, and it is the budget that comes first. An abort does not distinguish a
    # dead branch from an unvisited one, so a search that recorded one solution and then ran
    # out of budget has not shown that solution is unique -- a second linking ordering may
    # sit in the part never reached. Returning it as unique would feed a wrong actor
    # sequence to the subsequence assertion and report an ordering proof that was never
    # obtained. ADR 0008 promises "a search too large to settle (oversized) ... reported
    # unverifiable rather than guessed", and this is the line that keeps that promise.
    if budget <= 0:
        return None, "oversized"
    if len(solutions) == 1:
        return solutions[0], None
    if len(solutions) > 1:
        return None, "ambiguous"
    return None, "unlinked"


def _by_when(records: list[dict]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {}
    for record in records:
        buckets.setdefault(record["when"], []).append(record)
    return buckets


def _links(permutation, carried: str | None) -> bool:
    previous = carried
    for record in permutation:
        if previous is not None and record["old_value"] != previous:
            return False
        previous = record["new_value"]
    return True


# One line per non-answer, so the report names the reason as well as the field.
_CHAIN_DETAIL = {
    "unlinked": "unlinked: no ordering of the {count} observed records links to {final}",
    "ambiguous": ("ambiguous: more than one ordering of the {count} observed records "
                  "links to {final}"),
    "oversized": ("oversized: the search over the {count} observed records exceeded its "
                  "bounds"),
}


def _shapes(bug: ExpectedBug) -> dict[str, object]:
    """field -> the representation the fold chose for it.

    `ExpectedChange` fixes one representation per field, so any declared change for a
    field settles how every observed record for it is keyed: `None` for a generated-id
    field, a frozenset for a comma-joined set field, a string otherwise.
    """
    return {change.field: change.value for change in bug.history}


def _observed_key(record: Mapping[str, object], shapes: Mapping[str, object]) -> tuple:
    field = record["field"]
    declared = shapes.get(field, "")
    if declared is None:
        # depends_on and blocks carry comma-joined generated ids, so only the attribution
        # is assertable; check_links proves the edge itself.
        return (record["who"], field)
    if isinstance(declared, frozenset):
        return (record["who"], field, frozenset(str(record["new_value"]).split(", ")))
    return (record["who"], field, record["new_value"])


def _declared_key(change: ExpectedChange, who: str) -> tuple:
    if change.value is None:
        return (who, change.field)
    return (who, change.field, change.value)


def _change_value(change: ExpectedChange) -> str:
    return "(any value)" if change.value is None else _show(change.value)


def _attribution(bug: ExpectedBug, records: list,
                 emails: Mapping[str, str]) -> list[Finding]:
    """Multiset containment of the declared changes in the observed records."""
    shapes = _shapes(bug)
    observed = Counter(_observed_key(record, shapes) for record in records)
    findings = []
    for change, needed in Counter(bug.history).items():
        who = emails[change.actor]
        seen = observed[_declared_key(change, who)]
        if seen < needed:
            findings.append(Finding(
                "divergence", bug.alias, change.field,
                f"declared {who} | {change.field} | {_change_value(change)}, "
                f"observed {seen} of {needed} records"))
    return findings


def _subsequence(wanted: Sequence[str], actual: Sequence[str]) -> bool:
    # `in` on a live iterator consumes up to and including the match, so each item is
    # sought only in what follows the previous one.
    remaining = iter(actual)
    return all(item in remaining for item in wanted)


def _ordering(bug: ExpectedBug, records: list, observed_fields: Mapping[str, object],
              emails: Mapping[str, str]) -> list[Finding]:
    findings = []
    for field in sorted(CHAIN_FIELDS):
        declared = [c for c in bug.history if c.field == field and c.chain]
        seen = [r for r in records if r["field"] == field]
        # Both halves matter: bug.create seeds summary, target_milestone and assigned_to
        # into the field expectations while writing no bugs_activity row at all, so a
        # guard keyed on the bug declaring the field would order an empty record list.
        if not declared or not seen:
            continue
        # Every CHAIN_FIELDS name is its own `bug view` key -- expected._SCALARS maps each
        # of the five to itself -- so the tail anchor needs no further translation.
        ordered, reason = chain_order(seen, observed_fields.get(field))
        if reason is not None:
            kind = "divergence" if reason == "unlinked" else "unverifiable"
            findings.append(Finding(kind, bug.alias, field, _CHAIN_DETAIL[reason].format(
                count=len(seen), final=_show(observed_fields.get(field)))))
            continue
        wanted = [emails[change.actor] for change in declared]
        actual = [record["who"] for record in ordered]
        if not _subsequence(wanted, actual):
            findings.append(Finding(
                "divergence", bug.alias, field,
                f"declared actor sequence {' -> '.join(wanted)}, "
                f"observed {' -> '.join(actual)}"))
    return findings


def check_history(bug: ExpectedBug, records: list, observed_fields: Mapping[str, object],
                  emails: Mapping[str, str]) -> list[Finding]:
    """Declared changes and their order on one bug, against one `bug history` reply.

    Attribution is multiset containment rather than equality, because the server writes
    records the scenario never declared: the inverse `blocks` edge, the `cc` record
    beside a flag requestee, and the status and resolution a duplicate marking drives.
    Ordering is reconstructed by chain-linking; no assertion compares a `when`.

    `observed_fields` is the `bug view` reply the field check already read, which supplies
    each chain field's current value as the tail anchor. `emails` projects a declared
    actor alias to the login `who` carries -- the plan's published signature omitted it,
    but `ExpectedChange.actor` holds an alias and no other argument can resolve one, so
    this mirrors the projection `check_fields` already takes for a flag requestee.
    """
    return [*_attribution(bug, records, emails),
            *_ordering(bug, records, observed_fields, emails)]


def _edge_spec(record: Mapping[str, object],
               alias_of: Mapping[int, str]) -> tuple[str, str, str]:
    """One `bug links` record as the (alias, relation, direction) triple it compares as."""
    return (_alias(record["id"], alias_of), str(record["relation"]),
            str(record["direction"]))


def _direct(alias: str, declared: frozenset[tuple[str, str, str]], direct: list,
            alias_of: Mapping[int, str]) -> list[Finding]:
    seen = {_edge_spec(record, alias_of) for record in direct}
    details = [_detail(" ".join(edge), None) for edge in sorted(declared - seen)]
    details += [_detail(None, " ".join(edge)) for edge in sorted(seen - declared)]
    return [Finding("divergence", alias, "links", detail) for detail in details]


def _hop(other: str, depth: object, *, named: bool) -> str | None:
    """One side of a reachability detail. The node is named once, not on both sides."""
    if depth is None:
        return None
    return f"{other} at depth {depth}" if named else f"depth {depth}"


def _reachability(alias: str, declared_hops: Mapping[str, int], walk: list,
                  alias_of: Mapping[int, str]) -> list[Finding]:
    seen = {_alias(record["id"], alias_of): record["depth"] for record in walk}
    findings = []
    for other in sorted(set(declared_hops) | set(seen)):
        declared, observed = declared_hops.get(other), seen.get(other)
        if declared == observed:
            continue
        findings.append(Finding("divergence", alias, "reachability", _detail(
            _hop(other, declared, named=True),
            _hop(other, observed, named=declared is None))))
    return findings


def check_links(alias: str, declared_direct: frozenset[tuple[str, str, str]],
                declared_hops: Mapping[str, int], direct: list, walk: list,
                alias_of: Mapping[int, str]) -> list[Finding]:
    """The declared topology around one bug, against its two `bug links` replies.

    `direct` is `bug links <id>`, compared as a set of (alias, relation, direction) with
    the inverses `link_edges` materialises; an observed edge to a bug the scenario does
    not name is a divergence like any other, which is why this family stays unconditional.

    `walk` is `bug links <id> --recursive --depth <d>`, compared as {alias: depth} against
    `declared_hops`. Neither `relation` nor `direction` is read from it: bzr sorts its
    frontier by bug id (bzr src/commands/bug/links.rs:42), so the relation credited to a
    node reachable two ways depends on generated identifiers -- exactly what no assertion
    here may rest on. Depth is a property of the graph; the relation is proven by the
    depth-1 direct read. The caller skips the recursive read when the root's eccentricity
    is 1 or 0, and then passes an empty `declared_hops` with an empty `walk`: the two are
    one decision, and a populated `declared_hops` beside an empty `walk` reads as a graph
    the fixture does not hold.
    """
    return [*_direct(alias, declared_direct, direct, alias_of),
            *_reachability(alias, declared_hops, walk, alias_of)]


def _located(entries: list, key: str, marker: str) -> list[Mapping[str, object]]:
    """The entries whose `key` carries the marker token.

    Every append-class event stamps `[<marker>]` into the body or summary it writes
    (src/bzr_live/replay/actions.py), so the token is the only handle that survives a
    replay without depending on a generated id or on the reply's order.
    """
    token = f"[{marker}]"
    return [entry for entry in entries
            if isinstance(entry, Mapping) and token in str(entry.get(key, ""))]


def _entry_fields(bug: ExpectedBug, check: str, subject: str,
                  entry: Mapping[str, object],
                  declared: Mapping[str, object]) -> list[Finding]:
    return [Finding("divergence", bug.alias, check,
                    f"{subject} {key}: {_detail(value, entry.get(key))}")
            for key, value in declared.items() if entry.get(key) != value]


def _comment_zero(bug: ExpectedBug, declared: ExpectedComment, entries: list,
                  emails: Mapping[str, str]) -> list[Finding]:
    """The create description, which Bugzilla stores as comment 0 (Bug.pm:828)."""
    entry = next((e for e in entries
                  if isinstance(e, Mapping) and e.get("count") == 0), None)
    if entry is None:
        return [Finding("divergence", bug.alias, "comments",
                        "declared a create description, observed no comment 0")]
    return _entry_fields(bug, "comments", "comment 0", entry,
                         {"creator": emails[declared.author], "text": declared.text})


def _comment_order(bug: ExpectedBug,
                   matched: list[tuple[str, object]]) -> list[Finding]:
    """Declaration order against the `count` values, the reply's only ordering key.

    No assertion compares a timestamp: `bzr` emits `creation_time` alone and the fixture
    posts several comments inside one second, so only `count` orders a thread.
    """
    findings = []
    for (before, earlier), (marker, count) in zip(matched, matched[1:]):
        if isinstance(count, int) and isinstance(earlier, int) and count < earlier:
            findings.append(Finding(
                "divergence", bug.alias, "comments",
                f"[{marker}] is declared after [{before}] but observed at count "
                f"{count}, ahead of its count {earlier}"))
    return findings


def check_comments(bug: ExpectedBug, comments: list,
                   emails: Mapping[str, str]) -> list[Finding]:
    """One bug's declared thread against one insider `comment list` reply.

    Comments are located by marker token, never by index or id: Bugzilla injects its own
    comments into the thread -- the duplicate notice at `*** Bug N has been marked as a
    duplicate ***` and the `Created attachment N` notice -- so the declared comments are
    not a prefix of the observed ones and their counts are not contiguous.
    """
    findings: list[Finding] = []
    matched: list[tuple[str, object]] = []
    for declared in bug.comments:
        if declared.marker is None:
            findings += _comment_zero(bug, declared, comments, emails)
            continue
        entries = _located(comments, "text", declared.marker)
        if len(entries) != 1:
            findings.append(Finding(
                "divergence", bug.alias, "comments",
                f"[{declared.marker}] declared once, observed {len(entries)} times"))
            continue
        findings += _entry_fields(
            bug, "comments", f"[{declared.marker}]", entries[0],
            {"creator": emails[declared.author], "is_private": declared.private})
        matched.append((declared.marker, entries[0].get("count")))
    return findings + _comment_order(bug, matched)


def check_visibility(bug: ExpectedBug, comments: list) -> list[Finding]:
    """One bug's thread as an actor outside the insider group reads it.

    Both directions are divergences. A private comment the outsider can read is the
    leak the scenario declares against; a public one they cannot read is an
    over-restrictive fixture, which would otherwise let a thread that hides everything
    pass this check.
    """
    text = "".join(str(entry.get("text", "")) for entry in comments
                   if isinstance(entry, Mapping))
    findings = []
    for declared in bug.comments:
        if declared.marker is None:
            continue
        readable = f"[{declared.marker}]" in text
        if declared.private == readable:
            state = "readable by" if readable else "withheld from"
            findings.append(Finding(
                "divergence", bug.alias, "visibility",
                f"[{declared.marker}] is declared "
                f"{'private' if declared.private else 'public'} but is {state} an actor "
                f"outside the {INSIDER_GROUP!r} group"))
    return findings


def _checksum(bug: ExpectedBug, declared: ExpectedAttachment,
              entry: Mapping[str, object]) -> list[Finding]:
    subject = f"attachment {declared.alias!r}"
    if "data" not in entry:
        return [Finding(
            "unverifiable", bug.alias, "attachments",
            f"{subject}: the reply carries no data, so the stored bytes cannot be "
            "hashed; bzr requests data only on the XML-RPC arm (finding D9)")]
    try:
        raw = base64.b64decode(str(entry["data"]), validate=True)
    except (binascii.Error, ValueError):
        return [Finding("divergence", bug.alias, "attachments",
                        f"{subject}: the reply's data is not valid base64")]
    digest = hashlib.sha256(raw).hexdigest()
    if digest == declared.sha256:
        return []
    return [Finding("divergence", bug.alias, "attachments",
                    f"{subject} sha256: {_detail(declared.sha256, digest)}")]


def check_attachments(bug: ExpectedBug, attachments: list,
                      emails: Mapping[str, str]) -> list[Finding]:
    """One bug's declared attachments against one `attachment list` reply.

    The summary carries the marker and the declared digest, so metadata and content are
    both anchored to the scenario rather than to an id the server generated.
    """
    findings = []
    for declared in bug.attachments:
        entries = _located(attachments, "summary", declared.marker)
        if len(entries) != 1:
            findings.append(Finding(
                "divergence", bug.alias, "attachments",
                f"[{declared.marker}] declared once, observed {len(entries)} times"))
            continue
        findings += _entry_fields(
            bug, "attachments", f"attachment {declared.alias!r}", entries[0], {
                "summary": declared.summary,
                "creator": emails[declared.author],
                "content_type": declared.content_type,
                "is_private": declared.private,
                "is_obsolete": declared.obsolete,
            })
        findings += _checksum(bug, declared, entries[0])
    return findings
