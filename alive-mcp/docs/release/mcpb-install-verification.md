# MCPB install verification — Claude Desktop

Step-by-step walkthrough for validating the alive-mcp `.mcpb` bundle
end-to-end in Claude Desktop. Run this checklist on every release
candidate before tagging `vX.Y.Z`. The acceptance criteria from
`fn-10-60k.17` map directly onto the sections below.

## Scope

This document covers **Claude Desktop on macOS** because that is the
platform where one-click `.mcpb` install is currently most polished.
Windows follows the same flow with platform-appropriate paths
(`%APPDATA%\Claude\` instead of `~/Library/Application Support/Claude/`).
Linux is not yet a first-class Claude Desktop target and is out of
scope for this verification.

## Prerequisites

Install these once per machine you verify on. Keep them current between
release candidates so the verification reflects a realistic end-user
environment.

**Working directory for every shell command below:** the `alive-mcp/`
package root (the directory that contains `pyproject.toml`,
`scripts/`, `mcpb/`, and `docs/`). In the monorepo that is
`claude-code/alive-mcp/`; in a split checkout it is the repo root.
Every path in this doc is relative to that root unless explicitly
prefixed with `~/` or an absolute path.

1. **Claude Desktop ≥ 0.8** — the `.mcpb` drag-and-drop install path
   landed in 0.8. Verify your version at
   `Claude Desktop → Settings → About`.
2. **`uv` on PATH** — the bundle declares `server.type: "uv"`, which
   means Claude Desktop invokes `uv run` at first launch. Install via
   `curl -LsSf https://astral.sh/uv/install.sh | sh` if absent. Restart
   Claude Desktop after installing so it picks up the updated `PATH`.
3. **A World to point at** — either a real ALIVE world or the fixture
   world at `tests/fixtures/world-basic/` (relative to the alive-mcp
   package root). Note the absolute path; you will paste it into the
   environment config below.
4. **The bundle file** — either downloaded from the GitHub release
   (`alive-mcp-0.1.0.mcpb`) or built locally via
   `scripts/build-mcpb.sh --validate`.

## Phase 1 — Install

**1.1 Drag-and-drop install.** Drag `alive-mcp-0.1.0.mcpb` onto the
Claude Desktop window. Claude Desktop opens an install dialog showing
the manifest metadata: display name "ALIVE", the description, the
author, and the 10 tools listed in the manifest.

**1.2 Grant permissions.** Click through the permission prompts. The
uv-type server declares no `user_config` fields in v0.1, so there are
no API keys or secrets to enter at install time; the only
configuration happens in step 1.3.

**1.3 Set `ALIVE_WORLD_ROOT`.** After install, open
`Settings → Connections → ALIVE → Edit configuration` (exact menu
label may drift slightly across Claude Desktop builds). Add the
environment variable:

```
ALIVE_WORLD_ROOT=/absolute/path/to/your/world
```

Restart the server from the same menu (or restart Claude Desktop
entirely). The server cannot start without a world to point at — if
you skip this step and try to call a tool, you get a structured error
envelope with `code: "world_not_configured"`.

**1.4 Verify it appears in Settings.** Open
`Settings → Connections → Developer`. You should see an entry for
**ALIVE** with status **Connected** (green dot). Red dot means the
server failed to start; proceed to the **Troubleshooting** section
below before continuing.

## Phase 2 — Smoke-test a tool call

With the server Connected, open a new Claude Desktop conversation and
issue a prompt that forces a tool call. The canonical smoke test is:

> "Use the alive-mcp tool to list the walnuts in my world."

Claude will call `list_walnuts` via MCP. Expected outcomes:

- **Tool appears in the tool picker.** Claude Desktop shows the
  10-tool list when you explicitly invoke the MCP menu — `list_walnuts`,
  `get_walnut_state`, `read_walnut_kernel`, `list_bundles`,
  `get_bundle`, `read_bundle_manifest`, `read_log`, `list_tasks`,
  `search_world`, `search_walnut`.
- **First call succeeds.** The response contains a JSON envelope with
  a `walnuts` array whose entries match your world's actual walnut
  set. Against the fixture world you should see 3 walnuts.
- **No stderr spam in the log.** Claude Desktop writes per-server logs
  to `~/Library/Logs/Claude/` on macOS; the filename pattern is
  `mcp-server-<something>.log` and the exact `<something>` can differ
  by Claude Desktop build. Open the most recently modified
  `mcp-server-*.log` whose contents reference ALIVE / alive-mcp:

  ```bash
  ls -lt ~/Library/Logs/Claude/mcp-server-*.log | head -5
  grep -l alive-mcp ~/Library/Logs/Claude/mcp-server-*.log
  ```

  Startup messages should be structured JSON; there should be no
  Python tracebacks, no `ImportError`, no warnings about missing
  dependencies.

If all three check out, one-click install is working.

## Phase 3 — Uninstall

**3.1 Open the connection entry.** `Settings → Connections → Developer
→ ALIVE → Uninstall` (or the trash-icon affordance, depending on
Claude Desktop build).

**3.2 Confirm removal.** After confirming, the entry disappears from
the Connections list. The server process should terminate within a
second or two.

**3.3 Verify clean uninstall.**

- `~/Library/Application Support/Claude/mcp/alive-mcp/` (or the
  platform-equivalent install path) is removed.
- `pgrep -f alive-mcp` returns empty.
- The next invocation of `list_walnuts` fails with "tool not
  available" rather than hitting a zombie server.

Reinstall should succeed immediately afterward — idempotent install
path is part of the contract. Install / uninstall / reinstall and
confirm the first tool call still works.

## Acceptance checklist

Map each of these to the `fn-10-60k.17` acceptance criteria. All six
must be checked before the release ships.

- [ ] `scripts/build-mcpb.sh --validate` (run from the `alive-mcp/`
      package root) produces `dist/alive-mcp-0.1.0.mcpb` under 500KB
      (current build ~220KB).
- [ ] `mcpb validate mcpb/manifest.json` (run from the `alive-mcp/`
      package root) passes with no errors.
- [ ] Drag-and-drop install in Claude Desktop (macOS): server appears
      in `Settings → Connections → Developer` with status **Connected**.
- [ ] First tool call from a Claude Desktop conversation (`list_walnuts`)
      returns a correct response against the configured world.
- [ ] Uninstall via `Settings → Connections` removes the server
      cleanly; no orphaned process, no residual files under the
      Claude Desktop app-support directory.
- [ ] This verification doc is updated with screenshots (or explicitly
      marked screenshot-later) on the release PR before tagging.

## Troubleshooting

**Server status red in Settings → Connections.**

Open the most recent `mcp-server-*.log` under `~/Library/Logs/Claude/`
(see "No stderr spam in the log" above for how to find it). The top
of the log tells you why the server refused to start. Common cases:

| Log snippet | Fix |
|-------------|-----|
| `uv: command not found` | Install `uv` (see Prerequisites step 2) and restart Claude Desktop so the app picks up the updated PATH. |
| `ALIVE world not configured` | Set `ALIVE_WORLD_ROOT` in the server's environment config. |
| `world path does not exist` | The path you set is wrong. Verify with `ls "$ALIVE_WORLD_ROOT"` in a terminal. |
| `ModuleNotFoundError: No module named 'mcp'` | uv sync did not complete. Check `uv` version (≥0.4 recommended) and rerun: uninstall the bundle, reinstall, restart. |

**Tool call fails with an error envelope.**

alive-mcp returns structured error envelopes rather than dropping the
connection. The `code` field tells you which category of failure you
are hitting; see `docs/error-codes.md` for the full taxonomy. The most
common first-run error is `world_not_configured` (fix: step 1.3) or
`path_escape` (fix: the world path you set is outside your home
directory and triggers the path safety guard — move the world into
your home or override the guard explicitly per the README).

**First tool call hangs.**

First launch can take a few seconds while `uv` resolves the dep graph
against `uv.lock`. If it hangs longer than ~30 seconds, check the log
file — usually uv is rebuilding a wheel for your platform. Subsequent
launches hit the uv cache and start instantly.

## Rebuild + re-verify loop

While iterating on the manifest or the bundle shape:

```bash
# Run from the alive-mcp/ package root.

# 1. Rebuild
scripts/build-mcpb.sh --validate

# 2. Uninstall the previous version in Claude Desktop (Phase 3)

# 3. Drag the new .mcpb onto Claude Desktop (Phase 1)

# 4. Smoke test (Phase 2)
```

The build script is idempotent and fast (under 5 seconds on a warm
machine); the full verify loop takes about a minute per iteration.

## Notes for release sign-off

This verification is part of the release gate defined in
`fn-10-60k.18`. The release PR must link the commit range that was
verified, and the verifier's sign-off comment should include:

- Claude Desktop build number used for verification
- Platform (macOS / Windows) + OS version
- Bundle hash (`shasum -a 256 dist/alive-mcp-0.1.0.mcpb`)
- Pass/fail on each acceptance checkbox above

Screenshots are optional at this stage (v0.1 is a small-audience
release). The doc is structured so screenshots can be backfilled
later without rewriting the text — each Phase step stands alone.
