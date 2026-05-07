"""alive-demo helpers — bordered-block printing, ULID generation, label derivation.

Pure-Python, stdlib + vendored python-ulid only. Importing this module via the
plugin's normal entry points (`scripts/cli.py` → `cli_register.py` → `lib`) is
the only supported activation path: `_common` puts `_vendor/` on `sys.path`, so
`from ulid import ULID` resolves to the frozen vendored copy without any extra
wiring here.

Three concerns:

* **Bordered-block formatting (`format_block`, `format_table`).** Renders the
  3-character `╭ │ ╰` block with `🐿️` + `▸` markers used by the ALIVE visual
  contract (see `plugins/alive/CLAUDE.md` § "Visual Conventions — MANDATORY").
  Used by `alive demo status`, `alive demo list`, etc. The skill's `SKILL.md`
  emits its own inline markdown for blocks the LLM produces; this module only
  serves the CLI surface (per the codex review which rejected a unified
  Python-side `render_block` helper — LLM formatting can't be enforced from
  Python).
* **ULID generation (`new_world_ulid`).** Wraps the vendored `python-ulid`
  3.1.0 API: `from ulid import ULID; str(ULID()).lower()`. The `wld_` prefix
  is joined with an underscore. ULIDs are 26-char Crockford base32 (lowercase
  per ALIVE convention).
* **Label derivation (`derive_label`).** Free-text persona description →
  filesystem-safe slug. Strips punctuation, lowercases, takes first ~5
  meaningful words, joins with hyphens. Deterministic per input string;
  pure function. Output matches the slug rule documented in
  `_spike.md` § "Slug sanitization": `^[a-z0-9]+(-[a-z0-9]+)*$`.

Stop-words list is intentionally small and English-only — the demo skill only
supports English personas in v3.2 (per epic spec § "Out of scope"). Adding new
stop-words is a non-breaking change so long as `derive_label` remains pure.
"""

from __future__ import annotations

import datetime as _dt
import os as _os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

# `ulid` resolves via the `_vendor/` insert that `_common` performs at import
# time. Import it lazily so test code that exercises `derive_label` /
# `format_block` without going through `cli.py` still works (e.g. unit tests
# that import this module directly under HOME-monkeypatch).
def _import_ulid():
    """Import vendored python-ulid, ensuring `_vendor/` is on `sys.path` first."""
    try:
        from ulid import ULID  # noqa: PLC0415
        return ULID
    except ImportError:
        # Bootstrap `_vendor/` and retry. `_common` does this on its own
        # imports, but `lib.py` may be imported directly by tests that
        # haven't pulled `_common` yet.
        import os
        import sys as _sys

        here = os.path.dirname(os.path.abspath(__file__))
        plugin_root = os.path.normpath(os.path.join(here, os.pardir, os.pardir))
        vendor = os.path.join(plugin_root, "_vendor")
        if os.path.isdir(vendor) and vendor not in _sys.path:
            _sys.path.insert(0, vendor)
        from ulid import ULID  # noqa: PLC0415
        return ULID


# ---------------------------------------------------------------------------
# Bordered-block printing
# ---------------------------------------------------------------------------

# Box-drawing characters from `plugins/alive/CLAUDE.md` § "Visual Conventions".
_BLOCK_TOP = "╭─"
_BLOCK_LEFT = "│"
_BLOCK_BOT = "╰─"
_SQUIRREL = "🐿️"


def format_block(title: str, body: str) -> str:
    """Render a single bordered block as a string.

    Layout (matches the canonical shape in CLAUDE.md):

        ╭─ 🐿️ {title}
        │  {body line 1}
        │  {body line 2}
        ...
        ╰─

    Empty body lines render as a bare ``│`` (matches the spacing in the
    `/alive:save` and `/alive:world` skill examples).

    Args:
        title: One-line block title. Rendered after the squirrel emoji.
        body: Multi-line body. Each ``\\n``-separated line gets an
            ``│  `` prefix; an empty line yields a bare ``│``.

    Returns:
        The rendered block as a single string. NO trailing newline; the
        caller appends one if printing to stdout.
    """
    lines = [f"{_BLOCK_TOP} {_SQUIRREL} {title}"]
    for raw in body.split("\n"):
        if raw == "":
            lines.append(_BLOCK_LEFT)
        else:
            lines.append(f"{_BLOCK_LEFT}  {raw}")
    lines.append(_BLOCK_BOT)
    return "\n".join(lines)


def format_table(rows: Sequence[Sequence[str]], columns: Sequence[str]) -> str:
    """Render a column-aligned table for use INSIDE a `format_block` body.

    Returns the table as a string (no border characters of its own — the
    caller wraps the result via `format_block`). Column widths are computed
    from the maximum cell length per column, with a 2-space gutter between
    columns. Rows shorter than `columns` are padded with empty strings;
    rows longer are truncated.

    Args:
        rows: Sequence of row sequences. Cells are coerced to ``str``.
        columns: Header labels. Length determines column count.

    Returns:
        Multi-line string. Header is the first line, separator is a row of
        ``─`` matching each column width, then one line per row.
        Empty `rows` yields just the header + separator (so callers can
        still wrap it in a "no entries yet" block consistently).
    """
    if not columns:
        return ""

    n = len(columns)
    str_rows = []
    for row in rows:
        cells = [str(c) for c in row[:n]]
        cells.extend([""] * (n - len(cells)))
        str_rows.append(cells)

    widths = [len(str(c)) for c in columns]
    for row in str_rows:
        for i, cell in enumerate(row):
            if len(cell) > widths[i]:
                widths[i] = len(cell)

    def _fmt(cells):
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    out_lines = [_fmt([str(c) for c in columns])]
    out_lines.append(_fmt(["─" * w for w in widths]))
    for row in str_rows:
        out_lines.append(_fmt(row))
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# ULID generation
# ---------------------------------------------------------------------------

#: Prefixes documented in `templates/demo/prefix_table.md`. Only `wld_` is
#: actually generated in v3.2; the rest are reserved for v4 and listed here so
#: a future generator can call `_join_prefix("wal", new_id())` without a magic
#: string. Tests assert the table mentions every entry in this set.
SUPPORTED_PREFIXES = (
    "wld",
    "wal",
    "prs",
    "bnd",
    "ses",
    "tsk",
    "ent",
    "ist",
    "atm",
)


def _join_prefix(prefix: str, ulid_str: str) -> str:
    """Join a prefix to a lowercase ULID body. Pure string op."""
    if prefix not in SUPPORTED_PREFIXES:
        raise ValueError(
            f"prefix {prefix!r} is not in SUPPORTED_PREFIXES; see "
            f"templates/demo/prefix_table.md"
        )
    return f"{prefix}_{ulid_str}"


def new_world_ulid() -> str:
    """Mint a fresh `wld_<lowercase-ulid>` identifier.

    Uses the vendored `python-ulid` 3.1.0 (`from ulid import ULID`). The
    Crockford-base32 body is lowercased per ALIVE convention (`wld_` ids
    are always lowercase in `_kernel/key.md`).

    Returns:
        Identifier string of the shape ``wld_<26 lowercase Crockford b32>``.
        Total length is `4 + 26 = 30` characters.
    """
    ULID = _import_ulid()
    body = str(ULID()).lower()
    return _join_prefix("wld", body)


# ---------------------------------------------------------------------------
# Label derivation
# ---------------------------------------------------------------------------

#: English stop-words stripped from descriptions before label derivation.
#: Kept intentionally small — over-aggressive filtering would yield
#: empty labels on short personas. Matches the spec's "first ~5
#: meaningful words" guidance: punctuation + articles + common
#: prepositions / conjunctions, nothing more.
_STOP_WORDS = frozenset({
    "a", "an", "the",
    "and", "or", "but", "nor", "so", "yet",
    "of", "in", "on", "at", "by", "for", "to", "from", "with", "as",
    "is", "are", "was", "were", "be", "being", "been",
    "this", "that", "these", "those",
})

#: Maximum number of meaningful words retained in a derived label.
_MAX_LABEL_WORDS = 5

#: Slug regex from `_spike.md` § "Slug sanitization (path-safety guard)".
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _normalize_word(raw: str) -> str:
    """Lowercase + strip non-alnum from a single token. Empty if nothing left."""
    decomposed = unicodedata.normalize("NFKD", raw)
    ascii_only = "".join(ch for ch in decomposed if ord(ch) < 128)
    lowered = ascii_only.lower()
    cleaned = "".join(ch if ("a" <= ch <= "z" or "0" <= ch <= "9") else " " for ch in lowered)
    parts = cleaned.split()
    return parts[0] if parts else ""


def derive_label(description: str, *, max_words: int = _MAX_LABEL_WORDS) -> str:
    """Derive a filesystem-safe label slug from a free-text description.

    Pipeline:
      1. Lowercase + NFKD-normalize → ASCII (drops accents and unicode
         punctuation deterministically).
      2. Replace any non-`[a-z0-9]` run with a single space.
      3. Tokenize on whitespace.
      4. Drop entries in `_STOP_WORDS`.
      5. Keep the first `max_words` meaningful tokens.
      6. Join with `-`.

    The output always satisfies `^[a-z0-9]+(-[a-z0-9]+)*$` so it is safe to
    use as a directory name / URL slug. Returns an empty string if the
    description contains zero meaningful tokens (caller should fall back
    to e.g. the bare `wld_<ulid>` form).

    Deterministic per input — calling `derive_label("Alex Boring, angel...")`
    always returns the same slug. Used by Stage 0 to label `wld_*`-keyed
    directories before they are renamed to their final ULID-bearing form.

    Args:
        description: Free-text persona description.
        max_words: Max meaningful words to keep. Defaults to 5; pass a
            smaller value for very short labels.

    Returns:
        Slug string matching `^[a-z0-9]+(-[a-z0-9]+)*$`, or `""` if the
        description had no meaningful tokens.
    """
    if not isinstance(description, str):
        raise TypeError(f"description must be str; got {type(description).__name__}")

    decomposed = unicodedata.normalize("NFKD", description)
    ascii_only = "".join(ch for ch in decomposed if ord(ch) < 128)
    lowered = ascii_only.lower()
    cleaned = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    if not cleaned:
        return ""

    meaningful = [tok for tok in cleaned.split() if tok and tok not in _STOP_WORDS]
    kept = meaningful[: max(1, int(max_words))]
    if not kept:
        return ""

    slug = "-".join(kept)
    # The pipeline guarantees the regex by construction, but a defensive
    # assertion catches any future change that forgets the contract.
    assert _SLUG_RE.match(slug), f"derive_label produced invalid slug {slug!r}"
    return slug


def is_valid_slug(slug: str) -> bool:
    """True iff `slug` matches `^[a-z0-9]+(-[a-z0-9]+)*$`."""
    return isinstance(slug, str) and bool(_SLUG_RE.match(slug))


# ---------------------------------------------------------------------------
# Size enum mapping (orchestrator -> Stage 0)
# ---------------------------------------------------------------------------

#: Map from the user-facing size hint (the ``--size`` enum on
#: ``alive demo create prepare``) to the Stage 0 spine-prompt's size
#: selector (``S | M | L``). The orchestrator ``create.md`` calls
#: :func:`spine_size_for` before invoking ``stage0.run_stage0`` so the
#: prompt body sees the documented ``S``/``M``/``L`` letter rather
#: than the user-facing word.
_SIZE_HINT_TO_SPINE = {
    "small": "S",
    "medium": "M",
    "large": "L",
}


def spine_size_for(size_hint: Optional[str]) -> str:
    """Translate a user-facing size hint to Stage 0's spine-prompt selector.

    Args:
        size_hint: One of ``"small"``, ``"medium"``, ``"large"``, or
            ``None`` / unknown. The CLI's ``alive demo create prepare
            --size`` flag accepts only the three documented words; the
            handler stores the chosen value verbatim on the
            partial-generations row so the orchestrator's resume path
            re-renders Stage 0 with the same size.

    Returns:
        ``"S"``, ``"M"``, or ``"L"``. ``None`` (no size picked at the
        prepare prompt) defaults to ``"M"`` -- a moderate-sized
        spine matches the spec's median persona.

    The mapping is the boundary between the CLI / prose vocabulary
    (which talks to humans) and the Stage 0 prompt's internal
    selector. Both representations are stable; this helper is the
    single conversion point.
    """
    if size_hint is None:
        return "M"
    if not isinstance(size_hint, str):
        raise TypeError(
            f"size_hint must be str or None; got {type(size_hint).__name__}"
        )
    mapped = _SIZE_HINT_TO_SPINE.get(size_hint.strip().lower())
    if mapped is None:
        raise ValueError(
            f"size_hint {size_hint!r} not in {tuple(_SIZE_HINT_TO_SPINE.keys())}"
        )
    return mapped


# ---------------------------------------------------------------------------
# Partial-dir minting (Stage 0 entry point, fn-2-2zz.16)
# ---------------------------------------------------------------------------

def mint_partial_dir(
    *,
    base_dir: Optional[Any] = None,
    ulid: Optional[str] = None,
) -> "tuple[str, str]":
    """Create a fresh ``<base>/wld_<ulid>.partial/`` directory atomically.

    Used by the ``alive demo create prepare`` CLI handler to lay down the
    durable filesystem handle that Stages 0-4 will write into and that
    Stage 5 promotes via ``os.rename``. The naming convention is fixed:
    the trailing ``.partial`` suffix is what distinguishes an in-flight
    generation from a promoted world (``<base>/wld_<ulid>/``), and Stage 5
    step 2 relies on that suffix never overlapping with promoted-world
    paths.

    Atomicity contract:
      * The directory is created with ``os.makedirs(..., exist_ok=False)``
        so a same-ULID re-call raises :class:`FileExistsError` instead of
        silently re-using a partially-built tree. Idempotence on retry is
        provided by Stage 5's ``.alive-demo-activation.json`` sidecar
        marker (``scaffold.py``), not by this helper.
      * The ``_input/`` and ``_stage_outputs/`` subdirectories are pre-
        created so the persona description and stage outputs land on
        directories that exist (callers don't need to ``makedirs`` again
        before writing).

    Base-dir resolution mirrors :func:`_resolve_demo_base_dir`: explicit
    ``base_dir`` wins, then ``$ALIVE_DEMO_BASE_DIR``, then
    ``~/.alive-demos/``. The chosen base directory is created if it does
    not yet exist (via ``os.makedirs(..., exist_ok=True)``).

    Args:
        base_dir: Optional explicit base directory. ``None`` (default)
            falls back to ``$ALIVE_DEMO_BASE_DIR`` then
            ``~/.alive-demos/``. Test seam.
        ulid: Optional pre-minted ULID. ``None`` (default) calls
            :func:`new_world_ulid`. Tests pass a fixed value so paths
            are deterministic; production callers leave None.

    Returns:
        ``(partial_dir, ulid)`` where ``partial_dir`` is the absolute
        path of the freshly-created ``wld_<ulid>.partial/`` directory
        and ``ulid`` is the ``wld_<26-char>`` identifier string.

    Raises:
        FileExistsError: ``ulid`` was passed and a partial directory at
            that path already exists.
        OSError: the base directory or the partial directory could not
            be created (permission denied, read-only filesystem, etc.).
    """
    base = _resolve_demo_base_dir(base_dir)
    _os.makedirs(base, exist_ok=True)

    if ulid is None:
        ulid = new_world_ulid()
    elif not isinstance(ulid, str) or not ulid:
        raise TypeError("ulid must be a non-empty str when provided")

    partial = _os.path.join(base, f"{ulid}.partial")
    # exist_ok=False is the load-bearing choice: a same-ULID re-call must
    # raise rather than silently merge into an existing tree.
    _os.makedirs(partial, exist_ok=False)
    # Pre-create the documented subdirectory layout. exist_ok=True here
    # is fine because the parent was just created above.
    _os.makedirs(_os.path.join(partial, "_input"), exist_ok=True)
    _os.makedirs(_os.path.join(partial, "_stage_outputs"), exist_ok=True)
    return partial, ulid


# ---------------------------------------------------------------------------
# Activation pre-check (Stage 5 step 1)
# ---------------------------------------------------------------------------

#: Transcript size threshold (bytes). Sessions with `saves: 0` and a
#: transcript file SMALLER than this are treated as "opened-and-closed"
#: rather than real uncommitted work. The 4 KB cutoff is a calibration
#: from the spec (see fn-2-2zz.9 `Approach` section): empty-ish session
#: transcripts on the order of a few hundred bytes show up routinely;
#: anything past 4 KB has measurable conversation.
PRE_CHECK_TRANSCRIPT_BYTES_THRESHOLD = 4 * 1024


def _parse_yaml_squirrel_entry(text: str) -> dict:
    """Hand-rolled, narrow YAML parser for `<world>/.alive/_squirrels/*.yaml`.

    The squirrel-entry shape (`templates/squirrel/entry.yaml`) is a flat
    mapping of `key: value` pairs; values are scalars (strings, ints,
    null) plus the inline list shapes (`tags: []`, `stash: []`,
    `actions: []`, `working: []`). We only need a handful of keys for
    the pre-check predicates (`saves`, `transcript`, `recovery_state`,
    `walnut`), so a full YAML parser would be overkill -- the plugin is
    stdlib-only and has no `pyyaml` dependency.

    Returns a dict of parsed keys -> values. Unknown / unparseable
    lines are silently skipped (the predicate returns "no finding"
    when a key cannot be located, which is the correct conservative
    behaviour). Strings are unquoted; the bare literal `null` (or
    empty value) maps to `None`; bare integers are int-cast.
    """
    out: dict = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        # Strip leading whitespace; squirrel entries are flat (no
        # nesting we need to track here).
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        # Skip block-list openers like `actions:` followed by `  - ...`.
        # We only need scalar leaf values; if the value side is empty
        # (key: ), record None.
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # Bare empty / `null` / `~` -> None.
        if value == "" or value == "null" or value == "~":
            out[key] = None
            continue
        # Inline list / mapping markers. `[]` and `{}` are common; we
        # don't recurse into multi-line block lists.
        if value in ("[]", "{}"):
            out[key] = []
            continue
        # Strip surrounding quotes (single or double).
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ('"', "'")
        ):
            value = value[1:-1]
        # Try int.
        try:
            out[key] = int(value)
            continue
        except ValueError:
            pass
        # Otherwise it's a string scalar.
        out[key] = value
    return out


def _resolve_transcript_path(transcript_value, world_root, yaml_path):
    """Resolve a `transcript:` field to an absolute filesystem path.

    The squirrel-entry schema permits `transcript:` to be:
      * `null` / empty -> no transcript on disk; return None.
      * an absolute path -> returned as-is.
      * a path relative to the world root -> joined under `world_root`.
      * a path relative to the squirrel-entry yaml file -> joined.

    We try the absolute / world-root / yaml-dir resolution in that
    order and return the first one that exists. If none exist (or the
    value is null / empty), return None and let the predicate skip.
    """
    import os as _os  # noqa: PLC0415 -- locality for hot path

    if transcript_value is None:
        return None
    s = str(transcript_value).strip()
    if not s or s.lower() == "null":
        return None
    if _os.path.isabs(s):
        return s if _os.path.exists(s) else None
    candidates = []
    if world_root is not None:
        candidates.append(_os.path.join(str(world_root), s))
    yaml_dir = _os.path.dirname(_os.path.abspath(str(yaml_path)))
    candidates.append(_os.path.join(yaml_dir, s))
    for cand in candidates:
        if _os.path.exists(cand):
            return _os.path.abspath(cand)
    return None


def activation_pre_check(world_root) -> list:
    """Detect uncommitted work in a live world before activating a demo.

    Returns a list of finding dicts; empty list means the world is
    clean to overwrite (or there is no current world).

    Three predicates are evaluated. A finding is recorded when ANY of
    these matches (the predicates are independent; a single session
    can trigger more than one):

      1. ``<world>/.alive/_squirrels/*.yaml`` has ``saves: 0`` AND its
         referenced ``transcript:`` file exists with size > 4 KB.
         Per ``rules/squirrels.md:538``, ``saves: 0`` is the source of
         truth for unsaved sessions; the transcript-size cutoff
         filters opened-and-closed sessions from sessions with real
         conversation.
      2. Any ``<world>/<walnut>/_kernel/log.md`` mtime newer than
         ``<walnut>/_kernel/now.json`` mtime -- signals a save that
         hasn't been projected.
      3. ``<world>/.alive/_squirrels/*.yaml`` has ``saves: 0`` AND
         non-null ``recovery_state``. Paired with ``saves: 0`` because
         saved sessions can carry historical ``recovery_state`` per
         ``rules/squirrels.md:572`` -- a bare non-null check would
         over-classify.

    Each finding dict carries::

        {
            "predicate": <int>,              # 1, 2, or 3
            "evidence":  "<short string>",   # for the bordered-block
            "walnut":    "<slug or None>",   # if applicable
        }

    A ``world_root`` of ``None`` (no current live world) returns the
    empty list -- there is nothing to lose by activating, so the
    activation transaction proceeds without confirmation.
    """
    import os as _os  # noqa: PLC0415

    findings: list = []

    if world_root is None:
        return findings
    root = str(world_root)
    if not _os.path.isdir(root):
        return findings

    # --- Predicates 1 + 3: scan _squirrels/*.yaml ---
    squirrels_dir = _os.path.join(root, ".alive", "_squirrels")
    if _os.path.isdir(squirrels_dir):
        try:
            entries = sorted(_os.listdir(squirrels_dir))
        except OSError:
            entries = []
        for name in entries:
            if not name.endswith(".yaml"):
                continue
            yaml_path = _os.path.join(squirrels_dir, name)
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            data = _parse_yaml_squirrel_entry(text)
            saves_val = data.get("saves")
            try:
                saves = int(saves_val) if saves_val is not None else None
            except (TypeError, ValueError):
                saves = None
            if saves != 0:
                # Only `saves == 0` triggers predicates 1 / 3.
                continue

            walnut_val = data.get("walnut")
            walnut_evidence = (
                walnut_val if isinstance(walnut_val, str) else None
            )

            # Predicate 1: transcript > 4 KB.
            transcript_path = _resolve_transcript_path(
                data.get("transcript"), root, yaml_path,
            )
            if transcript_path is not None:
                try:
                    size = _os.path.getsize(transcript_path)
                except OSError:
                    size = -1
                if size > PRE_CHECK_TRANSCRIPT_BYTES_THRESHOLD:
                    findings.append({
                        "predicate": 1,
                        "evidence": (
                            f"{yaml_path} (saves=0); transcript "
                            f"{transcript_path} is {size} bytes"
                        ),
                        "walnut": walnut_evidence,
                    })

            # Predicate 3: recovery_state non-null AND saves==0.
            recovery = data.get("recovery_state")
            # `_parse_yaml_squirrel_entry` collapses the `null` / empty
            # / `~` literal to None; anything else is "non-null".
            if recovery is not None:
                # A `[]` value ends up as `[]` (empty list) here -- not
                # a meaningful "I stopped mid-something" breadcrumb.
                # Predicate 3 cares about explicit non-null content.
                is_truthy = recovery != [] and recovery != {} and recovery != ""
                if is_truthy:
                    findings.append({
                        "predicate": 3,
                        "evidence": (
                            f"{yaml_path} (saves=0); recovery_state="
                            f"{recovery!r}"
                        ),
                        "walnut": walnut_evidence,
                    })

    # --- Predicate 2: log.md mtime > now.json mtime per walnut ---
    # The world layout puts walnuts under canonical domain dirs (per
    # `_world_root_io.WALNUT_SCAN_DOMAIN_DIRS`); we walk those rather
    # than the whole world to avoid descending into vendor / build
    # trees. A walnut is recognized by `_kernel/key.md` per
    # `_common.find_all_walnuts`.
    domain_dirs = ("01_Archive", "02_Life", "04_Ventures", "05_Experiments")
    skip_dirs = frozenset({
        ".git", ".next", ".venv",
        "__pycache__", "build", "dist", "node_modules", "raw",
        "target", "venv",
    })
    for domain in domain_dirs:
        domain_path = _os.path.join(root, domain)
        if not _os.path.isdir(domain_path):
            continue
        for walk_root, dirs, _files in _os.walk(domain_path):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".") and d not in skip_dirs
            ]
            kernel_dir = _os.path.join(walk_root, "_kernel")
            log_path = _os.path.join(kernel_dir, "log.md")
            now_path = _os.path.join(kernel_dir, "now.json")
            key_path = _os.path.join(kernel_dir, "key.md")
            if not _os.path.isfile(key_path):
                continue
            # This is a walnut. Stop descending into nested walnuts.
            dirs[:] = []
            try:
                log_mtime = _os.path.getmtime(log_path)
            except OSError:
                continue
            try:
                now_mtime = _os.path.getmtime(now_path)
            except OSError:
                # No now.json projection at all is itself a sign of an
                # un-projected save.
                findings.append({
                    "predicate": 2,
                    "evidence": (
                        f"{log_path} exists but no {now_path}"
                    ),
                    "walnut": _os.path.relpath(walk_root, root),
                })
                continue
            if log_mtime > now_mtime:
                findings.append({
                    "predicate": 2,
                    "evidence": (
                        f"{log_path} mtime={log_mtime:.0f} > "
                        f"{now_path} mtime={now_mtime:.0f}"
                    ),
                    "walnut": _os.path.relpath(walk_root, root),
                })

    return findings


# ---------------------------------------------------------------------------
# Subcommand helpers (fn-2-2zz.12): list / activate / deactivate / delete / status
# ---------------------------------------------------------------------------

#: Default base directory for promoted demo worlds. Honours
#: ``$ALIVE_DEMO_BASE_DIR`` for partials + worlds (per epic locked
#: decisions). Mirrors the constant in ``scaffold.py`` so callers that
#: only need to enumerate worlds (list / resolve_ref) do not have to
#: import scaffold.
_DEFAULT_BASE_RELHOME = ".alive-demos"

#: Minimum prefix length accepted by ``resolve_ref`` ULID-prefix matching.
#: Below 3 chars the search becomes uselessly noisy on a populated demo
#: directory; we reject shorter prefixes with a friendly LookupError so
#: the squirrel surfaces a helpful message instead of a meaningless
#: ambiguous list.
ULID_PREFIX_MIN_CHARS = 3


def _resolve_demo_base_dir(base_dir: Optional[Any] = None) -> str:
    """Resolve the demo base directory.

    Same precedence as ``scaffold._demo_base_dir`` -- explicit ``base_dir``
    wins, then ``$ALIVE_DEMO_BASE_DIR``, then ``~/.alive-demos/``. Mirrors
    that helper rather than importing scaffold so ``lib.py`` can stay a
    leaf module.
    """
    if base_dir is not None:
        return _os.path.normpath(_os.path.abspath(_os.fspath(base_dir)))
    override = _os.environ.get("ALIVE_DEMO_BASE_DIR")
    if override:
        return _os.path.normpath(_os.path.abspath(override))
    return _os.path.normpath(
        _os.path.abspath(_os.path.expanduser("~/" + _DEFAULT_BASE_RELHOME))
    )


@dataclass
class WorldRecord:
    """One promoted demo world's identity + lifecycle metadata.

    Populated by ``list_demos``; consumed by ``resolve_ref``,
    ``format_list_table``, ``format_status``, and the CLI handlers in
    ``cli_register.py``.
    """

    ulid: str
    label: str
    path: str
    created_at: str
    last_activated_at: str
    disk_size_bytes: int
    status: str
    persona_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable dict (used by ``alive demo list`` envelope)."""
        return {
            "ulid": self.ulid,
            "label": self.label,
            "path": self.path,
            "created_at": self.created_at,
            "last_activated_at": self.last_activated_at,
            "disk_size_bytes": self.disk_size_bytes,
            "status": self.status,
            "persona_name": self.persona_name,
        }


class AmbiguousMatch(LookupError):
    """Raised by ``resolve_ref`` when more than one world matches.

    Carries ``candidates`` so the squirrel can render a picker block
    and dispatch ``AskUserQuestion``. Subclasses ``LookupError`` so
    callers can broaden the catch (``except LookupError``) when they
    want both "no match" and "ambiguous" routed to the same error
    surface.
    """

    def __init__(self, ref: str, candidates: List["WorldRecord"]) -> None:
        self.ref = ref
        self.candidates = candidates
        super().__init__(
            f"ref {ref!r} matched {len(candidates)} demo worlds: "
            f"{[c.ulid for c in candidates]}"
        )


_WLD_DIRNAME_RE = re.compile(r"^wld_[0-9a-z]{26}$")


def _safe_walk_disk_size(root_path: str) -> int:
    """Sum ``os.path.getsize`` over every regular file under ``root_path``.

    Symlinks, sockets, and devices are ignored. Permissions / IO errors
    on individual files are swallowed; the total returned is the
    best-effort sum. Returns ``-1`` if the entire walk failed (e.g. the
    root path is unreadable).
    """
    import stat as _stat  # noqa: PLC0415
    total = 0
    walked_any = False
    try:
        for dirpath, _dirnames, filenames in _os.walk(root_path, followlinks=False):
            walked_any = True
            for name in filenames:
                fp = _os.path.join(dirpath, name)
                try:
                    st = _os.lstat(fp)
                except OSError:
                    continue
                if _stat.S_ISREG(st.st_mode):
                    total += st.st_size
    except OSError:
        return -1
    if not walked_any:
        return -1
    return total


def _parse_build_log_frontmatter(text: str) -> Optional[Dict[str, str]]:
    """Re-implementation of the narrow build-log frontmatter parser.

    Mirrors ``state._parse_demo_build_log_frontmatter`` so ``lib.py``
    stays leaf-module-clean. Recognised keys include ``ulid``, ``label``,
    ``activated_at``, and (newer builds) ``persona_name``. Returns None
    on malformed input rather than raising -- list_demos must tolerate
    half-finished worlds without crashing the whole listing.
    """
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None
    body = text.replace("\r\n", "\n")
    if not body.startswith("---\n"):
        return None
    rest = body[4:]
    closing = rest.find("\n---\n")
    if closing < 0:
        if rest.rstrip("\n").endswith("---"):
            closing = rest.rfind("\n---")
            if closing < 0:
                return None
        else:
            return None
    fm_block = rest[:closing]
    out: Dict[str, str] = {}
    for raw in fm_block.split("\n"):
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        sep = line.find(":")
        if sep < 0:
            return None
        key = line[:sep].strip()
        value = line[sep + 1:].strip()
        if not key:
            return None
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        out[key] = value
    return out


def list_demos(base_dir: Optional[Any] = None) -> List[WorldRecord]:
    """Enumerate every promoted demo world under ``base_dir``.

    Scans ``<base>/wld_<ULID>/`` directories; for each, reads
    ``.alive/_demo-build-log.md`` frontmatter for label / activated_at
    / persona_name, walks the world tree to compute disk_size_bytes,
    and cross-references the demo-state ``active_world`` to set the
    ``last_activated_at`` field on the matching record.

    Sort order: active world (if any) first, then ``created_at``
    descending (most-recent first).

    Skipped silently for any of:
      * Non-directory entries.
      * Directory names that do not match ``wld_[0-9a-z]{26}``.
      * Worlds with a missing or unreadable ``_demo-build-log.md``.
      * Worlds whose build-log ``ulid`` does not match the directory
        basename (corrupted identity).

    Args:
        base_dir: Optional explicit base. Falls back to
            ``$ALIVE_DEMO_BASE_DIR`` then ``~/.alive-demos/``.

    Returns:
        List of ``WorldRecord``s, sorted as described above. Empty list
        when the base directory does not exist.
    """
    base = _resolve_demo_base_dir(base_dir)
    if not _os.path.isdir(base):
        return []

    # Read the active world from demo-state to flag the matching record.
    # Lazy import to avoid module-load cycles.
    active_path: Optional[str] = None
    active_activated_at: Optional[str] = None
    try:
        import importlib.util as _ilu  # noqa: PLC0415
        import sys as _sys  # noqa: PLC0415

        full_name = "alive_demo_lib._state_for_list"
        if full_name in _sys.modules:
            state_mod = _sys.modules[full_name]
        else:
            state_path_local = _os.path.join(_os.path.dirname(__file__), "state.py")
            spec = _ilu.spec_from_file_location(full_name, state_path_local)
            if spec is not None and spec.loader is not None:
                state_mod = _ilu.module_from_spec(spec)
                _sys.modules[full_name] = state_mod
                spec.loader.exec_module(state_mod)
            else:
                state_mod = None
        if state_mod is not None:
            try:
                state = state_mod.load_state()
            except Exception:
                state = None
            if isinstance(state, dict):
                active = state.get("active_world")
                if isinstance(active, dict):
                    p = active.get("path")
                    a = active.get("activated_at")
                    active_path = str(p) if isinstance(p, str) else None
                    active_activated_at = str(a) if isinstance(a, str) else None
    except Exception:
        pass

    try:
        entries = sorted(_os.listdir(base))
    except OSError:
        return []

    records: List[WorldRecord] = []
    for name in entries:
        if not _WLD_DIRNAME_RE.match(name):
            continue
        wld_path = _os.path.join(base, name)
        if not _os.path.isdir(wld_path):
            continue
        build_log_path = _os.path.join(wld_path, ".alive", "_demo-build-log.md")
        if not _os.path.isfile(build_log_path):
            continue
        try:
            with open(build_log_path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        fm = _parse_build_log_frontmatter(text)
        if fm is None:
            continue

        ulid = fm.get("ulid", "").strip()
        label = fm.get("label", "").strip()
        created_at = fm.get("activated_at", "").strip()
        persona_name = fm.get("persona_name", "").strip() or None

        # Sanity-check ulid against the directory name.
        if ulid != name:
            continue

        is_active = (
            active_path is not None
            and _os.path.normpath(_os.path.abspath(active_path))
            == _os.path.normpath(_os.path.abspath(wld_path))
        )

        if is_active:
            status = "active"
            last_activated = active_activated_at or created_at
        elif label and created_at:
            status = "available"
            last_activated = ""
        else:
            status = "unknown"
            last_activated = ""

        size = _safe_walk_disk_size(wld_path)

        records.append(WorldRecord(
            ulid=ulid,
            label=label,
            path=_os.path.normpath(_os.path.abspath(wld_path)),
            created_at=created_at,
            last_activated_at=last_activated,
            disk_size_bytes=size,
            status=status,
            persona_name=persona_name,
        ))

    def _sort_key(r: WorldRecord):
        return (0 if r.status == "active" else 1, _negate_iso(r.created_at), r.ulid)

    records.sort(key=_sort_key)
    return records


def _negate_iso(s: str) -> str:
    """Sort key helper: produce a string that sorts the inverse of `s`.

    For ASCII chars, mapping each char to its complement against 0xFF
    gives a strictly inverse total order. Empty timestamps sort last
    (largest) under both orderings so we special-case them so worlds
    with no created_at land at the bottom of the desc sort.
    """
    if not s:
        return "\xff"
    return "".join(chr(0xFF - (ord(c) & 0xFF)) for c in s)


def resolve_ref(
    ref: str,
    base_dir: Optional[Any] = None,
) -> WorldRecord:
    """Resolve a free-form reference string to a single ``WorldRecord``.

    3-step fallback:

      1. **Exact label match.** Case-sensitive comparison against each
         record's ``label`` field. If exactly one record matches, return
         it; if more than one, raise ``AmbiguousMatch``.
      2. **ULID prefix match.** Case-insensitive prefix match against
         each record's ``ulid``. The bare ``<ULID>`` form (no ``wld_``
         prefix) is also tested against ``ulid[len("wld_"):]``. Prefix
         must be at least ``ULID_PREFIX_MIN_CHARS`` (3) characters;
         shorter refs that fail step 1 raise ``LookupError``.
      3. **No match.** Raise ``LookupError`` with the ref echoed.

    The interactive picker (when ``AmbiguousMatch`` fires) is a
    SQUIRREL-LEVEL concern -- workers cannot fire ``AskUserQuestion``.
    The caller (skill prose) catches ``AmbiguousMatch.candidates``,
    renders a picker block, and dispatches ``AskUserQuestion`` itself.
    """
    if not isinstance(ref, str):
        raise TypeError(f"ref must be str; got {type(ref).__name__}")
    needle = ref.strip()
    if not needle:
        raise LookupError("empty ref; provide a label or ULID prefix")

    records = list_demos(base_dir=base_dir)

    # Step 1: exact label match.
    label_hits = [r for r in records if r.label and r.label == needle]
    if len(label_hits) == 1:
        return label_hits[0]
    if len(label_hits) > 1:
        raise AmbiguousMatch(needle, label_hits)

    # Step 2: ULID prefix match. The minimum-length rule applies to
    # the BODY (post `wld_` strip), not the raw needle, so a bare
    # `wld_` is rejected even though len("wld_") == 4. Without this,
    # `wld_` would pass the gate and match every world.
    lower = needle.lower()

    if lower.startswith("wld_"):
        body = lower[len("wld_"):]
        prefix_match_full = lower
    else:
        body = lower
        prefix_match_full = "wld_" + lower

    if len(body) < ULID_PREFIX_MIN_CHARS:
        raise LookupError(
            f"no demo matches: {ref!r} "
            f"(ULID prefix body must be at least {ULID_PREFIX_MIN_CHARS} "
            f"characters to disambiguate)"
        )

    ulid_hits: List[WorldRecord] = []
    for r in records:
        rid = r.ulid.lower()
        if rid.startswith(prefix_match_full):
            ulid_hits.append(r)
            continue
        rest = rid[len("wld_"):] if rid.startswith("wld_") else rid
        if rest.startswith(body):
            ulid_hits.append(r)

    seen: set = set()
    deduped: List[WorldRecord] = []
    for r in ulid_hits:
        if r.ulid in seen:
            continue
        seen.add(r.ulid)
        deduped.append(r)
    ulid_hits = deduped

    if len(ulid_hits) == 1:
        return ulid_hits[0]
    if len(ulid_hits) > 1:
        raise AmbiguousMatch(needle, ulid_hits)

    raise LookupError(f"no demo matches: {ref!r}")


def bytes_human(n: int) -> str:
    """Render a byte count as a short human-readable string.

    Uses base-1024 with one-decimal precision past KiB. Negative
    values render as ``"?"`` so callers can distinguish "could not
    measure" from a zero-byte world.
    """
    if n < 0:
        return "?"
    if n < 1024:
        return f"{n} B"
    units = ["KiB", "MiB", "GiB", "TiB"]
    val = float(n) / 1024.0
    unit_idx = 0
    while val >= 1024.0 and unit_idx < len(units) - 1:
        val /= 1024.0
        unit_idx += 1
    return f"{val:.1f} {units[unit_idx]}"


def _short_iso(s: str) -> str:
    """Truncate an ISO-8601 timestamp to ``YYYY-MM-DD`` for table display."""
    if not s:
        return "-"
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return "-"


def format_list_table(
    records: Sequence[WorldRecord],
    active_ulid: Optional[str] = None,
) -> str:
    """Render a 6-column fixed-width table of demo worlds.

    Columns: ``LABEL | ULID | SIZE | CREATED | LAST_ACTIVATED | STATUS``.
    The active world's STATUS cell is prefixed with ``*`` so a quick
    scan surfaces it without colour.
    """
    rows: List[List[str]] = []
    for r in records:
        size = bytes_human(r.disk_size_bytes)
        created = _short_iso(r.created_at)
        last_act = _short_iso(r.last_activated_at)
        is_active = (active_ulid is not None and r.ulid == active_ulid) or r.status == "active"
        status_cell = f"*{r.status}" if is_active else r.status
        rows.append([
            r.label or "(unknown)",
            r.ulid,
            size,
            created,
            last_act,
            status_cell,
        ])
    return format_table(
        rows=rows,
        columns=["LABEL", "ULID", "SIZE", "CREATED", "LAST_ACTIVATED", "STATUS"],
    )


def format_status(
    active: Optional[WorldRecord],
    previous_world_root: Optional[str],
) -> str:
    """Render the 5-7 line ``alive demo status`` body.

    Caller wraps in ``format_block(title="demo status", body=...)``.
    Two cases: active demo (5-6 lines) vs no active demo (3 lines).
    The output stays inside the 5-7 line budget mandated by the spec.
    """
    lines: List[str] = []
    if active is not None:
        lines.append(f"ulid:                {active.ulid}")
        lines.append(f"label:               {active.label or '(unknown)'}")
        lines.append(f"path:                {active.path}")
        lines.append(f"activated_at:        {active.last_activated_at or active.created_at or '(unknown)'}")
        if previous_world_root:
            lines.append(f"previous_world_root: {previous_world_root}")
        lines.append("hint:                /alive:demo deactivate to restore the previous world")
    else:
        lines.append("active_world:        (none)")
        if previous_world_root:
            lines.append(f"previous_world_root: {previous_world_root}")
        else:
            lines.append("previous_world_root: (none)")
        lines.append("hint:                /alive:demo to create a new demo world")
    return "\n".join(lines)


def format_picker_body(ref: str, candidates: Sequence[WorldRecord]) -> str:
    """Render the body of the ``AmbiguousMatch`` picker block.

    Squirrel-level UI: the parent skill catches ``AmbiguousMatch``,
    renders ``format_block("multiple matches for '<ref>'", body=...)``
    around this body, and dispatches ``AskUserQuestion`` with one
    option per candidate (plus Cancel).
    """
    lines = [
        f"Reference {ref!r} matches {len(candidates)} demo worlds:",
        "",
    ]
    for i, r in enumerate(candidates, 1):
        size = bytes_human(r.disk_size_bytes)
        created = _short_iso(r.created_at)
        lines.append(
            f"  {i}. {r.label or '(unknown)'}  -  {r.ulid}  ({size}, created {created})"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Failure-mode handlers (fn-2-2zz.13, spec failure modes 15a / 15b / 15c)
# ---------------------------------------------------------------------------

#: Hardcoded issue tracker the failure blocks point users at. Single source of
#: truth so the three failure handlers stay in sync. Per spec (fn-2-2zz.13),
#: this is intentionally not configurable in v3.2: the demo skill's failure
#: surfaces all funnel into one inbox so operator triage stays simple.
DEMO_ISSUE_TRACKER_URL = "https://github.com/alivecontext/alive/issues/new"

#: Cap on the number of validation errors echoed in a double-failure block.
#: The full error list is preserved on disk via the partial dir's raw output;
#: the rendered block is for human triage at the squirrel surface.
_DOUBLE_FAILURE_ERROR_LIMIT = 5


def _trim_evidence(evidence: str, limit: int = 140) -> str:
    """Truncate one error's evidence string for inline rendering.

    The on-disk raw output (referenced by path in the rendered block) carries
    the full evidence; the inline block only gets enough of each error to
    let the human spot a pattern.
    """
    if not isinstance(evidence, str):
        evidence = str(evidence)
    if len(evidence) <= limit:
        return evidence
    return evidence[: limit - 3] + "..."


def _load_state_module():
    """Lazy-load ``state.py`` under the same namespaced key cli_register uses.

    Lib.py is imported by the skill router and by tests independently of the
    CLI registration path, so we cannot rely on ``alive_demo.state`` already
    being in ``sys.modules``. Mirror the loader pattern from
    ``cli_register._load_sibling`` to land on the same module object.
    """
    import importlib.util as _ilu  # noqa: PLC0415
    import sys as _sys  # noqa: PLC0415

    full_name = "alive_demo.state"
    if full_name in _sys.modules:
        return _sys.modules[full_name]
    here = _os.path.dirname(_os.path.abspath(__file__))
    target = _os.path.join(here, "state.py")
    spec = _ilu.spec_from_file_location(full_name, target)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {target}")
    module = _ilu.module_from_spec(spec)
    _sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def _stage_id_to_state_key(stage_id: str) -> str:
    """Map a stage id ("0" / "2" / "3" / "4") to the state-schema label.

    ``state.py`` validates ``failed_at_stage`` against the canonical labels
    ("0_spine", "2_entities", "3_timeline", "4_insights", "5_promote"). The
    handlers accept either the bare digit (for callers reading from
    ValidationResult.stage which carries a digit) or the canonical label.
    """
    digit_to_label = {
        "0": "0_spine",
        "1": "1_anchor",
        "2": "2_entities",
        "3": "3_timeline",
        "4": "4_insights",
        "5": "5_promote",
    }
    if stage_id in digit_to_label:
        return digit_to_label[stage_id]
    return stage_id


def report_validation_double_failure(
    stage_id: str,
    validation_result: Any,
    partial_dir: str,
    raw_output_path: Optional[str] = None,
    *,
    state_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Render the user-facing block for a stage validator failing twice.

    Failure mode 15a from the epic spec. The stage dispatchers (Stage 0 has
    integrated retry; Stage 2/3/4 expose ``retry_dispatch`` primitives the
    skill orchestrator drives) call this on the second consecutive
    validation failure (or on first-run ``fatal``). The block is meant to
    be printed verbatim at the squirrel surface; the caller does NOT wrap
    it in another ``format_block``.

    Side effect: atomically marks the partial-generation entry's
    ``failed_at_stage`` field in ``demo-state.json`` so a later
    ``alive demo resume`` listing can offer to retry from the failed
    stage. The mutation is idempotent (calling twice with the same args
    is a no-op for a partial that is already marked failed at this stage).

    Args:
        stage_id: Bare digit ("0" / "2" / "3" / "4") or canonical state
            label ("0_spine", ...). The block title shows the digit form
            either way.
        validation_result: A ``validate.ValidationResult`` (or any object
            exposing an ``errors`` list of dicts with ``code`` / ``where``
            / ``evidence`` keys). The first ``_DOUBLE_FAILURE_ERROR_LIMIT``
            errors are inlined; the remainder is summarized with a count.
        partial_dir: Absolute path to the partial dir whose ulid identifies
            the in-flight generation in demo-state.
        raw_output_path: Optional path the caller wrote the failing
            subagent output to. Surfaced in the block so the user can
            inspect the full raw text.
        state_path: Test seam; when set, overrides the demo-state.json path
            used for the mark mutation. Production callers leave this None.

    Returns:
        ``{"rendered_block": str, "state_updated": bool, "partial_dir": str}``.
        ``state_updated`` is True iff the partial entry was found and its
        ``failed_at_stage`` was set or was already at this stage. False when
        no entry matched the partial's ulid (the caller may have created the
        partial without staging a demo-state row yet).
    """
    state_label = _stage_id_to_state_key(str(stage_id))
    digit = str(stage_id).split("_", 1)[0]

    errors = list(getattr(validation_result, "errors", []) or [])
    total_errors = len(errors)
    inlined = errors[:_DOUBLE_FAILURE_ERROR_LIMIT]

    body_lines: List[str] = [
        f"Validation failed twice on stage {digit}.",
        "",
        f"{total_errors} error(s) remain after one auto-retry.",
        "",
    ]
    if inlined:
        body_lines.append("Top errors:")
        for err in inlined:
            code = err.get("code", "?") if isinstance(err, dict) else "?"
            where = err.get("where", "?") if isinstance(err, dict) else "?"
            evidence = err.get("evidence", "") if isinstance(err, dict) else ""
            body_lines.append(
                f"  - [{code}] {where}: {_trim_evidence(evidence)}"
            )
        if total_errors > len(inlined):
            body_lines.append(f"  ... and {total_errors - len(inlined)} more.")
        body_lines.append("")

    if raw_output_path:
        body_lines.append(f"Raw subagent output preserved at:")
        body_lines.append(f"  {raw_output_path}")
        body_lines.append("")
    body_lines.append(f"Partial generation preserved at:")
    body_lines.append(f"  {partial_dir}")
    body_lines.append("")
    body_lines.append("Next steps:")
    body_lines.append("  1. Inspect the raw output and partial dir above.")
    body_lines.append("  2. Run `alive demo resume` to retry from this stage.")
    body_lines.append(f"  3. File a bug if the same input keeps failing:")
    body_lines.append(f"     {DEMO_ISSUE_TRACKER_URL}")

    title = f"\U0001f6d1 validation failed twice -- stage {digit}"
    rendered = format_block(title, "\n".join(body_lines))

    state_updated = False
    partial_ulid = _ulid_from_partial_dir(partial_dir)
    if partial_ulid is not None:
        try:
            state_mod = _load_state_module()
            state_mod.mark_partial_failed(
                partial_ulid,
                stage_id=state_label,
                reason="validation_double_failure",
                state_path=state_path,
            )
            state_updated = True
        except Exception:  # noqa: BLE001
            # State mutation is best-effort; the rendered block is the
            # authoritative user surface. Failing here would mask the
            # original validation failure.
            state_updated = False

    return {
        "rendered_block": rendered,
        "state_updated": state_updated,
        "partial_dir": partial_dir,
    }


def report_projection_failure(
    partial_dir: str,
    exception_summary: str,
    failing_walnut: Optional[str] = None,
    *,
    state_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Render the user-facing block for a Stage 5 projection step that crashed.

    Failure mode 15b from the epic spec. The Stage 5 orchestrator (steps 6
    and 7 shell out to ``project.py`` / ``generate-index.py``) wraps each
    subprocess in try/except; on nonzero exit or subprocess exception the
    orchestrator calls this to render the failure surface, marks the
    partial as failed at ``5_promote``, and aborts the transaction (the
    pointer commit at step 10 has not yet fired, so the canonical world
    is untouched).

    Args:
        partial_dir: Absolute path to the partial (or freshly-renamed
            world dir) the failing projection was operating against.
        exception_summary: One-line summary of the failure (e.g.
            ``"project.py --walnut alex-boring rc=1: ..."``). The caller
            is responsible for truncating before passing.
        failing_walnut: Optional slug of the walnut whose projection
            failed. Surfaced in the block title; ``None`` when the
            failure happened in step 7 (which operates over the whole
            world, not per-walnut).
        state_path: Test seam; see :func:`report_validation_double_failure`.

    Returns:
        ``{"rendered_block": str, "state_updated": bool, "partial_dir": str}``.
    """
    if failing_walnut:
        title = f"\U0001f6d1 stage 5 projection failed -- walnut {failing_walnut}"
    else:
        title = "\U0001f6d1 stage 5 projection failed"

    summary = exception_summary if isinstance(exception_summary, str) else str(exception_summary)
    if len(summary) > 400:
        summary = summary[:397] + "..."

    body_lines = [
        "The deterministic projection step crashed before the activation",
        "transaction reached its commit point.",
        "",
        "Failure summary:",
        f"  {summary}",
        "",
        f"Partial preserved at:",
        f"  {partial_dir}",
        "",
        "The world-root pointer was NOT updated. Your previous active",
        "world (if any) is still the canonical one.",
        "",
        "Next steps:",
        "  1. Inspect the partial dir for the spine + stage outputs.",
        "  2. Run `alive demo resume` to retry from stage 5.",
        f"  3. File a bug if the projection keeps crashing:",
        f"     {DEMO_ISSUE_TRACKER_URL}",
    ]
    rendered = format_block(title, "\n".join(body_lines))

    state_updated = False
    partial_ulid = _ulid_from_partial_dir(partial_dir)
    if partial_ulid is not None:
        try:
            state_mod = _load_state_module()
            state_mod.mark_partial_failed(
                partial_ulid,
                stage_id="5_promote",
                reason="projection_failure",
                state_path=state_path,
            )
            state_updated = True
        except Exception:  # noqa: BLE001
            state_updated = False

    return {
        "rendered_block": rendered,
        "state_updated": state_updated,
        "partial_dir": partial_dir,
    }


def report_atomic_write_failure(
    target_path: str,
    os_error: BaseException,
) -> Dict[str, Any]:
    """Render the user-facing block for an atomic write that hit an OS error.

    Failure mode 15c from the epic spec. The orchestrator wraps the
    demo-state.json mutation in step 9 (and any other atomic write whose
    failure leaves the user worried about state integrity) in try/except
    OSError; on failure it calls this. Crucially, this handler does NOT
    mutate demo-state.json (because the mutation that failed IS the
    demo-state mutation, so retrying it would deadlock or fail again).
    The user surface explicitly states that previous data is intact.

    Args:
        target_path: Absolute path of the file the atomic write was
            targeting.
        os_error: The captured ``OSError`` (or subclass: ``PermissionError``,
            ``FileNotFoundError``, ``OSError`` with ``ENOSPC``, ...). The
            errno is extracted when present.

    Returns:
        ``{"rendered_block": str, "errno": Optional[int], "target_path": str}``.
        Note: this handler intentionally does NOT report ``state_updated``;
        it never touches state.
    """
    errno = getattr(os_error, "errno", None)
    strerror = getattr(os_error, "strerror", None) or str(os_error)
    type_name = type(os_error).__name__

    if errno is not None:
        title = f"\U0001f6d1 disk write failed -- errno {errno}"
        head_line = f"Atomic write of {target_path}"
        detail_line = f"failed with {type_name} (errno {errno}): {strerror}"
    else:
        title = f"\U0001f6d1 disk write failed -- {type_name}"
        head_line = f"Atomic write of {target_path}"
        detail_line = f"failed with {type_name}: {strerror}"

    body_lines = [
        head_line,
        f"  {detail_line}",
        "",
        "demo-state.json was NOT corrupted. Your previous data is",
        "still intact: the failed write did not reach disk under the",
        "atomic-rename contract (`scripts/_common.atomic_write_*`).",
        "",
        "Common causes:",
        "  - Disk full (errno 28 / ENOSPC)",
        "  - Filesystem mounted read-only (errno 30 / EROFS)",
        "  - Permission denied (errno 13 / EACCES)",
        "",
        "Next steps:",
        "  1. Check disk usage: `df -h ~/.config/alive/`",
        "  2. Check permissions on the target path printed above.",
        "  3. Re-run the failing command once the cause is resolved.",
        f"  4. If the cause is unclear, file a bug:",
        f"     {DEMO_ISSUE_TRACKER_URL}",
    ]
    rendered = format_block(title, "\n".join(body_lines))

    return {
        "rendered_block": rendered,
        "errno": errno,
        "target_path": target_path,
    }


def _ulid_from_partial_dir(partial_dir: str) -> Optional[str]:
    """Extract the ``wld_<ulid>`` token from a partial dir path.

    Mirrors the convention used in ``stages/stage5.py`` /
    ``scaffold.py``: partial dirs are named ``<base>/wld_<ulid>.partial``
    and on rename land at ``<base>/wld_<ulid>``. We probe the basename
    for the ``wld_`` prefix and strip the optional ``.partial`` suffix.

    Returns:
        The full ``wld_<ulid>`` identifier, or ``None`` if the path's
        basename does not match the expected shape (in which case the
        caller skips the state mutation).
    """
    if not isinstance(partial_dir, str) or not partial_dir:
        return None
    base = _os.path.basename(_os.path.normpath(partial_dir))
    if base.endswith(".partial"):
        base = base[: -len(".partial")]
    if base.startswith("wld_") and len(base) > 4:
        return base
    return None

