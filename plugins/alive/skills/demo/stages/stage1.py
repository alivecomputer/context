"""Stage 1 -- anchor confirmation UX: per-moment 4-option loop helpers.

Stage 1 is **UX-only** in the `/alive:demo` pipeline: no LLM dispatch, no
validator, just an in-session confirmation loop driven by the parent
squirrel via `AskUserQuestion`. Stage 0 has already drafted the
`spine.json` (including `anchor_moments[]`); the human now ratifies each
moment one-by-one before downstream stages cross-reference them as
load-bearing narrative pivots.

This module owns the **Python side** of that loop: load the spine, render
the per-moment block (so the parent skill renders it inline), mutate
`anchor_moments.json` on disk for each user choice, and freeze the
envelope when the human is done. The four user-facing options are:

  1. **Accept** -- copy the moment from spine into the confirmed envelope.
  2. **Regenerate** -- return a fresh Stage 0 sub-prompt scoped to that
     single moment so the parent squirrel can re-fire the Agent tool
     and write the result back via :func:`apply_regenerated_moment`.
  3. **Edit prose** -- rewrite just the `summary` field (the 80-150 word
     hook). Validates length, second-person voice, no em dashes.
  4. **Replace** -- full replacement (`name`, `date`, `summary`,
     `walnut_slugs`, `people_slugs`). Validates slug format, ISO date,
     and that every `walnut_slugs[*]` / `people_slugs[*]` resolves to
     an entry in the spine's rosters.

The parent skill (`anchor_confirm.md`) drives the loop and calls these
functions to mutate state on disk. The runtime's `AskUserQuestion` tool
is the choice-prompt mechanism; this module never tries to call it
directly (Python can't invoke runtime tools from inside a stage
helper).

The `anchor_moments.json` envelope shape is::

    {
        "schema_version": "0.1",
        "confirmed": [<moment dict>, ...],
        "frozen": <bool>,
        "frozen_at": <ISO 8601 UTC string or null>
    }

Anchor IDs (the `slug` field) are immutable AFTER freeze. Downstream
stages cross-reference them; renaming would silently break Stage 2 / 3 /
4 prose grounding. :func:`freeze_anchors` is the locked transition; any
mutation after that raises :class:`Stage1Frozen`.

Stdlib-only. Imports `lib.is_valid_slug` (sibling) and (transitively
through path bootstrap) `_common.atomic_write_json` /
`_common.atomic_write_text` / `_common.iso_now`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Path bootstrap -- mirrors stage0.py's pattern so direct imports under
# tests resolve `_common` without going through `cli.py`.
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

    Mirrors the cli_register / stage0 loader pattern: the namespaced
    `alive_demo.lib` key is unique across the process, avoiding clashes
    if a future plugin ships its own `lib`.
    """
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


def _load_stage0():
    """Load the stage0 sibling for prompt-rendering reuse on regenerate."""
    full_name = "alive_demo.stage0"
    if full_name in sys.modules:
        return sys.modules[full_name]
    target = os.path.join(_HERE, "stage0.py")
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

#: Schema version stamped into `anchor_moments.json`. Independent of the
#: spine's schema_version: a future spine bump that doesn't change the
#: anchor-moment shape doesn't need to bump this.
SCHEMA_VERSION = "0.1"

#: Closed key set per anchor moment. Mirrors stage0._ANCHOR_KEYS so
#: replacements / edits don't drift from the spine schema.
_ANCHOR_KEYS = frozenset({
    "slug", "name", "date", "summary", "walnut_slugs", "people_slugs",
})

#: Word-count band for the `summary` (the 80-150 word "hook" the user
#: edits in option 3). Source: epic spec § "Stage 1: anchor confirmation
#: UX + few-shot exemplars" / task fn-2-2zz.5 acceptance row 4.
HOOK_MIN_WORDS = 80
HOOK_MAX_WORDS = 150

#: Strict ISO 8601 date pattern. Mirrors stage0._ISO_DATE_RE so a moment
#: replaced via :func:`replace_moment` cannot bypass the same shape rule
#: Stage 0 enforced.
_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

#: First-person pronoun + contraction set. Used by :func:`edit_moment_prose`
#: to keep edited summaries in second person ("you / your") matching the
#: ALIVE narrative voice + few-shot exemplars. Word-boundary matched
#: case-insensitively so "Imogen" / "myth" / "we'll" don't trip the check.
_FIRST_PERSON_TOKENS = (
    "i", "me", "my", "mine", "myself",
    "we", "us", "our", "ours", "ourselves",
    "i'm", "i've", "i'd", "i'll",
    "we're", "we've", "we'd", "we'll",
)
_FIRST_PERSON_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _FIRST_PERSON_TOKENS) + r")\b",
    re.IGNORECASE,
)

#: Em-dash + en-dash + horizontal-bar codepoints (U+2014, U+2013, U+2015)
#: disallowed in user-visible prose per the standing voice rule
#: (`feedback_no_em_dashes.md` in user memory). The horizontal-bar U+2015 is
#: included for completeness; macOS smart-substitution sometimes inserts it
#: in place of em-dash. These literals are the validator's targets, not
#: prose this codebase emits.
_DASH_CHARS = (
    "—",  # em-dash
    "–",  # en-dash
    "―",  # horizontal-bar
)

#: Paths inside the partial directory.
_STAGE_OUTPUTS_SUBDIR = "_stage_outputs"
_SPINE_FILENAME = "spine.json"
_ANCHORS_FILENAME = "anchor_moments.json"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class Stage1Error(RuntimeError):
    """Base error for Stage 1 mutations."""


class Stage1Frozen(Stage1Error):
    """Raised when a mutation is attempted after :func:`freeze_anchors`."""


class Stage1Validation(Stage1Error):
    """Raised when user-supplied content fails Stage 1 invariants.

    Carries an `errors` list of human-readable strings the parent skill
    surfaces inside a bordered block before re-prompting.
    """

    def __init__(self, errors: List[str]) -> None:
        self.errors = list(errors)
        super().__init__("Stage 1 validation failed: " + "; ".join(errors))


class Stage1NotFound(Stage1Error):
    """Raised when a referenced moment slug is not present in the spine."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _abspartial(partial_dir) -> str:
    """Canonicalize a partial directory path to absolute, normalised form.

    Accepts `str` or anything coercible via `os.fspath` (e.g. `pathlib.Path`).
    Mirrors stage0._abspartial: every path the parent skill hands to a
    runtime tool needs to be absolute so the dispatched subagent (which
    runs in a fresh cwd) doesn't silently break the disk contract.
    """
    if hasattr(partial_dir, "__fspath__"):
        partial_dir = os.fspath(partial_dir)
    if not isinstance(partial_dir, str):
        raise TypeError(
            f"partial_dir must be path-like; got {type(partial_dir).__name__}"
        )
    return os.path.normpath(os.path.abspath(partial_dir))


def stage_outputs_dir(partial_dir) -> str:
    """Absolute `<partial>/_stage_outputs/`. Created if needed."""
    out = os.path.join(_abspartial(partial_dir), _STAGE_OUTPUTS_SUBDIR)
    os.makedirs(out, exist_ok=True)
    return out


def spine_path(partial_dir) -> str:
    """Absolute path of the spine.json the Stage 0 subagent wrote."""
    return os.path.join(stage_outputs_dir(partial_dir), _SPINE_FILENAME)


def anchors_path(partial_dir) -> str:
    """Absolute path of the anchor_moments.json envelope owned by Stage 1."""
    return os.path.join(stage_outputs_dir(partial_dir), _ANCHORS_FILENAME)


# ---------------------------------------------------------------------------
# Spine loading
# ---------------------------------------------------------------------------

def load_spine(partial_dir) -> Dict[str, Any]:
    """Read + parse `<partial>/_stage_outputs/spine.json`.

    Returns the parsed dict on success. Raises :class:`Stage1Error` with
    a clear message on missing file or malformed JSON -- the parent skill
    catches this and renders a bordered-block error, then exits the
    Stage 1 loop so the human can decide whether to rerun Stage 0 or
    cancel.
    """
    target = spine_path(partial_dir)
    if not os.path.isfile(target):
        raise Stage1Error(
            f"spine.json not found at {target}; run Stage 0 first"
        )
    try:
        with open(target, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise Stage1Error(
            f"spine.json at {target} unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise Stage1Error(
            f"spine.json at {target} is not valid JSON: "
            f"line {exc.lineno} col {exc.colno}: {exc.msg}"
        ) from exc


def _spine_moment_by_slug(spine: Dict[str, Any], moment_slug: str) -> Dict[str, Any]:
    """Return the spine's anchor moment with `slug == moment_slug`.

    Raises :class:`Stage1NotFound` if the slug is not present. The lookup
    is O(N) -- anchor lists in v3.2 are small (3-12 entries per the size
    soft targets) so we don't bother with caching.
    """
    moments = spine.get("anchor_moments")
    if not isinstance(moments, list):
        raise Stage1Error("spine.json has no anchor_moments array")
    for m in moments:
        if isinstance(m, dict) and m.get("slug") == moment_slug:
            return m
    raise Stage1NotFound(
        f"anchor moment with slug {moment_slug!r} not in spine"
    )


# ---------------------------------------------------------------------------
# Anchor envelope IO
# ---------------------------------------------------------------------------

def _default_envelope() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "confirmed": [],
        "frozen": False,
        "frozen_at": None,
    }


def load_anchors(partial_dir) -> Dict[str, Any]:
    """Read the anchor_moments.json envelope (or return a fresh default).

    First-write semantics: if the file does not yet exist, return the
    default envelope without writing it. This keeps :func:`accept_moment`
    idempotent -- the first accept also creates the file.
    """
    target = anchors_path(partial_dir)
    if not os.path.isfile(target):
        return _default_envelope()
    try:
        with open(target, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise Stage1Error(
            f"anchor_moments.json at {target} unreadable: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        env = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Stage1Error(
            f"anchor_moments.json at {target} is not valid JSON: "
            f"line {exc.lineno} col {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(env, dict):
        raise Stage1Error(
            f"anchor_moments.json at {target} top-level value must be object"
        )
    # Tolerate older / hand-edited envelopes that lack the bookkeeping
    # fields -- surface them with safe defaults rather than crashing the
    # confirmation loop.
    env.setdefault("schema_version", SCHEMA_VERSION)
    env.setdefault("confirmed", [])
    env.setdefault("frozen", False)
    env.setdefault("frozen_at", None)
    return env


def _save_anchors(partial_dir, env: Dict[str, Any]) -> str:
    """Atomically write the envelope. Returns the absolute path written."""
    target = anchors_path(partial_dir)
    atomic_write_json(target, env)
    return target


def is_frozen(partial_dir) -> bool:
    """Return True iff the envelope's `frozen` flag is set.

    A non-existent envelope file is treated as not frozen so the parent
    skill can call this before any accept ever happened without
    side-effecting the partial directory.
    """
    env = load_anchors(partial_dir)
    return bool(env.get("frozen"))


def _check_unfrozen(partial_dir) -> Dict[str, Any]:
    """Load the envelope and raise :class:`Stage1Frozen` if it is frozen."""
    env = load_anchors(partial_dir)
    if env.get("frozen"):
        raise Stage1Frozen(
            "anchor_moments are frozen; mutations are no longer permitted"
        )
    return env


# ---------------------------------------------------------------------------
# Block rendering
# ---------------------------------------------------------------------------

def render_moment_block(moment: Dict[str, Any]) -> str:
    """Render a per-moment bordered block as a string.

    Layout::

        ╭─ 🐿️ anchor moment N: <name>
        │
        │   <ISO date>
        │
        │   <summary, line-wrapped at 78 cols if a single paragraph>
        │
        │   walnuts:  <slug>, <slug>
        │   people:   <slug>, <slug>
        │
        │   ▸ Accept / regenerate / edit prose / replace?
        ╰─

    The `▸` line is advisory only -- the actual choice flows through
    `AskUserQuestion`. The block is meant to be printed inline by the
    parent skill before the question tool fires.

    Uses `lib.format_block` for the visual contract so any future change
    to the block characters propagates automatically.
    """
    if not isinstance(moment, dict):
        raise TypeError(
            f"moment must be dict; got {type(moment).__name__}"
        )
    lib = _load_lib()
    name = str(moment.get("name", "(unnamed)"))
    date = str(moment.get("date", ""))
    summary = str(moment.get("summary", ""))
    walnuts = ", ".join(moment.get("walnut_slugs") or []) or "(none)"
    people = ", ".join(moment.get("people_slugs") or []) or "(none)"
    slug = str(moment.get("slug", ""))

    body_lines = [
        f"slug:  {slug}",
        f"date:  {date}",
        "",
        summary,
        "",
        f"walnuts:  {walnuts}",
        f"people:   {people}",
        "",
        "▸ Accept / regenerate / edit prose / replace?",
    ]
    return lib.format_block(f"anchor moment: {name}", "\n".join(body_lines))


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _word_count(text: str) -> int:
    """Whitespace-split word count. Empty / non-str → 0."""
    if not isinstance(text, str):
        return 0
    return len([w for w in text.split() if w])


def _validate_hook_prose(text: str, errors: List[str]) -> None:
    """Validate a hook (the `summary` field) against the voice contract.

    Rules:
      - 80-150 words inclusive (`HOOK_MIN_WORDS` / `HOOK_MAX_WORDS`).
      - No em / en / horizontal bar dashes (the standing rule).
      - No first-person pronouns / contractions (anchors are second-person).
    """
    if not isinstance(text, str) or not text.strip():
        errors.append("summary must be non-empty string")
        return
    wc = _word_count(text)
    if wc < HOOK_MIN_WORDS or wc > HOOK_MAX_WORDS:
        errors.append(
            f"summary must be {HOOK_MIN_WORDS}-{HOOK_MAX_WORDS} words; "
            f"got {wc}"
        )
    found_dashes = [d for d in _DASH_CHARS if d in text]
    if found_dashes:
        errors.append(
            "summary contains disallowed dash character(s) "
            f"(em / en / horizontal bar): use commas, periods, parens, "
            f"or colons instead"
        )
    fp = _FIRST_PERSON_RE.search(text)
    if fp is not None:
        errors.append(
            f"summary contains first-person token {fp.group(0)!r}; "
            f"anchor moments must be written in second person"
        )


def _validate_iso_date(value, where: str, errors: List[str]) -> None:
    """Strict zero-padded `YYYY-MM-DD` + real-calendar check.

    Mirrors stage0._validate_iso_date. Kept private here rather than
    importing across stages so Stage 1 owns its validation surface (no
    cross-stage import cycles to manage).
    """
    if not isinstance(value, str):
        errors.append(f"{where} must be string, got {type(value).__name__}")
        return
    if not _ISO_DATE_RE.match(value):
        errors.append(
            f"{where} must be strict zero-padded ISO 8601 YYYY-MM-DD, "
            f"got {value!r}"
        )
        return
    import datetime as _dt
    try:
        _dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        errors.append(
            f"{where} must be a real calendar date, got {value!r}: {exc}"
        )


def _spine_known_slugs(spine: Dict[str, Any]) -> Tuple[set, set]:
    """Return (walnut_slugs, people_slugs) sets from the spine rosters."""
    walnut_slugs = set()
    for w in spine.get("walnut_roster") or []:
        if isinstance(w, dict):
            s = w.get("slug")
            if isinstance(s, str):
                walnut_slugs.add(s)
    people_slugs = set()
    for p in spine.get("people_roster") or []:
        if isinstance(p, dict):
            s = p.get("slug")
            if isinstance(s, str):
                people_slugs.add(s)
    return walnut_slugs, people_slugs


def _validate_full_moment(
    moment: Dict[str, Any],
    *,
    spine: Dict[str, Any],
    expected_slug: Optional[str],
    errors: List[str],
) -> None:
    """Full structural + cross-reference validation of a replacement moment.

    `expected_slug`, when non-None, pins the moment's slug so a replace
    cannot rename anchors out from under downstream stages. Pass None
    only on the regenerate path where the subagent freshly re-rolls the
    moment under the same slug -- we still re-validate that the slug is
    well-formed and matches.
    """
    if not isinstance(moment, dict):
        errors.append(f"moment must be object, got {type(moment).__name__}")
        return

    actual = set(moment.keys())
    extra = actual - _ANCHOR_KEYS
    if extra:
        errors.append(f"moment has unexpected keys: {sorted(extra)}")
    missing = _ANCHOR_KEYS - actual
    if missing:
        errors.append(f"moment missing required keys: {sorted(missing)}")
    if extra or missing:
        return

    lib = _load_lib()
    slug = moment.get("slug")
    if not isinstance(slug, str) or not lib.is_valid_slug(slug):
        errors.append(f"moment.slug is not a valid slug: {slug!r}")
    elif expected_slug is not None and slug != expected_slug:
        errors.append(
            f"moment.slug must remain {expected_slug!r} (anchor IDs are "
            f"immutable across regenerate / replace); got {slug!r}"
        )

    name = moment.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("moment.name must be non-empty string")

    _validate_iso_date(moment.get("date"), "moment.date", errors)
    _validate_hook_prose(moment.get("summary", ""), errors)

    walnut_slugs, people_slugs = _spine_known_slugs(spine)
    for list_key, known in (
        ("walnut_slugs", walnut_slugs),
        ("people_slugs", people_slugs),
    ):
        v = moment.get(list_key)
        if not isinstance(v, list):
            errors.append(f"moment.{list_key} must be array")
            continue
        for i, s in enumerate(v):
            if not isinstance(s, str) or not lib.is_valid_slug(s):
                errors.append(
                    f"moment.{list_key}[{i}] is not a valid slug: {s!r}"
                )
                continue
            if s not in known:
                errors.append(
                    f"moment.{list_key}[{i}] {s!r} not in spine's "
                    f"{list_key.replace('_slugs', '_roster')}"
                )


# ---------------------------------------------------------------------------
# Mutation entry points (1: accept, 2: regenerate dispatch, 3: edit prose,
# 4: replace)
# ---------------------------------------------------------------------------

def _envelope_index(env: Dict[str, Any], slug: str) -> Optional[int]:
    """Return the index of `slug` in `env["confirmed"]`, or None."""
    confirmed = env.get("confirmed") or []
    for i, m in enumerate(confirmed):
        if isinstance(m, dict) and m.get("slug") == slug:
            return i
    return None


def accept_moment(partial_dir, moment_slug: str) -> Dict[str, Any]:
    """Option 1 -- accept the spine's draft of `moment_slug` as-is.

    Idempotent: a second accept on the same slug is a no-op (the first
    write already pinned that moment into the envelope; replaying the
    accept must not duplicate).

    Returns the updated envelope. Raises :class:`Stage1Frozen` if the
    envelope is already frozen, :class:`Stage1NotFound` if the slug is
    not in the spine.
    """
    env = _check_unfrozen(partial_dir)
    spine = load_spine(partial_dir)
    moment = _spine_moment_by_slug(spine, moment_slug)

    confirmed = list(env.get("confirmed") or [])
    idx = _envelope_index(env, moment_slug)
    if idx is None:
        confirmed.append(dict(moment))
    else:
        # Already-confirmed accept: rewrite with the spine's current
        # version (covers the case where regenerate happened between
        # accepts and the user wants the latest accepted as-is).
        confirmed[idx] = dict(moment)

    env["confirmed"] = confirmed
    _save_anchors(partial_dir, env)
    return env


def regenerate_moment_prompt(
    partial_dir,
    moment_slug: str,
    feedback: str,
    *,
    world_root: str,
    plugin_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Option 2 -- return the prompt + brief the parent must dispatch.

    Stage 1 helpers cannot transitively dispatch a subagent: the Agent
    tool is a runtime primitive the parent squirrel owns. So this
    function returns a structured envelope the parent skill consumes::

        {
            "subagent_type": "general-purpose",
            "description": "alive-demo stage 1 regenerate <slug>",
            "prompt": <CONTEXT/TASK-wrapped prompt>,
            "expected_output_path": <abs path to write the new moment to>,
        }

    The parent fires `Task(subagent_type=..., description=..., prompt=...)`
    with these fields; the dispatched subagent writes a single anchor
    moment object (not a full spine) to `expected_output_path`. The
    parent then calls :func:`apply_regenerated_moment` with that path.

    `feedback` is the user's free-text hint ("make it more about the
    pivot, less about Marcos") that the prompt embeds verbatim.

    Why split into prompt-render + apply: the dispatch boundary is
    asynchronous from this module's POV. Returning the prompt makes the
    behaviour testable (no mocking the runtime) and matches the
    stage0._dispatch convention.
    """
    env = _check_unfrozen(partial_dir)  # noqa: F841 (raises if frozen)
    spine = load_spine(partial_dir)
    current = _spine_moment_by_slug(spine, moment_slug)

    if not isinstance(feedback, str):
        raise TypeError(
            f"feedback must be str; got {type(feedback).__name__}"
        )

    canonical_partial = _abspartial(partial_dir)
    output_dir = os.path.join(canonical_partial, "_stage_outputs", "regenerated")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{moment_slug}.json")

    stage0 = _load_stage0()
    root = plugin_root or resolve_plugin_root()
    brief = stage0.render_subagent_brief(world_root=world_root, plugin_root=root)

    walnut_slugs, people_slugs = _spine_known_slugs(spine)
    body = (
        "# Stage 1 -- Regenerate single anchor moment\n"
        "\n"
        "The human has reviewed an anchor moment from the spine and asked\n"
        "for a fresh take. Your job is to rewrite ONE anchor moment to a\n"
        "single JSON file -- not the whole spine.\n"
        "\n"
        "## Anchor moment to regenerate\n"
        "\n"
        f"slug: `{moment_slug}` (this is IMMUTABLE -- keep it identical)\n"
        "\n"
        "Current draft:\n"
        "\n"
        "```json\n"
        + json.dumps(current, indent=2, ensure_ascii=False)
        + "\n"
        "```\n"
        "\n"
        "## Human feedback (verbatim)\n"
        "\n"
        f"{feedback or '(no feedback supplied -- propose a fresh angle)'}\n"
        "\n"
        "## Output contract\n"
        "\n"
        f"Write a single JSON object to:\n\n```\n{output_path}\n```\n\n"
        "via the standard atomic-write helper (`_common.atomic_write_json`).\n"
        "The object MUST have exactly these keys (no extras, none missing):\n"
        "\n"
        "  - `slug`: string, `^[a-z0-9]+(-[a-z0-9]+)*$`. Must equal "
        f"`{moment_slug}`.\n"
        "  - `name`: short human label, distinct from current.\n"
        "  - `date`: strict zero-padded `YYYY-MM-DD`.\n"
        "  - `summary`: 80-150 word second-person hook. NO em dashes "
        "(use commas / periods / parens / colons). No first-person "
        "pronouns.\n"
        "  - `walnut_slugs`: array of slugs drawn ONLY from the spine's "
        f"walnut_roster: {sorted(walnut_slugs)}\n"
        "  - `people_slugs`: array of slugs drawn ONLY from the spine's "
        f"people_roster: {sorted(people_slugs)}\n"
        "\n"
        "## Style\n"
        "\n"
        "Match the voice of the existing exemplars at "
        "`plugins/alive/templates/demo/anchor_moment_examples.json`:\n"
        "vivid, concrete, second-person, sensory specificity, ends on "
        "implicit forward tension rather than closure.\n"
        "\n"
        "## Return value\n"
        "\n"
        "After writing the file, return ONE LINE acknowledging completion. "
        "Do not paste the JSON. The dispatcher reads it from disk and "
        "validates against the spine.\n"
    )
    prompt = (
        "CONTEXT:\n"
        f"{brief}\n"
        "\n"
        "TASK:\n"
        f"{body}"
    )
    return {
        "subagent_type": "general-purpose",
        "description": f"alive-demo stage 1 regenerate {moment_slug}",
        "prompt": prompt,
        "expected_output_path": output_path,
    }


def apply_regenerated_moment(
    partial_dir,
    moment_slug: str,
    output_path: str,
) -> Dict[str, Any]:
    """Read the regenerate-subagent's output and store it as confirmed.

    Reads `output_path` (the file the dispatched subagent wrote),
    validates it against the spine, and writes it into the envelope.
    Raises :class:`Stage1Validation` on any structural or cross-reference
    failure so the parent skill can re-prompt.

    Note: regeneration goes straight into `confirmed[]` (the human asked
    for a regen and committed to it via that choice). If they want to
    review again, they re-enter the loop on the same slug.
    """
    env = _check_unfrozen(partial_dir)
    spine = load_spine(partial_dir)

    if not os.path.isfile(output_path):
        raise Stage1Error(
            f"regenerated moment file not found at {output_path}"
        )
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise Stage1Error(
            f"regenerated moment at {output_path} unreadable: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        moment = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Stage1Validation([
            f"regenerated moment at {output_path} is not valid JSON: "
            f"line {exc.lineno} col {exc.colno}: {exc.msg}"
        ]) from exc

    errors: List[str] = []
    _validate_full_moment(
        moment, spine=spine, expected_slug=moment_slug, errors=errors,
    )
    if errors:
        raise Stage1Validation(errors)

    confirmed = list(env.get("confirmed") or [])
    idx = _envelope_index(env, moment_slug)
    if idx is None:
        confirmed.append(dict(moment))
    else:
        confirmed[idx] = dict(moment)
    env["confirmed"] = confirmed
    _save_anchors(partial_dir, env)
    return env


def edit_moment_prose(
    partial_dir,
    moment_slug: str,
    new_summary: str,
) -> Dict[str, Any]:
    """Option 3 -- replace just the `summary` (the hook) of one moment.

    The other fields (slug, name, date, walnut_slugs, people_slugs) are
    preserved from whatever is currently confirmed (or, on first edit,
    from the spine draft). Validates the new summary against the
    voice contract: 80-150 words, no em/en/horizontal-bar dashes, no
    first-person pronouns.

    Raises :class:`Stage1Validation` on voice-rule failure with a list
    of human-readable errors. Raises :class:`Stage1Frozen` if the
    envelope is frozen.
    """
    env = _check_unfrozen(partial_dir)

    errors: List[str] = []
    _validate_hook_prose(new_summary, errors)
    if errors:
        raise Stage1Validation(errors)

    confirmed = list(env.get("confirmed") or [])
    idx = _envelope_index(env, moment_slug)
    if idx is None:
        # First edit: pull the rest of the moment from the spine.
        spine = load_spine(partial_dir)
        base = dict(_spine_moment_by_slug(spine, moment_slug))
    else:
        base = dict(confirmed[idx])

    base["summary"] = new_summary
    if idx is None:
        confirmed.append(base)
    else:
        confirmed[idx] = base
    env["confirmed"] = confirmed
    _save_anchors(partial_dir, env)
    return env


def replace_moment(
    partial_dir,
    moment_slug: str,
    new_moment: Dict[str, Any],
) -> Dict[str, Any]:
    """Option 4 -- full replacement of one moment with user-supplied content.

    `new_moment` must carry exactly the closed key set documented in
    `_ANCHOR_KEYS`: `slug`, `name`, `date`, `summary`, `walnut_slugs`,
    `people_slugs`. The slug MUST equal `moment_slug` (anchor IDs are
    immutable from this stage forward -- downstream cross-refs key on
    them). All entity refs MUST resolve to entries in the spine's
    walnut + people rosters; an unknown slug here is a Stage 0
    coherence-retry trigger and must be flagged before freeze.

    Raises :class:`Stage1Validation` with a full error list on any
    failure. The envelope is NOT mutated unless validation passes
    (atomic-or-nothing).
    """
    env = _check_unfrozen(partial_dir)
    spine = load_spine(partial_dir)

    errors: List[str] = []
    _validate_full_moment(
        new_moment, spine=spine, expected_slug=moment_slug, errors=errors,
    )
    if errors:
        raise Stage1Validation(errors)

    confirmed = list(env.get("confirmed") or [])
    idx = _envelope_index(env, moment_slug)
    clean = {k: new_moment[k] for k in _ANCHOR_KEYS}
    # Defensive copy of the slug-list values so the caller cannot mutate
    # them after we've stored them.
    clean["walnut_slugs"] = list(clean["walnut_slugs"])
    clean["people_slugs"] = list(clean["people_slugs"])
    if idx is None:
        confirmed.append(clean)
    else:
        confirmed[idx] = clean
    env["confirmed"] = confirmed
    _save_anchors(partial_dir, env)
    return env


# ---------------------------------------------------------------------------
# Freeze
# ---------------------------------------------------------------------------

def freeze_anchors(partial_dir) -> Dict[str, Any]:
    """Lock the envelope. After this, mutations raise :class:`Stage1Frozen`.

    Idempotent: calling on an already-frozen envelope re-stamps
    `frozen_at` to now (the parent skill never re-freezes in normal
    flow, but the property keeps the function safe). Requires:

      1. Every CURRENT spine moment has an entry in `confirmed[]`
         (no missing confirmations -- otherwise Stage 2 prose
         subagents would have to fall back to spine drafts that the
         human never approved).
      2. NO stale `confirmed[]` entries whose slug is absent from the
         CURRENT spine (a spine rewritten between confirmations would
         otherwise smuggle obsolete anchors into the frozen envelope,
         and downstream stages would cross-reference anchors that no
         longer exist in the spine contract).

    Both checks are surfaced as a single :class:`Stage1Validation` so
    the parent skill can render a complete error list rather than
    bouncing the human through two consecutive failures.
    """
    env = load_anchors(partial_dir)
    spine = load_spine(partial_dir)
    spine_slugs = []
    for m in spine.get("anchor_moments") or []:
        if isinstance(m, dict) and isinstance(m.get("slug"), str):
            spine_slugs.append(m["slug"])
    spine_slug_set = set(spine_slugs)
    confirmed_slugs = []
    for m in (env.get("confirmed") or []):
        if isinstance(m, dict) and isinstance(m.get("slug"), str):
            confirmed_slugs.append(m["slug"])
    confirmed_slug_set = set(confirmed_slugs)

    errors: List[str] = []
    missing = [s for s in spine_slugs if s not in confirmed_slug_set]
    if missing:
        errors.append(
            f"cannot freeze: {len(missing)} moment(s) not confirmed: "
            f"{missing}"
        )
    stale = [s for s in confirmed_slugs if s not in spine_slug_set]
    if stale:
        # Stale confirmed entries mean the spine was rewritten between
        # confirmations (regenerate elsewhere, hand-edit, etc.). The
        # frozen envelope is the contract for downstream stages -- it
        # MUST match the current spine, never a previous revision.
        errors.append(
            f"cannot freeze: {len(stale)} confirmed moment(s) no longer "
            f"in spine (stale): {stale}; re-run Stage 1 against the "
            f"current spine, or revert the spine, before freezing"
        )
    if errors:
        raise Stage1Validation(errors)

    env["frozen"] = True
    env["frozen_at"] = iso_now()
    env["schema_version"] = SCHEMA_VERSION
    _save_anchors(partial_dir, env)
    # fn-2-2zz.16: advance the demo-state partial-generations row to
    # the next in-flight stage so ``alive demo status`` / ``resume``
    # reflect Stage 1 freeze without the orchestrator owning the
    # state mutation. Best-effort: legacy partials / test fixtures
    # without a registered row are no-ops by design.
    _advance_demo_state_stage(partial_dir, "2_entities")
    return env


def _advance_demo_state_stage(partial_dir: str, new_stage: str) -> None:
    """Best-effort wrapper around ``state.advance_partial_stage``.

    Loads the sibling ``state.py`` via importlib + calls the public
    helper. Swallows ``DemoStateError`` / lock-timeout / missing-state
    failures: stage progression is a UX surface for ``alive demo
    status`` + ``resume``; an inability to write demo-state must not
    block the freeze itself, which is the load-bearing on-disk
    contract.
    """
    try:  # pragma: no cover - defensive against pathological env
        import importlib.util  # noqa: PLC0415
        full_name = "alive_demo.state_for_stage1"
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
        # Stage progression is best-effort.  The on-disk freeze is the
        # source of truth; demo-state is a read-through cache that
        # ``state.self_heal`` reconciles on the next load.
        pass


# ---------------------------------------------------------------------------
# Convenience: list-pending helper for the parent skill loop
# ---------------------------------------------------------------------------

def pending_slugs(partial_dir) -> List[str]:
    """Return the spine's anchor slugs that have NOT yet been confirmed.

    The parent skill drives the loop "for slug in pending_slugs(...):
    show block + ask". When :func:`pending_slugs` returns an empty list,
    the parent prompts to freeze.
    """
    env = load_anchors(partial_dir)
    spine = load_spine(partial_dir)
    confirmed = {
        m.get("slug")
        for m in (env.get("confirmed") or [])
        if isinstance(m, dict)
    }
    out: List[str] = []
    for m in spine.get("anchor_moments") or []:
        if isinstance(m, dict):
            s = m.get("slug")
            if isinstance(s, str) and s not in confirmed:
                out.append(s)
    return out


# ---------------------------------------------------------------------------
# Exemplar self-validation (used by tests + by the README author check)
# ---------------------------------------------------------------------------

def validate_exemplars_file(path: str) -> List[str]:
    """Validate `templates/demo/anchor_moment_examples.json`.

    Returns a list of human-readable error strings (empty == OK).
    Checks:
      - File exists, parses as JSON, top-level is object.
      - `schema_version`, `examples` keys present.
      - 5+ entries in `examples`.
      - All five diversity dimensions covered:
        career-pivot, relationship-shift, loss, creative-breakthrough,
        identity-shift.
      - Each entry: id (slug), diversity_dimension, name, date (ISO),
        hook (80-150 words, second-person, no dashes), entity_refs (list).
    """
    errors: List[str] = []
    if not os.path.isfile(path):
        errors.append(f"exemplars file not found at {path}")
        return errors
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        errors.append(
            f"exemplars file at {path} is not valid JSON: "
            f"line {exc.lineno} col {exc.colno}: {exc.msg}"
        )
        return errors
    if not isinstance(data, dict):
        errors.append("exemplars top-level value must be object")
        return errors

    for key in ("schema_version", "examples"):
        if key not in data:
            errors.append(f"exemplars missing required key {key!r}")
    examples = data.get("examples")
    if not isinstance(examples, list):
        errors.append("exemplars.examples must be array")
        return errors
    if len(examples) < 5:
        errors.append(
            f"exemplars must contain at least 5 entries; got {len(examples)}"
        )

    required_dimensions = {
        "career-pivot",
        "relationship-shift",
        "loss",
        "creative-breakthrough",
        "identity-shift",
    }
    seen_dimensions = set()

    lib = _load_lib()
    for i, ex in enumerate(examples):
        if not isinstance(ex, dict):
            errors.append(f"exemplars.examples[{i}] must be object")
            continue
        ex_id = ex.get("id")
        if not isinstance(ex_id, str) or not lib.is_valid_slug(ex_id):
            errors.append(
                f"exemplars.examples[{i}].id is not a valid slug: {ex_id!r}"
            )
        dim = ex.get("diversity_dimension")
        if not isinstance(dim, str):
            errors.append(
                f"exemplars.examples[{i}].diversity_dimension must be string"
            )
        else:
            seen_dimensions.add(dim)
        name = ex.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(
                f"exemplars.examples[{i}].name must be non-empty string"
            )
        _validate_iso_date(
            ex.get("date"),
            f"exemplars.examples[{i}].date",
            errors,
        )
        hook = ex.get("hook")
        hook_errors: List[str] = []
        _validate_hook_prose(hook, hook_errors)
        for he in hook_errors:
            errors.append(f"exemplars.examples[{i}].hook: {he}")
        refs = ex.get("entity_refs")
        if not isinstance(refs, list):
            errors.append(
                f"exemplars.examples[{i}].entity_refs must be array"
            )

    missing_dimensions = required_dimensions - seen_dimensions
    if missing_dimensions:
        errors.append(
            "exemplars do not cover all required diversity dimensions; "
            f"missing: {sorted(missing_dimensions)}"
        )

    return errors


__all__ = (
    "SCHEMA_VERSION",
    "HOOK_MIN_WORDS",
    "HOOK_MAX_WORDS",
    "Stage1Error",
    "Stage1Frozen",
    "Stage1Validation",
    "Stage1NotFound",
    "stage_outputs_dir",
    "spine_path",
    "anchors_path",
    "load_spine",
    "load_anchors",
    "is_frozen",
    "render_moment_block",
    "accept_moment",
    "regenerate_moment_prompt",
    "apply_regenerated_moment",
    "edit_moment_prose",
    "replace_moment",
    "freeze_anchors",
    "pending_slugs",
    "validate_exemplars_file",
)
