from __future__ import annotations

from collections.abc import Sequence

from ..provision.adapters import BUG_ABSENT_CODES
from ..replay.context import ReplayContext
from ..scenario import Reference
from . import ReadNotFound, VerifyError

# bzr's own ceiling on a recursive link walk (src/types/bug/links.rs:13). Above it the
# walk truncates and warns on stderr, which BzrClient.read discards on exit 0, so the
# bound is checked against the declared graph rather than trusted at read time.
LINKS_MAX_NODES = 1000

# bzr's ceiling on --depth, declared by clap as `value_parser!(u32).range(1..=10)` on
# LinksArgs::depth (src/cli/bug/links.rs:30-38 at 63abb94e). Above it bzr rejects the
# argument and exits 2 -- the same status it uses for not-found, which BzrClient.read maps
# to absent. An out-of-range depth would therefore arrive at the verifier indistinguishable
# from a missing bug, and on a group-restricted bug Verifier._links would report it as
# finding D12. Bounded against the declared graph before any read, so that argument is never
# constructed; measured: `bug links --recursive --depth=11 -- 1` exits 2 with "11 is not in
# 1..=10".
LINKS_MAX_DEPTH = 10

# The explicit field list every bug read requests. Without it `bzr bug view` omits every
# cf_* field; with it, `bug view 1 --fields id,summary,cf_risk` returns them. Requesting
# a field bzr does not know costs only a stderr warning, so the list stays declarative.
# groups and estimated_time need bzr a7f6ab70 or later (PR #646, closing bzr#641); below
# that they warn and are dropped, which is finding D3 and the reason README pins 63abb94e.
VIEW_FIELDS = (
    "id", "summary", "status", "resolution", "dupe_of", "product", "component",
    "version", "assigned_to", "keywords", "blocks", "depends_on", "cc",
    "target_milestone", "flags", "groups", "estimated_time",
)

# The transport the comment and attachment reads ask for. bzr picks its default from the
# server's version -- version_to_api_mode maps >= 5.1 to rest (bzr src/client/version.rs)
# and dispatch_xmlrpc_first then returns rest() without attempting XML-RPC at all
# (src/client/mod.rs:262-280) -- and this fixture answers 5.2+, so the XML-RPC arm both
# reads document as their only source is unreachable by default. That is finding D9, and
# it costs two of this issue's criteria: `attachment list` requests the attachment body
# only on that arm (src/xmlrpc/resources/attachment.rs:12-27, against exclude_fields=data
# on the REST arm at src/client/resources/attachment.rs:163), and `comment list` only
# authenticates the caller there, so REST drops a private comment the insider may read
# (finding D8 compounding D9).
#
# `--api hybrid` is bzr's own documented setting for exactly this: `bzr --help` states
# that under hybrid "comments and attachments use XML-RPC first to preserve private-data
# behavior", and that the auto-detected default is "hybrid for 5.0.x, rest for >= 5.1".
# It configures the client for the data being read; it substitutes no declared value and
# swaps no command. The reads that do not need it keep the default, because `comment
# list`, `bug view`, `bug history` and `bug links` are byte-identical across the two
# transports on a thread with no private comment -- verified live at 63abb94e.
#
# The package Task 0 added to the image is still required and not redundant with this:
# `--api hybrid` makes bzr *attempt* XML-RPC, and libxmlrpc-lite-perl makes the fixture
# *answer* it. Neither alone reaches the data.
HYBRID_API = ("--api", "hybrid")


class ServerReader:
    """The five bzr read paths a verification needs, as one actor."""

    def __init__(self, context: ReplayContext, actor: Reference) -> None:
        self._client = context.client(actor)
        self._actor = actor.name

    def _object(self, args, positionals, absent_codes) -> object:
        payload = self._client.read(args, positionals=positionals,
                                    absent_codes=absent_codes)
        if payload is None:
            # ReadNotFound, not a bare VerifyError: a caller that can explain why an
            # object was not found must be able to select this case without also
            # catching the unrecognised-shape refusals below, which say nothing about
            # whether the object exists.
            raise ReadNotFound(
                f"{' '.join(args)} {' '.join(positionals)} as {self._actor} reported "
                "not-found; the journal names a bug the fixture does not hold")
        return payload

    def bug(self, bug_id: int, fields: Sequence[str]) -> dict:
        payload = self._object(
            ["bug", "view", f"--fields={','.join(fields)}"], [str(bug_id)],
            BUG_ABSENT_CODES)
        if not isinstance(payload, dict) or "id" not in payload:
            raise VerifyError(f"bzr bug view {bug_id} returned an unrecognised shape")
        return payload

    def _list(self, args: list[str], bug_id: int) -> list:
        payload = self._object(args, [str(bug_id)], frozenset())
        if not isinstance(payload, list):
            raise VerifyError(
                f"bzr {' '.join(args)} {bug_id} returned an unrecognised shape")
        return payload

    def history(self, bug_id: int) -> list:
        return self._list(["bug", "history"], bug_id)

    def links(self, bug_id: int, depth: int | None = None) -> list:
        args = ["bug", "links"]
        if depth is not None:
            args += ["--recursive", f"--depth={depth}"]
        return self._list(args, bug_id)

    def comments(self, bug_id: int) -> list:
        return self._list([*HYBRID_API, "comment", "list"], bug_id)

    def attachments(self, bug_id: int) -> list:
        return self._list([*HYBRID_API, "attachment", "list"], bug_id)
