from __future__ import annotations

from collections.abc import Sequence

from ..provision.adapters import BUG_ABSENT_CODES
from ..replay.context import ReplayContext
from ..scenario import Reference
from . import VerifyError

# bzr's own ceiling on a recursive link walk (src/types/bug/links.rs:13). Above it the
# walk truncates and warns on stderr, which BzrClient.read discards on exit 0, so the
# bound is checked against the declared graph rather than trusted at read time.
LINKS_MAX_NODES = 1000

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


class ServerReader:
    """The five bzr read paths a verification needs, as one actor."""

    def __init__(self, context: ReplayContext, actor: Reference) -> None:
        self._client = context.client(actor)
        self._actor = actor.name

    def _object(self, args, positionals, absent_codes) -> object:
        payload = self._client.read(args, positionals=positionals,
                                    absent_codes=absent_codes)
        if payload is None:
            raise VerifyError(
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
        return self._list(["comment", "list"], bug_id)

    def attachments(self, bug_id: int) -> list:
        return self._list(["attachment", "list"], bug_id)
