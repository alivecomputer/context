"""Stage 5 entry point -- thin wrapper over ``scaffold.activate``.

Stage 5 is the deterministic-Python tail of the ``/alive:demo`` pipeline.
There is no LLM dispatch; the heavy lifting is documented at
``plugins/alive/skills/demo/scaffold.py`` (11-step transaction with
exactly one commit point at step 11).

This module exposes three CLI-friendly entry points:

  * :func:`prepare_activation` -- dry-run validator + plan
    (no writes; pre-check findings included).
  * :func:`run_activation` -- execute the 11-step transaction.
  * :func:`verify_activation` -- post-step-11 verification: pointer
    matches, demo-state matches, walnut now.json projections present,
    index file built.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.normpath(os.path.join(_HERE, os.pardir))
_PLUGIN_ROOT = os.path.normpath(os.path.join(_DEMO_DIR, os.pardir, os.pardir))
_SCRIPTS = os.path.join(_PLUGIN_ROOT, "scripts")
if os.path.isdir(_SCRIPTS) and _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def _load_demo_sibling(name: str):
    """Load ``<demo>/<name>.py`` under a namespaced sys.modules key."""
    full_name = f"alive_demo.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    target = os.path.join(_DEMO_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(full_name, target)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {full_name} from {target}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _scaffold():
    return _load_demo_sibling("scaffold")


def _state():
    return _load_demo_sibling("state")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def prepare_activation(
    partial_dir,
    *,
    base_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Dry-run: validate stage{0..4}_done markers + run pre-check.

    Returns the planning dict from :func:`scaffold.prepare_activation`.
    No filesystem writes occur; safe to call any number of times.
    """
    return _scaffold().prepare_activation(partial_dir, base_dir=base_dir)


def run_activation(
    partial_dir,
    *,
    confirm: bool = False,
    base_dir: Optional[str] = None,
    plugin_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute the 11-step activation transaction.

    On a clean live world (no pre-check findings), the transaction
    proceeds end-to-end and step 11 commits the world-root pointer.

    On a live world with findings, the function returns
    ``{"status": "needs_confirmation", "findings": [...], ...}`` so the
    parent skill can surface a bordered-block warning + AskUserQuestion;
    re-invoke with ``confirm=True`` to proceed.
    """
    return _scaffold().activate(
        partial_dir,
        confirm=confirm,
        base_dir=base_dir,
        plugin_root=plugin_root,
    )


def verify_activation(world_path) -> Dict[str, Any]:
    """Post-step-11 verification of an activated demo world.

    Checks (each contributes a finding on failure):
      * ``~/.config/alive/world-root`` reads back as ``world_path``.
      * ``demo-state.json[active_world][path]`` matches ``world_path``.
      * Every walnut directory has ``_kernel/now.json``.
      * ``<world>/.alive/_index.json`` and ``_index.yaml`` exist.
      * ``<world>/.alive/_demo-build-log.md`` exists.

    Returns ``{"status": "ok" | "failed", "findings": [...], "world_path": <abs>}``.
    """
    world_path = os.path.normpath(os.path.abspath(os.fspath(world_path)))
    findings = []

    state_mod = _state()
    state = state_mod.load_state()

    pointer = state_mod.read_world_root_file()
    pointer_str = str(pointer) if pointer is not None else None
    if pointer_str != world_path:
        findings.append({
            "issue": "world_root_mismatch",
            "evidence": (
                f"world-root pointer = {pointer_str!r}; "
                f"expected {world_path!r}"
            ),
        })

    active = state.get("active_world") or {}
    if active.get("path") != world_path:
        findings.append({
            "issue": "demo_state_active_world_mismatch",
            "evidence": (
                f"demo-state.json active_world.path = {active.get('path')!r}; "
                f"expected {world_path!r}"
            ),
        })

    build_log = os.path.join(world_path, ".alive", "_demo-build-log.md")
    if not os.path.isfile(build_log):
        findings.append({
            "issue": "build_log_missing",
            "evidence": f"missing {build_log}",
        })

    index_yaml = os.path.join(world_path, ".alive", "_index.yaml")
    index_json = os.path.join(world_path, ".alive", "_index.json")
    if not os.path.isfile(index_yaml):
        findings.append({
            "issue": "index_yaml_missing",
            "evidence": f"missing {index_yaml}",
        })
    if not os.path.isfile(index_json):
        findings.append({
            "issue": "index_json_missing",
            "evidence": f"missing {index_json}",
        })

    # Walk the walnut tree and confirm every walnut has a now.json.
    domain_dirs = ("01_Archive", "02_Life", "04_Ventures", "05_Experiments")
    skip_dirs = frozenset({
        ".git", ".next", ".venv",
        "__pycache__", "build", "dist", "node_modules", "raw",
        "target", "venv",
    })
    for domain in domain_dirs:
        dpath = os.path.join(world_path, domain)
        if not os.path.isdir(dpath):
            continue
        for walk_root, dirs, _files in os.walk(dpath):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".") and d not in skip_dirs
            ]
            kernel = os.path.join(walk_root, "_kernel")
            if not os.path.isfile(os.path.join(kernel, "key.md")):
                continue
            dirs[:] = []
            now_path = os.path.join(kernel, "now.json")
            if not os.path.isfile(now_path):
                findings.append({
                    "issue": "now_json_missing",
                    "evidence": f"walnut {walk_root}: missing {now_path}",
                })

    return {
        "status": "ok" if not findings else "failed",
        "findings": findings,
        "world_path": world_path,
    }


__all__ = (
    "prepare_activation",
    "run_activation",
    "verify_activation",
)
