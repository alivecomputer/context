"""Stage 0 — spine generator: dispatch helpers + structural validation.

Stage 0 of the `/alive:demo` generation pipeline turns a free-text persona
description into a `spine.json` document on disk. The LLM-facing prompt
lives at `templates/demo/stage_prompts/stage_0_spine.v1.md`; the optional
description-summarizer sub-stage lives at
`templates/demo/stage_prompts/summarize_description.v1.md`. This module
wires them together for the dispatching squirrel.

Responsibilities owned here:

  1. **Prompt rendering** — read template + brief, substitute
     `{WORLD_ROOT}` / `{PLUGIN_ROOT}` / `{{description}}` / `{{size}}` /
     `{{output_path}}` / `{{subagent_brief}}`, wrap in the canonical
     `CONTEXT:` / `TASK:` envelope from the spike's wrapper convention.

  2. **Description-length triage** — if the description exceeds the soft
     ~4 000-token cap (`len(text.split()) * 1.3` heuristic per the task
     spec's locked decision), return the summarizer prompt instead so the
     dispatcher can fan out a summarizer subagent first; the spine prompt
     is rendered against the summary on the second pass.

  3. **Persisting the raw input** — full description retained at
     `<partial>/_input/persona-description.md` even when summarized so
     the build log + Stage 1 confirmations can still reference the
     original text. (Locked decision per the task spec key context.)

  4. **Structural validation** — after the subagent writes
     `<partial>/_stage_outputs/spine.json`, `preflight_spine` parses the
     file and enforces the full structural contract documented in
     `templates/demo/schema/spine.schema.md` (and described in
     machine-readable Draft 2020-12 form at
     `templates/demo/schema/spine.schema.json` for portable downstream
     consumption — Stage 0 itself does NOT use `jsonschema` per the
     epic's stdlib-only validation decision):

       - Top-level required keys (and `additionalProperties: false`).
       - `schema_version == "0.1"`.
       - Every per-object key set is closed (no extra fields).
       - Every enum value in scope (walnut type / domain_dir / status,
         bundle status, session_cadence pattern) is one of the
         documented values.
       - `time_span.start` / `time_span.end` parse as ISO 8601
         `YYYY-MM-DD` and `start <= end`.
       - Every `anchor_moments[*].date` is also a valid ISO 8601 date.
       - `session_cadence.sessions_per_week` is a positive number `<= 14`.
       - Every slug across persona / walnut / people / bundle / anchor
         rosters satisfies `lib.is_valid_slug`.

     **Coherence** invariants — anchor dates within `time_span`,
     relationship-edge endpoints existing in the people roster,
     walnut-must-have-bundle for non-`minimal-life` walnuts,
     cross-roster slug uniqueness — are deferred to `validate.py`
     (fn-2-2zz.10). The split is "structure here, references there":
     pre-flight catches anything wrong with a single object's shape;
     `validate.py` catches anything wrong about how objects refer to
     each other.

The stamping of `schema_version` is **assertive, not coercive**: the
subagent is told to write `"0.1"` in the prompt, and pre-flight rejects
anything else. This catches a model that writes `"1.0"` or omits the
field entirely without silently rewriting the file under it.

Stdlib-only. Imports `lib.is_valid_slug` (sibling) and (transitively
through path bootstrap) `_common.atomic_write_text` for the
description-on-disk write.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Path bootstrap — mirrors `state.py`'s pattern so direct imports under tests
# resolve `_common` without going through `cli.py`.
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.normpath(os.path.join(_HERE, os.pardir))
_PLUGIN_ROOT = os.path.normpath(os.path.join(_DEMO_DIR, os.pardir, os.pardir))
_SCRIPTS = os.path.join(_PLUGIN_ROOT, "scripts")
if os.path.isdir(_SCRIPTS) and _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from _common import atomic_write_json, atomic_write_text, iso_now, resolve_plugin_root  # noqa: E402


def _load_lib():
    """Load the demo `lib.py` sibling under a namespaced module key.

    Mirrors the cli_register / test loader pattern: a generic name like
    `lib` would clash if a future plugin shipped its own; the namespaced
    `alive_demo.lib` key is unique across the process.
    """
    import importlib.util  # noqa: PLC0415
    full_name = "alive_demo.lib"
    if full_name in sys.modules:
        return sys.modules[full_name]
    target = os.path.join(_DEMO_DIR, "lib.py")
    spec = importlib.util.spec_from_file_location(full_name, target)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {target}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The canonical schema version stamped into every spine.json. Bumped only
#: on a breaking change to the spine shape; tests + the validator
#: cross-assert this constant against the on-disk value.
SCHEMA_VERSION = "0.1"

#: Soft ceiling, in tokens, above which the description summarizer
#: sub-stage runs first. Per the task's locked decision, the heuristic
#: is `len(text.split()) * 1.3` — a coarse word-to-token approximation
#: that does not require tiktoken or any other dependency.
DESCRIPTION_TOKEN_BUDGET = 4000

#: Top-level keys the spine.json document must carry. Pre-flight rejects
#: any document missing one of these. `validate.py` (fn-2-2zz.10) walks
#: the full schema; this is the dispatch-time minimum.
REQUIRED_TOP_LEVEL_KEYS = (
    "schema_version",
    "persona",
    "walnut_roster",
    "people_roster",
    "bundle_distribution",
    "time_span",
    "session_cadence",
    "anchor_moments",
)

#: Path of the per-stage prompt template, relative to the plugin root.
SPINE_PROMPT_RELPATH = os.path.join(
    "templates", "demo", "stage_prompts", "stage_0_spine.v1.md"
)

#: Path of the description-summarizer template, relative to the plugin root.
SUMMARIZE_PROMPT_RELPATH = os.path.join(
    "templates", "demo", "stage_prompts", "summarize_description.v1.md"
)

#: Path of the shared subagent-brief preamble, relative to the plugin root.
SUBAGENT_BRIEF_RELPATH = os.path.join("templates", "subagent-brief.md")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class Stage0Error(RuntimeError):
    """Base error for Stage 0 dispatch + pre-flight failures."""


class SpinePreflightError(Stage0Error):
    """Raised when `preflight_spine` rejects the on-disk file.

    Carries an `errors` list of human-readable strings — the dispatcher
    truncates the file's text + appends these strings as feedback on the
    one-shot retry per the spec's locked decision.
    """

    def __init__(self, errors: List[str]) -> None:
        self.errors = list(errors)
        super().__init__(
            "spine.json failed Stage 0 pre-flight: "
            + "; ".join(errors)
        )


# ---------------------------------------------------------------------------
# Token estimate
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Estimate the token count of `text` using the locked heuristic.

    `len(text.split()) * 1.3` per the task's "Approach" section. Coarse;
    overestimates short English, underestimates code / dense markup. That
    is intentional — the budget is soft and the cost of summarising one
    persona that wasn't strictly over is much smaller than the cost of
    blowing the spine prompt's window.
    """
    if not isinstance(text, str):
        raise TypeError(
            f"text must be str; got {type(text).__name__}"
        )
    return int(len(text.split()) * 1.3)


def needs_summary(description: str, *, budget: int = DESCRIPTION_TOKEN_BUDGET) -> bool:
    """True iff the description's token estimate exceeds `budget`."""
    return estimate_tokens(description) > budget


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _abspartial(partial_dir: str) -> str:
    """Canonicalize a partial-directory path to an absolute, normalized form.

    The Stage 0 prompt contract requires a "known absolute path" for the
    subagent's file handoff (the subagent runs in a fresh context — a
    relative path would resolve against whatever cwd the runtime gives
    it and silently break the disk contract). This helper enforces the
    invariant at the single boundary between caller-supplied paths and
    every helper that emits a path the subagent will read.

    `os.path.abspath` resolves against the dispatcher's cwd at call
    time; `os.path.normpath` canonicalises `..` segments. We don't
    `realpath`-resolve symlinks: the partial directory is owned by the
    demo skill and may legitimately be reached via a symlink (e.g. a
    home-directory mount on macOS where `/Users/...` is itself a
    symlink). Symlink-stripping would silently change the path the
    subagent sees from the path the dispatcher tracks.
    """
    if not isinstance(partial_dir, str):
        raise TypeError(
            f"partial_dir must be str; got {type(partial_dir).__name__}"
        )
    return os.path.normpath(os.path.abspath(partial_dir))


def partial_input_dir(partial_dir: str) -> str:
    """Return absolute `<partial>/_input/`, creating it if needed.

    Caller passes the partial directory (e.g.
    `<base>/wld_<ulid>.partial/`). Per the locked decision, the full
    persona description is retained on disk at `<_input>/persona-description.md`
    even when summarized for the prompt.

    The returned path is always absolute even when `partial_dir` is
    relative — see :func:`_abspartial` for the rationale.
    """
    out = os.path.join(_abspartial(partial_dir), "_input")
    os.makedirs(out, exist_ok=True)
    return out


def partial_stage_outputs_dir(partial_dir: str) -> str:
    """Return absolute `<partial>/_stage_outputs/`, creating it if needed."""
    out = os.path.join(_abspartial(partial_dir), "_stage_outputs")
    os.makedirs(out, exist_ok=True)
    return out


def spine_output_path(partial_dir: str) -> str:
    """Absolute path the Stage 0 subagent writes to."""
    return os.path.join(partial_stage_outputs_dir(partial_dir), "spine.json")


def done_marker_path(partial_dir: str) -> str:
    """Absolute path to the Stage 0 done marker.

    fn-2-2zz.16: Stage 0's primary artefact is ``spine.json``, but
    ``scaffold._validate_partial_ready`` (the Stage 5 activation
    pre-flight) checks for a frozen ``stage{N}_done.json`` for every
    stage 0..4. ``run_stage0`` stamps this marker on every successful
    return so the custom-path orchestrator's activation transaction
    has a uniform readiness contract.
    """
    return os.path.join(partial_stage_outputs_dir(partial_dir), "stage0_done.json")


def _write_stage0_done_marker(partial_dir: str) -> None:
    """Stamp ``stage{N}_done.json`` with a frozen marker.

    Idempotent: callers may invoke on every successful return path
    (including the retry success path). The marker shape mirrors the
    other stages' done markers so downstream readers (validate.py,
    scaffold.py, build_log) can treat all five stages uniformly.
    """
    marker = {
        "schema_version": SCHEMA_VERSION,
        "frozen": True,
        "frozen_at": iso_now(),
    }
    atomic_write_json(done_marker_path(partial_dir), marker)


def description_input_path(partial_dir: str) -> str:
    """Absolute path the full persona description is retained at."""
    return os.path.join(partial_input_dir(partial_dir), "persona-description.md")


def description_summary_path(partial_dir: str) -> str:
    """Absolute path the summarised description is written to."""
    return os.path.join(partial_input_dir(partial_dir), "persona-description.summary.md")


# ---------------------------------------------------------------------------
# Template I/O
# ---------------------------------------------------------------------------

def _read_template(relpath: str, *, plugin_root: Optional[str] = None) -> str:
    """Read a template file from `plugin_root/<relpath>`. Stdlib only."""
    root = plugin_root or resolve_plugin_root()
    target = os.path.join(root, relpath)
    with open(target, "r", encoding="utf-8") as f:
        return f.read()


def render_subagent_brief(
    *,
    world_root: str,
    plugin_root: Optional[str] = None,
) -> str:
    """Read `templates/subagent-brief.md` and substitute the two slots.

    Per `plugins/alive/rules/squirrels.md:335-340` the brief substitution
    is mandatory; `{WORLD_ROOT}` and `{PLUGIN_ROOT}` are the documented
    placeholders. We use `str.replace` rather than `str.format` so curly
    braces inside the brief body (none today, but defensive) do not blow
    up the substitution.
    """
    root = plugin_root or resolve_plugin_root()
    brief = _read_template(SUBAGENT_BRIEF_RELPATH, plugin_root=root)
    return (
        brief
        .replace("{WORLD_ROOT}", world_root)
        .replace("{PLUGIN_ROOT}", root)
    )


def _wrap_dispatch_prompt(*, brief: str, task_body: str) -> str:
    """Wrap a per-stage task body in the canonical `CONTEXT:` / `TASK:` envelope.

    The wrapper string shape is fixed by `plugins/alive/skills/demo/SKILL.md`
    § "Prompt-rendering wrapper (mandatory)". Subagents that don't see the
    brief in this exact shape may not know walnut/bundle/tasks.py
    conventions and will make mistakes. The body of `task_body` is
    inserted verbatim — substitution of `{{description}}` etc happens
    BEFORE wrapping so the dispatch wrapper itself is opaque to the body.
    """
    return (
        "CONTEXT:\n"
        f"{brief}\n"
        "\n"
        "TASK:\n"
        f"{task_body}"
    )


def _substitute(template: str, mapping: Dict[str, str]) -> str:
    """Replace `{{key}}` placeholders. Unknown keys are left untouched."""
    out = template
    for key, value in mapping.items():
        out = out.replace("{{" + key + "}}", value)
    return out


# ---------------------------------------------------------------------------
# Description-on-disk handling
# ---------------------------------------------------------------------------

def persist_description(partial_dir: str, description: str) -> str:
    """Write the full description to `<partial>/_input/persona-description.md`.

    Returns the absolute path written. The dispatcher calls this BEFORE
    deciding whether to summarize — full text is retained regardless so
    Stage 1 confirmations and the build log can still reference the
    original.
    """
    target = description_input_path(partial_dir)
    atomic_write_text(target, description)
    return target


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def render_summarize_prompt(
    *,
    description: str,
    partial_dir: str,
    world_root: str,
    plugin_root: Optional[str] = None,
) -> Tuple[str, str]:
    """Render the description-summarizer prompt.

    Returns `(prompt_text, output_path)`. The output_path is where the
    summarizer subagent must write its summary; the dispatcher reads
    that file before invoking the spine prompt with the summarised text.
    """
    root = plugin_root or resolve_plugin_root()
    brief = render_subagent_brief(world_root=world_root, plugin_root=root)
    body = _read_template(SUMMARIZE_PROMPT_RELPATH, plugin_root=root)
    output_path = description_summary_path(partial_dir)
    body = _substitute(
        body,
        {
            "subagent_brief": "[brief is wrapped via CONTEXT envelope below]",
            "description": description,
            "output_path": output_path,
        },
    )
    return _wrap_dispatch_prompt(brief=brief, task_body=body), output_path


def render_spine_prompt(
    *,
    description: str,
    size: str,
    partial_dir: str,
    world_root: str,
    plugin_root: Optional[str] = None,
) -> Tuple[str, str]:
    """Render the Stage 0 spine prompt.

    `description` is whatever the dispatcher decided to feed in: either
    the full text (token estimate <= budget) or the summary file's
    contents (token estimate > budget AND summarizer ran first).

    `size` is the world size selector — one of `"S"`, `"M"`, `"L"`. The
    prompt body explains soft targets per size.

    Returns `(prompt_text, output_path)`. `output_path` is where the
    spine subagent must write `spine.json`; the dispatcher pre-flights
    that file via `preflight_spine` before handing off to Stage 1.
    """
    if size not in ("S", "M", "L"):
        raise ValueError(f"size must be one of S, M, L; got {size!r}")

    root = plugin_root or resolve_plugin_root()
    brief = render_subagent_brief(world_root=world_root, plugin_root=root)
    body = _read_template(SPINE_PROMPT_RELPATH, plugin_root=root)
    output_path = spine_output_path(partial_dir)
    body = _substitute(
        body,
        {
            "subagent_brief": "[brief is wrapped via CONTEXT envelope below]",
            "description": description,
            "size": size,
            "output_path": output_path,
        },
    )
    return _wrap_dispatch_prompt(brief=brief, task_body=body), output_path


def render_spine_prompt_with_feedback(
    *,
    description: str,
    size: str,
    partial_dir: str,
    world_root: str,
    previous_output: str,
    errors: List[str],
    plugin_root: Optional[str] = None,
    truncate_at: int = 4000,
) -> Tuple[str, str]:
    """Render the Stage 0 spine prompt with retry feedback appended.

    Per the spec's locked decision (one auto-retry on validation failure
    with truncated previous output + error list as feedback), this is
    the second-pass renderer. It calls :func:`render_spine_prompt` and
    appends a feedback block listing the validator errors and a truncated
    fragment of what the model wrote last time.

    `previous_output` is the text of the rejected spine.json; we truncate
    to `truncate_at` characters so the retry doesn't blow the prompt
    budget on a model that emitted a 50 KB malformed file.
    """
    base_prompt, output_path = render_spine_prompt(
        description=description,
        size=size,
        partial_dir=partial_dir,
        world_root=world_root,
        plugin_root=plugin_root,
    )
    truncated = previous_output
    if len(truncated) > truncate_at:
        truncated = truncated[:truncate_at] + "\n... [truncated]"

    feedback = ["", "---", "", "## Retry feedback", ""]
    feedback.append(
        "Your previous attempt failed pre-flight. Fix the errors below "
        "and write a corrected spine.json to the same output path."
    )
    feedback.append("")
    feedback.append("### Errors")
    for err in errors:
        feedback.append(f"- {err}")
    feedback.append("")
    feedback.append("### Previous output (truncated)")
    feedback.append("")
    feedback.append("```")
    feedback.append(truncated)
    feedback.append("```")
    return base_prompt + "\n".join(feedback), output_path


# ---------------------------------------------------------------------------
# Structural schema validation (codex review round 2 — full structural
# enforcement at Stage 0; coherence invariants still owned by validate.py)
# ---------------------------------------------------------------------------

#: Closed key sets per object level. `additionalProperties: false` policy is
#: enforced by checking the object's actual keys against these sets.
_PERSONA_KEYS = frozenset({"name", "first_name", "label", "summary", "tone_hints"})
_PERSONA_REQUIRED = frozenset({"name", "first_name", "label", "summary", "tone_hints"})

_WALNUT_KEYS = frozenset({"slug", "name", "type", "domain_dir", "summary", "status"})
_WALNUT_REQUIRED = frozenset(_WALNUT_KEYS)

_PERSON_KEYS = frozenset({"slug", "name", "relationship", "relationships"})
_PERSON_REQUIRED = frozenset({"slug", "name", "relationship"})  # relationships optional

_RELATIONSHIP_KEYS = frozenset({"from", "to", "kind"})
_RELATIONSHIP_REQUIRED = frozenset(_RELATIONSHIP_KEYS)

_BUNDLE_KEYS = frozenset({"slug", "walnut_slug", "name", "summary", "status"})
_BUNDLE_REQUIRED = frozenset(_BUNDLE_KEYS)

_TIME_SPAN_KEYS = frozenset({"start", "end"})
_TIME_SPAN_REQUIRED = frozenset(_TIME_SPAN_KEYS)

_SESSION_CADENCE_KEYS = frozenset({"pattern", "sessions_per_week"})
_SESSION_CADENCE_REQUIRED = frozenset(_SESSION_CADENCE_KEYS)

_ANCHOR_KEYS = frozenset({"slug", "name", "date", "summary", "walnut_slugs", "people_slugs"})
_ANCHOR_REQUIRED = frozenset(_ANCHOR_KEYS)

_TOP_LEVEL_KEYS = frozenset(REQUIRED_TOP_LEVEL_KEYS)

#: Documented enum values from `templates/demo/schema/spine.schema.md`.
_WALNUT_TYPES = frozenset({"venture", "experiment", "life-area", "minimal-life"})
_DOMAIN_DIRS = frozenset({"01_Archive", "02_Life", "03_Inbox", "04_Ventures", "05_Experiments"})
_STATUSES = frozenset({"active", "working", "waiting", "archive"})
_CADENCE_PATTERNS = frozenset({"daily", "weekly", "sporadic"})


#: Strict zero-padded ISO 8601 date pattern. Matches the schema descriptor
#: at `templates/demo/schema/spine.schema.json:$defs.iso_date.pattern`.
#: `datetime.strptime("%Y-%m-%d", ...)` alone accepts non-zero-padded
#: months / days like "2025-2-1" on every Python the plugin supports —
#: the descriptor's regex rejects them, so the runtime must too or
#: Stage 0 ships "validated" spines the descriptor would reject.
_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _validate_iso_date(value, where: str, errors: List[str]) -> Optional[Tuple[int, int, int]]:
    """Validate `value` is a strict zero-padded `YYYY-MM-DD` string.

    Two-step check:
      1. Lexical shape via `_ISO_DATE_RE` — rejects "2025-2-1",
         "2025-02-01T12:00:00", "2025/02/01", etc. before any parsing.
      2. Calendar validity via `datetime.strptime` — rejects "2025-02-30",
         "2025-13-01", "2025-00-15", etc.
    """
    if not isinstance(value, str):
        errors.append(f"{where} must be string, got {type(value).__name__}")
        return None
    if not _ISO_DATE_RE.match(value):
        errors.append(
            f"{where} must be strict zero-padded ISO 8601 date "
            f"YYYY-MM-DD, got {value!r}"
        )
        return None
    import datetime as _dt  # noqa: PLC0415
    try:
        d = _dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        errors.append(
            f"{where} must be a real calendar date YYYY-MM-DD, "
            f"got {value!r}: {exc}"
        )
        return None
    return (d.year, d.month, d.day)


def _check_closed_object(
    obj,
    *,
    where: str,
    allowed: frozenset,
    required: frozenset,
    errors: List[str],
) -> bool:
    """Enforce additionalProperties: false + required-keys for one object."""
    if not isinstance(obj, dict):
        errors.append(f"{where} must be object, got {type(obj).__name__}")
        return False
    actual = set(obj.keys())
    extra = actual - allowed
    if extra:
        errors.append(f"{where} has unexpected keys: {sorted(extra)}")
    missing = required - actual
    if missing:
        errors.append(f"{where} missing required keys: {sorted(missing)}")
    return not extra and not missing


def _validate_persona(persona, *, errors, is_valid_slug):
    if not _check_closed_object(
        persona, where="persona", allowed=_PERSONA_KEYS,
        required=_PERSONA_REQUIRED, errors=errors,
    ):
        return
    for k in ("name", "first_name", "summary"):
        v = persona.get(k)
        if not isinstance(v, str) or not v:
            errors.append(f"persona.{k} must be non-empty string")
    label = persona.get("label")
    if not isinstance(label, str) or not is_valid_slug(label):
        errors.append(f"persona.label is not a valid slug: {label!r}")
    th = persona.get("tone_hints")
    if not isinstance(th, list):
        errors.append("persona.tone_hints must be array")
    else:
        if len(th) > 3:
            errors.append(f"persona.tone_hints must have at most 3 entries, got {len(th)}")
        for i, hint in enumerate(th):
            if not isinstance(hint, str) or not hint:
                errors.append(f"persona.tone_hints[{i}] must be non-empty string")


def _validate_walnut(entry, *, where, errors, is_valid_slug):
    if not _check_closed_object(
        entry, where=where, allowed=_WALNUT_KEYS,
        required=_WALNUT_REQUIRED, errors=errors,
    ):
        return
    slug = entry.get("slug")
    if not isinstance(slug, str) or not is_valid_slug(slug):
        errors.append(f"{where}.slug is not a valid slug: {slug!r}")
    if not isinstance(entry.get("name"), str) or not entry.get("name"):
        errors.append(f"{where}.name must be non-empty string")
    if not isinstance(entry.get("summary"), str) or not entry.get("summary"):
        errors.append(f"{where}.summary must be non-empty string")
    t = entry.get("type")
    if t not in _WALNUT_TYPES:
        errors.append(
            f"{where}.type must be one of {sorted(_WALNUT_TYPES)}, got {t!r}"
        )
    dd = entry.get("domain_dir")
    if dd not in _DOMAIN_DIRS:
        errors.append(
            f"{where}.domain_dir must be one of {sorted(_DOMAIN_DIRS)}, got {dd!r}"
        )
    st = entry.get("status")
    if st not in _STATUSES:
        errors.append(
            f"{where}.status must be one of {sorted(_STATUSES)}, got {st!r}"
        )


def _validate_person(entry, *, where, errors, is_valid_slug):
    if not _check_closed_object(
        entry, where=where, allowed=_PERSON_KEYS,
        required=_PERSON_REQUIRED, errors=errors,
    ):
        return
    slug = entry.get("slug")
    if not isinstance(slug, str) or not is_valid_slug(slug):
        errors.append(f"{where}.slug is not a valid slug: {slug!r}")
    if not isinstance(entry.get("name"), str) or not entry.get("name"):
        errors.append(f"{where}.name must be non-empty string")
    if not isinstance(entry.get("relationship"), str) or not entry.get("relationship"):
        errors.append(f"{where}.relationship must be non-empty string")
    rels = entry.get("relationships")
    if rels is not None:
        if not isinstance(rels, list):
            errors.append(f"{where}.relationships must be array")
        else:
            for i, r in enumerate(rels):
                rwhere = f"{where}.relationships[{i}]"
                if not _check_closed_object(
                    r, where=rwhere, allowed=_RELATIONSHIP_KEYS,
                    required=_RELATIONSHIP_REQUIRED, errors=errors,
                ):
                    continue
                # Endpoint slug shape (not cross-reference — that's coherence).
                for k in ("from", "to"):
                    v = r.get(k)
                    if not isinstance(v, str) or not is_valid_slug(v):
                        errors.append(
                            f"{rwhere}.{k} is not a valid slug: {v!r}"
                        )
                if not isinstance(r.get("kind"), str) or not r.get("kind"):
                    errors.append(f"{rwhere}.kind must be non-empty string")


def _validate_bundle(entry, *, where, errors, is_valid_slug):
    if not _check_closed_object(
        entry, where=where, allowed=_BUNDLE_KEYS,
        required=_BUNDLE_REQUIRED, errors=errors,
    ):
        return
    slug = entry.get("slug")
    if not isinstance(slug, str) or not is_valid_slug(slug):
        errors.append(f"{where}.slug is not a valid slug: {slug!r}")
    ws = entry.get("walnut_slug")
    if not isinstance(ws, str) or not is_valid_slug(ws):
        errors.append(f"{where}.walnut_slug is not a valid slug: {ws!r}")
    for k in ("name", "summary"):
        if not isinstance(entry.get(k), str) or not entry.get(k):
            errors.append(f"{where}.{k} must be non-empty string")
    st = entry.get("status")
    if st not in _STATUSES:
        errors.append(
            f"{where}.status must be one of {sorted(_STATUSES)}, got {st!r}"
        )


def _validate_anchor(entry, *, where, errors, is_valid_slug):
    if not _check_closed_object(
        entry, where=where, allowed=_ANCHOR_KEYS,
        required=_ANCHOR_REQUIRED, errors=errors,
    ):
        return
    slug = entry.get("slug")
    if not isinstance(slug, str) or not is_valid_slug(slug):
        errors.append(f"{where}.slug is not a valid slug: {slug!r}")
    for k in ("name", "summary"):
        if not isinstance(entry.get(k), str) or not entry.get(k):
            errors.append(f"{where}.{k} must be non-empty string")
    _validate_iso_date(entry.get("date"), f"{where}.date", errors)
    for list_key in ("walnut_slugs", "people_slugs"):
        v = entry.get(list_key)
        if not isinstance(v, list):
            errors.append(f"{where}.{list_key} must be array")
            continue
        for i, s in enumerate(v):
            if not isinstance(s, str) or not is_valid_slug(s):
                errors.append(
                    f"{where}.{list_key}[{i}] is not a valid slug: {s!r}"
                )


def _validate_time_span(ts, *, errors):
    if not _check_closed_object(
        ts, where="time_span", allowed=_TIME_SPAN_KEYS,
        required=_TIME_SPAN_REQUIRED, errors=errors,
    ):
        return
    start_parts = _validate_iso_date(ts.get("start"), "time_span.start", errors)
    end_parts = _validate_iso_date(ts.get("end"), "time_span.end", errors)
    if start_parts and end_parts and start_parts > end_parts:
        errors.append(
            f"time_span.start must be <= time_span.end "
            f"(start={ts.get('start')!r}, end={ts.get('end')!r})"
        )


def _validate_session_cadence(sc, *, errors):
    if not _check_closed_object(
        sc, where="session_cadence", allowed=_SESSION_CADENCE_KEYS,
        required=_SESSION_CADENCE_REQUIRED, errors=errors,
    ):
        return
    p = sc.get("pattern")
    if p not in _CADENCE_PATTERNS:
        errors.append(
            f"session_cadence.pattern must be one of "
            f"{sorted(_CADENCE_PATTERNS)}, got {p!r}"
        )
    spw = sc.get("sessions_per_week")
    if isinstance(spw, bool) or not isinstance(spw, (int, float)):
        errors.append(
            f"session_cadence.sessions_per_week must be number, "
            f"got {type(spw).__name__}"
        )
    elif spw <= 0 or spw > 14:
        errors.append(
            f"session_cadence.sessions_per_week must be in (0, 14], got {spw}"
        )


def preflight_spine(path: str) -> Dict[str, Any]:
    """Parse + structurally validate the spine.json the subagent wrote.

    Pre-flight enforces the **structural** schema documented in
    `templates/demo/schema/spine.schema.md`:

      1. File exists and parses as UTF-8 JSON.
      2. Top-level value is a `dict` with exactly the documented keys
         (no extras, none missing) and `schema_version == "0.1"`.
      3. Every persona / walnut / people / bundle / anchor /
         relationship object has the closed key set the schema declares
         (`additionalProperties: false`) with all required fields.
      4. Every enum value (walnut.type, walnut.domain_dir, walnut.status,
         bundle.status, session_cadence.pattern) is one of the
         documented options.
      5. Every date (`time_span.start`, `time_span.end`,
         `anchor_moments[*].date`) parses as `YYYY-MM-DD` ISO 8601 and
         `time_span.start <= time_span.end`.
      6. `session_cadence.sessions_per_week` is a number in `(0, 14]`.
      7. Every slug satisfies `^[a-z0-9]+(-[a-z0-9]+)*$`.

    What pre-flight DOES NOT check (deferred to `validate.py`,
    fn-2-2zz.10):

      * Cross-reference integrity: every `bundle.walnut_slug` resolves
        to an actual `walnut_roster[*].slug`; every
        `anchor_moments[*].walnut_slugs[*]` and `.people_slugs[*]`
        resolves; every `people_roster[*].relationships[*].{from, to}`
        resolves.
      * `anchor_moments[*].date` lies within `[time_span.start, time_span.end]`.
      * Walnut-must-have-bundle for non-`minimal-life` walnut types.
      * Cross-roster slug uniqueness.

    Pre-flight collects every problem in a single pass and raises
    :class:`SpinePreflightError` whose `errors` list carries them all.
    The retry prompt embeds the full list so the subagent can fix
    everything in one shot rather than iterating.
    """
    errors: List[str] = []

    if not os.path.isfile(path):
        raise SpinePreflightError([f"spine.json not found at {path}"])

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise SpinePreflightError(
            [f"spine.json at {path} unreadable: {type(exc).__name__}: {exc}"]
        ) from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpinePreflightError(
            [f"spine.json is not valid JSON: line {exc.lineno} col {exc.colno}: {exc.msg}"]
        ) from exc

    if not isinstance(data, dict):
        raise SpinePreflightError(
            [f"spine.json top-level value must be object, got {type(data).__name__}"]
        )

    # Top-level closed-object check (additionalProperties: false).
    actual = set(data.keys())
    extra = actual - _TOP_LEVEL_KEYS
    if extra:
        errors.append(f"spine.json has unexpected top-level keys: {sorted(extra)}")
    missing = [k for k in REQUIRED_TOP_LEVEL_KEYS if k not in data]
    if missing:
        errors.append(f"missing required top-level keys: {missing}")

    sv = data.get("schema_version")
    if sv != SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {SCHEMA_VERSION!r}, got {sv!r}"
        )

    lib = _load_lib()
    is_valid_slug = lib.is_valid_slug

    # Persona.
    persona = data.get("persona")
    if persona is not None:
        _validate_persona(persona, errors=errors, is_valid_slug=is_valid_slug)

    # Roster validation per object kind.
    walnuts = data.get("walnut_roster")
    if walnuts is not None:
        if not isinstance(walnuts, list):
            errors.append(
                f"walnut_roster must be array, got {type(walnuts).__name__}"
            )
        else:
            seen_walnut_slugs = set()
            for i, entry in enumerate(walnuts):
                _validate_walnut(
                    entry, where=f"walnut_roster[{i}]",
                    errors=errors, is_valid_slug=is_valid_slug,
                )
                # Slug-uniqueness within walnut_roster (schema § Walnut roster:
                # "Must be unique across the roster"). Track only well-formed
                # slugs — already-rejected slugs aren't worth a duplicate-of
                # noise on top of the slug-shape error.
                if isinstance(entry, dict):
                    s = entry.get("slug")
                    if isinstance(s, str) and is_valid_slug(s):
                        if s in seen_walnut_slugs:
                            errors.append(
                                f"walnut_roster[{i}].slug {s!r} duplicates "
                                f"an earlier entry"
                            )
                        seen_walnut_slugs.add(s)

    people = data.get("people_roster")
    if people is not None:
        if not isinstance(people, list):
            errors.append(
                f"people_roster must be array, got {type(people).__name__}"
            )
        else:
            seen_people_slugs = set()
            for i, entry in enumerate(people):
                _validate_person(
                    entry, where=f"people_roster[{i}]",
                    errors=errors, is_valid_slug=is_valid_slug,
                )
                # Slug-uniqueness within people_roster (schema § People roster:
                # "Unique across people_roster").
                if isinstance(entry, dict):
                    s = entry.get("slug")
                    if isinstance(s, str) and is_valid_slug(s):
                        if s in seen_people_slugs:
                            errors.append(
                                f"people_roster[{i}].slug {s!r} duplicates "
                                f"an earlier entry"
                            )
                        seen_people_slugs.add(s)

    bundles = data.get("bundle_distribution")
    if bundles is not None:
        if not isinstance(bundles, list):
            errors.append(
                f"bundle_distribution must be array, got {type(bundles).__name__}"
            )
        else:
            # Schema § Bundle distribution: "Unique within its parent walnut".
            # Bundles are scoped per `walnut_slug`, so the same bundle slug
            # CAN repeat across different walnuts but not within one walnut.
            seen_bundle_slugs_per_walnut: Dict[str, set] = {}
            for i, entry in enumerate(bundles):
                _validate_bundle(
                    entry, where=f"bundle_distribution[{i}]",
                    errors=errors, is_valid_slug=is_valid_slug,
                )
                if isinstance(entry, dict):
                    s = entry.get("slug")
                    ws = entry.get("walnut_slug")
                    if (
                        isinstance(s, str) and is_valid_slug(s)
                        and isinstance(ws, str) and is_valid_slug(ws)
                    ):
                        bucket = seen_bundle_slugs_per_walnut.setdefault(ws, set())
                        if s in bucket:
                            errors.append(
                                f"bundle_distribution[{i}].slug {s!r} "
                                f"duplicates an earlier bundle under "
                                f"walnut_slug {ws!r}"
                            )
                        bucket.add(s)

    # Anchor-moment slug uniqueness (single global namespace; schema treats
    # anchor slugs as keys downstream).
    anchors = data.get("anchor_moments")
    seen_anchor_slugs = set()

    ts = data.get("time_span")
    if ts is not None:
        _validate_time_span(ts, errors=errors)

    sc = data.get("session_cadence")
    if sc is not None:
        _validate_session_cadence(sc, errors=errors)

    if anchors is not None:
        if not isinstance(anchors, list):
            errors.append(
                f"anchor_moments must be array, got {type(anchors).__name__}"
            )
        else:
            for i, entry in enumerate(anchors):
                _validate_anchor(
                    entry, where=f"anchor_moments[{i}]",
                    errors=errors, is_valid_slug=is_valid_slug,
                )
                if isinstance(entry, dict):
                    s = entry.get("slug")
                    if isinstance(s, str) and is_valid_slug(s):
                        if s in seen_anchor_slugs:
                            errors.append(
                                f"anchor_moments[{i}].slug {s!r} duplicates "
                                f"an earlier entry"
                            )
                        seen_anchor_slugs.add(s)

    if errors:
        raise SpinePreflightError(errors)
    return data


# ---------------------------------------------------------------------------
# Orchestration runner
# ---------------------------------------------------------------------------

#: Default subagent kind for Stage 0 dispatch. Mirrors the skill router's
#: Primitive A contract (`subagent_type: "general-purpose"`).
DEFAULT_SUBAGENT_TYPE = "general-purpose"


class Stage0DispatchFailed(Stage0Error):
    """Raised when the dispatcher's callable raised or returned a non-string.

    Distinct from :class:`SpinePreflightError` so the skill router can tell
    "the subagent's file is malformed" (recoverable via retry) from "the
    runtime itself failed to dispatch" (not a retry case — the human
    needs to see the error).
    """


class Stage0RetryExhausted(Stage0Error):
    """Raised when the one-and-only retry also fails pre-flight.

    Carries the cumulative `errors` list from the LAST attempt. Per the
    spec's locked decision, on second failure the skill router surfaces
    a 3-option AskUserQuestion (accept partial / retry full / cancel);
    this exception is the signal to do that. The first-attempt errors
    are NOT carried — the second-attempt errors are what the human
    decides against.
    """

    def __init__(self, errors: List[str]) -> None:
        self.errors = list(errors)
        super().__init__(
            "spine.json failed Stage 0 pre-flight twice (after one retry): "
            + "; ".join(errors)
        )


def run_stage0(
    *,
    description: str,
    size: str,
    partial_dir: str,
    world_root: str,
    dispatch,
    plugin_root: Optional[str] = None,
    subagent_type: str = DEFAULT_SUBAGENT_TYPE,
    surface_failure_blocks: bool = False,
) -> Dict[str, Any]:
    """Run the full Stage 0 pipeline: persist → maybe-summarise → dispatch → preflight → retry.

    This is the orchestration entrypoint the skill router wires up. The
    actual Task tool call lives in the runtime, not in Python — so the
    runner takes a `dispatch` callable that the router supplies. The
    callable signature is::

        dispatch(prompt: str, *, subagent_type: str, description: str) -> str

    where `description` is a one-line label for the dispatch (e.g.
    "alive-demo stage 0 spine"). The return value is the subagent's
    one-line acknowledgement string; the runner ignores its content
    (the source of truth is the file the subagent wrote).

    Pipeline (locked order, matches `.flow/specs/fn-2-2zz.md` § "Approach"):

      1. **Persist** the full persona description at
         `<partial>/_input/persona-description.md` so Stage 1 + the
         build log can reference the original verbatim.
      2. **Triage** length: if `estimate_tokens(description) > budget`,
         dispatch the summariser subagent first; the summariser writes
         to `<partial>/_input/persona-description.summary.md`. Read
         that file back and feed its body into the spine prompt.
         (The full text is still on disk from step 1.)
      3. **Dispatch** the spine subagent. Wait for completion (the
         dispatch callable blocks). The subagent writes
         `<partial>/_stage_outputs/spine.json`.
      4. **Pre-flight** the file via :func:`preflight_spine`. On
         success, return the parsed dict + the artefact paths.
      5. **One retry on pre-flight failure**: render the prompt with
         `render_spine_prompt_with_feedback` (truncated previous output
         + error list), dispatch again, pre-flight again. On second
         failure raise :class:`Stage0RetryExhausted` so the skill
         router can surface the 3-option AskUserQuestion.

    The return envelope is::

        {
            "spine": <parsed dict>,
            "spine_path": <absolute path>,
            "description_path": <absolute path>,
            "summary_path": <absolute path or None>,
            "summarised": bool,
            "retried": bool,
            "first_attempt_errors": list[str] (empty when retried=False),
        }

    The caller (skill router) writes these fields into the build-log
    provenance entry (fn-2-2zz.9 step 8) and into demo-state.json's
    partial-generation entry (fn-2-2zz.3 + .9 step 9).

    Failure modes:

      * `dispatch` raised → re-raise wrapped as :class:`Stage0DispatchFailed`
        (NOT a retry case — the runtime itself failed).
      * `dispatch` returned a non-str → :class:`Stage0DispatchFailed` (same).
      * Pre-flight failed twice → :class:`Stage0RetryExhausted`.
      * Summariser was triggered but its output file is missing or
        unreadable → :class:`Stage0DispatchFailed` (the summariser
        contract is "write a file"; if the file isn't there, the
        runtime broke).
    """
    if not callable(dispatch):
        raise TypeError(
            f"dispatch must be callable; got {type(dispatch).__name__}"
        )

    canonical_partial = _abspartial(partial_dir)
    desc_path = persist_description(canonical_partial, description)

    # ------------------------------------------------------------------
    # Triage: long descriptions go through the summariser sub-stage first.
    # ------------------------------------------------------------------
    summarised = False
    summary_path: Optional[str] = None
    effective_description = description

    if needs_summary(description):
        summarise_prompt, summary_path = render_summarize_prompt(
            description=description,
            partial_dir=canonical_partial,
            world_root=world_root,
            plugin_root=plugin_root,
        )
        try:
            ack = dispatch(
                summarise_prompt,
                subagent_type=subagent_type,
                description="alive-demo stage 0 description summarise",
            )
        except Exception as exc:  # noqa: BLE001
            raise Stage0DispatchFailed(
                f"summariser dispatch raised: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(ack, str):
            raise Stage0DispatchFailed(
                f"summariser dispatch returned non-str: {type(ack).__name__}"
            )

        # Read the summary the subagent wrote. Missing / unreadable here
        # is a runtime bug, not a retry case.
        if not os.path.isfile(summary_path):
            raise Stage0DispatchFailed(
                f"summariser did not write {summary_path}"
            )
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                effective_description = f.read()
        except OSError as exc:
            raise Stage0DispatchFailed(
                f"summary file at {summary_path} unreadable: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        summarised = True

    # ------------------------------------------------------------------
    # First spine attempt.
    # ------------------------------------------------------------------
    spine_prompt, spine_path = render_spine_prompt(
        description=effective_description,
        size=size,
        partial_dir=canonical_partial,
        world_root=world_root,
        plugin_root=plugin_root,
    )
    try:
        ack = dispatch(
            spine_prompt,
            subagent_type=subagent_type,
            description="alive-demo stage 0 spine",
        )
    except Exception as exc:  # noqa: BLE001
        raise Stage0DispatchFailed(
            f"spine dispatch raised: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(ack, str):
        raise Stage0DispatchFailed(
            f"spine dispatch returned non-str: {type(ack).__name__}"
        )

    # ------------------------------------------------------------------
    # First-attempt preflight + coherence gate.
    #
    # Two layers run in sequence on each pass:
    #   1. preflight_spine: structural + intra-roster shape checks
    #   2. validate.validate_stage("0"): cross-stage coherence (anchor
    #      dates within time_span, relationship endpoints in roster,
    #      walnut-must-have-bundle, cross-roster slug uniqueness, etc.)
    #
    # If either layer fails, render feedback combining BOTH layers'
    # errors and dispatch the one allowed retry. After the retry, the
    # same two layers run again; any remaining coherence failure is
    # raised as Stage0RetryExhausted so the parent skill can surface
    # the three-option block via AskUserQuestion.
    # ------------------------------------------------------------------
    spine, first_attempt_errors = _preflight_and_validate(
        spine_path=spine_path,
        partial_dir=canonical_partial,
    )
    if first_attempt_errors:
        # One retry with feedback. Read the (rejected) file so we can
        # quote it back to the model -- but tolerate the file being
        # absent (runtime bug or model wrote nothing); fall back to a
        # placeholder rather than crashing the retry path.
        try:
            with open(spine_path, "r", encoding="utf-8") as f:
                previous_output = f.read()
        except OSError:
            previous_output = "[spine.json could not be read for feedback]"

        retry_prompt, _ = render_spine_prompt_with_feedback(
            description=effective_description,
            size=size,
            partial_dir=canonical_partial,
            world_root=world_root,
            previous_output=previous_output,
            errors=first_attempt_errors,
            plugin_root=plugin_root,
        )
        try:
            ack = dispatch(
                retry_prompt,
                subagent_type=subagent_type,
                description="alive-demo stage 0 spine (retry)",
            )
        except Exception as exc:  # noqa: BLE001
            raise Stage0DispatchFailed(
                f"spine retry dispatch raised: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(ack, str):
            raise Stage0DispatchFailed(
                f"spine retry dispatch returned non-str: {type(ack).__name__}"
            )

        spine, second_attempt_errors = _preflight_and_validate(
            spine_path=spine_path,
            partial_dir=canonical_partial,
        )
        if second_attempt_errors:
            if surface_failure_blocks:
                return _surface_double_failure_envelope(
                    second_attempt_errors,
                    partial_dir=canonical_partial,
                    spine_path=spine_path,
                    first_attempt_errors=list(first_attempt_errors),
                )
            raise Stage0RetryExhausted(second_attempt_errors)

        # fn-2-2zz.16: stamp ``stage0_done.json`` on success so Stage 5
        # activation pre-flight (scaffold._validate_partial_ready) finds
        # a uniform frozen marker for every stage 0..4. Stage 0's
        # primary artefact (spine.json) lives separately; this marker
        # is the readiness signal, not the data payload.
        _write_stage0_done_marker(canonical_partial)
        # fn-2-2zz.16: advance the demo-state partial-generations row
        # to "1_anchor" so the orchestrator's status / resume surface
        # reflects that Stage 0 has frozen and Stage 1 is now in
        # flight. Best-effort: legacy / fixture partials with no
        # registered row are no-ops by design.
        _advance_demo_state_stage(canonical_partial, "1_anchor")
        return {
            "spine": spine,
            "spine_path": spine_path,
            "description_path": desc_path,
            "summary_path": summary_path,
            "summarised": summarised,
            "retried": True,
            "first_attempt_errors": list(first_attempt_errors),
        }

    _write_stage0_done_marker(canonical_partial)
    _advance_demo_state_stage(canonical_partial, "1_anchor")
    return {
        "spine": spine,
        "spine_path": spine_path,
        "description_path": desc_path,
        "summary_path": summary_path,
        "summarised": summarised,
        "retried": False,
        "first_attempt_errors": [],
    }


def _advance_demo_state_stage(partial_dir: str, new_stage: str) -> None:
    """Best-effort wrapper around ``state.advance_partial_stage``.

    Mirrors the helper in the other stage modules; swallows all errors
    so a demo-state write failure cannot block the on-disk freeze
    (the spine.json file is the load-bearing contract).
    """
    try:  # pragma: no cover - defensive against pathological env
        import importlib.util  # noqa: PLC0415
        full_name = "alive_demo.state_for_stage0"
        if full_name in sys.modules:
            mod = sys.modules[full_name]
        else:
            target = os.path.join(_DEMO_DIR, "state.py")
            spec = importlib.util.spec_from_file_location(full_name, target)
            if spec is None or spec.loader is None:
                return
            mod = importlib.util.module_from_spec(spec)
            sys.modules[full_name] = mod
            spec.loader.exec_module(mod)
        mod.advance_partial_stage(partial_dir, new_stage)
    except Exception:  # noqa: BLE001
        pass


def _load_lib_for_failure():
    """Lazy-import lib.py for failure-block rendering (fn-2-2zz.13).

    Mirrors :func:`_load_validate`; namespaced sys.modules key so multiple
    plugins importing `lib` don't collide.
    """
    import importlib.util  # noqa: PLC0415
    full_name = "alive_demo.lib"
    if full_name in sys.modules:
        return sys.modules[full_name]
    target = os.path.join(_DEMO_DIR, "lib.py")
    spec = importlib.util.spec_from_file_location(full_name, target)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {target}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _surface_double_failure_envelope(
    errors: List[str],
    *,
    partial_dir: str,
    spine_path: str,
    first_attempt_errors: List[str],
) -> Dict[str, Any]:
    """Build the failure envelope returned by ``run_stage0`` on second-fail.

    Adapter between Stage 0's flat-string error list and ``lib.py``'s
    ValidationResult-shaped consumer. Wraps the strings into the
    canonical ``code/where/evidence`` finding shape so the rendered
    block lists them uniformly with stages 2/3/4. The
    ``rendered_block`` is the user-facing surface; the rest of the
    envelope mirrors ``run_stage0``'s success shape with ``failure_mode``
    set so the orchestrator can branch.
    """
    findings = []
    for err in errors:
        findings.append({
            "code": "preflight" if err.startswith("[") else "stage0",
            "where": "spine.json",
            "evidence": err,
        })

    class _Carrier:  # minimal duck-type for lib.report_validation_double_failure
        def __init__(self, errs):
            self.errors = errs

    lib = _load_lib_for_failure()
    report = lib.report_validation_double_failure(
        stage_id="0",
        validation_result=_Carrier(findings),
        partial_dir=partial_dir,
        raw_output_path=spine_path,
    )
    return {
        "spine": None,
        "spine_path": spine_path,
        "description_path": None,
        "summary_path": None,
        "summarised": False,
        "retried": True,
        "first_attempt_errors": list(first_attempt_errors),
        "failure_mode": "validation_double_failure",
        "second_attempt_errors": list(errors),
        "rendered_block": report["rendered_block"],
        "state_updated": report.get("state_updated", False),
    }


def _load_validate():
    """Load `validate.py` (sibling of stages/) under a namespaced key.

    Mirrors `_load_lib`: namespaced sys.modules key so multiple plugins
    importing `validate` don't collide. Resolved lazily so existing
    Stage 0 tests that only exercise preflight (and don't seed the
    cross-stage rosters) continue to import stage0 cheaply.
    """
    import importlib.util  # noqa: PLC0415
    full_name = "alive_demo.validate"
    if full_name in sys.modules:
        return sys.modules[full_name]
    target = os.path.join(_DEMO_DIR, "validate.py")
    spec = importlib.util.spec_from_file_location(full_name, target)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {target}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _preflight_and_validate(
    *,
    spine_path: str,
    partial_dir: str,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Run preflight + coherence in sequence; return ``(spine, errors)``.

    On preflight failure: returns ``(None, [errors...])``. The model
    needs to fix the structural shape before the coherence layer can
    even be evaluated, so we short-circuit.

    On preflight success but coherence failure: returns
    ``(spine, [coherence errors...])``. The retry feedback embeds
    both kinds.

    On full success: returns ``(spine, [])``.

    Errors are normalised to a flat ``List[str]`` so the existing
    retry feedback formatter (which already accepts a string list per
    :func:`render_spine_prompt_with_feedback`) consumes them without
    a shape change.
    """
    try:
        spine = preflight_spine(spine_path)
    except SpinePreflightError as exc:
        return None, list(exc.errors)

    validate = _load_validate()
    result = validate.validate_stage("0", partial_dir)
    if result.is_ok():
        return spine, []
    flat_errors: List[str] = []
    for err in result.errors:
        code = err.get("code", "?")
        where = err.get("where", "?")
        evidence = err.get("evidence", "")
        flat_errors.append(f"[{code}] {where}: {evidence}")
    return spine, flat_errors


__all__ = (
    "SCHEMA_VERSION",
    "DESCRIPTION_TOKEN_BUDGET",
    "REQUIRED_TOP_LEVEL_KEYS",
    "SPINE_PROMPT_RELPATH",
    "SUMMARIZE_PROMPT_RELPATH",
    "SUBAGENT_BRIEF_RELPATH",
    "DEFAULT_SUBAGENT_TYPE",
    "Stage0Error",
    "SpinePreflightError",
    "Stage0DispatchFailed",
    "Stage0RetryExhausted",
    "estimate_tokens",
    "needs_summary",
    "partial_input_dir",
    "partial_stage_outputs_dir",
    "spine_output_path",
    "done_marker_path",
    "description_input_path",
    "description_summary_path",
    "persist_description",
    "render_subagent_brief",
    "render_summarize_prompt",
    "render_spine_prompt",
    "render_spine_prompt_with_feedback",
    "preflight_spine",
    "run_stage0",
)
