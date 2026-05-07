"""``codex`` surface stub (T7 of fn-18).

Per the epic spec, the Codex plugin migrator is OUT OF SCOPE for v3.2
-- this surface is a stub interface only. It always returns
``compatible=False, version=None,
probe_error=ProbeError(kind="not_yet_shipped", ...)`` and never
claims dispatch.

When a real Codex CLI surface ships in a later release, this file
gets replaced with a real ``probe()`` (subprocess-based, contract
identical to ``alive_mcp``) + ``dispatch()``. Until then, the
registry needs a placeholder so the ``--surfaces=codex`` filter has
something to bind to.

Implementation note: The probe + dispatch bodies live in
``NotYetShippedSurface`` in ``_base.py``; this class only binds the
``name`` and the codex-specific handoff message. The zero-arg
constructor pattern is preserved so direct callers doing
``CodexSurface()`` keep working.
"""

from __future__ import annotations

from system_upgrade.surfaces._base import NotYetShippedSurface


__all__ = ("CodexSurface",)


_CODEX_HANDOFF_MESSAGE: str = (
    "codex surface is not yet shipped; the Codex "
    "plugin migrator is deferred to a future release"
)


class CodexSurface(NotYetShippedSurface):
    """Codex stub -- always reports not-yet-shipped (thin zero-arg subclass)."""

    def __init__(self) -> None:
        super().__init__(name="codex", handoff_message=_CODEX_HANDOFF_MESSAGE)
