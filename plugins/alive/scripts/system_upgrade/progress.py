"""Per-phase progress renderer.

Surfaces ``Phase 1: detection... done`` per-phase by default; ``-v``
adds step-level lines. The walkthrough phase (T8) calls ``pause()``
before the prompt fires and ``resume()`` after the prompt closes so
the prompt input is not corrupted by an in-flight progress line.

The renderer is intentionally minimal -- it writes to ``stderr`` (so
JSON output on stdout is not polluted) and flushes on every line so
the operator sees progress in real time even when the run is being
piped.
"""

from __future__ import annotations

import sys
from typing import IO, Optional


__all__ = ("ProgressRenderer",)


def _phase_label(name: str) -> str:
    """Format a numbered phase label per the spec contract.

    Returns ``Phase <N>: <name>`` when *name* matches the locked
    13-phase order; falls back to ``Phase: <name>`` for ad-hoc phase
    names a future task may surface. Importing PHASE_NUMBERS lazily
    avoids a circular import (orchestrator imports progress
    indirectly via the package).
    """
    try:
        from .orchestrator import PHASE_NUMBERS  # noqa: PLC0415
    except ImportError:
        return "Phase: {}".format(name)
    n = PHASE_NUMBERS.get(name)
    if n is None:
        return "Phase: {}".format(name)
    return "Phase {}: {}".format(n, name)


class ProgressRenderer:
    """Phase-aware progress writer with pause/resume support.

    Parameters
    ----------
    stream:
        Output stream (defaults to ``sys.stderr``). Tests inject a
        ``StringIO``.
    verbose:
        ``0`` -> phase-level only; ``>=1`` -> step-level too.
    enabled:
        ``False`` -> no-op renderer (every method is a noop). Useful
        when the orchestrator is invoked with ``--json`` and no human
        will read the progress stream.
    """

    def __init__(
        self,
        stream: Optional[IO[str]] = None,
        verbose: int = 0,
        enabled: bool = True,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._verbose = int(verbose)
        self._enabled = bool(enabled)
        self._paused = False
        self._current_phase: Optional[str] = None
        # Tracks whether the current phase line has been "closed" with
        # a "done"/"failed"/"skipped" marker. Used by pause/resume to
        # avoid leaking a partial line across a prompt.
        self._phase_open = False

    # ------------------------------------------------------------------
    # Phase / step writes
    # ------------------------------------------------------------------

    def phase_start(self, name: str) -> None:
        """Open a new phase line. ``name`` is the phase identifier."""
        if not self._enabled or self._paused:
            self._current_phase = name
            self._phase_open = False
            return
        self._stream.write("{}... ".format(_phase_label(name)))
        self._stream.flush()
        self._current_phase = name
        self._phase_open = True

    def phase_end(self, status: str = "done") -> None:
        """Close the current phase line with ``status``."""
        if not self._enabled:
            self._current_phase = None
            self._phase_open = False
            return
        if self._paused:
            # Will be flushed by resume() with the same status if needed;
            # for now, just track that the phase ended.
            self._current_phase = None
            self._phase_open = False
            return
        if self._phase_open:
            self._stream.write(status + "\n")
        else:
            # Phase was never visibly opened (e.g. paused at start).
            self._stream.write(
                "{}... {}\n".format(
                    _phase_label(self._current_phase or ""), status,
                )
            )
        self._stream.flush()
        self._current_phase = None
        self._phase_open = False

    def step(self, message: str) -> None:
        """Emit a step-level line (only at ``-v`` and above)."""
        if not self._enabled or self._paused:
            return
        if self._verbose < 1:
            return
        # If the current phase line is open, drop to a new line so the
        # step does not appear concatenated to "Phase: x... ".
        if self._phase_open:
            self._stream.write("\n")
            self._phase_open = False
        self._stream.write("  - {}\n".format(message))
        self._stream.flush()

    # ------------------------------------------------------------------
    # Pause / resume
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Pause the renderer for an interactive prompt.

        If a phase line is currently open, close it with a newline so
        the prompt does not start mid-line. The renderer remembers the
        pause state and silently drops writes until ``resume()``.
        """
        if not self._enabled:
            return
        if self._phase_open:
            # Close the open line with a newline so the prompt prints
            # cleanly. We mark the phase as no longer open but still
            # "current" so resume() can repaint it.
            self._stream.write("\n")
            self._stream.flush()
            self._phase_open = False
        self._paused = True

    def resume(self) -> None:
        """Resume rendering after an interactive prompt closes.

        If a phase was active when ``pause()`` was called, repaint the
        phase line so the operator sees where the run stands. Does not
        emit any duplicate "done"/"failed" markers from the pre-pause
        state (callers that want a fresh status call ``phase_end``
        themselves).
        """
        if not self._enabled:
            return
        self._paused = False
        if self._current_phase is not None and not self._phase_open:
            self._stream.write(
                "{}... ".format(_phase_label(self._current_phase))
            )
            self._stream.flush()
            self._phase_open = True

    # ------------------------------------------------------------------
    # Test seam
    # ------------------------------------------------------------------

    @property
    def paused(self) -> bool:
        return self._paused
