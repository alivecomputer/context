"""Tool layer for alive-mcp v0.1.

Each submodule groups related read-only tools that FastMCP exposes via
``mcp.tool()`` decorators on the server instance. Registration happens in
:func:`alive_mcp.server.build_server`, which imports each submodule's
``register(server)`` callable after wiring capabilities and lifespan.

v0.1 tool layout:

* :mod:`alive_mcp.tools.walnut` -- walnut-centric reads (T6):
  ``list_walnuts``, ``get_walnut_state``, ``read_walnut_kernel``.
* :mod:`alive_mcp.tools.bundle` -- bundle-centric reads (T7):
  ``list_bundles``, ``get_bundle``, ``read_bundle_manifest``.
* (T8+) ``alive_mcp.tools.search``, ``alive_mcp.tools.log_tasks`` --
  land in later tasks.

The :mod:`alive_mcp.tools._audit_stub` module carries the ``@audited``
decorator that every tool wraps itself in. T12 replaces the stub body
with the real audit writer; the signature stays stable so tool code does
not change.
"""
from __future__ import annotations

__all__: list[str] = []
