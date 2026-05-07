#!/usr/bin/env python3
"""ALIVE Context System -- top-level CLI dispatcher (`alive`).

Thin argparse wrapper. Subcommand handlers are registered here; the heavy
lifting lives in the subcommand modules (doctor, schema, log, ...). T1 ships
the dispatcher with `--version` and `--plugin-root` only; subcommands arrive
in T3-T5.

Invocation patterns:
    alive --version
    alive --plugin-root /path/to/plugin --version
    alive <subcommand> [args]

The top-level `--plugin-root <path>` override is available on every
subcommand via `args.plugin_root`. This is the Pattern-B-coupling mitigation
called out in the fn-12 spec: non-Claude-Code surfaces (Cowork, MCP, future
native apps) that don't set `$ALIVE_PLUGIN_ROOT` can pass the path
explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Ensure the scripts/ directory is on sys.path so that `import _common` works
# whether this file is executed directly (via bin/alive) or imported.
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _common import resolve_plugin_root  # noqa: E402


# ---------------------------------------------------------------------------
# Subcommand registry
# ---------------------------------------------------------------------------

# Each subcommand registers itself by appending a (name, register_fn) tuple.
# register_fn(subparsers) -> argparse.ArgumentParser. Handlers live inside the
# subcommand module. T3 adds `doctor`; T4/T5 extend with schema + log.
import doctor as _doctor_module  # noqa: E402
import log as _log_module  # noqa: E402
import promote as _promote_module  # noqa: E402
import schema as _schema_module  # noqa: E402
from system_upgrade import cli as _system_upgrade_cli  # noqa: E402

# `demo` lives outside `scripts/` (under `skills/demo/`) because the
# subcommand backs the user-callable `/alive:demo` skill router; its
# `cli_register` module wires argparse + dispatches to the skill's
# state primitives (fn-2-2zz.3). We load it via importlib so neither the
# generic-named `state.py` / `lib.py` siblings nor the `cli_register`
# module itself collide with any future top-level module of the same name.
def _load_demo_register():
    """Return the `register` callable for the `alive demo` subcommand group."""
    import importlib.util  # noqa: PLC0415
    demo_dir = os.path.normpath(
        os.path.join(_SCRIPTS_DIR, os.pardir, "skills", "demo")
    )
    target = os.path.join(demo_dir, "cli_register.py")
    spec = importlib.util.spec_from_file_location(
        "alive_demo.cli_register", target
    )
    if spec is None or spec.loader is None:
        raise ImportError(
            "could not load alive demo cli_register from {}".format(target)
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules["alive_demo.cli_register"] = module
    spec.loader.exec_module(module)
    return module.register


_demo_register = _load_demo_register()

_SUBCOMMANDS: list = [
    ("doctor", _doctor_module.register),
    ("log", _log_module.register),
    ("schema", _schema_module.register),
    ("tasks", _promote_module.register),
    ("demo", _demo_register),
    ("system-upgrade", _system_upgrade_cli.register),
]


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

def _read_plugin_version(plugin_root):
    """Read plugin version from .claude-plugin/plugin.json. Never raises."""
    manifest = os.path.join(plugin_root, ".claude-plugin", "plugin.json")
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("version", "unknown")
    except (OSError, json.JSONDecodeError):
        return "unknown"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _build_parser():
    parser = argparse.ArgumentParser(
        prog="alive",
        description="ALIVE Context System -- agent-facing CLI",
    )
    parser.add_argument(
        "--plugin-root",
        default=None,
        help="Override the ALIVE plugin root directory "
             "(defaults: $ALIVE_PLUGIN_ROOT, then auto-discovery).",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the plugin version and exit.",
    )
    subparsers = parser.add_subparsers(dest="command")
    for _name, register in _SUBCOMMANDS:
        register(subparsers)
    return parser


def main(argv=None):
    parser = _build_parser()
    # parse_intermixed_args won't work here because argparse disallows it in
    # combination with subparsers (the subparser is `nargs=A...`). Standard
    # argparse semantics -- top-level flags go BEFORE the subcommand --
    # apply: `alive --plugin-root X doctor` works;
    # `alive doctor --plugin-root X` doesn't parse the top-level flag.
    # T3+ subcommand registrations mirror `--plugin-root` onto each
    # subparser so either position is accepted (the subparser copy wins
    # when both appear, which is fine -- they name the same path).
    args = parser.parse_args(argv)

    if args.version:
        # --version is the only top-level path that consumes plugin_root
        # itself; fail loud on misconfigured overrides rather than
        # silently falling through to auto-discovery (violates the
        # "deterministic override" contract).
        try:
            resolved = resolve_plugin_root(args.plugin_root)
        except FileNotFoundError as e:
            print("Error: {}".format(e), file=sys.stderr)
            sys.exit(1)
        print(_read_plugin_version(resolved))
        return 0

    if not args.command:
        parser.print_help()
        return 1

    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return 1
    # Subcommand handlers own plugin-root resolution -- they can surface
    # resolve failures inside their own JSON envelope with the correct
    # error code (``plugin_root_error`` / ``usage`` / etc.) instead of
    # relying on cli.py to pre-resolve and potentially mask the failure.
    return handler(args) or 0


if __name__ == "__main__":
    sys.exit(main())
