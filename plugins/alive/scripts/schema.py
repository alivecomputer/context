#!/usr/bin/env python3
"""``alive schema`` -- argparse-introspection CLI-contract emitter.

Walks ``cli.py``'s subparser tree (recursively, so nested subcommands
like ``alive log prepend`` flatten out to ``"command": "log prepend"``)
and merges each subcommand module's ``SCHEMA_METADATA`` constant into a
JSON document that agents consume without invoking ``--help``.

Output shape
------------
``alive schema`` -> ``{"subcommands": [entry, ...]}``
``alive schema <command>`` -> one ``entry`` (no wrapper).

Entry: command, description, args [{name, type, required, default,
choices, help}, ...]; plus stdout_shape / exit_codes / examples when the
module declares them. Missing-metadata modules degrade to args-only --
omitted fields are omitted (not stubbed).

Exports ``SCHEMA_METADATA`` itself so ``alive schema schema`` is
self-describing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


SCHEMA_METADATA = {
    "description": (
        "Emit the alive CLI contract as JSON. Default run lists every "
        "registered subcommand; `alive schema <command>` narrows to one."
    ),
    "stdout_shape": {
        "subcommands": (
            "list[object] -- default run; each entry has command, "
            "description, args, and (when declared) stdout_shape, "
            "exit_codes, examples."
        ),
        "command": "str -- narrow run (no `subcommands` wrapper)",
        "description": "str",
        "args": (
            "list[object] -- each: name, type, required, default, "
            "choices, help."
        ),
        "stdout_shape": "object|omitted",
        "exit_codes": "object<str,str>|omitted",
        "examples": "list[object]|omitted",
    },
    "exit_codes": {
        "0": "schema emitted",
        "1": "unknown subcommand name on narrow run",
        "2": "usage error (argparse)",
    },
    "examples": [
        {
            "input": "alive schema",
            "output_excerpt": '{"subcommands": [{"command": "doctor", ...}]}',
        },
        {
            "input": "alive schema schema",
            "output_excerpt": '{"command": "schema", "description": "..."}',
        },
    ],
}


# Keys we pass through onto emitted entries. Module-defined keys outside
# this set (e.g. doctor's ``checks``) are domain-specific and dropped.
#
# fn-15-la5 T7: ``world_root_strategies`` is a public-ish JSON contract
# (locked v3.x stable) describing which resolver strategies are live and
# which deprecated ids translate to live ones. External consumers
# (skill code, third-party scripts) discover the contract via
# ``alive schema doctor`` -> ``world_root_strategies`` -> {current,
# labels, deprecated, stable_since}; passing it through here is the
# advertised entry point.
_SEMANTIC_KEYS = (
    "stdout_shape", "exit_codes", "examples", "world_root_strategies",
)

# Required keys on SCHEMA_METADATA. Missing any -> stderr warning; shape
# checks (``exit_codes`` must be str->str etc.) warn without rejecting.
_REQUIRED_KEYS = ("description", "stdout_shape", "exit_codes", "examples")

#: Key under which subparsers stash SCHEMA_METADATA via ``set_defaults``.
#: Preferred over ``sys.modules[name]`` (robust against alias / package
#: path drift).
SCHEMA_METADATA_DEFAULT_KEY = "_schema_metadata"


def _warn(msg):
    print("Warning: " + msg, file=sys.stderr)


def _validate_metadata(name, meta):
    """Warn on missing/mis-shaped keys; return meta (or {} if non-dict)."""
    if not isinstance(meta, dict):
        _warn("{} SCHEMA_METADATA is not a dict; ignoring".format(name))
        return {}
    missing = [k for k in _REQUIRED_KEYS if k not in meta]
    if missing:
        _warn(
            "{} SCHEMA_METADATA missing keys: {}".format(
                name, ", ".join(missing)
            )
        )
    ec = meta.get("exit_codes")
    if ec is not None and not isinstance(ec, dict):
        _warn("{} SCHEMA_METADATA.exit_codes is not a dict".format(name))
    elif isinstance(ec, dict):
        # JSON-friendly: stringify int keys (argparse-style exit codes
        # often live as ints in source) so the emitted dict round-trips.
        if any(not isinstance(k, str) for k in ec):
            meta = dict(meta)
            meta["exit_codes"] = {str(k): v for k, v in ec.items()}
    ex = meta.get("examples")
    if ex is not None and not isinstance(ex, list):
        _warn("{} SCHEMA_METADATA.examples is not a list".format(name))
    return meta


def _type_label(action):
    """Human-readable type label (``int`` not ``<class 'int'>``)."""
    if isinstance(
        action, (argparse._StoreTrueAction, argparse._StoreFalseAction)
    ):
        return "bool"
    t = action.type
    return "str" if t is None else getattr(t, "__name__", str(t))


def _action_required(action):
    """True when argparse would require *action* to be specified.

    Option flags read ``.required``. Positionals derive from ``nargs``:
    None / "+" / int>0 -> required; "?" / "*" -> optional.
    """
    if action.option_strings:
        return bool(getattr(action, "required", False))
    n = action.nargs
    if n in ("?", "*"):
        return False
    if isinstance(n, int):
        return n > 0
    return True


def _arg_entry(action):
    """Serialize one argparse action into the schema's ``args`` shape."""
    default = action.default
    if default is argparse.SUPPRESS:
        default = None
    else:
        try:
            json.dumps(default)
        except TypeError:
            default = repr(default)
    name = (
        "/".join(action.option_strings) if action.option_strings
        else action.dest
    )
    return {
        "name": name,
        "type": _type_label(action),
        "required": _action_required(action),
        "default": default,
        "choices": list(action.choices) if action.choices else None,
        "help": action.help or "",
    }


def _collect_args(parser):
    out = []
    for act in parser._actions:  # noqa: SLF001
        if isinstance(
            act, (argparse._HelpAction, argparse._SubParsersAction)
        ):
            continue
        out.append(_arg_entry(act))
    return out


def _module_metadata(name, subparser):
    """Resolve SCHEMA_METADATA for *name*: parser-stashed > sys.modules."""
    stashed = subparser.get_default(SCHEMA_METADATA_DEFAULT_KEY)
    if stashed is not None:
        return _validate_metadata(name, stashed)
    mod = sys.modules.get(name)
    meta = getattr(mod, "SCHEMA_METADATA", None) if mod else None
    return _validate_metadata(name, meta) if meta else {}


def _entry_for(path, subparser):
    """Build one schema entry. ``path`` is the space-joined command path."""
    meta = _module_metadata(path, subparser)
    # Fallback chain: metadata description -> argparse description -> help.
    description = (
        meta.get("description")
        or (subparser.description or "")
        or subparser.get_default("_schema_help")
        or ""
    )
    entry = {
        "command": path,
        "description": description,
        "args": _collect_args(subparser),
    }
    for key in _SEMANTIC_KEYS:
        if key in meta:
            entry[key] = meta[key]
    return entry


def _walk(parser, prefix=""):
    """Yield (command_path, subparser) for every leaf in the tree.

    A leaf is a subparser with no nested subparsers group OR whose
    subcommand dest doesn't register any sub-commands. The walker
    recurses into nested ``_SubParsersAction`` instances so
    ``alive log prepend`` emits as ``"command": "log prepend"`` when
    T5/T6 add it.
    """
    has_nested = False
    for act in parser._actions:  # noqa: SLF001
        if isinstance(act, argparse._SubParsersAction) and act.choices:
            has_nested = True
            for name, sub in sorted(act.choices.items()):
                path = "{} {}".format(prefix, name).strip()
                yield from _walk(sub, path)
    if prefix and not has_nested:
        yield prefix, parser


def build_schema():
    """Build ``{"subcommands": [...]}`` by walking cli.py's parser tree."""
    import cli  # lazy; avoids dispatcher side-effects at module import
    parser = cli._build_parser()  # noqa: SLF001
    return {
        "subcommands": [_entry_for(path, sp) for path, sp in _walk(parser)]
    }


def handle(args):
    """Emit schema JSON; return exit code."""
    full = build_schema()
    target = getattr(args, "subcommand", None)
    if target:
        for entry in full["subcommands"]:
            if entry["command"] == target:
                print(json.dumps(entry, indent=2))
                return 0
        print(json.dumps({
            "error": "unknown subcommand: {}".format(target),
            "hint": "run `alive schema` to see registered subcommands",
        }, indent=2))
        return 1
    print(json.dumps(full, indent=2))
    return 0


def _json_error_handler(parser):
    """Wrap ``parser.error`` to emit a JSON envelope on stdout (exit 2).

    Same contract as doctor: agents parse stdout, so usage errors need
    to land there too (not on plain-text stderr).
    """
    def _error(message):
        print(json.dumps({
            "error": "usage: {}".format(message),
            "hint": "run `alive schema --help` for flag list",
        }, indent=2))
        sys.exit(2)
    return _error


def register(subparsers):
    """Register the ``schema`` subcommand on the dispatcher."""
    parser = subparsers.add_parser(
        "schema",
        help=SCHEMA_METADATA["description"],
        description=SCHEMA_METADATA["description"],
    )
    parser.add_argument(
        "--plugin-root",
        default=None,
        help=(
            "Override the ALIVE plugin root directory "
            "(defaults: $ALIVE_PLUGIN_ROOT, then auto-discovery)."
        ),
    )
    parser.add_argument(
        "subcommand",
        nargs="?",
        default=None,
        help=(
            "Optional subcommand name. When present, emit only that "
            "entry (no `{subcommands: [...]}` wrapper)."
        ),
    )
    parser.error = _json_error_handler(parser)  # type: ignore[assignment]
    parser.set_defaults(
        _handler=handle,
        **{SCHEMA_METADATA_DEFAULT_KEY: SCHEMA_METADATA},
    )
    return parser


def _standalone_main(argv=None):
    parser = argparse.ArgumentParser(prog="alive-schema")
    subparsers = parser.add_subparsers(dest="command")
    register(subparsers)
    args = parser.parse_args(["schema"] + (list(argv) if argv else []))
    return handle(args)


if __name__ == "__main__":
    sys.exit(_standalone_main(sys.argv[1:]))
