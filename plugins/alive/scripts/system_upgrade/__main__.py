"""``python -m system_upgrade`` entry point.

Mirrors ``alive system-upgrade <args>`` for callers that want to
invoke the package directly (e.g. tests, CI matrix jobs that don't
have ``bin/alive`` on PATH). Internally builds a one-subcommand
argparse tree and dispatches to ``cli.handle``.

Run from the ``plugins/alive/scripts/`` directory (or with that
directory on ``PYTHONPATH``)::

    PYTHONPATH=plugins/alive/scripts python3 -m system_upgrade --help
"""

from __future__ import annotations

import argparse
import sys

from system_upgrade.cli import register, handle


def main(argv=None) -> int:
    # Hand the args directly to the subparser so ``python -m
    # system_upgrade --help`` reads identically to ``alive
    # system-upgrade --help`` (no doubled ``system-upgrade
    # system-upgrade`` prog string).
    container = argparse.ArgumentParser(prog="alive", add_help=False)
    sub_action = container.add_subparsers(dest="command")
    sub = register(sub_action)
    sub.prog = "alive system-upgrade"
    args = sub.parse_args(argv)
    return handle(args)


if __name__ == "__main__":
    sys.exit(main())
