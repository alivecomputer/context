"""Vendored kernel utilities from the ALIVE plugin.

This subpackage holds a frozen copy of library-safe helpers vendored from
``claude-code/plugins/alive/scripts/`` in the ``alivecontext/alive`` monorepo.
See ``VENDORING.md`` in this directory for the source commit hashes, the
direct-copy vs extract split, and the refresh policy.

Import contract: every module in this subpackage (and in ``_pure/``) MUST be
import-safe with zero side effects -- no ``print()`` on import, no
``sys.exit()``, no network, no filesystem writes. Importing corrupts stdout
framing for the long-lived stdio JSON-RPC server otherwise.
"""

__all__: list[str] = []
