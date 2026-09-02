"""Actor-scoped event replay with journal-backed safe resume (issue #6, ADR 0006)."""

from .context import ReplayContext, ReplayError

__all__ = ["ReplayContext", "ReplayError"]
