from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

from ..provision.adapters import ProvisionError
from ..scenario import (
    CompletedRecord,
    InFlightRecord,
    JournalStore,
    JsonValue,
    PlannedEvent,
    ValidatedScenario,
)
from ..scenario.journal import _ATTEMPT_FILE
from .actions import AMBIGUOUS_HINT, HANDLERS, Invocation, Reconciliation
from .context import ReplayContext, ReplayError

_RECONCILED_EXIT = -1     # the invocation's status was never observed (ADR 0006)

_Record = InFlightRecord | CompletedRecord | None


class ReplayEngine:
    """Preconditions, the ordered event loop, and every journal write for one run."""

    def __init__(self, scenario: ValidatedScenario, context: ReplayContext,
                 store: JournalStore, journal_dir: str | Path, *,
                 out: Callable[[str], None] = print) -> None:
        self._scenario = scenario
        self._context = context
        self._store = store
        self._journal_dir = journal_dir
        self._out = out

    # --- entry points ------------------------------------------------------

    def replay(self) -> list[tuple[str, str]]:
        latest = self._check_local_preconditions()
        self._require_empty_journal()
        self._sweep_pristine()
        return self._run(latest)

    def resume(self) -> list[tuple[str, str]]:
        return self._run(self._check_local_preconditions())

    # --- preconditions -----------------------------------------------------

    def _require_empty_journal(self) -> None:
        """Any attempt file at all, not just one this scenario still names.

        Scanning only the current event names would let a journal survive an event
        rename unseen -- and the digest check cannot fire on a record no event name
        reaches, because it reads records through those same names.
        """
        for name in sorted(os.listdir(self._journal_dir)):
            if _ATTEMPT_FILE.fullmatch(name) is not None:
                raise ReplayError(
                    f"this scenario has already been replayed under this state root "
                    f"(found journal record {name!r}); use resume, or reset the fixture "
                    "and remove the journal directory")

    def _check_local_preconditions(self) -> dict[str, _Record]:
        for event in self._scenario.events:
            HANDLERS[event.action].check_supported(event)
        self._require_no_stray_records()
        latest: dict[str, _Record] = {}
        for event in self._scenario.events:
            record = self._store.read(event.name)
            if record is not None and record.scenario_digest != self._scenario.digest:
                raise ReplayError(
                    f"event {event.name!r} was journalled under scenario digest "
                    f"{record.scenario_digest} but this scenario hashes to "
                    f"{self._scenario.digest}; restore the scenario, or reset the "
                    "fixture (CONFIRM_RESET=1 make reset) and replay")
            latest[event.name] = record
        return latest

    def _require_no_stray_records(self) -> None:
        """A record the scenario's event names no longer reach is still binding.

        The digest check reads records *through* current event names, so a journalled
        event since renamed is unreachable and its digest never compared -- the same
        blind spot `_require_empty_journal` closes for `replay`. Completion criterion 4
        says `resume` refuses unless the digest matches, unqualified, so the scan
        belongs on both paths. `_ATTEMPT_FILE`'s group 1 is the event name
        (`src/bzr_live/scenario/journal.py:45`).
        """
        known = {event.name for event in self._scenario.events}
        for name in sorted(os.listdir(self._journal_dir)):
            match = _ATTEMPT_FILE.fullmatch(name)
            if match is not None and match.group(1) not in known:
                raise ReplayError(
                    f"journal record {name!r} belongs to an event this scenario no "
                    "longer names, so its digest cannot be checked; restore the "
                    "scenario, or reset the fixture (CONFIRM_RESET=1 make reset) and "
                    "remove the journal directory")

    def _require_absent(self, event: PlannedEvent) -> None:
        """Refuse if this event's bug already exists. No-op for non-create events."""
        if event.action != "bug.create":
            return
        alias = event.expected_postcondition["values"]["server_alias"]
        # read_bug lets api_code 100/101 mean absent; 102 (access denied) still raises,
        # so an invisible pre-existing bug refuses instead of passing.
        if self._context.read_bug(event.actor, [alias]) is not None:
            raise ReplayError(
                f"bug alias {alias} (event {event.name!r}) already exists in the "
                "fixture; replay requires the pristine baseline -- run "
                "scripts/checkpoint restore pristine, then replay")

    def _sweep_pristine(self) -> None:
        for event in self._scenario.events:
            self._require_absent(event)

    # --- the loop ----------------------------------------------------------

    def _run(self, latest: Mapping[str, _Record]) -> list[tuple[str, str]]:
        report: list[tuple[str, str]] = []
        for event in self._scenario.events:
            status = self._advance(event, latest[event.name])
            report.append((status, event.name))
            self._out(f"{status} {event.name}")
        done = sum(1 for status, _ in report if status != "skipped")
        self._out(f"summary: {done} executed, {len(report) - done} already complete")
        return report

    def _advance(self, event: PlannedEvent, record: _Record) -> str:
        if record is None:
            # No journal record means no proof this run attempted the event, under
            # either subcommand -- so adoption is not available to it and a present
            # alias refuses. replay's up-front sweep is the same check, fail-fast.
            self._require_absent(event)
            return self._execute(event, 1)
        if isinstance(record, CompletedRecord):
            if record.next_safe_action == "advance":
                self._context.adopt(record.resolved_ids)
                return "skipped"
            if record.next_safe_action == "stop":
                raise ReplayError(
                    f"event {event.name!r} was recorded as ambiguous by an earlier run; "
                    f"{AMBIGUOUS_HINT}")
            if record.next_safe_action != "retry":
                # "reconcile" is legal in CompletedRecord's Literal but this engine
                # never writes it; a record it cannot interpret refuses rather than
                # falling through into a silent re-execution.
                raise ReplayError(
                    f"event {event.name!r} was recorded with next safe action "
                    f"{record.next_safe_action!r}, which this engine cannot act on; "
                    f"{AMBIGUOUS_HINT}")
            return self._execute(event, record.attempt + 1)
        # An in-flight record from an earlier run: settle it, then continue per its
        # answer.
        result = self._settle(event, record.attempt)
        if result.next_action == "advance":
            return "resumed"
        if result.next_action == "stop":
            raise ReplayError(f"event {event.name!r}: {result.detail}")
        return self._execute(event, record.attempt + 1)

    def _execute(self, event: PlannedEvent, attempt: int) -> str:
        self._context.actor_key(event.actor)      # fail fast, and register the secret
        invocation = HANDLERS[event.action].build(self._context, event)
        self._store.write_in_flight(
            InFlightRecord(
                self._scenario.digest, event.name, attempt, event.actor,
                event.action_class, event.expected_postcondition,
                event.reconciliation_marker),
            known_secrets=self._context.known_secrets)
        # ADR 0006 names three triggers for an in-run reconciliation, and all three land
        # here: a non-zero exit and an unparseable reply both raise ProvisionError out of
        # BzrClient, and an exit-0 reply carrying no usable identifier raises ReplayError
        # out of resolved_ids. The third is inside the `try` for that reason -- leaving
        # it out would abort the run with "returned no bug id" on a create that may well
        # have committed, and leave the answer to the operator's next `resume`.
        try:
            output = self._context.invoke(event.actor, invocation)
            ids = HANDLERS[event.action].resolved_ids(event, output)
        except (ProvisionError, ReplayError) as exc:
            result = self._settle(event, attempt, invocation=invocation)
            if result.next_action == "advance":
                return "reconciled"
            # One execution per event per run: the next attempt belongs to `resume`.
            raise ReplayError(
                f"event {event.name!r} failed: {exc}. {result.detail}") from None
        self._write_completed(event, attempt, invocation, output, 0, ids, "advance")
        self._context.adopt(ids)
        return "executed"

    def _settle(self, event: PlannedEvent, attempt: int, *,
                invocation: Invocation | None = None) -> Reconciliation:
        """Reconcile against the fixture and record the outcome.

        Returns `retry` or `stop` rather than raising, so the caller decides what each
        means: on a resumed in-flight record a `retry` means "execute attempt n+1 now",
        while after a failure this run already caused it means "abort -- the next
        attempt belongs to `resume`". A failing reconciliation *read* still propagates,
        because ADR 0006 requires that no completed record be written over a read that
        failed, leaving the in-flight record for a later `resume`.

        The invocation is rebuilt when it was not supplied, because a resumed in-flight
        record needs an `InvocationMetadata` for its completed record and the original
        run's is gone. Rebuilding is deterministic for every handler except `bug.update`,
        whose delta depends on a fresh read; that is correct, because the recorded
        metadata then describes what the resumed state actually implies.
        """
        result = HANDLERS[event.action].reconcile(self._context, event)
        invocation = invocation or HANDLERS[event.action].build(self._context, event)
        self._write_completed(
            event, attempt, invocation, result.output, _RECONCILED_EXIT,
            result.resolved_ids, result.next_action)
        if result.next_action == "advance":
            self._context.adopt(result.resolved_ids)
        return result

    def _write_completed(self, event: PlannedEvent, attempt: int,
                         invocation: Invocation, output: JsonValue, exit_status: int,
                         resolved_ids: Mapping[str, int], next_action: str) -> None:
        self._store.replace_completed(
            CompletedRecord(
                self._scenario.digest, event.name, attempt, event.actor,
                event.action_class, event.expected_postcondition,
                event.reconciliation_marker, invocation.metadata, output,
                exit_status, dict(resolved_ids), next_action),
            known_secrets=self._context.known_secrets)
