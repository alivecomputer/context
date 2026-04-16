"""CLI entry point for alive-mcp.

Two modes:

* ``alive-mcp --version`` — print the package version to stdout and exit 0.
  This is the only path in the module that writes to stdout. It is safe
  because no server is running; the stdio transport only claims stdout
  once :func:`alive_mcp.server.main` is called.

* ``alive-mcp`` (bare invocation) — run the stdio MCP server. From this
  point on, stdout is OWNED by the JSON-RPC transport; every diagnostic
  must go to stderr via the logging module. Violating this invariant
  corrupts framing and makes the server silently unusable.

T1 scaffolding printed the version unconditionally; T5 replaces that
behavior with the real server startup. The ``--version`` flag remains so
``alive-mcp --version`` keeps working in Docker/CI scripts that grep for
the version without invoking the protocol.
"""

from __future__ import annotations

import argparse
import sys

from alive_mcp import __version__


def main(argv: list[str] | None = None) -> int:
    """Entry point wired via ``[project.scripts] alive-mcp``.

    Parses argv, dispatches to the version path or the server path.
    Returns the process exit code; tests call this with ``argv=[]`` or
    ``argv=["--version"]`` to exercise both branches without invoking
    ``sys.exit`` inside the suite.
    """
    parser = argparse.ArgumentParser(
        prog="alive-mcp",
        description=(
            "Read-only MCP server exposing the ALIVE Context System. "
            "Bare invocation starts the stdio server; --version prints the "
            "version and exits."
        ),
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print version and exit (does not start the server)",
    )
    args = parser.parse_args(argv)

    if args.version:
        # Safe to write to stdout here: the server has not started, so
        # nothing owns the JSON-RPC frame channel yet. CI scripts that
        # grep for the version expect this on stdout.
        print(__version__)
        return 0

    # Server mode. Importing :mod:`alive_mcp.server` here (rather than at
    # module top-level) keeps ``--version`` cheap — no FastMCP import,
    # no watchdog thread, no logging reconfiguration. The extra import
    # cost on the server path is negligible (one level of indirection).
    from alive_mcp.server import main as server_main

    return server_main(argv)


if __name__ == "__main__":
    sys.exit(main())
