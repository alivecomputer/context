# Vendoring policy

This directory holds a frozen slice of ALIVE plugin kernel utilities so the
`alive-mcp` stdio JSON-RPC server can use them without the CLI hazards of the
upstream scripts (`print()` corrupts JSON-RPC framing; `sys.exit()` kills the
whole server process).

## Source

Upstream repository: `alivecontext/alive` (`github.com/alivecontext/alive`).
All paths below are relative to `claude-code/plugins/alive/scripts/` in that
repository.

## Copy date

2026-04-16 (fn-10-60k.2, task T2 of the alive-mcp v0.1 epic).

## Source commit hashes

Each source file is pinned at the commit that most recently touched it on the
`main` branch at vendor time:

| Source file          | Upstream commit                            |
|----------------------|--------------------------------------------|
| `walnut_paths.py`    | `525ab597caa8442c428f334e3dc3fca2d791813d` |
| `project.py`         | `f91553c1796726eb3eb40490bd2056f3c99f7459` |
| `tasks.py`           | `2e3d77e1bff47fa620e0dfb8273b033ab98cd520` |

The commit for `walnut_paths.py` was resolved from the primary worktree's
`main` branch; it is `feat(p2p-v3): port crypto/tar/sig foundations +
walnut_paths module`. The file lands in `main` ahead of the alive-mcp-v0.1
branch and is not yet present on the branch at task-claim time, which is
expected -- T2 vendors it directly from `main`.

## Direct copy

One file is vendored verbatim because it was purpose-built as a library
(docstring declares the public API, zero `print()`, zero `sys.exit()`):

- `walnut_paths.py` -- bundle path resolution and discovery. Layout-agnostic
  across v1 (`_core/_capsules/`), v2 (`bundles/`), and v3 (flat) walnuts.
  Stdlib only.

## Extract-to-pure

Two source CLIs had their pure logic lifted into new modules under
`_pure/`. The CLIs themselves are NOT vendored -- their `print()` /
`sys.exit()` surface is forbidden inside a stdio MCP server.

| Upstream                                      | Extracted into              |
|-----------------------------------------------|-----------------------------|
| `project.py::parse_log` (L23-L199)            | `_pure/project_pure.py`     |
| `project.py::scan_bundles` (L206-L254)        | `_pure/project_pure.py`     |
| `project.py::parse_manifest` (L257-L304)      | `_pure/project_pure.py`     |
| `project.py::read_unscoped_tasks` (L351-L361) | `_pure/project_pure.py`     |
| `project.py::find_world_root` (L368-L379)     | `_pure/project_pure.py`     |
| `project.py::read_squirrel_sessions` (L382-)  | `_pure/project_pure.py`     |
| `project.py::scan_nested_walnuts` (L498-L546) | `_pure/project_pure.py`     |
| `project.py::assemble` (L553-L722)            | `_pure/project_pure.py`     |
| `tasks.py::_all_task_files` (L72-L103)        | `_pure/tasks_pure.py`       |
| `tasks.py::_collect_all_tasks` (L149-L156)    | `_pure/tasks_pure.py`       |
| `tasks.py::cmd_summary` body (L424-L584)      | `_pure/tasks_pure.py::summary_from_walnut` |

### Divergences from upstream

Documented inline in the module headers; summary here:

1. `find_world_root` raises `WorldNotFoundError` instead of returning
   `None`. Callers that used to check for `None` now catch the exception;
   the internal callers (`read_squirrel_sessions`) catch it to preserve
   upstream "empty-on-miss" semantics.
2. `parse_log` raises `KernelFileError` on unreadable log. Missing log
   remains a non-error (returns empty projection).
3. Malformed YAML / JSON no longer prints to stderr -- it emits
   `MalformedYAMLWarning` so the MCP audit layer can capture it via the
   standard `warnings` module.
4. `assemble` no longer shells out to `tasks.py` via subprocess. Callers
   compose task data with `tasks_pure.summary_from_walnut` and pass the
   dict in as an argument (or omit it for the direct-`tasks.json` fallback).
5. No `argparse`, no `main()`, no `__main__` block. These are libraries.

## Error taxonomy

Defined in `_pure/__init__.py`:

| Name                    | Base           | When raised                             |
|-------------------------|----------------|-----------------------------------------|
| `WorldNotFoundError`    | `Exception`    | No ancestor of a path contains `.alive/` |
| `KernelFileError`       | `Exception`    | Required `_kernel/*` file unreadable    |
| `MalformedYAMLWarning`  | `Warning`      | Manifest / YAML parse failed            |

Exception classes map 1-to-1 onto v0.1 error-taxonomy codes that T4 defines
(`ERR_NO_WORLD`, `ERR_KERNEL_FILE_MISSING` / `ERR_KERNEL_FILE_CORRUPT`,
`ERR_MANIFEST_MALFORMED`).

## Refresh policy

Manual. On every upstream change to any of the three source files:

1. Check `git log -1 --format=%H -- claude-code/plugins/alive/scripts/{file}`
   in the upstream checkout.
2. If the hash differs from the table above:
   - **Direct-copy files** (`walnut_paths.py`): replace verbatim, update
     the commit hash in this file.
   - **Extracted files** (`project_pure.py`, `tasks_pure.py`): diff the
     upstream function against the extracted copy, port semantic changes,
     update the commit hash in this file.
3. Run `python3 -m unittest discover tests` from the `alive-mcp/` root to
   confirm the smoke suite still passes.
4. Commit with message `chore(vendor): refresh {walnut_paths|project|tasks}
   to upstream {short-hash}`.

No automated sync. Upstream churn in these files is low-frequency; the cost
of drift is lower than the cost of an auto-sync bot pulling a breaking
change into the MCP server on its own.

## Zero-side-effect import contract

Every module in `_vendor/` (including `_pure/`) MUST be import-safe: no
`print()`, no `sys.exit()`, no `warnings.warn` at import time, no filesystem
writes, no network. `tests/test_vendor_smoke.py` verifies this by importing
each module in a subprocess with stdout captured and asserting the capture
is empty.
