"""``hermes`` surface (T7 of fn-18).

The 30-min discovery pass for Hermes (Open Q #4 in the epic spec)
established that:

* Hermes is **not** a CLI binary in this repo -- it is the
  ``NousResearch/hermes-agent`` host with which our memory-provider
  plugin + skills + cron-templates integrate. Layout under
  ``hermes/``:

      hermes/
        memory-provider/   plugin.yaml + Python smart-prefetch impl
        hermes-skills/     interactive skills (alive-create, ...)
        cron-templates/    scheduled hermes runs
        agents.md
        install.sh
        setup-crons.sh
        soul-patch.md

  The user installs hermes-agent separately, then copies our
  artifacts into ``~/.hermes/`` and edits ``~/.hermes/config.yaml``
  per ``hermes/README.md``.

* No ``hermes --version`` exists. State files (memory cache, skill
  history) live under ``~/.hermes/`` -- outside the world root, so
  the plugin-cleanup sweep doesn't touch them by default. There is
  no orchestrator-side migrator to invoke; any breaking change in
  our ``memory-provider`` schema is shipped via a fresh copy from
  the plugin tree (``cp -r hermes/memory-provider/* ~/.hermes/...``)
  rather than via a ``hermes upgrade`` subcommand.

T7 ships the **detect-only fallback** documented in the epic spec:
the surface returns ``compatible=False`` with an actionable handoff
message. The orchestrator records the surface entry but never
dispatches a migrator. If a future Hermes integration grows a real
CLI surface, this file gets extended; meanwhile the surfaces
registry has a placeholder for it.

Implementation note: The probe + dispatch bodies live in
``NotYetShippedSurface`` in ``_base.py``; this class only binds the
``name`` and the actionable Hermes-specific handoff message. The
zero-arg constructor pattern is preserved so direct callers doing
``HermesSurface()`` keep working.
"""

from __future__ import annotations

from system_upgrade.surfaces._base import NotYetShippedSurface


__all__ = ("HermesSurface", "HANDOFF_MESSAGE")


HANDOFF_MESSAGE: str = (
    "hermes integration is configuration-driven (no CLI version "
    "probe). To upgrade your hermes-agent integration, re-copy the "
    "plugin tree (`cp -r hermes/memory-provider/* "
    "~/.hermes/hermes-agent/plugins/memory/alive/`) and restart "
    "hermes-agent."
)


class HermesSurface(NotYetShippedSurface):
    """Hermes detect-only surface (thin zero-arg subclass)."""

    def __init__(self) -> None:
        super().__init__(name="hermes", handoff_message=HANDOFF_MESSAGE)
