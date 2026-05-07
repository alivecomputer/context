"""Stage 3 -- timeline materialisation: dispatch + collect-validate + freeze.

Stage 3 of the `/alive:demo` generation pipeline runs a SINGLE subagent
that reads the frozen spine + anchor envelope + Stage 2 entity scaffolds
and writes the full log timeline:

  * `<partial>/_stage_outputs/log.md`              -- world-level cross-walnut log
  * `<partial>/_stage_outputs/people-logs/<slug>.md` -- one per person slug
  * `<partial>/_stage_outputs/walnut-logs/<slug>.md` -- one per walnut slug

Bundle activity is recorded inside the parent walnut's log (matching
v3 layout: bundles do not own a separate log file).

Per the spec's locked decisions:

  * Single subagent (not parallel) so one head can hold the full
    timeline in context and keep cross-references coherent.
  * Subagent writes to disk; this module reads the files back.
  * Squirrel IDs are deterministic via
    ``compute_squirrel_id(date_iso, entity_slugs)``; the validator
    re-derives the hash to catch fabricated ids.
  * Anchor coverage rule: each anchor moment must yield >=3 log entries
    referencing the moment's title or one of its hook entities.
  * Cross-walnut rule: regular (non-anchor) entries cite at most one
    walnut slug; anchor entries are the only entries permitted to span
    multiple walnuts.
  * Decision-WHY rule: any entry containing a Decision must include a
    matching ``WHY:`` line beneath.
  * Entity-ref rule: every ``[[slug]]`` resolves to a real walnut or
    person slug; one-off proper nouns (orgs, places, products) appear
    as plain text matching the documented "color" allowlist regex.

The runtime constraint (worker cannot fire Agent tool calls directly)
means this module exposes the four entry points the parent skill
consumes:

  * :func:`prepare_dispatch` -- gates on stage2_done.json, builds the
    single dispatch descriptor (prompt + paths + subagent_type).
  * :func:`collect_outputs` -- walks the world log, people-logs/,
    walnut-logs/ and reports presence + entry counts per file.
  * :func:`validate_timeline` -- hand-rolled stdlib validator returning
    a flat findings list (severity error / warn).
  * :func:`retry_dispatch` -- builds a one-shot retry descriptor with
    the failed-validator findings appended as feedback.
  * :func:`freeze_stage` -- writes ``_stage_outputs/stage3_done.json``
    after presence + validation pass.

Helper :func:`compute_squirrel_id` is exported for fixture tests + the
prompt template's worked example.

Stdlib-only. No yaml / jsonschema. Frontmatter parsing reuses the
hand-rolled extractor from `stage2.py` (loaded via importlib namespace
key, same pattern stage2 uses for stage0).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Path bootstrap -- mirrors stage0 / stage2 so direct imports under tests
# resolve `_common` without going through `cli.py`.
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
    """Load `stages/stage2.py` for path helpers + frontmatter parser."""
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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Schema version stamped into stage3_done.json. Independent of spine /
#: stage2 schema versions.
SCHEMA_VERSION = "0.1"

#: Default subagent kind for Stage 3 (single subagent, sequential).
DEFAULT_SUBAGENT_TYPE = "general-purpose"

#: Path of the per-stage prompt template, relative to the plugin root.
TIMELINE_PROMPT_RELPATH = os.path.join(
    "templates", "demo", "stage_prompts", "stage_3_timeline.v1.md"
)

#: Filenames + subdirs Stage 3 owns inside `_stage_outputs/`.
_STAGE_OUTPUTS_SUBDIR = "_stage_outputs"
_PEOPLE_LOGS_SUBDIR = "people-logs"
_WALNUT_LOGS_SUBDIR = "walnut-logs"
_WORLD_LOG_FILENAME = "log.md"
_STAGE2_DONE_FILENAME = "stage2_done.json"
_STAGE3_DONE_FILENAME = "stage3_done.json"
_SPINE_FILENAME = "spine.json"
_ANCHORS_FILENAME = "anchor_moments.json"

#: Slug regex matching `lib._SLUG_RE`. Mirrored to keep this module
#: independent of `lib`.
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: Disallowed dash characters in body prose (mirrors stage2._DASH_CHARS).
_DASH_CHARS = ("—", "–", "―")

#: Color-allowlist regex for one-off proper nouns appearing in plain
#: prose (NOT inside `[[...]]`). Matches an initial capital letter
#: followed by 1-60 chars from a conservative ASCII proper-noun
#: alphabet: letters, digits, spaces, period, ampersand, apostrophe,
#: hyphen. The allowlist is intentionally narrow so the validator
#: rejects free-form chatter that the writer probably forgot to
#: bracket. The validator does NOT enumerate every plain proper noun
#: in prose; it only surfaces a finding when the writer wraps a
#: non-resolving slug in `[[...]]`.
COLOR_NAME_RE = re.compile(r"^[A-Z][a-zA-Z0-9 .&'\-]{1,60}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class Stage3Error(RuntimeError):
    """Base error for Stage 3 dispatch + validation failures."""


class Stage3NotReady(Stage3Error):
    """Raised when stage2_done.json is missing or under-specified.

    The parent skill should fall back through the user-facing "run
    Stage 2 first" hint when it sees this; the message + hint shape
    matches Stage 2's `Stage2NotReady` envelope.
    """


class Stage3DispatchError(Stage3Error):
    """Raised when the dispatcher cannot construct a valid descriptor."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _abspartial(partial_dir) -> str:
    """Canonicalize a partial-directory path. Mirrors stage2._abspartial."""
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


def people_logs_dir(partial_dir) -> str:
    out = os.path.join(stage_outputs_dir(partial_dir), _PEOPLE_LOGS_SUBDIR)
    os.makedirs(out, exist_ok=True)
    return out


def walnut_logs_dir(partial_dir) -> str:
    out = os.path.join(stage_outputs_dir(partial_dir), _WALNUT_LOGS_SUBDIR)
    os.makedirs(out, exist_ok=True)
    return out


def world_log_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _WORLD_LOG_FILENAME)


def stage2_done_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _STAGE2_DONE_FILENAME)


def stage3_done_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _STAGE3_DONE_FILENAME)


def spine_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _SPINE_FILENAME)


def anchors_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _ANCHORS_FILENAME)


def entities_dir(partial_dir) -> str:
    """Absolute `<partial>/_stage_outputs/entities/`. Read-only for Stage 3."""
    return os.path.join(stage_outputs_dir(partial_dir), "entities")


def person_log_path(partial_dir, slug: str) -> str:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ValueError(f"person slug {slug!r} does not match the slug rule")
    return os.path.join(people_logs_dir(partial_dir), f"{slug}.md")


def walnut_log_path(partial_dir, slug: str) -> str:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ValueError(f"walnut slug {slug!r} does not match the slug rule")
    return os.path.join(walnut_logs_dir(partial_dir), f"{slug}.md")


# ---------------------------------------------------------------------------
# Squirrel-ID hashing (deterministic)
# ---------------------------------------------------------------------------

def compute_squirrel_id(date_iso: str, entity_slugs: Sequence[str]) -> str:
    """Compute a deterministic 16-char hex squirrel id.

    Inputs:
      * `date_iso` -- the entry's `## <date>` heading prefix. The full
        string is hashed, including any time component if present.
      * `entity_slugs` -- the slugs participating in this session. The
        function de-duplicates and sorts before hashing so caller
        ordering is irrelevant.

    Output: the first 16 hex chars of
    `sha256(date_iso + sha256(",".join(sorted_slugs)).hexdigest())`.

    The function is pure and stable across runs; the validator
    re-derives every entry's id and rejects mismatches.
    """
    if not isinstance(date_iso, str) or not date_iso:
        raise ValueError(f"date_iso must be non-empty str; got {date_iso!r}")
    if not isinstance(entity_slugs, (list, tuple)):
        raise TypeError(
            f"entity_slugs must be list/tuple; got {type(entity_slugs).__name__}"
        )
    sorted_slugs = sorted({str(s) for s in entity_slugs})
    entity_hash = hashlib.sha256(
        ",".join(sorted_slugs).encode("utf-8"),
    ).hexdigest()
    composite = (date_iso + entity_hash).encode("utf-8")
    return hashlib.sha256(composite).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Spine + anchor envelope + stage2 marker loading
# ---------------------------------------------------------------------------

def _load_json(path: str, label: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        raise Stage3NotReady(f"{label} not found at {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise Stage3Error(
            f"{label} at {path} unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Stage3Error(
            f"{label} at {path} is not valid JSON: "
            f"line {exc.lineno} col {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise Stage3Error(f"{label} top-level value must be object")
    return data


def load_spine(partial_dir) -> Dict[str, Any]:
    """Read + parse `<partial>/_stage_outputs/spine.json`."""
    return _load_json(spine_path(partial_dir), "spine.json")


def load_anchors(partial_dir) -> Dict[str, Any]:
    """Read + parse `<partial>/_stage_outputs/anchor_moments.json`."""
    return _load_json(anchors_path(partial_dir), "anchor_moments.json")


def load_stage2_done(partial_dir) -> Dict[str, Any]:
    """Read + parse `<partial>/_stage_outputs/stage2_done.json`.

    Stage 3 gates on this marker; if it's missing, prepare_dispatch
    raises :class:`Stage3NotReady`. The marker shape (per
    stage2.freeze_stage) is::

        {
            "schema_version": "0.1",
            "frozen": true,
            "frozen_at": "<ISO 8601 UTC>",
            "entity_count": <int>,
            "entity_slugs": [<slug>, ...]
        }

    A frozen=False or schema_version-mismatch marker also raises
    :class:`Stage3NotReady`.
    """
    marker = _load_json(stage2_done_path(partial_dir), "stage2_done.json")
    if not marker.get("frozen"):
        raise Stage3NotReady(
            "stage2_done.json present but `frozen` is not true; "
            "re-run Stage 2 freeze before dispatching Stage 3"
        )
    return marker


# ---------------------------------------------------------------------------
# Dispatch descriptor builder
# ---------------------------------------------------------------------------

def _read_template(plugin_root: str) -> str:
    target = os.path.join(plugin_root, TIMELINE_PROMPT_RELPATH)
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
    world_pth: str,
    people_logs_pth: str,
    walnut_logs_pth: str,
    stage_outputs_pth: str,
    template: str,
    brief: str,
) -> str:
    """Render the Stage 3 prompt body with explicit paths only.

    Per the task spec's token-budget consideration: the prompt embeds
    only file paths, never the full prose bodies. The subagent reads
    the prose off disk. This keeps the prompt size bounded by the
    number of paths (small constant) rather than the volume of prose
    (large variable).
    """
    body = _substitute(
        template,
        {
            "subagent_brief": "[brief is wrapped via CONTEXT envelope below]",
            "partial_dir": partial_dir,
            "spine_path": spine_pth,
            "anchor_moments_path": anchors_pth,
            "entities_dir": entities_pth,
            "world_log_path": world_pth,
            "people_logs_dir": people_logs_pth,
            "walnut_logs_dir": walnut_logs_pth,
            "stage_outputs_dir": stage_outputs_pth,
        },
    )
    return _wrap_dispatch_prompt(brief=brief, task_body=body)


def prepare_dispatch(
    partial_dir,
    *,
    world_root: str,
    plugin_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the single Stage 3 dispatch descriptor.

    Gates:
      * spine.json exists, parses, has `walnut_roster` + `people_roster`.
      * anchor_moments.json exists, parses, `frozen=True`.
      * stage2_done.json exists, parses, `frozen=True`.

    Returns a descriptor of the form::

        {
            "subagent_type":    "general-purpose",
            "description":      "alive-demo stage 3 timeline",
            "prompt":           <CONTEXT/TASK-wrapped prompt>,
            "output_paths":     {
                "world_log":      <abs path>,
                "people_logs":    [<abs path per person slug>, ...],
                "walnut_logs":    [<abs path per walnut slug>, ...],
            },
            "expected_people":  [<slug>, ...],
            "expected_walnuts": [<slug>, ...],
        }

    The parent skill fires a single Agent tool call using `subagent_type`
    + `description` + `prompt`; the rest of the descriptor is tracked
    so :func:`collect_outputs` and :func:`validate_timeline` know what
    to look for.

    Raises:
      * :class:`Stage3NotReady` if any input is missing / under-specified.
      * :class:`Stage3DispatchError` if a roster slug is invalid.
    """
    canonical = _abspartial(partial_dir)
    # Order matters: stage2 marker before spine/anchors so the parent
    # gets the most informative "Stage 2 first" error.
    load_stage2_done(canonical)

    spine = load_spine(canonical)
    anchors_env = load_anchors(canonical)
    if not anchors_env.get("frozen"):
        raise Stage3NotReady(
            "anchor_moments.json is not frozen; run Stage 1 to confirm "
            "and freeze before dispatching Stage 3"
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
            raise Stage3DispatchError(
                f"people_roster entry has invalid slug: {slug!r}"
            )
        expected_people.append(slug)

    expected_walnuts: List[str] = []
    for entry in spine.get("walnut_roster") or []:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise Stage3DispatchError(
                f"walnut_roster entry has invalid slug: {slug!r}"
            )
        expected_walnuts.append(slug)

    world_pth = world_log_path(canonical)
    people_pth = people_logs_dir(canonical)
    walnut_pth = walnut_logs_dir(canonical)
    entities_pth = entities_dir(canonical)
    stage_pth = stage_outputs_dir(canonical)

    prompt = _render_prompt(
        partial_dir=canonical,
        spine_pth=spine_path(canonical),
        anchors_pth=anchors_path(canonical),
        entities_pth=entities_pth,
        world_pth=world_pth,
        people_logs_pth=people_pth,
        walnut_logs_pth=walnut_pth,
        stage_outputs_pth=stage_pth,
        template=template,
        brief=brief,
    )

    output_paths = {
        "world_log": world_pth,
        "people_logs": [
            os.path.join(people_pth, f"{s}.md") for s in expected_people
        ],
        "walnut_logs": [
            os.path.join(walnut_pth, f"{s}.md") for s in expected_walnuts
        ],
    }

    return {
        "subagent_type": DEFAULT_SUBAGENT_TYPE,
        "description": "alive-demo stage 3 timeline",
        "prompt": prompt,
        "output_paths": output_paths,
        "expected_people": expected_people,
        "expected_walnuts": expected_walnuts,
    }


# ---------------------------------------------------------------------------
# Output collection
# ---------------------------------------------------------------------------

#: Match a per-entry `## <date>` heading. Captures the date prefix and the
#: squirrel id suffix. The date is permitted as either `YYYY-MM-DD` or
#: `YYYY-MM-DDTHH:MM:SS` (or a strftime-superset thereof) so the
#: validator can re-derive the squirrel id from whichever form the
#: writer used.
_ENTRY_HEADER_RE = re.compile(
    r"^##\s+(?P<date>\S+)\s+--\s+squirrel:(?P<sid>[0-9a-f]{16})\s*$",
    re.MULTILINE,
)


def _split_entries(body: str) -> List[Tuple[str, str, str]]:
    """Split a log body into `(date, sid, entry_text)` tuples.

    `entry_text` runs from the matched `## <date> -- squirrel:<sid>`
    heading (inclusive) up to the next `## ` heading or end of body.
    Frontmatter MUST already be stripped before calling.
    """
    headers = list(_ENTRY_HEADER_RE.finditer(body))
    out: List[Tuple[str, str, str]] = []
    for i, match in enumerate(headers):
        start = match.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(body)
        out.append((match.group("date"), match.group("sid"), body[start:end]))
    return out


def collect_outputs(
    partial_dir,
    expected_people: Optional[Sequence[str]] = None,
    expected_walnuts: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Walk the world log + people-logs/ + walnut-logs/ and report presence.

    Returns::

        {
            "world_log": {
                "path":         <abs>,
                "present":      <bool>,
                "entry_count":  <int or null>,
            },
            "people_logs": {
                "<slug>": {"path": <abs>, "present": <bool>, "entry_count": <int|null>},
                ...
            },
            "walnut_logs": {
                "<slug>": {"path": <abs>, "present": <bool>, "entry_count": <int|null>},
                ...
            },
        }

    `expected_people` / `expected_walnuts` (when provided) drive the
    iteration so missing files surface as `present: false`. Without
    them, the function infers expected files by listing each subdir.
    """
    canonical = _abspartial(partial_dir)
    out: Dict[str, Any] = {}

    world_pth = world_log_path(canonical)
    out["world_log"] = _file_summary(world_pth)

    people_pth = people_logs_dir(canonical)
    walnut_pth = walnut_logs_dir(canonical)

    person_slugs = (
        list(expected_people) if expected_people is not None
        else [n[:-3] for n in sorted(os.listdir(people_pth)) if n.endswith(".md")]
    )
    out["people_logs"] = {
        slug: _file_summary(os.path.join(people_pth, f"{slug}.md"))
        for slug in person_slugs
    }

    walnut_slugs = (
        list(expected_walnuts) if expected_walnuts is not None
        else [n[:-3] for n in sorted(os.listdir(walnut_pth)) if n.endswith(".md")]
    )
    out["walnut_logs"] = {
        slug: _file_summary(os.path.join(walnut_pth, f"{slug}.md"))
        for slug in walnut_slugs
    }

    return out


def _file_summary(path: str) -> Dict[str, Any]:
    """Per-file summary used by collect_outputs."""
    if not os.path.isfile(path):
        return {"path": path, "present": False, "entry_count": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {"path": path, "present": True, "entry_count": None}
    body = _strip_frontmatter(text)
    entry_count = len(_ENTRY_HEADER_RE.findall(body))
    return {"path": path, "present": True, "entry_count": entry_count}


def _strip_frontmatter(text: str) -> str:
    """Return the body after the leading `--- ... ---` block. Tolerant.

    If the text doesn't start with a frontmatter block, returns the
    text unchanged. The validator will surface a missing-frontmatter
    finding separately.
    """
    stage2 = _load_stage2()
    try:
        _, body = stage2._parse_frontmatter(text)
        return body
    except ValueError:
        return text


# ---------------------------------------------------------------------------
# Validation (hand-rolled, stdlib-only)
# ---------------------------------------------------------------------------

#: Match every `[[<slug>]]` wikilink. The body matcher is broader than
#: `_SLUG_RE` so bundle compound slugs containing `__` can be detected
#: and rejected explicitly with a more specific finding.
_BODY_WIKILINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9_\-]*)\]\]")

#: Decision-section detection. Triggers WHY-rule enforcement when any
#: of these patterns appears inside an entry body.
_DECISION_SECTION_RE = re.compile(r"(?ms)^###\s+Decisions\s*$")
_DECISION_BULLET_RE = re.compile(r"(?m)^-\s+\*\*[^*]+\*\*\s+--")
_DECISION_HEADER_RE = re.compile(r"(?m)^##\s+Decision\b")
_DECISION_PROSE_RE = re.compile(r"(?m)^Decision:\s")

#: WHY line detection inside a Decision context.
_WHY_RE = re.compile(r"(?m)^\s*WHY:\s+\S")

#: Bundle compound slug pattern (rejected from wikilink targets).
_BUNDLE_COMPOUND_RE = re.compile(
    r"^[a-z0-9]+(-[a-z0-9]+)*__[a-z0-9]+(-[a-z0-9]+)*$"
)


def _is_bundle_compound_slug(s: Any) -> bool:
    return isinstance(s, str) and bool(_BUNDLE_COMPOUND_RE.match(s))


def _has_dash(s: Any) -> bool:
    return isinstance(s, str) and any(d in s for d in _DASH_CHARS)


def _read_text_or_finding(
    path: str,
    *,
    log_kind: str,
    findings: List[Dict[str, Any]],
) -> Optional[str]:
    """Read a log file; on missing/unreadable, append an error finding."""
    if not os.path.isfile(path):
        findings.append({
            "log": log_kind, "severity": "error",
            "issue": "missing_file", "evidence": path,
        })
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        findings.append({
            "log": log_kind, "severity": "error",
            "issue": "unreadable_file",
            "evidence": f"{path}: {type(exc).__name__}: {exc}",
        })
        return None


def _entry_walnut_refs(entry_body: str, known_walnuts: set) -> List[str]:
    """Distinct walnut slugs cited via `[[slug]]` in an entry body."""
    out: List[str] = []
    for match in _BODY_WIKILINK_RE.finditer(entry_body):
        target = match.group(1)
        if target in known_walnuts and target not in out:
            out.append(target)
    return out


def _entry_has_decision(entry_body: str) -> bool:
    """True iff the entry contains any decision pattern."""
    if _DECISION_SECTION_RE.search(entry_body):
        return True
    if _DECISION_HEADER_RE.search(entry_body):
        return True
    if _DECISION_PROSE_RE.search(entry_body):
        return True
    if _DECISION_BULLET_RE.search(entry_body):
        return True
    return False


def _entry_has_why(entry_body: str) -> bool:
    """True iff the entry contains a `WHY:` line with content."""
    return bool(_WHY_RE.search(entry_body))


def _entry_is_anchor(
    entry_body: str,
    anchor_titles: Sequence[str],
    anchor_entity_slugs: Sequence[set],
) -> bool:
    """True iff the entry references any anchor moment.

    Matches if the entry body contains an anchor's `name` verbatim OR
    cites any slug in any anchor's hook entity set via `[[slug]]`.
    `anchor_entity_slugs` is parallel to `anchor_titles`: the i-th set
    is the i-th anchor's union of walnut + people slugs.
    """
    for title in anchor_titles:
        if title and title in entry_body:
            return True
    refs = {m.group(1) for m in _BODY_WIKILINK_RE.finditer(entry_body)}
    for slug_set in anchor_entity_slugs:
        if refs & slug_set:
            return True
    return False


def _validate_one_log(
    text: str,
    *,
    log_kind: str,
    expected_walnut_field: Optional[str],
    known_walnuts: set,
    known_people: set,
    findings: List[Dict[str, Any]],
) -> List[Tuple[str, str, str]]:
    """Validate one log file. Returns the parsed entry tuples for the caller.

    Each tuple is `(date, sid, entry_body)`. On parse failure returns
    an empty list AND appends a `frontmatter_parse_error` finding so
    the aggregate validator can keep going.

    `log_kind` is a stable identifier used in finding evidence:
    `"world"`, `"person:<slug>"`, `"walnut:<slug>"`.
    """
    stage2 = _load_stage2()
    try:
        fm, body = stage2._parse_frontmatter(text)
    except ValueError as exc:
        findings.append({
            "log": log_kind, "severity": "error",
            "issue": "frontmatter_parse_error",
            "evidence": f"{log_kind}: {exc}",
        })
        return []

    walnut_field = fm.get("walnut")
    if expected_walnut_field is not None and walnut_field != expected_walnut_field:
        findings.append({
            "log": log_kind, "severity": "error",
            "issue": "frontmatter_walnut_mismatch",
            "evidence": (
                f"{log_kind}: walnut={walnut_field!r}; "
                f"expected {expected_walnut_field!r}"
            ),
        })

    # Body dash check (vibe rule applies to whole body).
    if _has_dash(body):
        findings.append({
            "log": log_kind, "severity": "error",
            "issue": "body_dash_character",
            "evidence": f"{log_kind}: body contains em / en / horizontal-bar dash",
        })

    entries = _split_entries(body)
    if not entries:
        findings.append({
            "log": log_kind, "severity": "error",
            "issue": "no_entries",
            "evidence": f"{log_kind}: log body has zero `## <date> -- squirrel:<id>` headings",
        })

    # Frontmatter entry-count cross-check (advisory).
    raw_count = fm.get("entry-count")
    try:
        fm_count = int(str(raw_count).strip())
    except (TypeError, ValueError):
        fm_count = None
    if fm_count is not None and fm_count != len(entries):
        findings.append({
            "log": log_kind, "severity": "error",
            "issue": "frontmatter_entry_count_mismatch",
            "evidence": (
                f"{log_kind}: frontmatter entry-count={fm_count} but body "
                f"has {len(entries)} entries"
            ),
        })

    return entries


def _validate_entry_squirrel_id(
    entry: Tuple[str, str, str],
    *,
    log_kind: str,
    known_walnuts: set,
    known_people: set,
    findings: List[Dict[str, Any]],
) -> None:
    """Check the entry's squirrel id matches `compute_squirrel_id` over its
    cited entity slugs.
    """
    date, sid, body = entry
    refs = sorted({m.group(1) for m in _BODY_WIKILINK_RE.finditer(body)})
    cited = [s for s in refs if s in known_walnuts or s in known_people]
    expected = compute_squirrel_id(date, cited)
    if sid != expected:
        findings.append({
            "log": log_kind, "severity": "error",
            "issue": "squirrel_id_mismatch",
            "evidence": (
                f"{log_kind}: entry {date!r} has squirrel:{sid}; "
                f"expected {expected} from entity slugs {cited}"
            ),
        })


def _validate_entry_wikilinks(
    entry: Tuple[str, str, str],
    *,
    log_kind: str,
    known_walnuts: set,
    known_people: set,
    findings: List[Dict[str, Any]],
) -> None:
    """Reject wikilinks that don't resolve to a real walnut or person slug.

    Bundle compound slugs (`<walnut>__<bundle>`) get a kind-specific
    finding so the writer knows the rule. Plain proper nouns NOT wrapped
    in `[[...]]` are not validated by this function (per the color
    allowlist documented in the prompt).
    """
    date, _, body = entry
    seen: set = set()
    for match in _BODY_WIKILINK_RE.finditer(body):
        target = match.group(1)
        if target in seen:
            continue
        seen.add(target)
        if _is_bundle_compound_slug(target):
            findings.append({
                "log": log_kind, "severity": "error",
                "issue": "wikilink_target_kind_invalid",
                "evidence": (
                    f"{log_kind}: entry {date!r} cites [[{target}]]; "
                    f"bundle compound slugs are not permitted as wikilink "
                    f"targets in log entries"
                ),
            })
        elif not _SLUG_RE.match(target):
            findings.append({
                "log": log_kind, "severity": "error",
                "issue": "wikilink_invalid_slug",
                "evidence": f"{log_kind}: entry {date!r} cites [[{target}]]",
            })
        elif target not in known_walnuts and target not in known_people:
            findings.append({
                "log": log_kind, "severity": "error",
                "issue": "wikilink_unresolved",
                "evidence": (
                    f"{log_kind}: entry {date!r} cites [[{target}]]; "
                    f"not a real walnut or person slug from the spine"
                ),
            })


def _validate_entry_decision_why(
    entry: Tuple[str, str, str],
    *,
    log_kind: str,
    findings: List[Dict[str, Any]],
) -> None:
    date, _, body = entry
    if _entry_has_decision(body) and not _entry_has_why(body):
        findings.append({
            "log": log_kind, "severity": "error",
            "issue": "decision_missing_why",
            "evidence": (
                f"{log_kind}: entry {date!r} contains a Decision but no "
                f"`WHY:` line; rationale is required"
            ),
        })


def _validate_entry_cross_walnut(
    entry: Tuple[str, str, str],
    *,
    log_kind: str,
    known_walnuts: set,
    anchor_titles: Sequence[str],
    anchor_entity_slugs: Sequence[set],
    findings: List[Dict[str, Any]],
) -> None:
    """Non-anchor entries cite at most one walnut slug."""
    date, _, body = entry
    if _entry_is_anchor(body, anchor_titles, anchor_entity_slugs):
        return
    walnuts_cited = _entry_walnut_refs(body, known_walnuts)
    if len(walnuts_cited) > 1:
        findings.append({
            "log": log_kind, "severity": "error",
            "issue": "non_anchor_multi_walnut",
            "evidence": (
                f"{log_kind}: entry {date!r} cites {walnuts_cited} but is "
                f"not an anchor entry; only anchor entries may span "
                f"multiple walnuts"
            ),
        })


def _entry_anchor_match_slugs(
    entry_body: str,
    anchor_titles: Sequence[str],
    anchor_entity_slugs: Sequence[set],
) -> List[int]:
    """Return indices of every anchor moment this entry references.

    A reference is either: (a) the anchor's `name` appears verbatim in
    the body, OR (b) the entry cites at least one of the anchor's
    walnut/people slugs via `[[slug]]`.
    """
    refs = {m.group(1) for m in _BODY_WIKILINK_RE.finditer(entry_body)}
    out: List[int] = []
    for i, title in enumerate(anchor_titles):
        if title and title in entry_body:
            out.append(i)
            continue
        if refs & anchor_entity_slugs[i]:
            out.append(i)
    return out


def validate_timeline(
    partial_dir,
    *,
    expected_people: Optional[Sequence[str]] = None,
    expected_walnuts: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Run hand-rolled stdlib validation on the Stage 3 timeline outputs.

    Validators (each yields one or more `severity: error` findings on
    failure):

      1. Every expected log file present (world log, every person slug
         in `expected_people`, every walnut slug in `expected_walnuts`).
      2. Each present log parses (frontmatter block + at least one
         `## <date>` entry heading); entry-count frontmatter matches body.
      3. Anchor coverage: every anchor moment in `anchor_moments.json`
         yields >=3 entries across all logs that reference its title or
         hook entities.
      4. Decision-WHY rule: any entry with a Decision pattern includes
         a `WHY:` line.
      5. Entity-ref resolution: every `[[slug]]` resolves to a real
         walnut or person slug; bundle compound slugs are rejected
         with a kind-specific finding.
      6. Squirrel-ID stability: re-deriving the hash from the entry's
         date+entity_hash matches the entry's id.
      7. Cross-walnut rule: non-anchor entries cite at most one walnut
         slug.

    `expected_people` / `expected_walnuts` (when provided) drive the
    iteration so missing files surface as findings. Without them, the
    function reads spine.json + anchor_moments.json from disk to derive
    them.

    Returns a flat list of findings. Empty list means everything is
    well-formed.
    """
    canonical = _abspartial(partial_dir)
    findings: List[Dict[str, Any]] = []

    # Resolve expected slugs from disk if not supplied.
    if expected_people is None or expected_walnuts is None:
        try:
            spine = load_spine(canonical)
        except (Stage3NotReady, Stage3Error) as exc:
            findings.append({
                "log": "spine", "severity": "error",
                "issue": "spine_not_loadable",
                "evidence": str(exc),
            })
            return findings
        if expected_people is None:
            expected_people = [
                e.get("slug") for e in (spine.get("people_roster") or [])
                if isinstance(e, dict) and isinstance(e.get("slug"), str)
            ]
        if expected_walnuts is None:
            expected_walnuts = [
                e.get("slug") for e in (spine.get("walnut_roster") or [])
                if isinstance(e, dict) and isinstance(e.get("slug"), str)
            ]
    else:
        try:
            spine = load_spine(canonical)
        except (Stage3NotReady, Stage3Error):
            spine = {}

    # Build display-name lookup for frontmatter `walnut: <name>` checks.
    name_for_slug: Dict[str, str] = {}
    for entry in (spine.get("walnut_roster") or []):
        if isinstance(entry, dict):
            slug = entry.get("slug")
            name = entry.get("name")
            if isinstance(slug, str) and isinstance(name, str):
                name_for_slug[slug] = name
    for entry in (spine.get("people_roster") or []):
        if isinstance(entry, dict):
            slug = entry.get("slug")
            name = entry.get("name")
            if isinstance(slug, str) and isinstance(name, str):
                name_for_slug[slug] = name

    # Anchor moments.
    try:
        anchors_env = load_anchors(canonical)
    except (Stage3NotReady, Stage3Error) as exc:
        findings.append({
            "log": "anchors", "severity": "error",
            "issue": "anchors_not_loadable",
            "evidence": str(exc),
        })
        anchors_env = {"confirmed": []}

    confirmed = anchors_env.get("confirmed") or []
    anchor_titles: List[str] = []
    anchor_entity_slugs: List[set] = []
    for moment in confirmed:
        if not isinstance(moment, dict):
            anchor_titles.append("")
            anchor_entity_slugs.append(set())
            continue
        anchor_titles.append(moment.get("name") or "")
        ws = set(moment.get("walnut_slugs") or [])
        ps = set(moment.get("people_slugs") or [])
        anchor_entity_slugs.append(ws | ps)

    known_walnuts: set = set(expected_walnuts or [])
    known_people: set = set(expected_people or [])

    # Read every log + collect entries (for cross-log anchor coverage).
    all_entries: List[Tuple[str, Tuple[str, str, str]]] = []  # (log_kind, entry)

    world_pth = world_log_path(canonical)
    world_text = _read_text_or_finding(world_pth, log_kind="world", findings=findings)
    if world_text is not None:
        # The world log's frontmatter `walnut` is the literal string `world`.
        entries = _validate_one_log(
            world_text, log_kind="world",
            expected_walnut_field="world",
            known_walnuts=known_walnuts,
            known_people=known_people,
            findings=findings,
        )
        for e in entries:
            all_entries.append(("world", e))

    for slug in expected_people or []:
        log_kind = f"person:{slug}"
        path = os.path.join(people_logs_dir(canonical), f"{slug}.md")
        text = _read_text_or_finding(path, log_kind=log_kind, findings=findings)
        if text is None:
            continue
        entries = _validate_one_log(
            text, log_kind=log_kind,
            expected_walnut_field=name_for_slug.get(slug),
            known_walnuts=known_walnuts,
            known_people=known_people,
            findings=findings,
        )
        for e in entries:
            all_entries.append((log_kind, e))

    for slug in expected_walnuts or []:
        log_kind = f"walnut:{slug}"
        path = os.path.join(walnut_logs_dir(canonical), f"{slug}.md")
        text = _read_text_or_finding(path, log_kind=log_kind, findings=findings)
        if text is None:
            continue
        entries = _validate_one_log(
            text, log_kind=log_kind,
            expected_walnut_field=name_for_slug.get(slug),
            known_walnuts=known_walnuts,
            known_people=known_people,
            findings=findings,
        )
        for e in entries:
            all_entries.append((log_kind, e))

    # Per-entry validators.
    for log_kind, entry in all_entries:
        _validate_entry_wikilinks(
            entry, log_kind=log_kind,
            known_walnuts=known_walnuts, known_people=known_people,
            findings=findings,
        )
        _validate_entry_decision_why(entry, log_kind=log_kind, findings=findings)
        _validate_entry_squirrel_id(
            entry, log_kind=log_kind,
            known_walnuts=known_walnuts, known_people=known_people,
            findings=findings,
        )
        _validate_entry_cross_walnut(
            entry, log_kind=log_kind,
            known_walnuts=known_walnuts,
            anchor_titles=anchor_titles,
            anchor_entity_slugs=anchor_entity_slugs,
            findings=findings,
        )

    # Anchor coverage rule: every anchor needs >=3 entries referencing it.
    if confirmed:
        coverage_counts = [0] * len(confirmed)
        for _, entry in all_entries:
            _, _, body = entry
            indices = _entry_anchor_match_slugs(
                body, anchor_titles, anchor_entity_slugs,
            )
            for i in indices:
                coverage_counts[i] += 1
        for i, moment in enumerate(confirmed):
            if not isinstance(moment, dict):
                continue
            slug = moment.get("slug") or f"<index {i}>"
            count = coverage_counts[i]
            if count < 3:
                findings.append({
                    "log": "world", "severity": "error",
                    "issue": "anchor_coverage_under_threshold",
                    "evidence": (
                        f"anchor {slug!r}: {count} entries reference its "
                        f"title or hook entities (need >=3)"
                    ),
                })

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
            "Your previous attempt failed Stage 3 validation. Fix the "
            "errors below and write corrected log files to the same "
            "output paths via the standard atomic-write helpers."
        ),
        "",
        "### Findings",
    ]
    if not error_findings:
        feedback_lines.append("- (no error findings; retry is a no-op)")
    for finding in error_findings:
        log = finding.get("log", "?")
        issue = finding.get("issue", "?")
        evidence = finding.get("evidence", "")
        feedback_lines.append(f"- [{log}] {issue}: {evidence}")
    feedback = "\n".join(feedback_lines)
    new_prompt = descriptor["prompt"] + "\n" + feedback
    retry = dict(descriptor)
    retry["prompt"] = new_prompt
    retry["description"] = descriptor.get("description", "alive-demo stage 3 timeline") + " (retry)"
    retry["is_retry"] = True
    return retry


# ---------------------------------------------------------------------------
# Stage freeze
# ---------------------------------------------------------------------------

def freeze_stage(
    partial_dir,
    *,
    expected_people: Optional[Sequence[str]] = None,
    expected_walnuts: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Write the Stage 3 done marker after presence + validation pass.

    Pre-conditions:
      * world log + every per-person + every per-walnut log present.
      * :func:`validate_timeline` returns no `severity == "error"`
        findings.

    Idempotent: calling against an already-frozen marker rewrites it
    with a refreshed `frozen_at` timestamp. The marker shape is::

        {
            "schema_version":  "0.1",
            "frozen":          true,
            "frozen_at":       "<ISO 8601 UTC>",
            "world_log":       <abs path>,
            "people_count":    <int>,
            "walnut_count":    <int>,
            "entry_count":     <int>  // total across all logs
        }

    Raises :class:`Stage3Error` (with a list of blocking issues) if the
    pre-conditions fail.
    """
    canonical = _abspartial(partial_dir)
    coverage = collect_outputs(
        canonical,
        expected_people=expected_people,
        expected_walnuts=expected_walnuts,
    )
    missing: List[str] = []
    if not coverage["world_log"]["present"]:
        missing.append("world_log")
    for slug, info in coverage["people_logs"].items():
        if not info["present"]:
            missing.append(f"people-logs/{slug}.md")
    for slug, info in coverage["walnut_logs"].items():
        if not info["present"]:
            missing.append(f"walnut-logs/{slug}.md")
    if missing:
        raise Stage3Error(
            f"cannot freeze stage 3: {len(missing)} log file(s) missing: "
            f"{missing}"
        )

    findings = validate_timeline(
        canonical,
        expected_people=expected_people,
        expected_walnuts=expected_walnuts,
    )
    errors = [f for f in findings if f.get("severity") == "error"]
    if errors:
        issues = sorted({f.get("issue", "?") for f in errors})
        raise Stage3Error(
            f"cannot freeze stage 3: {len(errors)} validation error(s); "
            f"issues: {issues}"
        )

    total_entries = 0
    if coverage["world_log"]["entry_count"] is not None:
        total_entries += coverage["world_log"]["entry_count"]
    for info in coverage["people_logs"].values():
        if info["entry_count"] is not None:
            total_entries += info["entry_count"]
    for info in coverage["walnut_logs"].values():
        if info["entry_count"] is not None:
            total_entries += info["entry_count"]

    marker = {
        "schema_version": SCHEMA_VERSION,
        "frozen": True,
        "frozen_at": iso_now(),
        "world_log": coverage["world_log"]["path"],
        "people_count": len(coverage["people_logs"]),
        "walnut_count": len(coverage["walnut_logs"]),
        "entry_count": total_entries,
    }
    atomic_write_json(stage3_done_path(canonical), marker)
    # fn-2-2zz.16: advance the demo-state partial-generations row to
    # the next in-flight stage so ``alive demo status`` / ``resume``
    # reflect Stage 3 freeze. Best-effort.
    _advance_demo_state_stage(canonical, "4_insights")
    return marker


def _advance_demo_state_stage(partial_dir: str, new_stage: str) -> None:
    """Best-effort wrapper around ``state.advance_partial_stage``."""
    try:  # pragma: no cover - defensive against pathological env
        full_name = "alive_demo.state_for_stage3"
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
    """Build the user-facing failure envelope for a Stage 3 second-fail.

    Adapter to ``lib.report_validation_double_failure`` (fn-2-2zz.13).
    The skill orchestrator calls this when ``validate_timeline`` (or the
    unified ``validate_stage("3", ...)`` facade) returns errors after the
    one-shot retry through :func:`retry_dispatch` has also failed.
    """
    lib = _load_lib_for_failure()
    report = lib.report_validation_double_failure(
        stage_id="3",
        validation_result=validation_result,
        partial_dir=partial_dir,
        raw_output_path=raw_output_path,
    )
    return {
        "failure_mode": "validation_double_failure",
        "stage": "3",
        "rendered_block": report["rendered_block"],
        "state_updated": report.get("state_updated", False),
        "partial_dir": partial_dir,
    }


__all__ = (
    "SCHEMA_VERSION",
    "DEFAULT_SUBAGENT_TYPE",
    "TIMELINE_PROMPT_RELPATH",
    "COLOR_NAME_RE",
    "Stage3Error",
    "Stage3NotReady",
    "Stage3DispatchError",
    "stage_outputs_dir",
    "people_logs_dir",
    "walnut_logs_dir",
    "world_log_path",
    "stage2_done_path",
    "stage3_done_path",
    "spine_path",
    "anchors_path",
    "entities_dir",
    "person_log_path",
    "walnut_log_path",
    "compute_squirrel_id",
    "load_spine",
    "load_anchors",
    "load_stage2_done",
    "prepare_dispatch",
    "collect_outputs",
    "validate_timeline",
    "retry_dispatch",
    "freeze_stage",
    "surface_double_failure",
)
