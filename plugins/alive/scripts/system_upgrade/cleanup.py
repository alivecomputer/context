"""Phase-8 world-state cleanup (T5 of fn-18).

Consumes the ``retired_patterns.py`` catalog as the source of truth for
cleanup targets. Filters to ``cleanup_action == "cleanup"`` entries and
removes their realised paths from the world. Walkthrough rewrites
(``walkthrough_rewrite``) are NOT touched here; v2->v3 migration inputs
(``migrate_input``) are explicitly EXCLUDED so phase 9 can consume them
first; ``verify_only`` entries surface in the verify report only.

Containment + safety guarantees (R15):
    * Every target path is resolved with ``os.path.realpath`` before any
      destructive op.
    * Paths whose realpath escapes the resolved world root are skipped.
    * Symlinks (``os.path.islink``) are NEVER followed -- detected via
      ``os.lstat`` before resolve, recorded under ``skipped[]``.
    * Submodule walnuts (walnut path is a ``.git`` file OR appears in
      the world's ``.gitmodules``) are sweep-no-op outside ``_kernel/``.
    * The orchestrator-supplied ``surface_state_paths`` set is subtracted
      from the deletion plan when phase 4 ran. When ``mode ==
      "surfaces_none"``, the catalog's ``surface_overlap_risk`` field
      drives a conservative-refusal allowlist: ``plugin_owned`` and
      ``world_state`` patterns are deleted; ``potentially_surface``
      patterns are skipped.

User-content callouts (per bible's Cleanup-briefing convention):
    For every retired-pattern directory scheduled for deletion, the
    cleanup walks the directory and enumerates any *non-plugin*
    filenames (filenames not in the catalog entry's
    ``expected_filenames`` set). Both the dry-run plan and the
    post-upgrade summary surface these by name; the summary also emits
    a ``tar -xzf <tarball> -C <world> <filename>`` restore command so
    any operator can recover user-authored files from the pre-upgrade
    tarball.

R7-audit posture: this module legitimately contains write primitives
gated by the orchestrator's dry-run check (``dry_run=True`` produces a
report without disk writes). Per 's modules are
explicitly EXCLUDED from R7's audit scope, which only covers the
verification + parse modules. The dry-run guard is enforced by a
**behaviour test**: a fixture invocation with ``dry_run=True`` MUST
leave every target path on disk.

Stdlib-only (R10).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from .retired_patterns import CATALOG, RetiredPattern


__all__ = (
    "CleanupReport",
    "CleanupTarget",
    "UserContentCallout",
    "build_cleanup_plan",
    "cleanup",
    "MODE_NORMAL",
    "MODE_SURFACES_NONE",
)


#: ``mode`` values consumed by :func:`cleanup`. ``MODE_NORMAL`` honours
#: the supplied ``surface_state_paths`` exclusion. ``MODE_SURFACES_NONE``
#: means phase 4 was skipped (operator passed ``--surfaces=none``); the
#: cleanup falls back to the catalog's ``surface_overlap_risk``
#: classification to decide what is safe to delete.
MODE_NORMAL: str = "normal"
MODE_SURFACES_NONE: str = "surfaces_none"


@dataclass(frozen=True)
class UserContentCallout:
    """One ``(path, filenames[])`` enumeration for a retired-pattern dir.

    *path* is the world-relative directory path (with trailing slash
    preserved if the catalog entry's target had one).
    *filenames* is the sorted list of filenames present inside *path*
    that are NOT in the catalog entry's ``expected_filenames`` set --
    i.e., user-authored content slated for deletion.
    """

    path: str
    filenames: Tuple[str, ...]


@dataclass(frozen=True)
class CleanupTarget:
    """One concrete deletion target resolved from a catalog entry."""

    pattern_id: int
    target_path_glob: str
    pattern_type: str  # "directory" | "file"
    absolute_path: str
    surface_overlap_risk: str
    expected_filenames: Optional[FrozenSet]


@dataclass
class CleanupReport:
    """Outcome of :func:`cleanup`.

    ``deleted`` is the list of absolute paths that were removed.
    ``skipped`` is a list of ``(path, reason)`` pairs.
    ``reasons`` is a richer per-bucket aggregation:

        * ``boundary_violations`` -- paths whose realpath escaped the
          world root.
        * ``symlink_skipped``     -- paths where ``lstat`` detected a
          symlink.
        * ``submodule_skipped``   -- per-walnut paths inside a submodule
          walnut, outside ``_kernel/``.
        * ``surface_state_excluded`` -- paths excluded because phase 4
          marked them as surface state.
        * ``cleanup_skipped_due_to_surface_uncertainty`` -- paths
          skipped under ``MODE_SURFACES_NONE`` because their
          ``surface_overlap_risk`` is ``potentially_surface``.

    ``user_content_callouts`` enumerates every non-plugin filename
    inside the affected directories (dry-run + real-run alike). The
    post-upgrade summary writer (orchestrator phase 12) consumes this
    to render the restore-command block.
    """

    deleted: List[str] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    reasons: Dict[str, List[str]] = field(default_factory=dict)
    user_content_callouts: List[UserContentCallout] = field(default_factory=list)

    def _bucket(self, name: str, value: str) -> None:
        self.reasons.setdefault(name, []).append(value)


# ---------------------------------------------------------------------------
# Submodule detection
# ---------------------------------------------------------------------------

def _read_gitmodules(world_root: str) -> Set[str]:
    """Return walnut-relative paths listed in ``<world_root>/.gitmodules``.

    Pure stdlib parsing -- looks for ``path = <relpath>`` lines under
    submodule sections. Best-effort: a malformed ``.gitmodules`` yields
    an empty set (the cleanup safer-default is "treat nothing as a
    submodule" rather than block everything).
    """
    gm = os.path.join(world_root, ".gitmodules")
    if not os.path.isfile(gm):
        return set()
    out: Set[str] = set()
    try:
        with open(gm, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("path"):
                    # Format: ``path = <relpath>``
                    if "=" in line:
                        _, rhs = line.split("=", 1)
                        rel = rhs.strip()
                        if rel:
                            out.add(rel.rstrip("/"))
    except OSError:
        return set()
    return out


def _walnut_kernel_dirs(world_root_resolved: str) -> List[str]:
    """Return realpath'd walnut roots discovered under *world_root_resolved*.

    Delegates to :func:`_common.find_all_walnuts` for the actual walk
    (so the predicate -- "directory containing ``_kernel/key.md``" --
    stays canonical). Each returned path is the realpath of the walnut
    root (NOT ``<walnut>/_kernel/``); callers append ``_kernel`` when
    they need the kernel subtree.

    Failure to import / walk yields an empty list; the cleanup still
    runs (without the submodule guard), and the orchestrator's
    higher-level safety nets remain in effect.
    """
    try:
        from _common import find_all_walnuts  # noqa: PLC0415
    except ImportError:
        return []
    try:
        walnuts = find_all_walnuts(world_root_resolved)
    except (OSError, ValueError):
        return []
    out: List[str] = []
    for w in walnuts:
        try:
            out.append(os.path.realpath(w))
        except OSError:
            continue
    return out


def _find_owning_walnut(
    abs_path: str, walnuts: List[str],
) -> Optional[str]:
    """Return the walnut root that owns *abs_path*, or ``None``.

    Both inputs are expected to be realpath'd. The match is segment-
    aware -- ``_find_owning_walnut("/w/04/foo", ["/w/04/foo-bar"])``
    returns ``None`` even though the lexical prefix matches.
    """
    for w in walnuts:
        if abs_path == w:
            return w
        if abs_path.startswith(w.rstrip(os.sep) + os.sep):
            return w
    return None


def _is_submodule_walnut(walnut_abs: str, world_root: str) -> bool:
    """``True`` if *walnut_abs* is a submodule of the world repo.

    Detection is the union of two signals:
        * the walnut's ``.git`` is a FILE, not a directory (gitlink); or
        * the walnut's world-relative path appears in
          ``<world_root>/.gitmodules``.
    """
    git_path = os.path.join(walnut_abs, ".git")
    if os.path.isfile(git_path):
        return True
    submodules = _read_gitmodules(world_root)
    if not submodules:
        return False
    try:
        rel = os.path.relpath(walnut_abs, world_root).replace(os.sep, "/")
    except ValueError:
        return False
    return rel.rstrip("/") in submodules


# ---------------------------------------------------------------------------
# Containment + symlink probes
# ---------------------------------------------------------------------------

def _resolved_world_root(world_root: str) -> str:
    return os.path.realpath(world_root)


def _is_within(child: str, parent: str) -> bool:
    """``True`` if realpath(child) is *parent* or under *parent*."""
    if child == parent:
        return True
    return child.startswith(parent + os.sep)


def _safe_to_delete(
    abs_path: str,
    *,
    world_root_resolved: str,
    report: CleanupReport,
) -> bool:
    """Return True iff *abs_path* may be deleted without escaping the world.

    The function records every refusal in *report* via
    :meth:`CleanupReport._bucket` so the operator can audit which
    candidates were dropped. Refusals never propagate as exceptions --
    cleanup must remain best-effort under hostile filesystems.
    """
    # Symlink probe FIRST -- ``os.path.realpath`` would silently follow
    # the link otherwise.
    try:
        st = os.lstat(abs_path)
    except FileNotFoundError:
        # Disappeared between plan and execution; not a violation.
        return False
    except OSError as exc:
        report.skipped.append((abs_path, "lstat-error:{}".format(exc)))
        return False
    import stat as _stat
    if _stat.S_ISLNK(st.st_mode):
        report._bucket("symlink_skipped", abs_path)
        report.skipped.append((abs_path, "symlink"))
        return False

    # Resolve and confirm containment.
    try:
        resolved = os.path.realpath(abs_path)
    except OSError as exc:
        report.skipped.append((abs_path, "realpath-error:{}".format(exc)))
        return False
    if not _is_within(resolved, world_root_resolved):
        report._bucket("boundary_violations", abs_path)
        report.skipped.append((abs_path, "outside-world-root"))
        return False
    return True


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------

def build_cleanup_plan(
    world_root_resolved: str,
    *,
    catalog: Optional[List[RetiredPattern]] = None,
) -> List[CleanupTarget]:
    """Return ``CleanupTarget`` for every catalog cleanup entry that exists.

    Pure / read-only. ``cleanup_action == "cleanup"`` AND
    ``pattern_type in {"directory", "file"}`` are the only entries
    considered. The result is the deterministic input to
    :func:`cleanup`'s decision tree.

    ``target_path_glob`` may be either a literal world-relative path
    (e.g. ``.alive/scripts/``) OR a glob containing ``*`` / ``**`` for
    per-walnut cleanup (e.g. ``**/_kernel/tasks.md.bak`` or
    ``**/_kernel/*.bak``). The planner detects globs by the presence
    of a ``*`` segment and expands them via stdlib
    ``glob.glob(pattern, recursive=True)``. Each matching path
    becomes its own ``CleanupTarget`` (so `find` reports exact
    deletions rather than wildcard plans).

    Path-type sanity check: when the catalog entry declares
    ``pattern_type == "directory"`` but the matched path is a file
    (or vice-versa), the match is dropped -- this lets a glob like
    ``**/*.bak`` co-exist with directory-typed entries without
    cross-contaminating the plan.
    """
    cat = catalog if catalog is not None else CATALOG
    targets: List[CleanupTarget] = []
    for idx, pat in enumerate(cat):
        if pat.cleanup_action != "cleanup":
            continue
        if pat.pattern_type not in ("directory", "file"):
            continue
        rel = pat.target_path_glob.rstrip("/")
        # Glob detection: any ``*`` segment in the relative path
        # routes through ``glob.glob(..., recursive=True)``. Literals
        # take the cheap `os.path.exists` fast path.
        if "*" in rel:
            import glob as _glob  # noqa: PLC0415
            pattern = os.path.join(world_root_resolved, rel)
            matches = sorted(_glob.glob(pattern, recursive=True))
            for abs_path in matches:
                # Type-sanity: directory-typed entries must match
                # actual directories on disk; file-typed must match
                # files. Symlinks are never auto-followed by the
                # planner -- ``cleanup()`` re-checks via ``lstat``.
                if pat.pattern_type == "directory":
                    if not os.path.isdir(abs_path):
                        continue
                else:  # file
                    if os.path.isdir(abs_path):
                        continue
                    if not os.path.exists(abs_path) and not os.path.islink(
                        abs_path,
                    ):
                        continue
                targets.append(
                    CleanupTarget(
                        pattern_id=idx,
                        target_path_glob=pat.target_path_glob,
                        pattern_type=pat.pattern_type,
                        absolute_path=abs_path,
                        surface_overlap_risk=pat.surface_overlap_risk,
                        expected_filenames=pat.expected_filenames,
                    )
                )
            continue
        # Literal path -- the historic fast path.
        abs_path = os.path.join(world_root_resolved, rel)
        if pat.pattern_type == "directory":
            if not os.path.isdir(abs_path) or os.path.islink(abs_path):
                # Directory absent OR is a symlink (never followed);
                # we still surface symlinks via the safe-to-delete
                # probe in cleanup(), but planning excludes the
                # missing-dir case so the report is clean.
                if not os.path.exists(abs_path) and not os.path.islink(
                    abs_path
                ):
                    continue
        else:  # file
            if not os.path.exists(abs_path) and not os.path.islink(abs_path):
                continue
        targets.append(
            CleanupTarget(
                pattern_id=idx,
                target_path_glob=pat.target_path_glob,
                pattern_type=pat.pattern_type,
                absolute_path=abs_path,
                surface_overlap_risk=pat.surface_overlap_risk,
                expected_filenames=pat.expected_filenames,
            )
        )
    return targets


# ---------------------------------------------------------------------------
# User-content enumeration
# ---------------------------------------------------------------------------

def _enumerate_user_content(
    target: CleanupTarget,
) -> Optional[UserContentCallout]:
    """For directory targets, list non-plugin paths inside *target*.

    Returns ``None`` when the target is a file, when no user content was
    found, or when the directory is missing. ``expected_filenames is
    None`` means the directory is treated as wholly plugin-owned -- no
    enumeration is performed (everything inside is plugin content).

    The walker is **recursive** (``os.walk``) so nested user content
    (e.g. ``.alive/scripts/custom/tool.sh``) is named by full
    directory-relative path -- not just the top-level component
    (``custom``). This matches the bible's "name the files
    specifically" convention so the post-upgrade restore command
    (``tar -xzf <tarball> -C <world> .alive/scripts/custom/tool.sh``)
    targets the user-authored file precisely.

    Plugin-content matching:
        * A top-level entry whose basename is in
          ``expected_filenames`` is treated as plugin-owned. If it is
          a directory, the entire subtree is treated as plugin-owned
          and skipped -- the catalog's
          ``expected_filenames`` set lists historic plugin
          file/directory basenames at retirement, and a top-level
          plugin directory's contents are by definition plugin-owned.
        * Any other top-level entry is user content. Files are
          recorded by their directory-relative path; directories
          contribute every file beneath them recursively.
    """
    if target.pattern_type != "directory":
        return None
    if target.expected_filenames is None:
        return None
    base = target.absolute_path
    # Symlink refusal (R15): a symlinked directory must NEVER be
    # followed -- not even for the "harmless" enumeration pass. The
    # ``os.listdir`` below would otherwise resolve through the link
    # and read external content, contradicting the safety contract.
    # The cleanup pass's own ``_safe_to_delete`` probe will then
    # refuse the deletion, but the enumeration already happened by
    # then. Detect via ``lstat``: if the target is a link, return
    # ``None`` -- the operator-facing report will still surface the
    # symlink under ``skipped[]`` via the deletion-side guard.
    try:
        st = os.lstat(base)
    except OSError:
        return None
    import stat as _stat  # noqa: PLC0415
    if _stat.S_ISLNK(st.st_mode):
        return None
    try:
        top_level = sorted(os.listdir(base))
    except FileNotFoundError:
        return None
    except OSError:
        return None
    user_paths: List[str] = []
    for name in top_level:
        full = os.path.join(base, name)
        if name in target.expected_filenames:
            # Plugin-owned: the entire subtree is plugin content;
            # skip without enumerating descendants.
            continue
        # User content. If it's a file (or symlink), record the
        # basename. If it's a directory, walk recursively and record
        # every file beneath it as ``<name>/<relpath>``.
        try:
            is_dir = os.path.isdir(full) and not os.path.islink(full)
        except OSError:
            continue
        if not is_dir:
            user_paths.append(name)
            continue
        # Recursive walk -- sorted for stable test output. Errors are
        # tolerated silently; the cleanup pass will surface filesystem
        # issues via its own ``skipped[]`` bucket.
        try:
            for root, dirs, files in os.walk(full):
                dirs.sort()
                for fname in sorted(files):
                    f_full = os.path.join(root, fname)
                    rel = os.path.relpath(f_full, base).replace(os.sep, "/")
                    user_paths.append(rel)
        except OSError:
            continue
    user_paths.sort()
    if not user_paths:
        return None
    # Preserve trailing slash on the displayed path for directory
    # patterns (matches the briefing convention).
    display = target.target_path_glob
    if not display.endswith("/"):
        display += "/"
    return UserContentCallout(path=display, filenames=tuple(user_paths))


# ---------------------------------------------------------------------------
# Surface-state path exclusion
# ---------------------------------------------------------------------------

def _normalize_surface_state(
    paths: Optional[Iterable[str]],
) -> Set[str]:
    """Return the realpath-normalised exclusion set.

    Each entry in *paths* is realpath'd so the comparison is symlink-
    robust. ``None`` and empty inputs yield an empty set.
    """
    if not paths:
        return set()
    out: Set[str] = set()
    for p in paths:
        try:
            out.add(os.path.realpath(p))
        except OSError:
            # Bad input shouldn't crash cleanup -- record nothing,
            # caller already saw the error during phase 4.
            continue
    return out


def _paths_intersect(target: str, surface: str) -> bool:
    """``True`` if *target* and *surface* refer to the same path OR
    one contains the other.

    Both arguments must already be ``os.path.realpath``-normalised.
    The intersection test catches three R19 violations the previous
    exact-equality check missed:

        * ``target == surface`` -- the historic case (deleting the
          surfaced path directly).
        * ``surface`` is INSIDE ``target`` -- e.g. ``surface =
          .alive/scripts/custom/state.json`` and ``target =
          .alive/scripts/`` -- removing *target* would destroy
          surfaced state.
        * ``target`` is INSIDE ``surface`` -- e.g. ``surface =
          .alive/`` and ``target = .alive/scripts/`` -- the surface
          claims an ancestor, so the descendant must also be
          preserved.

    The helper uses path-segment-aware comparison (``+ os.sep``) so
    ``.alive/scripts`` does NOT spuriously match ``.alive/scripts.bak``.
    """
    if target == surface:
        return True
    target_prefix = target.rstrip(os.sep) + os.sep
    surface_prefix = surface.rstrip(os.sep) + os.sep
    if surface.startswith(target_prefix):
        return True
    if target.startswith(surface_prefix):
        return True
    return False


def _should_skip_for_surface(
    target: CleanupTarget,
    *,
    mode: str,
    surface_state_resolved: Set[str],
    report: CleanupReport,
) -> bool:
    """``True`` if *target* must be skipped due to surface considerations.

    Two paths:
        * ``MODE_NORMAL`` -- consults the realpath'd
          *surface_state_resolved* set via ``_paths_intersect``: any
          target that EQUALS, CONTAINS, or IS CONTAINED BY a surfaced
          state path is excluded with reason
          ``surface_state_excluded``. The intersection test (vs the
          old exact-equality test) closes the R19 hole where a state
          path nested inside a retired directory would still allow
          the directory to be deleted.
        * ``MODE_SURFACES_NONE`` -- consult the catalog's
          ``surface_overlap_risk``: ``plugin_owned`` and
          ``world_state`` proceed; ``potentially_surface`` is skipped
          and recorded under
          ``cleanup_skipped_due_to_surface_uncertainty``.
    """
    if mode == MODE_NORMAL:
        try:
            resolved = os.path.realpath(target.absolute_path)
        except OSError:
            return False
        for surface in surface_state_resolved:
            if _paths_intersect(resolved, surface):
                report._bucket(
                    "surface_state_excluded", target.absolute_path,
                )
                report.skipped.append(
                    (target.absolute_path, "surface_state"),
                )
                return True
        return False
    # MODE_SURFACES_NONE
    if target.surface_overlap_risk == "potentially_surface":
        report._bucket(
            "cleanup_skipped_due_to_surface_uncertainty",
            target.absolute_path,
        )
        report.skipped.append(
            (target.absolute_path, "surface_uncertainty"),
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def cleanup(
    world_root: str,
    *,
    snapshot: Any = None,
    plan: Optional[List[CleanupTarget]] = None,
    dry_run: bool = False,
    mode: str = MODE_NORMAL,
    surface_state_paths: Optional[Iterable[str]] = None,
) -> CleanupReport:
    """Execute (or plan) the world-state cleanup phase.

    Parameters
    ----------
    world_root : str
        World root path. Realpath'd internally; the canonical
        comparison root is the ``realpath(world_root)``.
    snapshot : Any, optional
        Phase-2 ``FileSnapshot``. Currently informational; T5's
        decision tree is built from the live disk + catalog. Reserved
        for forensic logging in future iterations.
    plan : list[CleanupTarget], optional
        Pre-built plan from :func:`build_cleanup_plan`. When omitted,
        the plan is built from the live world state (``CATALOG``
        entries that exist on disk).
    dry_run : bool, default False
        When True, NO disk writes occur. The report still enumerates
        every ``deleted`` path the run *would* have removed (for plan
        rendering) under a separate field is unnecessary -- callers
        consume ``deleted[]`` and check ``dry_run`` independently. The
        ``user_content_callouts`` and ``skipped`` buckets are still
        populated.
    mode : str, default ``MODE_NORMAL``
        See :data:`MODE_NORMAL` / :data:`MODE_SURFACES_NONE`.
    surface_state_paths : iterable[str], optional
        Phase-4-collected union of surface state paths. Required when
        ``mode == MODE_NORMAL`` and surfaces were probed; ignored under
        ``MODE_SURFACES_NONE`` (the catalog's
        ``surface_overlap_risk`` drives that branch instead).

    Returns
    -------
    CleanupReport
        Bucketed deletion + skip summary, plus user-content callouts.
    """
    if mode not in (MODE_NORMAL, MODE_SURFACES_NONE):
        raise ValueError("unknown cleanup mode: {!r}".format(mode))
    world_root_resolved = _resolved_world_root(world_root)
    surface_state_resolved = _normalize_surface_state(surface_state_paths)
    if plan is None:
        plan = build_cleanup_plan(world_root_resolved)

    report = CleanupReport()

    # Cache submodule detection per walnut: walking ``.gitmodules`` and
    # ``.git`` once per cleanup run is cheaper than repeating it for
    # every target inside the same walnut.
    submodule_cache: Dict[str, bool] = {}
    walnut_kernels = _walnut_kernel_dirs(world_root_resolved)

    for target in plan:
        # Symlink short-circuit (R15): symlinks are NEVER followed --
        # not by the surface-filter realpath, not by the user-content
        # enumeration, not by the submodule check. Any subsequent
        # helper that calls ``os.path.realpath(target.absolute_path)``
        # would resolve through the link and read external content.
        # Probe via ``lstat`` first; record + continue without
        # invoking any helper that takes a peek through the link.
        try:
            link_st = os.lstat(target.absolute_path)
        except FileNotFoundError:
            # Target disappeared between plan and execution -- treat
            # as a clean no-op.
            continue
        except OSError as exc:
            report.skipped.append(
                (target.absolute_path, "lstat-error:{}".format(exc)),
            )
            continue
        import stat as _stat  # noqa: PLC0415
        if _stat.S_ISLNK(link_st.st_mode):
            report._bucket("symlink_skipped", target.absolute_path)
            report.skipped.append((target.absolute_path, "symlink"))
            continue

        # User-content enumeration runs BEFORE deletion so the
        # snapshot reflects what's on disk at decision time, but the
        # callout is only EMITTED after every skip check passes --
        # an excluded directory is preserved on disk and reporting
        # its filenames would mislead the operator into thinking the
        # restore command applies to a deletion that never happened.
        provisional_callout = _enumerate_user_content(target)

        # Submodule walnut guard (R15): if the target lives inside a
        # submodule walnut AND outside that walnut's ``_kernel/``
        # subtree, refuse to delete -- the rest of a submodule walnut
        # is owned by the submodule's own repository and the upgrade
        # must not mutate it. The check runs against the realpath so
        # symlinked walnuts don't bypass it.
        try:
            target_resolved = os.path.realpath(target.absolute_path)
        except OSError:
            target_resolved = target.absolute_path
        owning_walnut = _find_owning_walnut(
            target_resolved, walnut_kernels,
        )
        if owning_walnut is not None:
            is_sub = submodule_cache.get(owning_walnut)
            if is_sub is None:
                is_sub = _is_submodule_walnut(
                    owning_walnut, world_root_resolved,
                )
                submodule_cache[owning_walnut] = is_sub
            if is_sub:
                walnut_kernel = os.path.join(owning_walnut, "_kernel")
                kernel_prefix = walnut_kernel.rstrip(os.sep) + os.sep
                inside_kernel = (
                    target_resolved == walnut_kernel
                    or target_resolved.startswith(kernel_prefix)
                )
                if not inside_kernel:
                    report._bucket(
                        "submodule_skipped", target.absolute_path,
                    )
                    report.skipped.append(
                        (target.absolute_path, "submodule_outside_kernel"),
                    )
                    continue

        # Surface-state filtering.
        if _should_skip_for_surface(
            target,
            mode=mode,
            surface_state_resolved=surface_state_resolved,
            report=report,
        ):
            continue

        # Containment + symlink probe.
        if not _safe_to_delete(
            target.absolute_path,
            world_root_resolved=world_root_resolved,
            report=report,
        ):
            continue

        # Past every skip check -- the target IS slated for deletion
        # in this run. NOW emit the user-content callout so the
        # post-upgrade summary's restore command lines up with the
        # actual deletions.
        if provisional_callout is not None:
            report.user_content_callouts.append(provisional_callout)

        if dry_run:
            # Plan-only: record what WOULD be deleted, no disk writes.
            report.deleted.append(target.absolute_path)
            continue

        # Real run: remove. shutil.rmtree for dirs, os.remove for files.
        try:
            if target.pattern_type == "directory":
                shutil.rmtree(target.absolute_path)
            else:
                os.remove(target.absolute_path)
        except FileNotFoundError:
            # Lost the race; treat as a clean no-op.
            continue
        except OSError as exc:
            report.skipped.append(
                (target.absolute_path, "rm-error:{}".format(exc)),
            )
            continue
        report.deleted.append(target.absolute_path)

    return report
