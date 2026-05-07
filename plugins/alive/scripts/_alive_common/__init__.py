"""Shared private helpers extracted from ``alive-p2p.py``.

This package houses substantial extracted concerns that were too large
to live inside the deliberately-narrow ``_common.py`` module:

- ``tarball``  -- ``safe_tar_create`` / ``safe_tar_extract`` /
  ``safe_extractall`` / ``tar_list_entries`` and the LD22 pre-validator.
- ``yaml_emit`` -- the hand-rolled YAML 1.2 subset writer + reader for
  the v3 bundle / manifest YAML schema. Stdlib-only; emits the exact
  subset the manifest format uses (LD20).
- ``migrate``  -- ``migrate_v2_layout`` and its v2-checklist parser.

The leading underscore on the package name preserves the
"internal helper, not the plugin's public surface" convention used
elsewhere (``_common.py``, ``_atomic_io.py``, ``_world_root_io.py``).
Submodules are stdlib-only (per ``rules/world.md`` LD22 / R10).
"""
