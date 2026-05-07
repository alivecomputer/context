"""alive-demo generation pipeline stage helpers.

Each `stage<N>.py` module owns the *Python* side of one stage in the
generative pipeline:

  * Reading + substituting the per-stage prompt template.
  * Reading + substituting the shared `subagent-brief.md` preamble.
  * Wrapping the result in the canonical `CONTEXT:` / `TASK:` envelope.
  * Pre-flighting whatever the subagent writes to disk before the next
    stage / the validator (`validate.py`, fn-2-2zz.10) takes over.

The actual subagent dispatch (the `Task` tool call) is performed by the
runtime, not by these modules. Each helper returns a fully-rendered prompt
string that the skill router hands to the dispatch primitive.

Stage 1 (`stage1.py`) is the exception: it is UX-only (no LLM dispatch
in the loop body, no preflight). It owns the per-moment confirmation
loop helpers and the `anchor_moments.json` envelope on disk. The
parent skill drives the loop via `AskUserQuestion`; this module
mutates state on disk in lockstep.
"""
