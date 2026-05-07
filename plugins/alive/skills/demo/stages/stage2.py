"""Stage 2 -- entity prose subagents: parallel dispatch + structural validation.

Stage 2 of the `/alive:demo` generation pipeline fans out one parallel
subagent per entity (person walnut, venture/experiment/life-area walnut,
bundle) and produces the per-entity kernel files (or bundle scaffold)
on disk. The runtime constraint is that this Python module CANNOT itself
fire Agent tool calls -- only the parent squirrel session can. So this
module owns four entry points the parent skill consumes:

  * :func:`prepare_dispatches` -- read the frozen spine + anchor envelope,
    slice anchor-moment refs per entity, build the per-entity prompt, and
    return a list of dispatch descriptors. The parent skill emits N Agent
    tool calls in a single message using these descriptors.

  * :func:`batch_dispatches` -- chunk the dispatch list into batches of
    `batch_size` (default 6 per Anthropic's ~7 concurrent guidance, one
    headroom slot reserved for the parent itself).

  * :func:`collect_outputs` -- after the parallel batch returns, walk
    each `entities/<slug>/` directory and report file presence so the
    parent skill knows which slugs need a retry.

  * :func:`validate_entity_outputs` -- hand-rolled stdlib validator
    enforcing the structural contract documented at
    `plugins/alive/templates/demo/schema/entity.schema.md`. Returns a
    list of findings; coherence-with-other-stages assertions are
    deferred to fn-2-2zz.10's `validate.py`.

  * :func:`retry_dispatches` -- build retry descriptors for the slugs
    whose validation findings are non-empty, appending the findings as
    feedback to the prompt. One-shot retry per epic-level locked
    decision; second failure surfaces a 3-option `AskUserQuestion` in
    the parent skill (the same pattern Stage 0 uses).

  * :func:`freeze_stage` -- write `<partial>/_stage_outputs/stage2_done.json`
    after all entities are present and pass validation; the marker is the
    handoff signal Stage 3 reads.

Per-entity output paths are deterministic:

    <partial>/_stage_outputs/entities/<slug>/

For bundles whose slug repeats across walnuts (the spine permits
`seed-round` under both `marcos-clothings` and `harbor-foods`), the
dispatcher uses a compound id `<walnut_slug>__<bundle_slug>` so the
two directories never collide.

The race-protection guarantee (per epic spec § "Stage 2 race protection"):
one subagent owns one slug; the directories never overlap. Within each
slug, the subagent's writes go through `_common.atomic_write_text` /
`_common.atomic_write_json` (temp + `os.replace`).

Stdlib-only. No `pyyaml`, no `jsonschema`. Frontmatter parsing is a
small hand-rolled extractor that handles the closed key sets the schema
documents. Tests cover the parser on every shape that appears in
practice.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Path bootstrap -- mirrors stage0.py / stage1.py
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.normpath(os.path.join(_HERE, os.pardir))
_PLUGIN_ROOT = os.path.normpath(os.path.join(_DEMO_DIR, os.pardir, os.pardir))
_SCRIPTS = os.path.join(_PLUGIN_ROOT, "scripts")
if os.path.isdir(_SCRIPTS) and _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from _common import atomic_write_json, atomic_write_text, iso_now, resolve_plugin_root  # noqa: E402


def _load_lib():
    """Load the demo `lib.py` sibling under a namespaced module key."""
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
    """Load the stage0 sibling for `render_subagent_brief` reuse."""
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

#: Schema version stamped into the stage-2 done marker. Independent of the
#: spine schema_version.
SCHEMA_VERSION = "0.1"

#: Path of the per-entity prompt template, relative to the plugin root.
ENTITY_PROMPT_RELPATH = os.path.join(
    "templates", "demo", "stage_prompts", "stage_2_entity.v1.md"
)

#: Path of the schema doc (referenced by tests for cross-link assertion).
ENTITY_SCHEMA_RELPATH = os.path.join(
    "templates", "demo", "schema", "entity.schema.md"
)

#: Default subagent kind for Stage 2 dispatch (matches Primitive B in
#: SKILL.md § "Dispatch primitives").
DEFAULT_SUBAGENT_TYPE = "general-purpose"

#: Anthropic guidance is ~7 concurrent; we reserve one headroom slot for
#: the dispatching squirrel itself, so 6 per batch is the conservative
#: default. Configurable per call.
DEFAULT_BATCH_SIZE = 6

#: Slug regex (mirrors `lib._SLUG_RE`). Duplicated here so the validator
#: works without importing `lib` if the test harness skips that path.
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: Strict zero-padded ISO 8601 date pattern (mirrors stage0._ISO_DATE_RE).
_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

#: Bundle compound slug pattern (`<walnut_slug>__<bundle_slug>`). Used by
#: the link validator to emit a target-kind-invalid finding distinct
#: from the malformed-slug finding when the operator tries to link to a
#: bundle from person/walnut frontmatter.
_BUNDLE_COMPOUND_RE = re.compile(
    r"^[a-z0-9]+(-[a-z0-9]+)*__[a-z0-9]+(-[a-z0-9]+)*$"
)


def _is_bundle_compound_slug(s: Any) -> bool:
    return isinstance(s, str) and bool(_BUNDLE_COMPOUND_RE.match(s))


def _frontmatter_links_set(fm_links: Any) -> set:
    """Extract a set of inner slugs from a frontmatter `links` list.

    `fm_links` is the parsed value of the `links` key (a list of
    `"[[slug]]"` strings). This returns the set of inner slug strings
    with brackets stripped, dropping anything malformed (those are
    caught by the closed-key validation already). Used by the
    bidirectional `links[*]` <-> `## Connections` consistency check.
    """
    out = set()
    if not isinstance(fm_links, list):
        return out
    for entry in fm_links:
        if isinstance(entry, str) and entry.startswith("[[") and entry.endswith("]]"):
            out.add(entry[2:-2])
    return out


def _connections_section_text(body: str) -> str:
    """Slice the `## Connections` section out of a key.md body.

    Returns the substring from `## Connections` (inclusive) up to the
    next `##` heading or end-of-file. If the heading is absent, returns
    an empty string. The caller passes this slice to the body wikilink
    matcher so links inside other sections (e.g. `## Voice` quoting a
    person by name without bracketing) do not pollute the bidirectional
    check.
    """
    match = re.search(r"(?ms)^##\s+Connections\s*$", body)
    if not match:
        return ""
    rest = body[match.end():]
    next_h2 = re.search(r"(?ms)^##\s+", rest)
    if next_h2:
        return rest[:next_h2.start()]
    return rest


def _key_people_section_text(body: str) -> str:
    """Slice the `## Key People` section out of a walnut key.md body."""
    match = re.search(r"(?ms)^##\s+Key People\s*$", body)
    if not match:
        return ""
    rest = body[match.end():]
    next_h2 = re.search(r"(?ms)^##\s+", rest)
    if next_h2:
        return rest[:next_h2.start()]
    return rest


def _section_text(body: str, heading: str) -> Optional[str]:
    """Return the text under an exact `## <heading>` H2 in `body`.

    Returns None if the heading is absent (caller emits a structural
    finding). Returns the empty string if the heading is present but
    empty (caller emits a "section empty" finding). The slice runs from
    just after the heading line to the next `##` heading or end-of-file.
    """
    pattern = re.compile(
        r"(?ms)^##\s+" + re.escape(heading) + r"\s*$",
    )
    match = pattern.search(body)
    if not match:
        return None
    rest = body[match.end():]
    next_h2 = re.search(r"(?ms)^##\s+", rest)
    if next_h2:
        return rest[:next_h2.start()]
    return rest


def _check_required_section(
    body: str,
    *,
    heading: str,
    slug: str,
    issue_missing: str,
    issue_empty: str,
    findings: List[Dict[str, Any]],
) -> None:
    """Require the section is present AND has non-blank prose."""
    section = _section_text(body, heading)
    if section is None:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": issue_missing,
            "evidence": f"key.md body must contain `## {heading}` H2",
        })
        return
    if not section.strip():
        findings.append({
            "slug": slug, "severity": "error",
            "issue": issue_empty,
            "evidence": f"key.md `## {heading}` section must be non-empty",
        })


def _check_links_body_bijection(
    fm_link_set: set,
    body_link_set: set,
    *,
    slug: str,
    body_section_name: str,
    findings: List[Dict[str, Any]],
) -> None:
    """Bidirectional check: every frontmatter link must appear in body
    section, and every body wikilink in that section must appear in
    frontmatter `links`.

    Bundle compound slugs are excluded from both sides (they were
    flagged separately by the kind-check). The bijection is computed
    on the symmetric difference of the two slug sets after stripping
    compound entries.
    """
    fm = {s for s in fm_link_set if not _is_bundle_compound_slug(s)}
    body = {s for s in body_link_set if not _is_bundle_compound_slug(s)}
    only_in_fm = fm - body
    only_in_body = body - fm
    if only_in_fm:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "links_body_mismatch_fm_only",
            "evidence": (
                f"{body_section_name}: frontmatter links {sorted(only_in_fm)} "
                f"have no matching wikilink in the body section"
            ),
        })
    if only_in_body:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "links_body_mismatch_body_only",
            "evidence": (
                f"{body_section_name}: body wikilinks {sorted(only_in_body)} "
                f"are not declared in frontmatter `links[*]`"
            ),
        })

#: Disallowed dash characters in user-visible prose (the standing
#: voice rule). Same set as stage1._DASH_CHARS.
_DASH_CHARS = ("—", "–", "―")

#: Closed key sets per entity_type's `key.md` frontmatter.
_PERSON_KEY_KEYS = frozenset({
    "type", "name", "slug", "voice", "role", "links", "created",
})
_WALNUT_KEY_KEYS = frozenset({
    "type", "name", "slug", "goal", "rhythm", "parent", "people", "links", "created",
})
_BUNDLE_MANIFEST_KEYS = frozenset({
    "name", "goal", "species", "phase", "parent_walnut", "created", "tags", "people",
})

_WALNUT_TYPES = frozenset({"venture", "experiment", "life-area", "minimal-life"})
_RHYTHMS = frozenset({"daily", "weekly", "sporadic"})
_BUNDLE_SPECIES = frozenset({"outcome", "evergreen"})
_BUNDLE_PHASES = frozenset({"draft", "prototype", "published", "done"})

#: Per-stage paths.
_STAGE_OUTPUTS_SUBDIR = "_stage_outputs"
_ENTITIES_SUBDIR = "entities"
_SPINE_FILENAME = "spine.json"
_ANCHORS_FILENAME = "anchor_moments.json"
_DONE_MARKER_FILENAME = "stage2_done.json"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class Stage2Error(RuntimeError):
    """Base error for Stage 2 dispatch + validation failures."""


class Stage2NotReady(Stage2Error):
    """Raised when the spine or anchor envelope is missing / not frozen."""


class Stage2DispatchError(Stage2Error):
    """Raised when the dispatcher cannot construct a valid descriptor."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _abspartial(partial_dir) -> str:
    """Canonicalize a partial directory path. Mirrors stage0._abspartial."""
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


def entities_dir(partial_dir) -> str:
    """Absolute `<partial>/_stage_outputs/entities/`. Created if needed."""
    out = os.path.join(stage_outputs_dir(partial_dir), _ENTITIES_SUBDIR)
    os.makedirs(out, exist_ok=True)
    return out


def entity_dir(partial_dir, slug: str) -> str:
    """Absolute path of one entity's per-slug directory.

    The dispatcher MUST canonicalize the slug before passing it (compound
    `<walnut>__<bundle>` for bundles, plain slug for persons + walnuts).
    The directory is created here so the subagent's atomic-writes have a
    target to land in even if the runtime spawns it in a fresh cwd.
    """
    if not isinstance(slug, str) or not slug:
        raise ValueError(f"entity slug must be non-empty string; got {slug!r}")
    out = os.path.join(entities_dir(partial_dir), slug)
    os.makedirs(out, exist_ok=True)
    return out


def spine_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _SPINE_FILENAME)


def anchors_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _ANCHORS_FILENAME)


def done_marker_path(partial_dir) -> str:
    return os.path.join(stage_outputs_dir(partial_dir), _DONE_MARKER_FILENAME)


# ---------------------------------------------------------------------------
# Spine + anchor envelope loading
# ---------------------------------------------------------------------------

def _load_json(path: str, label: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        raise Stage2NotReady(f"{label} not found at {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise Stage2Error(
            f"{label} at {path} unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Stage2Error(
            f"{label} at {path} is not valid JSON: "
            f"line {exc.lineno} col {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise Stage2Error(f"{label} top-level value must be object")
    return data


def load_spine(partial_dir) -> Dict[str, Any]:
    """Read + parse `<partial>/_stage_outputs/spine.json`."""
    return _load_json(spine_path(partial_dir), "spine.json")


def load_anchors(partial_dir) -> Dict[str, Any]:
    """Read + parse `<partial>/_stage_outputs/anchor_moments.json`."""
    return _load_json(anchors_path(partial_dir), "anchor_moments.json")


def _require_anchors_frozen(env: Dict[str, Any]) -> None:
    """Raise :class:`Stage2NotReady` if anchor envelope is not frozen."""
    if not env.get("frozen"):
        raise Stage2NotReady(
            "anchor_moments.json is not frozen; run Stage 1 to confirm "
            "and freeze before dispatching Stage 2"
        )


# ---------------------------------------------------------------------------
# Anchor slicing
# ---------------------------------------------------------------------------

def filter_anchor_refs_for_slug(
    anchors_env: Dict[str, Any],
    slug: str,
) -> List[Dict[str, Any]]:
    """Return the confirmed anchor moments referencing `slug`.

    The match looks at both `walnut_slugs` and `people_slugs`, since
    Stage 2 entities can be either kind. Bundles are matched by their
    `walnut_slug` (the parent walnut) -- a bundle inherits its parent
    walnut's anchors. The dispatcher passes the parent walnut slug for
    bundles via the optional `match_slugs` parameter on
    :func:`prepare_dispatches`.

    Returns a fresh list (never an internal reference into the envelope)
    so the caller can append to it without mutating frozen state.
    """
    confirmed = anchors_env.get("confirmed") or []
    out: List[Dict[str, Any]] = []
    for moment in confirmed:
        if not isinstance(moment, dict):
            continue
        walnuts = moment.get("walnut_slugs") or []
        people = moment.get("people_slugs") or []
        if slug in walnuts or slug in people:
            out.append(dict(moment))
    return out


# ---------------------------------------------------------------------------
# Per-entity dispatch descriptor builders
# ---------------------------------------------------------------------------

def _bundle_compound_slug(walnut_slug: str, bundle_slug: str) -> str:
    """`<walnut>__<bundle>` compound id used for bundle directories.

    Two underscores separates the parts unambiguously: spine slugs match
    `^[a-z0-9]+(-[a-z0-9]+)*$` so they cannot themselves contain double
    underscores. The dispatcher uses this id as the per-slug directory
    name AND as the lookup key in the dispatch list.
    """
    if not _SLUG_RE.match(walnut_slug):
        raise ValueError(
            f"walnut_slug {walnut_slug!r} does not match the slug rule"
        )
    if not _SLUG_RE.match(bundle_slug):
        raise ValueError(
            f"bundle_slug {bundle_slug!r} does not match the slug rule"
        )
    return f"{walnut_slug}__{bundle_slug}"


def _read_entity_template(plugin_root: str) -> str:
    target = os.path.join(plugin_root, ENTITY_PROMPT_RELPATH)
    with open(target, "r", encoding="utf-8") as f:
        return f.read()


def _substitute(template: str, mapping: Dict[str, str]) -> str:
    """Replace `{{key}}` placeholders. Unknown keys are left untouched."""
    out = template
    for key, value in mapping.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def _wrap_dispatch_prompt(*, brief: str, task_body: str) -> str:
    return (
        "CONTEXT:\n"
        f"{brief}\n"
        "\n"
        "TASK:\n"
        f"{task_body}"
    )


def _render_one_dispatch(
    *,
    entity_type: str,
    entity_slug: str,
    entity_data: Dict[str, Any],
    anchor_refs: List[Dict[str, Any]],
    output_dir: str,
    template: str,
    brief: str,
) -> str:
    """Render the prompt body for one entity. Pure string-substitution."""
    body = _substitute(
        template,
        {
            "subagent_brief": "[brief is wrapped via CONTEXT envelope below]",
            "entity_type": entity_type,
            "entity_data": json.dumps(entity_data, indent=2, ensure_ascii=False),
            "anchor_moment_refs": json.dumps(
                anchor_refs, indent=2, ensure_ascii=False,
            ),
            "output_dir": output_dir,
        },
    )
    return _wrap_dispatch_prompt(brief=brief, task_body=body)


def prepare_dispatches(
    partial_dir,
    *,
    world_root: str,
    plugin_root: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build the list of dispatch descriptors the parent skill fires.

    Reads the spine + anchor envelope from disk, validates the envelope
    is frozen, then for every persona / walnut / person / bundle entry
    in the spine produces a descriptor of the form::

        {
            "slug":                 <directory key>,
            "entity_type":          <"person" | "walnut" | "bundle">,
            "entity_data":          <JSON-serialisable slice of spine>,
            "anchor_moment_refs":   <list of dicts, possibly empty>,
            "output_dir":           <abs path to entities/<slug>/>,
            "subagent_type":        "general-purpose",
            "description":          <one-line label for Agent tool>,
            "prompt":               <CONTEXT/TASK-wrapped prompt>,
        }

    Bundle entities use the compound `<walnut_slug>__<bundle_slug>`
    directory key so two bundles named `seed-round` under different
    walnuts do not collide.

    Raises:
      - :class:`Stage2NotReady` if spine.json or anchor_moments.json is
        missing or the envelope is not frozen.
      - :class:`Stage2Error` on file IO / parse errors.
    """
    canonical = _abspartial(partial_dir)
    spine = load_spine(canonical)
    anchors_env = load_anchors(canonical)
    _require_anchors_frozen(anchors_env)

    plugin_root = plugin_root or resolve_plugin_root()
    template = _read_entity_template(plugin_root)
    stage0 = _load_stage0()
    brief = stage0.render_subagent_brief(world_root=world_root, plugin_root=plugin_root)

    descriptors: List[Dict[str, Any]] = []

    # Walnuts.
    for entry in spine.get("walnut_roster") or []:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise Stage2DispatchError(
                f"walnut_roster entry has invalid slug: {slug!r}"
            )
        out_dir = entity_dir(canonical, slug)
        anchor_refs = filter_anchor_refs_for_slug(anchors_env, slug)
        prompt = _render_one_dispatch(
            entity_type="walnut",
            entity_slug=slug,
            entity_data=entry,
            anchor_refs=anchor_refs,
            output_dir=out_dir,
            template=template,
            brief=brief,
        )
        descriptors.append({
            "slug": slug,
            "entity_type": "walnut",
            "entity_data": dict(entry),
            "anchor_moment_refs": anchor_refs,
            "output_dir": out_dir,
            "subagent_type": DEFAULT_SUBAGENT_TYPE,
            "description": f"alive-demo stage 2 walnut {slug}",
            "prompt": prompt,
        })

    # People.
    for entry in spine.get("people_roster") or []:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise Stage2DispatchError(
                f"people_roster entry has invalid slug: {slug!r}"
            )
        out_dir = entity_dir(canonical, slug)
        anchor_refs = filter_anchor_refs_for_slug(anchors_env, slug)
        prompt = _render_one_dispatch(
            entity_type="person",
            entity_slug=slug,
            entity_data=entry,
            anchor_refs=anchor_refs,
            output_dir=out_dir,
            template=template,
            brief=brief,
        )
        descriptors.append({
            "slug": slug,
            "entity_type": "person",
            "entity_data": dict(entry),
            "anchor_moment_refs": anchor_refs,
            "output_dir": out_dir,
            "subagent_type": DEFAULT_SUBAGENT_TYPE,
            "description": f"alive-demo stage 2 person {slug}",
            "prompt": prompt,
        })

    # Bundles (compound id).
    for entry in spine.get("bundle_distribution") or []:
        if not isinstance(entry, dict):
            continue
        bundle_slug = entry.get("slug")
        walnut_slug = entry.get("walnut_slug")
        if (
            not isinstance(bundle_slug, str) or not _SLUG_RE.match(bundle_slug)
            or not isinstance(walnut_slug, str) or not _SLUG_RE.match(walnut_slug)
        ):
            raise Stage2DispatchError(
                f"bundle_distribution entry has invalid slug pair: "
                f"{walnut_slug!r}, {bundle_slug!r}"
            )
        compound = _bundle_compound_slug(walnut_slug, bundle_slug)
        out_dir = entity_dir(canonical, compound)
        # Bundles inherit anchors via their parent walnut.
        anchor_refs = filter_anchor_refs_for_slug(anchors_env, walnut_slug)
        prompt = _render_one_dispatch(
            entity_type="bundle",
            entity_slug=compound,
            entity_data=entry,
            anchor_refs=anchor_refs,
            output_dir=out_dir,
            template=template,
            brief=brief,
        )
        descriptors.append({
            "slug": compound,
            "entity_type": "bundle",
            "entity_data": dict(entry),
            "anchor_moment_refs": anchor_refs,
            "output_dir": out_dir,
            "subagent_type": DEFAULT_SUBAGENT_TYPE,
            "description": f"alive-demo stage 2 bundle {compound}",
            "prompt": prompt,
        })

    return descriptors


def batch_dispatches(
    dispatches: Sequence[Dict[str, Any]],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> List[List[Dict[str, Any]]]:
    """Chunk a dispatch list into batches of `batch_size`.

    Per Anthropic's concurrent-tool-call guidance (~7), the default is
    6 with one headroom slot reserved for the parent's own bookkeeping.
    The parent skill emits one batch per assistant turn: all calls in
    the batch fire in a single message, the runtime fans out
    concurrently, the parent waits for all returns, then proceeds to
    the next batch.

    Empty input returns an empty list (NOT a list with one empty
    batch). A `batch_size` of 0 or negative raises ValueError.
    """
    if not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError(f"batch_size must be positive int; got {batch_size!r}")
    if not dispatches:
        return []
    out: List[List[Dict[str, Any]]] = []
    for i in range(0, len(dispatches), batch_size):
        out.append(list(dispatches[i:i + batch_size]))
    return out


# ---------------------------------------------------------------------------
# Output collection
# ---------------------------------------------------------------------------

def _expected_files(entity_type: str) -> List[str]:
    """Per-entity-type list of files Stage 2 must produce."""
    if entity_type == "person" or entity_type == "walnut":
        return ["key.md", "log.md", "insights.md"]
    if entity_type == "bundle":
        return ["context.manifest.yaml", "tasks.json"]
    raise ValueError(f"unknown entity_type: {entity_type!r}")


def collect_outputs(
    partial_dir,
    dispatches: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Walk each entity directory and report file presence per slug.

    Returns a dict keyed by slug::

        {
            "<slug>": {
                "entity_type": "person" | "walnut" | "bundle",
                "files": [<filename>, ...],     # files present on disk
                "missing": [<filename>, ...],   # expected but absent
                "status": "present" | "partial" | "missing",
            },
            ...
        }

    Status semantics:
      * `present` -- every expected file present.
      * `partial` -- some expected files present, some missing.
      * `missing` -- no expected files present (or directory missing).

    If `dispatches` is provided (the descriptor list from
    :func:`prepare_dispatches`), the iteration is over those slugs
    (ensuring missing directories show up in the result). Without it,
    the function walks `<partial>/_stage_outputs/entities/` and infers
    entity_type from file presence (key.md ⇒ person/walnut,
    context.manifest.yaml ⇒ bundle).
    """
    canonical = _abspartial(partial_dir)
    base = entities_dir(canonical)
    out: Dict[str, Dict[str, Any]] = {}

    if dispatches is not None:
        for d in dispatches:
            slug = d["slug"]
            etype = d["entity_type"]
            expected = _expected_files(etype)
            slug_dir = os.path.join(base, slug)
            present_files: List[str] = []
            missing_files: List[str] = []
            for fn in expected:
                if os.path.isfile(os.path.join(slug_dir, fn)):
                    present_files.append(fn)
                else:
                    missing_files.append(fn)
            if not present_files:
                status = "missing"
            elif missing_files:
                status = "partial"
            else:
                status = "present"
            out[slug] = {
                "entity_type": etype,
                "files": present_files,
                "missing": missing_files,
                "status": status,
            }
        return out

    # Inference path: walk the directory tree.
    if not os.path.isdir(base):
        return out
    for slug in sorted(os.listdir(base)):
        slug_dir = os.path.join(base, slug)
        if not os.path.isdir(slug_dir):
            continue
        has_key = os.path.isfile(os.path.join(slug_dir, "key.md"))
        has_manifest = os.path.isfile(os.path.join(slug_dir, "context.manifest.yaml"))
        if has_manifest and not has_key:
            etype = "bundle"
        elif has_key:
            # Persons and walnuts share file shape; we cannot disambiguate
            # without reading the frontmatter, but `validate_entity_outputs`
            # does that anyway. Tag the inferred type as "walnut" so the
            # validator can reclassify on read.
            etype = "walnut"
        else:
            # Empty directory: report as missing under a tentative type.
            etype = "walnut"
        expected = _expected_files(etype)
        present_files = [
            fn for fn in expected
            if os.path.isfile(os.path.join(slug_dir, fn))
        ]
        missing_files = [
            fn for fn in expected
            if not os.path.isfile(os.path.join(slug_dir, fn))
        ]
        if not present_files:
            status = "missing"
        elif missing_files:
            status = "partial"
        else:
            status = "present"
        out[slug] = {
            "entity_type": etype,
            "files": present_files,
            "missing": missing_files,
            "status": status,
        }
    return out


# ---------------------------------------------------------------------------
# Frontmatter parsing (stdlib-only)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<body>.*?)\n---\s*(?:\n|$)",
    re.DOTALL,
)


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse a fenced YAML-ish frontmatter block.

    Supports the closed key sets the schema documents. Each line is
    either:

      * `key: value`         -- scalar (string, bool, null, int, float)
      * `key:`               -- start of a list (subsequent `  - item` lines)
      * `  - item`           -- list element (string only)
      * `key: "quoted str"`  -- explicit string

    Returns `(frontmatter_dict, body_after_frontmatter)`.

    Raises ValueError on parse failure with a helpful message; the
    validator catches and surfaces this as a finding.
    """
    if not isinstance(text, str):
        raise ValueError(f"frontmatter parser requires str; got {type(text).__name__}")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("missing or malformed YAML frontmatter (expected leading `---`)")
    body_block = match.group("body")
    rest = text[match.end():]

    out: Dict[str, Any] = {}
    current_list_key: Optional[str] = None
    for raw_line in body_block.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            current_list_key = None
            continue
        # Comments.
        if line.lstrip().startswith("#"):
            continue
        # List element.
        stripped = line.lstrip()
        if stripped.startswith("- ") or stripped == "-":
            if current_list_key is None:
                raise ValueError(
                    f"unexpected list element outside of any key: {raw_line!r}"
                )
            value = stripped[1:].strip()
            value = _coerce_scalar(value) if value else ""
            out.setdefault(current_list_key, []).append(value)
            continue
        # key: [value]
        if ":" not in line:
            raise ValueError(
                f"unparseable frontmatter line: {raw_line!r}"
            )
        key, _, raw_value = line.partition(":")
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            raise ValueError(f"empty key in frontmatter line: {raw_line!r}")
        if not raw_value:
            # List or empty value. Mark for upcoming `- item` lines.
            out[key] = []
            current_list_key = key
            continue
        # Inline list `key: []` shortcut.
        if raw_value == "[]":
            out[key] = []
            current_list_key = None
            continue
        out[key] = _coerce_scalar(raw_value)
        current_list_key = None
    return out, rest


def _coerce_scalar(value: str) -> Any:
    """Coerce a frontmatter scalar string to its Python type.

    Quoted strings keep their inner content. Bare `null` -> None,
    `true`/`false` -> bool. Otherwise the string is returned verbatim
    (numbers stay strings unless explicitly needed; the schema doc
    treats every scalar as a string except where it documents bool /
    int / list).
    """
    s = value.strip()
    if not s:
        return ""
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if s == "null":
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    return s


# ---------------------------------------------------------------------------
# Validation (hand-rolled, stdlib-only)
# ---------------------------------------------------------------------------

def _is_slug(s: Any) -> bool:
    return isinstance(s, str) and bool(_SLUG_RE.match(s))


def _is_iso_date(s: Any) -> bool:
    if not isinstance(s, str) or not _ISO_DATE_RE.match(s):
        return False
    import datetime as _dt
    try:
        _dt.datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return False
    return True


def _has_dash(s: Any) -> bool:
    return isinstance(s, str) and any(d in s for d in _DASH_CHARS)


def _read_text_or_finding(
    path: str,
    *,
    slug: str,
    findings: List[Dict[str, Any]],
) -> Optional[str]:
    if not os.path.isfile(path):
        findings.append({
            "slug": slug,
            "severity": "error",
            "issue": "missing_file",
            "evidence": path,
        })
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        findings.append({
            "slug": slug,
            "severity": "error",
            "issue": "unreadable_file",
            "evidence": f"{path}: {type(exc).__name__}: {exc}",
        })
        return None


#: Match every wikilink in body prose, INCLUDING bundle compound slugs
#: that contain `__`. The validator branches on the matched form to
#: emit a target-kind-invalid finding for compound slugs, an
#: invalid-slug finding for malformed shapes, and an unresolved
#: finding for well-formed slugs not in `known_slugs`.
_BODY_WIKILINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9_\-]*)\]\]")


def _validate_wikilinks(
    body: str,
    *,
    slug: str,
    known_slugs: set,
    section_name: str,
    findings: List[Dict[str, Any]],
) -> List[str]:
    """Extract `[[slug]]` wikilinks from `body`; flag unknown / wrong-kind slugs.

    Returns the list of distinct wikilinks (slugs only, brackets stripped)
    so the caller can cross-check against frontmatter `links`. The body
    matcher is broader than `_SLUG_RE` so bundle compound refs of the
    form `[[<walnut>__<bundle>]]` are caught and rejected explicitly
    rather than silently skipped.
    """
    found: List[str] = []
    for match in _BODY_WIKILINK_RE.finditer(body):
        target = match.group(1)
        if target not in found:
            found.append(target)
        if _is_bundle_compound_slug(target):
            findings.append({
                "slug": slug, "severity": "error",
                "issue": "wikilink_target_kind_invalid",
                "evidence": (
                    f"{section_name}: [[{target}]] is a bundle compound "
                    f"slug; person / walnut bodies accept only person "
                    f"or walnut slugs"
                ),
            })
        elif not _is_slug(target):
            findings.append({
                "slug": slug,
                "severity": "error",
                "issue": "wikilink_invalid_slug",
                "evidence": f"{section_name}: [[{target}]]",
            })
        elif target not in known_slugs:
            findings.append({
                "slug": slug,
                "severity": "error",
                "issue": "wikilink_unresolved",
                "evidence": f"{section_name}: [[{target}]]",
            })
    return found


def _validate_person_key(
    text: str,
    *,
    slug: str,
    known_slugs: set,
    link_targets: set,
    findings: List[Dict[str, Any]],
) -> None:
    """Validate person `key.md`. `link_targets` is the closed set of slugs
    permitted as `links[*]` targets (person + walnut entity slugs only;
    bundle compound slugs are intentionally excluded per the schema doc).
    `known_slugs` is the broader set used for body wikilinks where bundle
    references are also permitted.
    """
    try:
        fm, body = _parse_frontmatter(text)
    except ValueError as exc:
        findings.append({
            "slug": slug,
            "severity": "error",
            "issue": "frontmatter_parse_error",
            "evidence": f"key.md: {exc}",
        })
        return

    actual = set(fm.keys())
    extra = actual - _PERSON_KEY_KEYS
    missing = _PERSON_KEY_KEYS - actual
    if extra:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_extra_keys",
            "evidence": f"key.md: {sorted(extra)}",
        })
    if missing:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_missing_keys",
            "evidence": f"key.md: {sorted(missing)}",
        })
    if extra or missing:
        return

    if fm.get("type") != "person":
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_type_mismatch",
            "evidence": f"key.md type={fm.get('type')!r} expected 'person'",
        })
    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_name_empty",
            "evidence": "key.md: name must be non-empty string",
        })
    fm_slug = fm.get("slug")
    if fm_slug != slug:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_slug_mismatch",
            "evidence": f"key.md slug={fm_slug!r} expected {slug!r}",
        })
    for k in ("voice", "role"):
        v = fm.get(k)
        if not isinstance(v, str) or not v.strip():
            findings.append({
                "slug": slug, "severity": "error",
                "issue": f"frontmatter_{k}_empty",
                "evidence": f"key.md: {k} must be non-empty string",
            })
    links = fm.get("links")
    if not isinstance(links, list) or not links:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_links_empty",
            "evidence": "key.md: links must be non-empty list of [[slug]] entries",
        })
    else:
        for entry in links:
            if not isinstance(entry, str) or not (
                entry.startswith("[[") and entry.endswith("]]")
            ):
                findings.append({
                    "slug": slug, "severity": "error",
                    "issue": "frontmatter_link_format",
                    "evidence": f"key.md links entry not [[slug]]: {entry!r}",
                })
                continue
            inner = entry[2:-2]
            # Bundle compound slug shape (`<walnut>__<bundle>`) is a
            # documented Stage 2 entity key BUT not a permitted link
            # target; surface a kind-specific finding so retry feedback
            # makes the rule unambiguous.
            if _is_bundle_compound_slug(inner):
                findings.append({
                    "slug": slug, "severity": "error",
                    "issue": "frontmatter_link_target_kind_invalid",
                    "evidence": (
                        f"key.md links: [[{inner}]] is a bundle "
                        f"compound slug; person `links` accept only "
                        f"person or walnut slugs"
                    ),
                })
            elif not _is_slug(inner):
                findings.append({
                    "slug": slug, "severity": "error",
                    "issue": "frontmatter_link_invalid_slug",
                    "evidence": f"key.md links: [[{inner}]]",
                })
            elif inner not in link_targets:
                findings.append({
                    "slug": slug, "severity": "error",
                    "issue": "frontmatter_link_unresolved",
                    "evidence": f"key.md links: [[{inner}]] not in entities/",
                })
    if not _is_iso_date(fm.get("created")):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_created_iso",
            "evidence": f"key.md created={fm.get('created')!r}",
        })

    # Body voice rules.
    if _has_dash(body):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "body_dash_character",
            "evidence": "key.md body contains em / en / horizontal-bar dash",
        })
    # Required body sections per the schema (Person):
    #   `## Voice`, `## Role in the world`, `## Connections`.
    _check_required_section(
        body, heading="Voice", slug=slug,
        issue_missing="body_missing_voice_section",
        issue_empty="body_voice_section_empty",
        findings=findings,
    )
    _check_required_section(
        body, heading="Role in the world", slug=slug,
        issue_missing="body_missing_role_section",
        issue_empty="body_role_section_empty",
        findings=findings,
    )
    _check_required_section(
        body, heading="Connections", slug=slug,
        issue_missing="body_missing_connections_section",
        issue_empty="body_connections_section_empty",
        findings=findings,
    )
    # Cross-check Connections wikilinks against the link-target set
    # (mirrors the frontmatter rule). Match only inside the Connections
    # section so an inline wiki ref elsewhere in the body does not
    # trip the bidirectional consistency check.
    connections = _connections_section_text(body)
    body_links = _validate_wikilinks(
        connections, slug=slug, known_slugs=link_targets,
        section_name="key.md ## Connections", findings=findings,
    )
    _check_links_body_bijection(
        _frontmatter_links_set(fm.get("links")),
        set(body_links),
        slug=slug,
        body_section_name="key.md ## Connections",
        findings=findings,
    )


def _validate_walnut_key(
    text: str,
    *,
    slug: str,
    known_slugs: set,
    known_people: set,
    link_targets: set,
    findings: List[Dict[str, Any]],
) -> None:
    """Validate walnut `key.md`. `link_targets` is the closed set of
    slugs permitted as `links[*]` targets (person + walnut entity slugs
    only; bundle compound slugs are intentionally excluded per the
    schema doc). `known_people` is used for `## Key People` body
    wikilinks (person slugs only).
    """
    try:
        fm, body = _parse_frontmatter(text)
    except ValueError as exc:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_parse_error",
            "evidence": f"key.md: {exc}",
        })
        return

    actual = set(fm.keys())
    extra = actual - _WALNUT_KEY_KEYS
    missing = _WALNUT_KEY_KEYS - actual
    if extra:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_extra_keys",
            "evidence": f"key.md: {sorted(extra)}",
        })
    if missing:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_missing_keys",
            "evidence": f"key.md: {sorted(missing)}",
        })
    if extra or missing:
        return

    if fm.get("type") not in _WALNUT_TYPES:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_type_invalid",
            "evidence": f"key.md type={fm.get('type')!r}; expected one of {sorted(_WALNUT_TYPES)}",
        })
    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_name_empty",
            "evidence": "key.md: name must be non-empty string",
        })
    fm_slug = fm.get("slug")
    if fm_slug != slug:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_slug_mismatch",
            "evidence": f"key.md slug={fm_slug!r} expected {slug!r}",
        })
    goal = fm.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_goal_empty",
            "evidence": "key.md: goal must be non-empty string",
        })
    if fm.get("rhythm") not in _RHYTHMS:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_rhythm_invalid",
            "evidence": f"key.md rhythm={fm.get('rhythm')!r}; expected {sorted(_RHYTHMS)}",
        })
    parent = fm.get("parent")
    if parent is not None and (not isinstance(parent, str) or not _is_slug(parent)):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_parent_invalid",
            "evidence": f"key.md parent={parent!r}; must be slug or null",
        })
    people = fm.get("people")
    if not isinstance(people, list):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_people_not_list",
            "evidence": f"key.md people={people!r}",
        })
    else:
        for p in people:
            if not _is_slug(p):
                findings.append({
                    "slug": slug, "severity": "error",
                    "issue": "frontmatter_people_invalid_slug",
                    "evidence": f"key.md people entry: {p!r}",
                })
            elif p not in known_people:
                findings.append({
                    "slug": slug, "severity": "error",
                    "issue": "frontmatter_people_unresolved",
                    "evidence": f"key.md people: {p!r} not a person entity",
                })
    links = fm.get("links")
    if not isinstance(links, list):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_links_not_list",
            "evidence": f"key.md links={links!r}",
        })
    elif not links:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_links_empty",
            "evidence": "key.md: links must be non-empty list of [[slug]] entries",
        })
    if isinstance(links, list):
        for entry in links:
            if not isinstance(entry, str) or not (
                entry.startswith("[[") and entry.endswith("]]")
            ):
                findings.append({
                    "slug": slug, "severity": "error",
                    "issue": "frontmatter_link_format",
                    "evidence": f"key.md links entry not [[slug]]: {entry!r}",
                })
                continue
            inner = entry[2:-2]
            if _is_bundle_compound_slug(inner):
                findings.append({
                    "slug": slug, "severity": "error",
                    "issue": "frontmatter_link_target_kind_invalid",
                    "evidence": (
                        f"key.md links: [[{inner}]] is a bundle "
                        f"compound slug; walnut `links` accept only "
                        f"person or walnut slugs"
                    ),
                })
            elif not _is_slug(inner):
                findings.append({
                    "slug": slug, "severity": "error",
                    "issue": "frontmatter_link_invalid_slug",
                    "evidence": f"key.md links: [[{inner}]]",
                })
            elif inner not in link_targets:
                findings.append({
                    "slug": slug, "severity": "error",
                    "issue": "frontmatter_link_unresolved",
                    "evidence": f"key.md links: [[{inner}]]",
                })
    if not _is_iso_date(fm.get("created")):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_created_iso",
            "evidence": f"key.md created={fm.get('created')!r}",
        })

    if _has_dash(body):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "body_dash_character",
            "evidence": "key.md body contains em / en / horizontal-bar dash",
        })
    # Required body sections per the schema (Walnut):
    #   `## Key People` (header always required; non-empty for venture
    #    / experiment / life-area; empty permitted for minimal-life per
    #    `entity.schema.md` § Walnut),
    #   `## Context` (always non-empty).
    walnut_type = fm.get("type")
    key_people_section = _section_text(body, "Key People")
    if key_people_section is None:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "body_missing_key_people_section",
            "evidence": "key.md body must contain `## Key People` H2",
        })
    elif walnut_type != "minimal-life" and not key_people_section.strip():
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "body_key_people_section_empty",
            "evidence": (
                f"key.md `## Key People` section must be non-empty for "
                f"walnut type {walnut_type!r}; only minimal-life walnuts "
                f"may have a zero-entry section"
            ),
        })
    _check_required_section(
        body, heading="Context", slug=slug,
        issue_missing="body_missing_context_section",
        issue_empty="body_context_section_empty",
        findings=findings,
    )
    # `## Key People` body wikilinks must resolve to person entities
    # (the walnut body's load-bearing role-bullet section). Bundle
    # compound refs are caught by `_validate_wikilinks` via the broader
    # body matcher and emitted as `wikilink_target_kind_invalid`.
    key_people = _key_people_section_text(body)
    _validate_wikilinks(
        key_people, slug=slug, known_slugs=known_people,
        section_name="key.md ## Key People", findings=findings,
    )


#: Closed key set for the Stage 2 log.md placeholder. Mirrors the prompt
#: template's documented frontmatter; the validator is the load-bearing
#: enforcement.
_LOG_PLACEHOLDER_KEYS = frozenset({
    "walnut", "created", "last-entry", "entry-count", "summary",
})

#: Closed key set for the Stage 2 insights.md placeholder.
_INSIGHTS_PLACEHOLDER_KEYS = frozenset({"walnut", "updated"})


#: Exact `summary` string required in the Stage 2 log.md placeholder.
#: Mirrors the prompt template + schema doc.
LOG_PLACEHOLDER_SUMMARY = "Stage 2 placeholder; populated in Stage 3."


def _validate_log_placeholder(
    text: str,
    *,
    slug: str,
    expected_walnut_name: Optional[str],
    findings: List[Dict[str, Any]],
) -> None:
    """Enforce the full log.md placeholder shape documented at
    `templates/demo/schema/entity.schema.md` § Person / Walnut.

    Required: closed frontmatter key set, ISO dates on `created` +
    `last-entry`, `entry-count` parses as 0, `summary` is the exact
    documented placeholder string, and `walnut` matches
    `expected_walnut_name` (the entity's display `name` from the
    spine descriptor or, in inference mode, from the parsed `key.md`
    frontmatter).

    The Stage 3 subagent overwrites this file later; rejecting an
    under-specified placeholder here means Stage 3 gets a known
    starting shape.
    """
    try:
        fm, body = _parse_frontmatter(text)
    except ValueError as exc:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_parse_error",
            "evidence": f"log.md: {exc}",
        })
        return
    # Reject any non-whitespace body content. Stage 2's log.md is
    # documented as frontmatter-only; the Stage 3 subagent overwrites
    # the file later with full log entries.
    if body.strip():
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "log_body_not_empty",
            "evidence": (
                "log.md must be frontmatter-only in Stage 2; "
                "Stage 3 populates the body"
            ),
        })
    actual = set(fm.keys())
    extra = actual - _LOG_PLACEHOLDER_KEYS
    missing = _LOG_PLACEHOLDER_KEYS - actual
    if extra:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "log_frontmatter_extra_keys",
            "evidence": f"log.md: {sorted(extra)}",
        })
    if missing:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "log_frontmatter_missing_keys",
            "evidence": f"log.md: {sorted(missing)}",
        })
    if extra or missing:
        return
    walnut = fm.get("walnut")
    if not isinstance(walnut, str) or not walnut.strip():
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "log_frontmatter_walnut_empty",
            "evidence": f"log.md walnut={walnut!r}",
        })
    elif (
        expected_walnut_name is not None
        and walnut != expected_walnut_name
    ):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "log_frontmatter_walnut_mismatch",
            "evidence": (
                f"log.md walnut={walnut!r}; expected the entity "
                f"display name {expected_walnut_name!r}"
            ),
        })
    for date_key in ("created", "last-entry"):
        if not _is_iso_date(fm.get(date_key)):
            findings.append({
                "slug": slug, "severity": "error",
                "issue": f"log_frontmatter_{date_key.replace('-', '_')}_iso",
                "evidence": f"log.md {date_key}={fm.get(date_key)!r}",
            })
    # entry-count must parse as the integer 0 (string "0" is also acceptable
    # since the YAML-ish parser leaves bare scalars as strings).
    raw_entry_count = fm.get("entry-count")
    try:
        entry_count_int = int(str(raw_entry_count).strip())
    except (TypeError, ValueError):
        entry_count_int = None
    if entry_count_int != 0:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "log_frontmatter_entry_count_not_zero",
            "evidence": f"log.md entry-count={raw_entry_count!r}; expected 0",
        })
    summary = fm.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "log_frontmatter_summary_empty",
            "evidence": "log.md: summary required (Stage 2 placeholder string)",
        })
    elif summary != LOG_PLACEHOLDER_SUMMARY:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "log_frontmatter_summary_mismatch",
            "evidence": (
                f"log.md summary={summary!r}; expected the exact "
                f"placeholder string {LOG_PLACEHOLDER_SUMMARY!r}"
            ),
        })


def _validate_insights_placeholder(
    text: str,
    *,
    slug: str,
    expected_walnut_name: Optional[str],
    findings: List[Dict[str, Any]],
) -> None:
    """Enforce the full insights.md placeholder shape.

    Required: closed frontmatter key set (`walnut`, `updated`),
    non-empty `walnut` matching `expected_walnut_name`, ISO date on
    `updated`, body contains the documented `## Strategy` H2 scaffold
    (the Stage 4 subagent appends to this section).
    """
    try:
        fm, body = _parse_frontmatter(text)
    except ValueError as exc:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_parse_error",
            "evidence": f"insights.md: {exc}",
        })
        return
    actual = set(fm.keys())
    extra = actual - _INSIGHTS_PLACEHOLDER_KEYS
    missing = _INSIGHTS_PLACEHOLDER_KEYS - actual
    if extra:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "insights_frontmatter_extra_keys",
            "evidence": f"insights.md: {sorted(extra)}",
        })
    if missing:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "insights_frontmatter_missing_keys",
            "evidence": f"insights.md: {sorted(missing)}",
        })
    if extra or missing:
        return
    walnut = fm.get("walnut")
    if not isinstance(walnut, str) or not walnut.strip():
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "insights_frontmatter_walnut_empty",
            "evidence": f"insights.md walnut={walnut!r}",
        })
    elif (
        expected_walnut_name is not None
        and walnut != expected_walnut_name
    ):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "insights_frontmatter_walnut_mismatch",
            "evidence": (
                f"insights.md walnut={walnut!r}; expected the entity "
                f"display name {expected_walnut_name!r}"
            ),
        })
    if not _is_iso_date(fm.get("updated")):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "insights_frontmatter_updated_iso",
            "evidence": f"insights.md updated={fm.get('updated')!r}",
        })
    if not re.search(r"(?m)^##\s+Strategy\s*$", body):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "insights_body_missing_strategy_h2",
            "evidence": "insights.md body must contain `## Strategy` scaffold",
        })
        return
    # Body must normalise to exactly `## Strategy` with optional
    # surrounding whitespace. Any non-whitespace content other than
    # the heading itself is the Stage 4 subagent's territory; rejecting
    # populated content here keeps the placeholder contract crisp.
    normalised = body.strip()
    if normalised != "## Strategy":
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "insights_body_not_empty_placeholder",
            "evidence": (
                "insights.md body must contain only `## Strategy` in "
                "Stage 2; Stage 4 populates the section"
            ),
        })


def _validate_bundle_manifest(
    text: str,
    *,
    slug: str,
    expected_walnut: Optional[str],
    known_slugs: set,
    known_people: set,
    findings: List[Dict[str, Any]],
) -> None:
    try:
        fm, _ = _parse_frontmatter(text)
    except ValueError as exc:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "frontmatter_parse_error",
            "evidence": f"context.manifest.yaml: {exc}",
        })
        return
    actual = set(fm.keys())
    extra = actual - _BUNDLE_MANIFEST_KEYS
    missing = _BUNDLE_MANIFEST_KEYS - actual
    if extra:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "manifest_extra_keys",
            "evidence": f"context.manifest.yaml: {sorted(extra)}",
        })
    if missing:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "manifest_missing_keys",
            "evidence": f"context.manifest.yaml: {sorted(missing)}",
        })
    if extra or missing:
        return

    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "manifest_name_empty",
            "evidence": "context.manifest.yaml: name must be non-empty string",
        })
    goal = fm.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "manifest_goal_empty",
            "evidence": "context.manifest.yaml: goal must be non-empty string",
        })
    if fm.get("species") not in _BUNDLE_SPECIES:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "manifest_species_invalid",
            "evidence": f"context.manifest.yaml species={fm.get('species')!r}",
        })
    if fm.get("phase") not in _BUNDLE_PHASES:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "manifest_phase_invalid",
            "evidence": f"context.manifest.yaml phase={fm.get('phase')!r}",
        })
    pw = fm.get("parent_walnut")
    if not _is_slug(pw):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "manifest_parent_invalid_slug",
            "evidence": f"context.manifest.yaml parent_walnut={pw!r}",
        })
    elif expected_walnut is not None and pw != expected_walnut:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "manifest_parent_mismatch",
            "evidence": (
                f"context.manifest.yaml parent_walnut={pw!r}; "
                f"expected {expected_walnut!r}"
            ),
        })
    elif pw not in known_slugs:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "manifest_parent_unresolved",
            "evidence": f"context.manifest.yaml parent_walnut={pw!r}",
        })
    if not _is_iso_date(fm.get("created")):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "manifest_created_iso",
            "evidence": f"context.manifest.yaml created={fm.get('created')!r}",
        })
    tags = fm.get("tags")
    if not isinstance(tags, list):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "manifest_tags_not_list",
            "evidence": f"context.manifest.yaml tags={tags!r}",
        })
    people = fm.get("people")
    if not isinstance(people, list):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "manifest_people_not_list",
            "evidence": f"context.manifest.yaml people={people!r}",
        })
    else:
        for p in people:
            if not _is_slug(p):
                findings.append({
                    "slug": slug, "severity": "error",
                    "issue": "manifest_people_invalid_slug",
                    "evidence": f"context.manifest.yaml people: {p!r}",
                })
            elif p not in known_people:
                findings.append({
                    "slug": slug, "severity": "error",
                    "issue": "manifest_people_unresolved",
                    "evidence": f"context.manifest.yaml people: {p!r}",
                })


def _validate_bundle_tasks(
    text: str,
    *,
    slug: str,
    findings: List[Dict[str, Any]],
) -> None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "tasks_json_parse_error",
            "evidence": f"tasks.json: line {exc.lineno} col {exc.colno}: {exc.msg}",
        })
        return
    if not isinstance(data, dict):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "tasks_json_top_level",
            "evidence": "tasks.json top-level must be object",
        })
        return
    if list(data.keys()) != ["tasks"]:
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "tasks_json_extra_keys",
            "evidence": f"tasks.json keys={sorted(data.keys())}",
        })
    if not isinstance(data.get("tasks"), list):
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "tasks_json_tasks_not_list",
            "evidence": f"tasks.json tasks={data.get('tasks')!r}",
        })
    elif data["tasks"]:
        # The Stage 2 contract pins `tasks.json` to `{"tasks": []}` so
        # backdated tasks land via Stage 5's deterministic Python path,
        # not via the LLM-driven Stage 2 bundle scaffold. Treat any
        # pre-populated entry as an error (blocks `freeze_stage`).
        findings.append({
            "slug": slug, "severity": "error",
            "issue": "tasks_json_not_empty",
            "evidence": (
                "tasks.json must be empty in Stage 2; backdated tasks are "
                "Stage 5's responsibility"
            ),
        })


def validate_entity_outputs(
    partial_dir,
    dispatches: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Run hand-rolled stdlib validation on every entity directory.

    Returns a flat findings list. Each finding is::

        {
            "slug": "<directory key>",
            "severity": "error" | "warning",
            "issue": "<short stable code>",
            "evidence": "<human-readable detail>",
        }

    Empty list means everything is well-formed.

    Cross-stage coherence (every anchor's `walnut_slugs` resolves, etc.)
    is NOT enforced here; that belongs to fn-2-2zz.10's `validate.py`.
    Stage 2 only checks what Stage 2 produced.

    `dispatches` (when provided) lets the validator know what was
    dispatched so it can flag missing slugs as well as malformed ones.
    Without it, only directories present on disk are validated.
    """
    canonical = _abspartial(partial_dir)
    base = entities_dir(canonical)
    findings: List[Dict[str, Any]] = []

    # Build the sets of known entity slugs so cross-references inside
    # Stage 2 outputs (links, parent_walnut, people) can resolve. The
    # inference path classifies each non-bundle directory by reading
    # its `key.md` frontmatter type field before populating
    # `known_people` / `known_walnuts`; pre-populating both sets with
    # every non-bundle slug would let a walnut slug masquerade as a
    # valid person ref (and vice versa).
    known_slugs: set = set()
    known_people: set = set()
    known_walnuts: set = set()
    iter_entries: Sequence[Tuple[str, str, Optional[str], Optional[str]]]

    if dispatches is not None:
        for d in dispatches:
            known_slugs.add(d["slug"])
            if d["entity_type"] == "person":
                known_people.add(d["slug"])
            elif d["entity_type"] == "walnut":
                known_walnuts.add(d["slug"])
        # Preserve the spine descriptor's display name so the
        # placeholder validators can enforce `walnut: <name>` exact
        # match. For bundles we don't validate placeholders -- the
        # name is unused there.
        iter_entries = [
            (
                d["slug"],
                d["entity_type"],
                d.get("entity_data", {}).get("walnut_slug"),
                d.get("entity_data", {}).get("name"),
            )
            for d in dispatches
        ]
    else:
        # Single inference pass: classify each directory by frontmatter
        # type, then populate the type-specific known sets. We also
        # record the parsed display `name` so the placeholder
        # validators can enforce the `walnut: <name>` field.
        inferred: List[Tuple[str, str, Optional[str], Optional[str]]] = []
        if os.path.isdir(base):
            for name in sorted(os.listdir(base)):
                slug_dir = os.path.join(base, name)
                if not os.path.isdir(slug_dir):
                    continue
                known_slugs.add(name)
                manifest_path = os.path.join(slug_dir, "context.manifest.yaml")
                key_path = os.path.join(slug_dir, "key.md")
                if os.path.isfile(manifest_path):
                    inferred.append((name, "bundle", None, None))
                    continue
                # Default to walnut when frontmatter is missing or
                # unparseable; the validator's downstream checks will
                # surface the missing-key.md / parse-error finding so
                # the misclassification does not silently pass.
                inferred_type = "walnut"
                inferred_name: Optional[str] = None
                if os.path.isfile(key_path):
                    try:
                        with open(key_path, "r", encoding="utf-8") as f:
                            fm, _ = _parse_frontmatter(f.read())
                        if fm.get("type") == "person":
                            inferred_type = "person"
                        elif fm.get("type") in _WALNUT_TYPES:
                            inferred_type = "walnut"
                        candidate_name = fm.get("name")
                        if isinstance(candidate_name, str) and candidate_name.strip():
                            inferred_name = candidate_name
                    except (OSError, ValueError):
                        pass
                if inferred_type == "person":
                    known_people.add(name)
                else:
                    known_walnuts.add(name)
                inferred.append((name, inferred_type, None, inferred_name))
        iter_entries = inferred

    for slug, etype, expected_walnut, expected_name in iter_entries:
        slug_dir = os.path.join(base, slug)
        if not os.path.isdir(slug_dir):
            findings.append({
                "slug": slug, "severity": "error",
                "issue": "directory_missing",
                "evidence": slug_dir,
            })
            continue

        # Frontmatter `links[*]` permits only person + walnut entity
        # slugs. Bundle compound slugs of the form `<walnut>__<bundle>`
        # are intentionally NOT link targets per
        # `templates/demo/schema/entity.schema.md` § Person / Walnut.
        link_targets = known_people | known_walnuts

        if etype in ("person", "walnut"):
            key_text = _read_text_or_finding(
                os.path.join(slug_dir, "key.md"),
                slug=slug, findings=findings,
            )
            if key_text is not None:
                if etype == "person":
                    _validate_person_key(
                        key_text, slug=slug,
                        known_slugs=known_slugs,
                        link_targets=link_targets,
                        findings=findings,
                    )
                else:
                    _validate_walnut_key(
                        key_text, slug=slug,
                        known_slugs=known_slugs,
                        known_people=known_people,
                        link_targets=link_targets,
                        findings=findings,
                    )
                # In inference mode we may not have had `expected_name`
                # at slug-classification time (e.g. malformed
                # frontmatter); fall back to the just-parsed key.md
                # `name` if present.
                if expected_name is None:
                    try:
                        fm_for_name, _ = _parse_frontmatter(key_text)
                        cand = fm_for_name.get("name")
                        if isinstance(cand, str) and cand.strip():
                            expected_name = cand
                    except ValueError:
                        pass
            log_text = _read_text_or_finding(
                os.path.join(slug_dir, "log.md"),
                slug=slug, findings=findings,
            )
            if log_text is not None:
                _validate_log_placeholder(
                    log_text, slug=slug,
                    expected_walnut_name=expected_name,
                    findings=findings,
                )
            insights_text = _read_text_or_finding(
                os.path.join(slug_dir, "insights.md"),
                slug=slug, findings=findings,
            )
            if insights_text is not None:
                _validate_insights_placeholder(
                    insights_text, slug=slug,
                    expected_walnut_name=expected_name,
                    findings=findings,
                )
        elif etype == "bundle":
            manifest_text = _read_text_or_finding(
                os.path.join(slug_dir, "context.manifest.yaml"),
                slug=slug, findings=findings,
            )
            if manifest_text is not None:
                _validate_bundle_manifest(
                    manifest_text, slug=slug,
                    expected_walnut=expected_walnut,
                    known_slugs=known_walnuts,
                    known_people=known_people,
                    findings=findings,
                )
            tasks_text = _read_text_or_finding(
                os.path.join(slug_dir, "tasks.json"),
                slug=slug, findings=findings,
            )
            if tasks_text is not None:
                _validate_bundle_tasks(
                    tasks_text, slug=slug, findings=findings,
                )

    return findings


# ---------------------------------------------------------------------------
# Retry construction
# ---------------------------------------------------------------------------

def retry_dispatches(
    failed: Sequence[Dict[str, Any]],
    findings: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build retry descriptors with feedback-bearing prompts.

    `failed` is a list of dispatch descriptors (output of
    :func:`prepare_dispatches`) for the slugs whose validation findings
    indicate a failure. `findings` is the validator's flat list; this
    function groups them per slug and appends a feedback block to each
    descriptor's prompt.

    Per the epic-level locked decision, retries are one-shot: this
    function returns ONE retry per descriptor regardless of how many
    times it has been called. The dispatcher must enforce the second-
    failure escalation; this function is idempotent on repeat calls
    against the same descriptor (the feedback block is appended once
    per call -- callers should not re-feed an already-retried
    descriptor).

    Returns a fresh list of new descriptors (does not mutate input).
    """
    by_slug: Dict[str, List[Dict[str, Any]]] = {}
    for f in findings:
        slug = f.get("slug")
        if not isinstance(slug, str):
            continue
        by_slug.setdefault(slug, []).append(f)

    out: List[Dict[str, Any]] = []
    for d in failed:
        slug = d["slug"]
        slug_findings = by_slug.get(slug, [])
        if not slug_findings:
            # No findings for this slug; skip.
            continue
        feedback_lines = [
            "",
            "---",
            "",
            "## Retry feedback",
            "",
            (
                "Your previous attempt failed Stage 2 validation. Fix the "
                "errors below and write corrected files to the same output "
                "paths via the standard atomic-write helpers."
            ),
            "",
            "### Findings",
        ]
        for finding in slug_findings:
            severity = finding.get("severity", "error")
            issue = finding.get("issue", "?")
            evidence = finding.get("evidence", "")
            feedback_lines.append(f"- [{severity}] {issue}: {evidence}")
        feedback = "\n".join(feedback_lines)
        new_prompt = d["prompt"] + "\n" + feedback
        retry = dict(d)
        retry["prompt"] = new_prompt
        retry["description"] = f"{d['description']} (retry)"
        retry["is_retry"] = True
        out.append(retry)
    return out


# ---------------------------------------------------------------------------
# Stage freeze
# ---------------------------------------------------------------------------

def freeze_stage(
    partial_dir,
    *,
    dispatches: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Write the Stage 2 done marker after every entity validates.

    Pre-conditions:
      * Every dispatched slug has all its expected files on disk
        (:func:`collect_outputs` reports `status == "present"`).
      * :func:`validate_entity_outputs` returns no `severity == "error"`
        findings.

    Idempotent: calling against an already-frozen marker rewrites it
    with a refreshed `frozen_at` timestamp. The marker shape is::

        {
            "schema_version": "0.1",
            "frozen": true,
            "frozen_at": "<ISO 8601 UTC>",
            "entity_count": <int>,
            "entity_slugs": [<slug>, ...]
        }

    Raises :class:`Stage2Error` (with a list of the blocking issues) if
    the pre-conditions fail; the parent skill surfaces them inside a
    bordered block before re-prompting.
    """
    canonical = _abspartial(partial_dir)
    coverage = collect_outputs(canonical, dispatches=dispatches)
    incomplete = [
        slug for slug, info in coverage.items()
        if info["status"] != "present"
    ]
    if incomplete:
        raise Stage2Error(
            f"cannot freeze stage 2: {len(incomplete)} entity directories "
            f"are not present: {sorted(incomplete)}"
        )

    findings = validate_entity_outputs(canonical, dispatches=dispatches)
    errors = [f for f in findings if f.get("severity") == "error"]
    if errors:
        slugs = sorted({f.get("slug", "?") for f in errors})
        raise Stage2Error(
            f"cannot freeze stage 2: {len(errors)} validation error(s) across "
            f"{len(slugs)} slug(s): {slugs}"
        )

    marker = {
        "schema_version": SCHEMA_VERSION,
        "frozen": True,
        "frozen_at": iso_now(),
        "entity_count": len(coverage),
        "entity_slugs": sorted(coverage.keys()),
    }
    atomic_write_json(done_marker_path(canonical), marker)
    # fn-2-2zz.16: advance the demo-state partial-generations row to
    # the next in-flight stage so the orchestrator's status / resume
    # surface reflects Stage 2 freeze without prose-driven mutation.
    # Best-effort: legacy / fixture partials with no registered row
    # are no-ops by design.
    _advance_demo_state_stage(canonical, "3_timeline")
    return marker


def _advance_demo_state_stage(partial_dir: str, new_stage: str) -> None:
    """Best-effort wrapper around ``state.advance_partial_stage``.

    Mirrors the helper in ``stage1.py`` / ``stage3.py`` / ``stage4.py``;
    swallows all errors so a demo-state write failure cannot block the
    on-disk freeze (the freeze marker is the load-bearing contract).
    """
    try:  # pragma: no cover - defensive against pathological env
        full_name = "alive_demo.state_for_stage2"
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


def surface_double_failure(
    validation_result: Any,
    *,
    partial_dir: str,
    raw_output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the user-facing failure envelope for a Stage 2 second-fail.

    Adapter to ``lib.report_validation_double_failure`` (fn-2-2zz.13,
    failure mode 15a). The skill orchestrator calls this when
    ``validate_entity_outputs`` (or the unified ``validate_stage("2", ...)``
    facade) returns errors after the one-shot retry through
    :func:`retry_dispatches` has also failed validation.

    Returns the envelope shape ``{"rendered_block", "state_updated",
    "partial_dir", "failure_mode": "validation_double_failure"}``. The
    rendered block is the surface the squirrel prints verbatim.
    """
    lib = _load_lib()
    report = lib.report_validation_double_failure(
        stage_id="2",
        validation_result=validation_result,
        partial_dir=partial_dir,
        raw_output_path=raw_output_path,
    )
    return {
        "failure_mode": "validation_double_failure",
        "stage": "2",
        "rendered_block": report["rendered_block"],
        "state_updated": report.get("state_updated", False),
        "partial_dir": partial_dir,
    }


__all__ = (
    "SCHEMA_VERSION",
    "ENTITY_PROMPT_RELPATH",
    "ENTITY_SCHEMA_RELPATH",
    "DEFAULT_SUBAGENT_TYPE",
    "DEFAULT_BATCH_SIZE",
    "Stage2Error",
    "Stage2NotReady",
    "Stage2DispatchError",
    "stage_outputs_dir",
    "entities_dir",
    "entity_dir",
    "spine_path",
    "anchors_path",
    "done_marker_path",
    "load_spine",
    "load_anchors",
    "filter_anchor_refs_for_slug",
    "prepare_dispatches",
    "batch_dispatches",
    "collect_outputs",
    "validate_entity_outputs",
    "retry_dispatches",
    "freeze_stage",
    "surface_double_failure",
)
