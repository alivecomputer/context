"""Stage 4 -- insights synthesis: dispatch + collect-validate + freeze.

Stage 4 of the `/alive:demo` generation pipeline runs a SINGLE subagent
that reads the frozen spine + anchor envelope + Stage 2 entity
scaffolds + the Stage 3 timeline (world log + per-person logs +
per-walnut logs) and writes the standing insights:

  * `<partial>/_stage_outputs/insights.md`              -- world-level
    cross-walnut standing insights
  * `<partial>/_stage_outputs/walnut-insights/<slug>.md` -- per-walnut
    insights, only where the timeline supports a real recurring pattern
    (NOT every walnut produces one)

Per the spec's locked decisions:

  * Single subagent (mirrors Stage 3 -- one head holds the timeline in
    context to keep cross-references coherent).
  * Subagent writes to disk via the standard atomic helpers; this
    module reads the files back.
  * Citation format on every insight bullet:
    `(YYYY-MM-DD, squirrel:<8-char>)`. Multiple citations separated
    with `; ` inside one paren group.
  * Coherence rule: every insight bullet has at least one citation.
    Citation RESOLUTION (does the cited entry actually exist?) is the
    fn-2-2zz.10 validator's job; this stage validates only FORMAT and
    presence.
  * Section vocabulary follows `templates/walnut/insights.md`:
    `## Strategy / ## Process / ## Technical / ## People / ## Patterns /
    ## Tensions / ## Open Questions / ## Other`. Extra sections are
    permitted but emit a `warn` finding.

The runtime constraint (worker cannot fire Agent tool calls directly)
means this module exposes the four entry points the parent skill
consumes:

  * :func:`prepare_dispatch` -- gates on stage3_done.json, builds the
    single dispatch descriptor (prompt + paths + subagent_type).
  * :func:`collect_outputs` -- walks world insights + walnut-insights/
    and reports presence + insight counts per file.
  * :func:`validate_insights` -- hand-rolled stdlib validator returning
    a flat findings list (severity error / warn).
  * :func:`retry_dispatch` -- builds a one-shot retry descriptor with
    the failed-validator findings appended as feedback.
  * :func:`freeze_stage` -- writes ``_stage_outputs/stage4_done.json``
    after presence + validation pass.

Helpers exported for fn-2-2zz.10's validator:

  * `CITATION_RE` -- the parenthetical citation regex.
  * `WALNUT_INSIGHTS_DIR_NAME` -- the walnut-insights subdir name.
  * `ALLOWED_SECTIONS` -- the canonical section vocabulary.

Stdlib-only. No yaml / jsonschema. Frontmatter parsing reuses the
hand-rolled extractor from `stage2.py` (loaded via importlib namespace
key, same pattern stage3 uses).
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Path bootstrap -- mirrors stage0 / stage2 / stage3 so direct imports under
# tests resolve `_common` without going through `cli.py`.
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.normpath(os.path.join(_HERE, os.pardir))
_PLUGIN_ROOT = os.path.normpath(os.path.join(_DEMO_DIR, os.pardir, os.pardir))
_SCRIPTS = os.path.join(_PLUGIN_ROOT, "scripts")
if os.path.isdir(_SCRIPTS) and _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from _common import atomic_write_json, iso_now, resolve_plugin_root  # noqa: E402


def _load_stage0():
    """Load `stages/stage0.py` for `render_subagent_brief` reuse."""
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


def _load_stage2():
    """Load `stages/stage2.py` for the frontmatter parser."""
    full_name = "alive_demo.stage2"
    if full_name in sys.modules:
        return sys.modules[full_name]
    target = os.path.join(_HERE, "stage2.py")
    spec = importlib.util.spec_from_file_location(full_name, target)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {target}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_stage3():
    """Load `stages/stage3.py` for path helpers + entry split helpers."""
    full_name = "alive_demo.stage3"
    if full_name in sys.modules:
        return sys.modules[full_name]
    target = os.path.join(_HERE, "stage3.py")
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

#: Schema version stamped into stage4_done.json. Independent of other
#: stage schema versions.
SCHEMA_VERSION = "0.1"

#: Default subagent kind for Stage 4 (single subagent).
DEFAULT_SUBAGENT_TYPE = "general-purpose"

#: Path of the per-stage prompt template, relative to the plugin root.
INSIGHTS_PROMPT_RELPATH = os.path.join(
    "templates", "demo", "stage_prompts", "stage_4_insights.v1.md"
)

#: Filenames + subdirs Stage 4 owns inside `_stage_outputs/`.
_STAGE_OUTPUTS_SUBDIR = "_stage_outputs"
WALNUT_INSIGHTS_DIR_NAME = "walnut-insights"
_WORLD_INSIGHTS_FILENAME = "insights.md"
_STAGE3_DONE_FILENAME = "stage3_done.json"
_STAGE4_DONE_FILENAME = "stage4_done.json"
_SPINE_FILENAME = "spine.json"
_ANCHORS_FILENAME = "anchor_moments.json"
_WORLD_LOG_FILENAME = "log.md"

#: Slug regex matching `lib._SLUG_RE` and stage3._SLUG_RE.
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: Disallowed dash characters in body prose (mirrors stage3._DASH_CHARS).
_DASH_CHARS = ("—", "–", "―")

#: Canonical section vocabulary (per templates/walnut/insights.md). The
#: validator emits a `warn` finding (severity=warn, not error) for any
#: `## ...` heading outside this list. Custom sections are permitted
#: but discouraged.
ALLOWED_SECTIONS = (
    "Strategy",
    "Process",
    "Technical",
    "People",
    "Patterns",
    "Tensions",
    "Open Questions",
    "Other",
)

#: Citation regex. Matches a parenthetical citation block at any position
#: in a bullet line. Each citation inside the parens is a single
#: `YYYY-MM-DD, squirrel:<8-hex>` pair; multiple pairs are separated by
#: `; ` (semicolon + space). The OUTER regex captures the whole paren
#: group; the inner pair regex (`_CITATION_PAIR_RE`) is used to walk
#: each pair and validate the date / id format.
#:
#: NOTE on form: the outer regex is intentionally permissive on
#: whitespace inside the parens so the validator can give a more
#: specific finding when format is almost-but-not-quite right (e.g.
#: a 7-char squirrel id, a missing comma, a `YYYY/MM/DD` date). The
#: strict rule is enforced by the inner pair regex.
CITATION_RE = re.compile(
    r"\("
    r"(?P<body>"
    r"\d{4}-\d{2}-\d{2}, squirrel:[a-f0-9]{8}"
    r"(?:; \d{4}-\d{2}-\d{2}, squirrel:[a-f0-9]{8})*"
    r")"
    r"\)"
)

#: Single citation pair regex. Used to split `; `-separated multi-cites
#: and validate each pair on its own. The 8-hex squirrel id is mandatory.
_CITATION_PAIR_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}), squirrel:(?P<sid>[a-f0-9]{8})$"
)

#: Loose paren-block matcher used by the validator to flag malformed
#: citations (right shape but wrong contents -- e.g. 7-hex squirrel,
#: `YYYY/MM/DD` date). A finding is emitted only if the loose match
#: contains the substring `squirrel:` AND the strict CITATION_RE does
#: NOT match the same span.
_LOOSE_CITATION_RE = re.compile(r"\(([^()]*squirrel:[^()]*)\)")

#: Bullet-line detection inside a section body. Stage 4 insights are
#: ALWAYS bullets (single line each), never numbered or prose
#: paragraphs. The validator only enforces citations on bullet lines.
_BULLET_LINE_RE = re.compile(r"^\s*-\s+\S", re.MULTILINE)

#: Section heading detection. Captures the heading text after `## `.
_SECTION_HEADING_RE = re.compile(r"(?m)^##\s+(?P<title>.+?)\s*$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class Stage4Error(RuntimeError):
    """Base error for Stage 4 dispatch + validation failures."""


class Stage4NotReady(Stage4Error):
    """Raised when stage3_done.json is missing or under-specified.

    The parent skill should fall back through the user-facing "run
    Stage 3 first" hint when it sees this; the message + hint shape
    matches Stage 3's `Stage3NotReady` envelope.
    """


class Stage4DispatchError(Stage4Error):
    """Raised when the dispatcher cannot construct a valid descriptor."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _abspartial(partial_dir) -> str:
    """Canonicalize a partial-directory path. Mirrors stage3._abspartial."""
    if hasattr(partial_dir, "__fspath__"):
        partial_dir = os.fspath(partial_dir)
    if not isinstance(partial_dir, str):
        raise TypeError(
            f"partial_dir must be path-like; got {type(partial_dir).__name__}"
        )
    return os.path.normpath(os.path.abspath(partial_dir))


def stage_outputs_dir(partial_dir) -> str:
    out = os.path.join(_abspartial(partial_dir), _STAGE_OUTPUTS_SUBDIR)
    os.makedirs(out, exist_ok=True)
    return out


def walnut_insights_dir(partial_dir) -> str:
    out = os.path.join(stage_outputs_dir(partial_dir), WALNUT_INSIGHTS_DIR_NAME)
    os.makedirs(out, exist_ok=True)
    return out


def world_insights_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _WORLD_INSIGHTS_FILENAME)


def walnut_insights_path(partial_dir, slug: str) -> str:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ValueError(f"walnut slug {slug!r} does not match the slug rule")
    return os.path.join(walnut_insights_dir(partial_dir), f"{slug}.md")


def stage3_done_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _STAGE3_DONE_FILENAME)


def stage4_done_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _STAGE4_DONE_FILENAME)


def spine_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _SPINE_FILENAME)


def anchors_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _ANCHORS_FILENAME)


def world_log_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _WORLD_LOG_FILENAME)


def people_logs_dir(partial_dir) -> str:
    """Read-only path; Stage 4 reads, never writes."""
    return os.path.join(stage_outputs_dir(partial_dir), "people-logs")


def walnut_logs_dir(partial_dir) -> str:
    """Read-only path; Stage 4 reads, never writes."""
    return os.path.join(stage_outputs_dir(partial_dir), "walnut-logs")


def entities_dir(partial_dir) -> str:
    """Read-only path; Stage 4 reads, never writes."""
    return os.path.join(stage_outputs_dir(partial_dir), "entities")


# ---------------------------------------------------------------------------
# JSON loaders + stage3 marker gate
# ---------------------------------------------------------------------------

def _load_json(path: str, label: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        raise Stage4NotReady(f"{label} not found at {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise Stage4Error(
            f"{label} at {path} unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Stage4Error(
            f"{label} at {path} is not valid JSON: "
            f"line {exc.lineno} col {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise Stage4Error(f"{label} top-level value must be object")
    return data


def load_spine(partial_dir) -> Dict[str, Any]:
    return _load_json(spine_path(partial_dir), "spine.json")


def load_anchors(partial_dir) -> Dict[str, Any]:
    return _load_json(anchors_path(partial_dir), "anchor_moments.json")


def load_stage3_done(partial_dir) -> Dict[str, Any]:
    """Read + parse `<partial>/_stage_outputs/stage3_done.json`.

    Stage 4 gates on this marker; if it's missing, prepare_dispatch
    raises :class:`Stage4NotReady`. The marker shape (per
    stage3.freeze_stage) is::

        {
            "schema_version": "0.1",
            "frozen": true,
            "frozen_at": "<ISO 8601 UTC>",
            "world_log": <abs path>,
            "people_count": <int>,
            "walnut_count": <int>,
            "entry_count": <int>
        }

    A frozen=False marker raises :class:`Stage4NotReady`.
    """
    marker = _load_json(stage3_done_path(partial_dir), "stage3_done.json")
    if not marker.get("frozen"):
        raise Stage4NotReady(
            "stage3_done.json present but `frozen` is not true; "
            "re-run Stage 3 freeze before dispatching Stage 4"
        )
    return marker


# ---------------------------------------------------------------------------
# Dispatch descriptor builder
# ---------------------------------------------------------------------------

def _read_template(plugin_root: str) -> str:
    target = os.path.join(plugin_root, INSIGHTS_PROMPT_RELPATH)
    with open(target, "r", encoding="utf-8") as f:
        return f.read()


def _substitute(template: str, mapping: Dict[str, str]) -> str:
    """Replace `{{key}}` placeholders. Unknown keys are left untouched."""
    out = template
    for key, value in mapping.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def _wrap_dispatch_prompt(*, brief: str, task_body: str) -> str:
    """Wrap a per-stage task body in the canonical CONTEXT/TASK envelope."""
    return (
        "CONTEXT:\n"
        f"{brief}\n"
        "\n"
        "TASK:\n"
        f"{task_body}"
    )


def _render_prompt(
    *,
    partial_dir: str,
    spine_pth: str,
    anchors_pth: str,
    entities_pth: str,
    world_log_pth: str,
    people_logs_pth: str,
    walnut_logs_pth: str,
    world_insights_out: str,
    walnut_insights_out: str,
    template: str,
    brief: str,
) -> str:
    """Render the Stage 4 prompt body with explicit paths only.

    Per the spec's token-budget rationale (mirrors Stage 3): the prompt
    embeds only file paths, never full prose bodies. The subagent reads
    the prose off disk. Prompt size is bounded by the constant set of
    paths rather than the variable volume of timeline prose.
    """
    body = _substitute(
        template,
        {
            "subagent_brief": "[brief is wrapped via CONTEXT envelope below]",
            "partial_dir": partial_dir,
            "spine_path": spine_pth,
            "anchor_moments_path": anchors_pth,
            "entities_dir": entities_pth,
            "world_log_path": world_log_pth,
            "people_logs_dir": people_logs_pth,
            "walnut_logs_dir": walnut_logs_pth,
            "world_insights_output_path": world_insights_out,
            "walnut_insights_dir": walnut_insights_out,
        },
    )
    return _wrap_dispatch_prompt(brief=brief, task_body=body)


def prepare_dispatch(
    partial_dir,
    *,
    world_root: str,
    plugin_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the single Stage 4 dispatch descriptor.

    Gates:
      * spine.json exists, parses, has `walnut_roster` + `people_roster`.
      * anchor_moments.json exists, parses, `frozen=True`.
      * stage3_done.json exists, parses, `frozen=True`.

    Returns a descriptor of the form::

        {
            "subagent_type":    "general-purpose",
            "description":      "alive-demo stage 4 insights",
            "prompt":           <CONTEXT/TASK-wrapped prompt>,
            "output_paths":     {
                "world_insights":   <abs path>,
                "walnut_insights":  [<abs path per walnut slug>, ...],
                "walnut_insights_dir": <abs dir path>,
            },
            "expected_walnuts": [<slug>, ...],
            "expected_people":  [<slug>, ...],   # informational only
        }

    Stage 4's per-walnut insights files are CONDITIONAL (only walnuts
    with a real recurring pattern get one), so `output_paths.walnut_insights`
    enumerates the POSSIBLE per-walnut paths. The validator does not
    error on a missing per-walnut file; it only validates files that
    exist. The world insights file is always required.
    """
    canonical = _abspartial(partial_dir)
    # Order matters: stage3 marker before spine/anchors so the parent
    # gets the most informative "Stage 3 first" error.
    load_stage3_done(canonical)

    spine = load_spine(canonical)
    anchors_env = load_anchors(canonical)
    if not anchors_env.get("frozen"):
        raise Stage4NotReady(
            "anchor_moments.json is not frozen; re-run Stage 1 to confirm "
            "and freeze before dispatching Stage 4"
        )

    # Verify the Stage 2 + Stage 3 input artefacts the subagent will be
    # told to read are actually present on disk. Without this check, a
    # corrupted partial (e.g. someone manually deleted log.md after
    # stage3_done.json was written) would still produce a "successful"
    # dispatch descriptor pointing at missing paths, and the subagent
    # would synthesise from nothing. The marker file alone is not
    # sufficient evidence that the inputs survive.
    world_log_pth = world_log_path(canonical)
    if not os.path.isfile(world_log_pth):
        raise Stage4NotReady(
            f"Stage 3 world log not found at {world_log_pth}; "
            f"re-run Stage 3 (or restore the partial) before Stage 4"
        )
    people_logs_pth_check = people_logs_dir(canonical)
    if not os.path.isdir(people_logs_pth_check):
        raise Stage4NotReady(
            f"Stage 3 people-logs/ directory not found at "
            f"{people_logs_pth_check}; re-run Stage 3 before Stage 4"
        )
    walnut_logs_pth_check = walnut_logs_dir(canonical)
    if not os.path.isdir(walnut_logs_pth_check):
        raise Stage4NotReady(
            f"Stage 3 walnut-logs/ directory not found at "
            f"{walnut_logs_pth_check}; re-run Stage 3 before Stage 4"
        )
    entities_pth_check = entities_dir(canonical)
    if not os.path.isdir(entities_pth_check):
        raise Stage4NotReady(
            f"Stage 2 entities/ directory not found at "
            f"{entities_pth_check}; re-run Stage 2 before Stage 4"
        )

    plugin_root = plugin_root or resolve_plugin_root()
    template = _read_template(plugin_root)
    stage0 = _load_stage0()
    brief = stage0.render_subagent_brief(world_root=world_root, plugin_root=plugin_root)

    expected_people: List[str] = []
    for entry in spine.get("people_roster") or []:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise Stage4DispatchError(
                f"people_roster entry has invalid slug: {slug!r}"
            )
        expected_people.append(slug)

    expected_walnuts: List[str] = []
    for entry in spine.get("walnut_roster") or []:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise Stage4DispatchError(
                f"walnut_roster entry has invalid slug: {slug!r}"
            )
        expected_walnuts.append(slug)

    # Verify the FULL expected per-slug input set Stage 4 depends on.
    # The top-level directory check above (people-logs/, walnut-logs/,
    # entities/) is a coarse gate; this loop is the fine-grained gate
    # that catches a partial where only some per-slug files survived
    # (e.g. someone manually pruned a log to "fix" something). Stage 4
    # synthesises from the union of these inputs; any missing slug
    # silently degrades the synthesis.
    missing_inputs: List[str] = []
    for slug in expected_people:
        person_log = os.path.join(people_logs_pth_check, f"{slug}.md")
        if not os.path.isfile(person_log):
            missing_inputs.append(f"people-logs/{slug}.md")
        # Stage 2 key.md is the canonical voice anchor for the
        # subagent's prose voice; the prompt explicitly tells the
        # subagent to read it.
        person_key = os.path.join(entities_pth_check, slug, "key.md")
        if not os.path.isfile(person_key):
            missing_inputs.append(f"entities/{slug}/key.md")
    for slug in expected_walnuts:
        walnut_log = os.path.join(walnut_logs_pth_check, f"{slug}.md")
        if not os.path.isfile(walnut_log):
            missing_inputs.append(f"walnut-logs/{slug}.md")
        walnut_key = os.path.join(entities_pth_check, slug, "key.md")
        if not os.path.isfile(walnut_key):
            missing_inputs.append(f"entities/{slug}/key.md")
    if missing_inputs:
        # Cap the listed paths so the message stays scannable; the full
        # count is included for diagnosis.
        sample = missing_inputs[:6]
        more = "" if len(missing_inputs) <= 6 else (
            f" (+{len(missing_inputs) - 6} more)"
        )
        raise Stage4NotReady(
            f"Stage 4 inputs incomplete: {len(missing_inputs)} per-slug "
            f"file(s) missing: {sample}{more}; "
            f"re-run Stage 2/3 (or restore the partial) before Stage 4"
        )

    world_insights_out = world_insights_path(canonical)
    walnut_insights_out_dir = walnut_insights_dir(canonical)
    entities_pth = entities_dir(canonical)
    world_log_pth = world_log_path(canonical)
    people_logs_pth = people_logs_dir(canonical)
    walnut_logs_pth = walnut_logs_dir(canonical)

    prompt = _render_prompt(
        partial_dir=canonical,
        spine_pth=spine_path(canonical),
        anchors_pth=anchors_path(canonical),
        entities_pth=entities_pth,
        world_log_pth=world_log_pth,
        people_logs_pth=people_logs_pth,
        walnut_logs_pth=walnut_logs_pth,
        world_insights_out=world_insights_out,
        walnut_insights_out=walnut_insights_out_dir,
        template=template,
        brief=brief,
    )

    output_paths = {
        "world_insights": world_insights_out,
        "walnut_insights_dir": walnut_insights_out_dir,
        "walnut_insights": [
            os.path.join(walnut_insights_out_dir, f"{s}.md")
            for s in expected_walnuts
        ],
    }

    return {
        "subagent_type": DEFAULT_SUBAGENT_TYPE,
        "description": "alive-demo stage 4 insights",
        "prompt": prompt,
        "output_paths": output_paths,
        "expected_walnuts": expected_walnuts,
        "expected_people": expected_people,
    }


# ---------------------------------------------------------------------------
# Output collection
# ---------------------------------------------------------------------------

def _file_summary_with_count(path: str) -> Dict[str, Any]:
    """Per-file summary used by collect_outputs.

    `insight_count` is the number of bullet lines (single `-` bullets)
    inside section bodies (i.e., after the first `## Section` heading).
    A file with frontmatter only and no bullets reports 0.
    """
    if not os.path.isfile(path):
        return {"path": path, "present": False, "insight_count": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {"path": path, "present": True, "insight_count": None}
    body = _strip_frontmatter(text)
    # Only count bullets that appear after at least one `## Section`
    # heading (intro prose bullets above the first heading do not
    # count as insights for the count purposes).
    sections = list(_SECTION_HEADING_RE.finditer(body))
    if not sections:
        # No section headings, no insights to count.
        return {"path": path, "present": True, "insight_count": 0}
    after_first = body[sections[0].end():]
    count = len(_BULLET_LINE_RE.findall(after_first))
    return {"path": path, "present": True, "insight_count": count}


def _strip_frontmatter(text: str) -> str:
    """Return the body after the leading `--- ... ---` block. Tolerant.

    On parse failure returns the text unchanged. The validator surfaces
    a `frontmatter_parse_error` finding separately.
    """
    stage2 = _load_stage2()
    try:
        _, body = stage2._parse_frontmatter(text)
        return body
    except ValueError:
        return text


def collect_outputs(
    partial_dir,
    expected_walnuts: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Walk the world insights file + walnut-insights/ and report presence.

    Returns::

        {
            "world_insights": {
                "path":            <abs>,
                "present":         <bool>,
                "insight_count":   <int or null>,
            },
            "walnut_insights": {
                "<slug>": {"path": <abs>, "present": <bool>, "insight_count": <int|null>},
                ...
            },
        }

    `expected_walnuts` (when provided) drives the walnut iteration so
    missing walnut-insights files surface as `present: false`. Without
    it, the function infers from disk (lists `walnut-insights/`).

    Stage 4's per-walnut insights files are CONDITIONAL -- a walnut
    without a per-walnut insights file is NOT a failure (only the world
    file is required). collect_outputs surfaces presence so the parent
    skill can render counts; validate_insights enforces only what is
    required.
    """
    canonical = _abspartial(partial_dir)
    out: Dict[str, Any] = {}

    world_pth = world_insights_path(canonical)
    out["world_insights"] = _file_summary_with_count(world_pth)

    wi_dir = walnut_insights_dir(canonical)
    if expected_walnuts is not None:
        slugs = list(expected_walnuts)
    else:
        slugs = [
            n[:-3] for n in sorted(os.listdir(wi_dir)) if n.endswith(".md")
        ]
    out["walnut_insights"] = {
        slug: _file_summary_with_count(os.path.join(wi_dir, f"{slug}.md"))
        for slug in slugs
    }
    return out


# ---------------------------------------------------------------------------
# Validation (hand-rolled, stdlib-only)
# ---------------------------------------------------------------------------

def _has_dash(s: Any) -> bool:
    return isinstance(s, str) and any(d in s for d in _DASH_CHARS)


def _read_text_or_finding(
    path: str,
    *,
    file_kind: str,
    findings: List[Dict[str, Any]],
) -> Optional[str]:
    """Read an insights file; on missing/unreadable, append an error finding.

    `file_kind` is `"world"` for the world insights file and
    `"walnut:<slug>"` for per-walnut files.
    """
    if not os.path.isfile(path):
        findings.append({
            "file": file_kind, "severity": "error",
            "issue": "missing_file", "evidence": path,
        })
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        findings.append({
            "file": file_kind, "severity": "error",
            "issue": "unreadable_file",
            "evidence": f"{path}: {type(exc).__name__}: {exc}",
        })
        return None


def _date_parses(date_str: str) -> bool:
    """Strict YYYY-MM-DD parse. Returns False on any other format / invalid date."""
    try:
        _dt.date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return False
    # Reject `YYYY-MM-DDTHH:...`-style strings even though fromisoformat
    # would accept them on 3.11+. The citation grammar is exact.
    return len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-"


def _split_sections(body: str) -> List[Tuple[str, str]]:
    """Split a body into `(section_title, section_body)` pairs.

    Section bodies run from immediately after the heading to the next
    `## ` heading or end of body. Prose above the first heading is NOT
    returned (it is intro prose, not validated for citations).
    """
    headings = list(_SECTION_HEADING_RE.finditer(body))
    out: List[Tuple[str, str]] = []
    for i, match in enumerate(headings):
        title = match.group("title").strip()
        start = match.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        out.append((title, body[start:end]))
    return out


def _bullet_lines(section_body: str) -> List[str]:
    """Return single-line bullet entries inside a section body.

    Multi-line bullets (continuation indented under a `-` line) are
    joined into one logical bullet line for validation purposes; the
    citation rule applies to the joined line.
    """
    lines = section_body.splitlines()
    bullets: List[str] = []
    current: Optional[str] = None
    for raw in lines:
        line = raw.rstrip()
        # New bullet.
        if re.match(r"^\s*-\s+\S", line):
            if current is not None:
                bullets.append(current)
            current = line.strip()
            continue
        # Continuation (indented, non-bullet).
        if current is not None and line.startswith(("  ", "\t")) and line.strip():
            current = current + " " + line.strip()
            continue
        # Blank or non-continuation -- close the current bullet.
        if current is not None:
            bullets.append(current)
            current = None
    if current is not None:
        bullets.append(current)
    return bullets


def _validate_citations_in_bullet(
    bullet: str,
    *,
    file_kind: str,
    section_title: str,
    findings: List[Dict[str, Any]],
) -> bool:
    """Validate every parenthetical citation in a single bullet.

    Behaviour:
      * If the bullet has at least one CITATION_RE match, the bullet
        passes the "every insight has a citation" rule.
      * Every match is split on `; ` and each pair is validated:
        date format strict YYYY-MM-DD AND parses; squirrel id is
        exactly 8 lowercase hex chars (already enforced by the regex,
        but we double-check the date semantically).
      * If a `(...squirrel:...)` block exists that does NOT match
        CITATION_RE, surface a `citation_format_invalid` finding
        (loose match path).

    Returns True iff at least one valid citation was found in the
    bullet. Returns False otherwise (caller emits the
    `insight_missing_citation` finding).
    """
    found_valid = False

    # Strict matches.
    for m in CITATION_RE.finditer(bullet):
        body = m.group("body")
        pairs = body.split("; ")
        all_pairs_ok = True
        for pair in pairs:
            pair_match = _CITATION_PAIR_RE.match(pair)
            if not pair_match:
                # CITATION_RE wouldn't have matched if the inner pair
                # was malformed, but be defensive.
                all_pairs_ok = False
                findings.append({
                    "file": file_kind, "severity": "error",
                    "issue": "citation_pair_malformed",
                    "evidence": (
                        f"section {section_title!r} bullet "
                        f"contains pair {pair!r}; expected "
                        f"`YYYY-MM-DD, squirrel:<8-hex>`"
                    ),
                })
                continue
            date_str = pair_match.group("date")
            if not _date_parses(date_str):
                all_pairs_ok = False
                findings.append({
                    "file": file_kind, "severity": "error",
                    "issue": "citation_date_invalid",
                    "evidence": (
                        f"section {section_title!r} bullet cites "
                        f"{date_str!r}; not a real YYYY-MM-DD date"
                    ),
                })
        if all_pairs_ok:
            found_valid = True

    # Loose matches (paren block contains `squirrel:` but did NOT match
    # CITATION_RE). Surface format finding so the writer knows what
    # broke.
    for m in _LOOSE_CITATION_RE.finditer(bullet):
        span = m.span()
        # Skip if this loose match is also a strict match (covered
        # above).
        loose_text = bullet[span[0]:span[1]]
        if CITATION_RE.fullmatch(loose_text):
            continue
        # Strict match anywhere overlapping this span?
        overlapping_strict = False
        for sm in CITATION_RE.finditer(bullet):
            if sm.start() == span[0] and sm.end() == span[1]:
                overlapping_strict = True
                break
        if overlapping_strict:
            continue
        findings.append({
            "file": file_kind, "severity": "error",
            "issue": "citation_format_invalid",
            "evidence": (
                f"section {section_title!r} bullet has "
                f"`{loose_text}`; expected "
                f"`(YYYY-MM-DD, squirrel:<8-hex>[; ...])`"
            ),
        })

    return found_valid


#: Frontmatter keys required on every Stage 4 insights file. The set
#: is closed: extra keys surface a `frontmatter_unknown_key` error so
#: drift is caught early. Per
#: `plugins/alive/templates/demo/stage_prompts/stage_4_insights.v1.md:114-118`
#: (world) and `:139-145` (per-walnut), the contract is exactly these
#: three keys.
_REQUIRED_FRONTMATTER_KEYS = ("walnut", "updated", "summary")


def _validate_frontmatter_contract(
    fm: Dict[str, Any],
    *,
    file_kind: str,
    expected_walnut: Optional[str],
    findings: List[Dict[str, Any]],
) -> None:
    """Enforce the Stage 4 insights frontmatter contract.

    For the world file (`file_kind == "world"`), `expected_walnut`
    is the literal string ``"world"``. For per-walnut files
    (`file_kind == f"walnut:<slug>"`), `expected_walnut` is the
    walnut display name resolved from the spine roster (or ``None``
    if the slug is not in the spine, in which case only the closed
    key set + format checks apply).

    Errors emitted:
      * `frontmatter_missing_key`           -- a required key absent
      * `frontmatter_unknown_key`           -- an extra key present
      * `frontmatter_walnut_mismatch`       -- value does not match expected
      * `frontmatter_updated_invalid`       -- value not strict YYYY-MM-DD
      * `frontmatter_summary_invalid`       -- not a non-empty string
    """
    keys = set(fm.keys())
    for required in _REQUIRED_FRONTMATTER_KEYS:
        if required not in keys:
            findings.append({
                "file": file_kind, "severity": "error",
                "issue": "frontmatter_missing_key",
                "evidence": (
                    f"{file_kind}: frontmatter missing required key "
                    f"{required!r}"
                ),
            })
    extra = keys - set(_REQUIRED_FRONTMATTER_KEYS)
    for key in sorted(extra):
        findings.append({
            "file": file_kind, "severity": "error",
            "issue": "frontmatter_unknown_key",
            "evidence": (
                f"{file_kind}: frontmatter has unknown key {key!r}; "
                f"closed key set is {list(_REQUIRED_FRONTMATTER_KEYS)}"
            ),
        })

    walnut_value = fm.get("walnut")
    if expected_walnut is not None and walnut_value != expected_walnut:
        findings.append({
            "file": file_kind, "severity": "error",
            "issue": "frontmatter_walnut_mismatch",
            "evidence": (
                f"{file_kind}: frontmatter walnut={walnut_value!r}; "
                f"expected {expected_walnut!r}"
            ),
        })

    updated = fm.get("updated")
    if updated is not None:
        if not isinstance(updated, str) or not _date_parses(updated):
            findings.append({
                "file": file_kind, "severity": "error",
                "issue": "frontmatter_updated_invalid",
                "evidence": (
                    f"{file_kind}: frontmatter updated={updated!r}; "
                    f"expected strict YYYY-MM-DD"
                ),
            })

    summary = fm.get("summary")
    if summary is not None:
        if not isinstance(summary, str) or not summary.strip():
            findings.append({
                "file": file_kind, "severity": "error",
                "issue": "frontmatter_summary_invalid",
                "evidence": (
                    f"{file_kind}: frontmatter summary={summary!r}; "
                    f"expected non-empty string"
                ),
            })


def _validate_one_insights_file(
    text: str,
    *,
    file_kind: str,
    expected_walnut: Optional[str],
    findings: List[Dict[str, Any]],
) -> None:
    """Validate one insights file. Appends findings; returns None.

    Validators applied:
      * Frontmatter parses (returns one error and stops if not).
      * Frontmatter contract: closed key set {walnut, updated, summary};
        `walnut` matches `expected_walnut`; `updated` is strict
        YYYY-MM-DD; `summary` is a non-empty string.
      * Body has zero em / en / horizontal-bar dashes.
      * Each `## Section` heading is in ALLOWED_SECTIONS (warn if not).
      * Every bullet line under a section has at least one valid
        citation matching CITATION_RE (error if not).
      * Every citation date parses as a real YYYY-MM-DD (error if not).
      * Loose citation blocks (containing `squirrel:` but not matching
        CITATION_RE) emit `citation_format_invalid`.
    """
    stage2 = _load_stage2()
    try:
        fm, body = stage2._parse_frontmatter(text)
    except ValueError as exc:
        findings.append({
            "file": file_kind, "severity": "error",
            "issue": "frontmatter_parse_error",
            "evidence": f"{file_kind}: {exc}",
        })
        return

    _validate_frontmatter_contract(
        fm,
        file_kind=file_kind,
        expected_walnut=expected_walnut,
        findings=findings,
    )

    if _has_dash(body):
        findings.append({
            "file": file_kind, "severity": "error",
            "issue": "body_dash_character",
            "evidence": f"{file_kind}: body contains em / en / horizontal-bar dash",
        })

    sections = _split_sections(body)
    if not sections:
        findings.append({
            "file": file_kind, "severity": "error",
            "issue": "no_sections",
            "evidence": (
                f"{file_kind}: insights file has no `## Section` headings; "
                f"every file needs at least one section with at least one bullet"
            ),
        })
        return

    total_bullets = 0
    for title, sec_body in sections:
        if title not in ALLOWED_SECTIONS:
            findings.append({
                "file": file_kind, "severity": "warn",
                "issue": "section_outside_vocabulary",
                "evidence": (
                    f"{file_kind}: section {title!r} is outside the "
                    f"canonical vocabulary {list(ALLOWED_SECTIONS)}"
                ),
            })
        bullets = _bullet_lines(sec_body)
        total_bullets += len(bullets)
        for bullet in bullets:
            has_valid = _validate_citations_in_bullet(
                bullet,
                file_kind=file_kind,
                section_title=title,
                findings=findings,
            )
            if not has_valid:
                findings.append({
                    "file": file_kind, "severity": "error",
                    "issue": "insight_missing_citation",
                    "evidence": (
                        f"{file_kind}: section {title!r} bullet has no "
                        f"`(YYYY-MM-DD, squirrel:<8-hex>)` citation: "
                        f"{bullet[:120]!r}"
                    ),
                })

    # An insights file with section headings but zero bullets is
    # vacuous synthesis. The module-level contract is "every file needs
    # at least one section with at least one bullet"; enforce it here
    # so `freeze_stage` cannot mark a Stage 4 marker as `frozen=true`
    # on top of an empty synthesis.
    if total_bullets == 0:
        findings.append({
            "file": file_kind, "severity": "error",
            "issue": "no_bullets",
            "evidence": (
                f"{file_kind}: insights file has section headings but "
                f"zero bullets; every file needs at least one section "
                f"with at least one bullet"
            ),
        })


def validate_insights(
    partial_dir,
    *,
    expected_walnuts: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Run hand-rolled stdlib validation on the Stage 4 insights outputs.

    Validators (each yields one or more `severity: error` findings on
    failure unless noted):

      1. World insights file present.
      2. World insights file parses (frontmatter + at least one
         `## Section` heading).
      3. Every per-walnut insights file PRESENT on disk parses (per
         the contract, these are CONDITIONAL -- absence is not an
         error).
      4. Every section heading is in ALLOWED_SECTIONS (warn-level
         finding outside the vocabulary; not an error).
      5. Every bullet line under a section has at least one citation
         matching CITATION_RE.
      6. Every citation's date is a real YYYY-MM-DD that parses.
      7. Loose `(...squirrel:...)` blocks that don't match CITATION_RE
         surface as `citation_format_invalid`.

    `expected_walnuts` (when provided) is INFORMATIONAL only -- the
    validator does NOT require a per-walnut insights file to exist for
    every walnut. Instead, the validator iterates files actually
    present in `walnut-insights/` and validates each. Without
    `expected_walnuts`, the same disk-iteration behaviour applies.

    Citation RESOLUTION (does the cited entry actually exist in the
    Stage 3 logs?) is the fn-2-2zz.10 validator's job; this stage
    validates only FORMAT and the "every bullet has a citation" rule.

    Returns a flat list of findings. Empty list means everything is
    well-formed.
    """
    canonical = _abspartial(partial_dir)
    findings: List[Dict[str, Any]] = []

    # Resolve walnut display names from spine for per-walnut frontmatter
    # cross-checks. Best-effort: a missing/unparseable spine downgrades
    # those checks (the validator still runs structural + citation
    # validation) -- the caller will already have surfaced gating errors
    # from prepare_dispatch in that case.
    name_for_slug: Dict[str, str] = {}
    try:
        spine = load_spine(canonical)
    except (Stage4NotReady, Stage4Error):
        spine = {}
    for entry in (spine.get("walnut_roster") or []):
        if isinstance(entry, dict):
            slug = entry.get("slug")
            name = entry.get("name")
            if isinstance(slug, str) and isinstance(name, str):
                name_for_slug[slug] = name

    world_pth = world_insights_path(canonical)
    world_text = _read_text_or_finding(world_pth, file_kind="world", findings=findings)
    if world_text is not None:
        _validate_one_insights_file(
            world_text,
            file_kind="world",
            expected_walnut="world",
            findings=findings,
        )

    wi_dir = walnut_insights_dir(canonical)
    if os.path.isdir(wi_dir):
        for name in sorted(os.listdir(wi_dir)):
            if not name.endswith(".md"):
                continue
            slug = name[:-3]
            file_kind = f"walnut:{slug}"
            # Reject stray insights files whose slug is not in the
            # spine. Without this check, a typo (`ghost.md` instead of
            # `harbor-foods.md`) or a copy-paste error would silently
            # pass validation and ship to activation.
            if name_for_slug and slug not in name_for_slug:
                findings.append({
                    "file": file_kind, "severity": "error",
                    "issue": "unknown_walnut_insights_slug",
                    "evidence": (
                        f"{file_kind}: walnut-insights/{name} does not "
                        f"correspond to any walnut in the spine roster"
                    ),
                })
                # Skip per-file structural validation when the slug is
                # unknown -- the file is fundamentally an artefact of
                # the wrong identity, not a parse / format problem.
                continue
            text = _read_text_or_finding(
                os.path.join(wi_dir, name),
                file_kind=file_kind,
                findings=findings,
            )
            if text is None:
                continue
            _validate_one_insights_file(
                text,
                file_kind=file_kind,
                expected_walnut=name_for_slug.get(slug),
                findings=findings,
            )

    return findings


# ---------------------------------------------------------------------------
# Retry construction
# ---------------------------------------------------------------------------

def retry_dispatch(
    descriptor: Dict[str, Any],
    findings: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a one-shot retry descriptor with feedback appended.

    `descriptor` is the original dispatch descriptor from
    :func:`prepare_dispatch`. `findings` is the validator's flat list.
    Returns a new descriptor (does not mutate input) with the same
    output paths but a feedback-augmented prompt.

    Idempotent: calling against the same descriptor twice produces the
    same retry. The dispatcher must enforce the second-failure
    escalation by ITSELF (this function does not track retry counts).
    """
    if not isinstance(descriptor, dict):
        raise TypeError(
            f"descriptor must be dict; got {type(descriptor).__name__}"
        )
    error_findings = [f for f in findings if f.get("severity") == "error"]
    feedback_lines = [
        "",
        "---",
        "",
        "## Retry feedback",
        "",
        (
            "Your previous attempt failed Stage 4 validation. Fix the "
            "errors below and write corrected insights files to the "
            "same output paths via the standard atomic-write helpers."
        ),
        "",
        "### Findings",
    ]
    if not error_findings:
        feedback_lines.append("- (no error findings; retry is a no-op)")
    for finding in error_findings:
        kind = finding.get("file", "?")
        issue = finding.get("issue", "?")
        evidence = finding.get("evidence", "")
        feedback_lines.append(f"- [{kind}] {issue}: {evidence}")
    feedback = "\n".join(feedback_lines)
    new_prompt = descriptor["prompt"] + "\n" + feedback
    retry = dict(descriptor)
    retry["prompt"] = new_prompt
    retry["description"] = descriptor.get(
        "description", "alive-demo stage 4 insights",
    ) + " (retry)"
    retry["is_retry"] = True
    return retry


# ---------------------------------------------------------------------------
# Stage freeze
# ---------------------------------------------------------------------------

def freeze_stage(
    partial_dir,
    *,
    expected_walnuts: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Write the Stage 4 done marker after presence + validation pass.

    Pre-conditions:
      * world insights file present.
      * :func:`validate_insights` returns no `severity == "error"`
        findings (warn findings are acceptable).

    Per-walnut insights files are CONDITIONAL (per the contract); their
    absence is not a freeze-blocker.

    Idempotent: calling against an already-frozen marker rewrites it
    with a refreshed `frozen_at` timestamp. The marker shape is::

        {
            "schema_version":           "0.1",
            "frozen":                   true,
            "frozen_at":                "<ISO 8601 UTC>",
            "world_insights_path":      <abs path>,
            "walnut_insights_count":    <int>,
            "world_insight_count":      <int>,
            "total_insight_count":      <int>
        }

    Raises :class:`Stage4Error` (with a list of blocking issues) if the
    pre-conditions fail.
    """
    canonical = _abspartial(partial_dir)
    coverage = collect_outputs(
        canonical,
        expected_walnuts=expected_walnuts,
    )
    if not coverage["world_insights"]["present"]:
        raise Stage4Error(
            "cannot freeze stage 4: world insights file missing at "
            f"{coverage['world_insights']['path']}"
        )

    findings = validate_insights(
        canonical,
        expected_walnuts=expected_walnuts,
    )
    errors = [f for f in findings if f.get("severity") == "error"]
    if errors:
        issues = sorted({f.get("issue", "?") for f in errors})
        raise Stage4Error(
            f"cannot freeze stage 4: {len(errors)} validation error(s); "
            f"issues: {issues}"
        )

    # Counts. Per-walnut files that exist on disk count toward
    # `walnut_insights_count`, regardless of expected_walnuts (a walnut
    # not in the expected list but with a file produced by the subagent
    # still counts; absence is not an error).
    walnut_present_count = 0
    total_insights = 0
    if coverage["world_insights"]["insight_count"] is not None:
        total_insights += coverage["world_insights"]["insight_count"]
    for info in coverage["walnut_insights"].values():
        if info["present"]:
            walnut_present_count += 1
            if info["insight_count"] is not None:
                total_insights += info["insight_count"]

    marker = {
        "schema_version": SCHEMA_VERSION,
        "frozen": True,
        "frozen_at": iso_now(),
        "world_insights_path": coverage["world_insights"]["path"],
        "world_insight_count": coverage["world_insights"]["insight_count"] or 0,
        "walnut_insights_count": walnut_present_count,
        "total_insight_count": total_insights,
    }
    atomic_write_json(stage4_done_path(canonical), marker)
    # fn-2-2zz.16: advance the demo-state partial-generations row to
    # ``5_promote`` so the orchestrator's status / resume surface
    # reflects Stage 4 freeze and the next in-flight stage is the
    # deterministic activation transaction. Best-effort.
    _advance_demo_state_stage(canonical, "5_promote")
    return marker


def _advance_demo_state_stage(partial_dir: str, new_stage: str) -> None:
    """Best-effort wrapper around ``state.advance_partial_stage``."""
    try:  # pragma: no cover - defensive against pathological env
        full_name = "alive_demo.state_for_stage4"
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
    """Lazy load lib.py for the failure-block helpers (fn-2-2zz.13)."""
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


def surface_double_failure(
    validation_result: Any,
    *,
    partial_dir: str,
    raw_output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the user-facing failure envelope for a Stage 4 second-fail.

    Adapter to ``lib.report_validation_double_failure`` (fn-2-2zz.13).
    The skill orchestrator calls this when ``validate_insights`` (or the
    unified ``validate_stage("4", ...)`` facade) returns errors after the
    one-shot retry through :func:`retry_dispatch` has also failed.
    """
    lib = _load_lib_for_failure()
    report = lib.report_validation_double_failure(
        stage_id="4",
        validation_result=validation_result,
        partial_dir=partial_dir,
        raw_output_path=raw_output_path,
    )
    return {
        "failure_mode": "validation_double_failure",
        "stage": "4",
        "rendered_block": report["rendered_block"],
        "state_updated": report.get("state_updated", False),
        "partial_dir": partial_dir,
    }


__all__ = (
    "SCHEMA_VERSION",
    "DEFAULT_SUBAGENT_TYPE",
    "INSIGHTS_PROMPT_RELPATH",
    "WALNUT_INSIGHTS_DIR_NAME",
    "ALLOWED_SECTIONS",
    "CITATION_RE",
    "Stage4Error",
    "Stage4NotReady",
    "Stage4DispatchError",
    "stage_outputs_dir",
    "walnut_insights_dir",
    "world_insights_path",
    "walnut_insights_path",
    "stage3_done_path",
    "stage4_done_path",
    "spine_path",
    "anchors_path",
    "world_log_path",
    "people_logs_dir",
    "walnut_logs_dir",
    "entities_dir",
    "load_spine",
    "load_anchors",
    "load_stage3_done",
    "prepare_dispatch",
    "collect_outputs",
    "validate_insights",
    "retry_dispatch",
    "freeze_stage",
    "surface_double_failure",
)
