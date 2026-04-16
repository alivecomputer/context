# alive-mcp

Read-only [Model Context Protocol](https://modelcontextprotocol.io)
server that exposes the [ALIVE Context System](https://github.com/alivecontext/alive)
to every MCP-capable agent: Claude Desktop, Cursor, Codex CLI,
Gemini CLI, Continue.dev.

v0.1 is read-only by design. Ten tools, four kernel resources per
walnut, subscriptions, audit log. No writes, no network, no phone-home.
Writes arrive in v0.2 behind explicit consent gates.

---

## Install

Pick one path. `uvx` is primary.

### uvx (primary)

```bash
uvx alive-mcp@0.1.0
```

`uvx` ships with [`uv`](https://docs.astral.sh/uv/). It resolves
alive-mcp into an ephemeral environment, pins the version, and never
touches your system Python. This is the path every config snippet in
`docs/configs/` uses.

If you do not have `uv` yet:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"  # Windows
```

### .mcpb single-click (Claude Desktop, v0.2)

A one-click `.mcpb` bundle for Claude Desktop lands in v0.2 alongside
the PyPI release. Follow the v0.1 config snippet for now
(`docs/configs/claude-desktop.json`).

### pip (manual fallback)

```bash
pip install alive-mcp==0.1.0
alive-mcp --version
```

Use this when your environment cannot run `uv` (locked-down corporate
machines, for example). You are responsible for pinning Python and
isolating the install; `uvx` handles both automatically.

### uvx from git (advanced)

Pin to a specific commit or tag (useful for testing a PR or pre-release):

```bash
uvx --from git+https://github.com/alivecontext/alive.git@v0.1.0#subdirectory=claude-code/alive-mcp alive-mcp
```

Drop `@v0.1.0` for `main`, or substitute a branch / commit SHA. The
`subdirectory=` qualifier is required because alive-mcp lives inside
the `alivecontext/alive` monorepo at `claude-code/alive-mcp/`.

---

## Configure

One config per client. Every snippet pins `alive-mcp@0.1.0` to prevent
silent upgrades, and every snippet expects an absolute path in
`ALIVE_WORLD_ROOT` (primary env var; `ALIVE_WORLD_PATH` is accepted as
a forward-compat alias, `ALIVE_WORLD_ROOT` wins if both are set).

| Client         | Config file                                                      | Snippet                               |
| -------------- | ---------------------------------------------------------------- | ------------------------------------- |
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) / `%APPDATA%\Claude\claude_desktop_config.json` (Windows) | [`docs/configs/claude-desktop.json`](docs/configs/claude-desktop.json) |
| Cursor         | `<project>/.cursor/mcp.json` or `~/.cursor/mcp.json`             | [`docs/configs/cursor.json`](docs/configs/cursor.json) |
| Codex CLI      | `~/.codex/config.toml`                                           | [`docs/configs/codex.toml`](docs/configs/codex.toml) |
| Gemini CLI     | `~/.gemini/settings.json`                                        | [`docs/configs/gemini.json`](docs/configs/gemini.json) |
| Continue.dev   | `~/.continue/config.yaml` or `<project>/.continue/config.yaml`   | [`docs/configs/continue.yaml`](docs/configs/continue.yaml) |
| ChatGPT        | Not supported in v0.1 (needs remote Streamable HTTP + OAuth)     | [`docs/configs/chatgpt.md`](docs/configs/chatgpt.md) |

> **macOS Claude Desktop users:** if the server fails to connect on first
> try with "spawn uvx ENOENT", replace `"command": "uvx"` with the
> absolute path from `which uvx`. GUI apps on macOS inherit a minimal
> PATH that excludes `~/.local/bin`. Full details in
> [troubleshooting #1](docs/troubleshooting.md#1-uvuvx-not-found-macos-claude-desktop).

### World discovery

alive-mcp serves exactly one ALIVE World per server process. It
resolves the World in this order:

1. **MCP Roots** the client sends after initialize. Each Root is walked
   upward within its own bounds, looking for a directory containing
   `.alive/` or the legacy `01_Archive/` + `02_Life/` pair. First
   match wins. v0.1 does NOT walk DOWN into Roots - a Root above a
   World is rejected with `ERR_NO_WORLD`.
2. **`ALIVE_WORLD_ROOT`** env var (the primary fallback). Every config
   snippet sets this. `ALIVE_WORLD_PATH` is accepted as a forward-
   compat alias; `ALIVE_WORLD_ROOT` wins if both are set.

Set `ALIVE_WORLD_ROOT` in your client config. Every snippet in
`docs/configs/` has the slot.

---

## Tools

Exactly ten tools, frozen for v0.1. Every tool is annotated
`readOnlyHint: true, openWorldHint: false, destructiveHint: false, idempotentHint: true`.
Every tool returns an MCP `CallToolResult` envelope of the form:

```json
{
  "content": [{"type": "text", "text": "<JSON of structuredContent>"}],
  "structuredContent": { /* ... see per-tool examples below ... */ },
  "isError": false
}
```

The **example responses below show `structuredContent` only**. The
outer `content` / `isError` wrapper is always present and follows the
spec. On error, `isError: true` and `structuredContent` carries
`{error, message, suggestions}` per [`docs/error-codes.md`](docs/error-codes.md).

The authoritative contract is the committed snapshot at
[`tests/fixtures/contracts/tools.snapshot.json`](tests/fixtures/contracts/tools.snapshot.json).
CI diffs the live server's `tools/list` output against that snapshot
on every PR - so tool names, descriptions, parameters, and annotations
cannot drift silently. The examples below are hand-written and
illustrative; they match the snapshot at release time but are not
auto-regenerated.

### `list_walnuts`

List walnuts in the active ALIVE World. Use `path` as the canonical
identifier in follow-up calls; `name` is display-only. Cursor-paginate
when `next_cursor` is non-null.

**Parameters**

- `limit: int = 50`
- `cursor: str | null = null`

**Example response**

```json
{
  "walnuts": [
    {
      "path": "02_Life/people/ben-flint",
      "name": "ben-flint",
      "domain": "02_Life",
      "goal": "Engineering lead at SpaceStation; weekly async",
      "health": "active",
      "updated": "2026-04-14T09:12:00Z"
    }
  ],
  "next_cursor": null,
  "total": 43
}
```

### `get_walnut_state`

Read the current state projection (`now.json`) for a walnut. Returns
the parsed dict: phase, updated, next, bundles, context. Does not
assemble on demand - reads what is on disk.

**Parameters**

- `walnut: str` (POSIX-relative path from the World root)

**Example response**

```json
{
  "phase": "testing",
  "updated": "2026-02-23T14:00:00",
  "bundle": "shielding-review",
  "next": "Review telemetry from test window",
  "squirrel": "2a8c95e9",
  "context": "Shielding vendor shortlisted after 3 rounds. Test window confirmed March 4."
}
```

### `read_walnut_kernel`

Read a kernel file whole. For paginated log reads use `read_log`
instead.

**Parameters**

- `walnut: str`
- `file: "key" | "log" | "insights" | "now"`

**Example response**

```json
{
  "content": "---\nwalnut: nova-station\n...",
  "mime": "text/markdown"
}
```

### `list_bundles`

List bundles in a walnut. Returns the list-view subset of manifest keys
(drops `context` and `active_sessions`). Use `path` as the canonical
bundle identifier in `get_bundle` / `read_bundle_manifest`.

**Parameters**

- `walnut: str`

**Example response**

```json
{
  "bundles": [
    {
      "path": "bundles/shielding-review",
      "name": "shielding-review",
      "goal": "Shortlist orbital shielding vendors",
      "status": "prototype",
      "updated": "2026-04-10",
      "due": "2026-05-01",
      "outcome": "one-pager",
      "phase": "review"
    }
  ]
}
```

### `get_bundle`

Read a bundle's manifest plus derived counts. Manifest carries all 9
frozen keys; missing values appear as `null`.

**Parameters**

- `walnut: str`
- `bundle: str` (relative to walnut root, e.g. `bundles/shielding-review`)

**Example response**

```json
{
  "manifest": {
    "name": "shielding-review",
    "goal": "Shortlist orbital shielding vendors",
    "outcome": "one-pager",
    "status": "prototype",
    "phase": "review",
    "updated": "2026-04-10",
    "due": "2026-05-01",
    "context": "Vendor proposals parsed; two shortlisted.",
    "active_sessions": ["a8c95e9"]
  },
  "derived": {
    "task_counts": {"active": 2, "todo": 4, "done": 3},
    "raw_file_count": 7,
    "last_updated": "2026-04-10T16:22:00Z"
  }
}
```

### `read_bundle_manifest`

Read a bundle's manifest without derived counts. Cheaper than
`get_bundle` because it skips disk scans.

**Parameters**

- `walnut: str`
- `bundle: str`

**Example response**

```json
{
  "manifest": { "name": "shielding-review", "goal": "...", "...": "..." },
  "warnings": []
}
```

### `search_world`

Substring-search every searchable file across every walnut in the
active World. Case-insensitive by default. Files > 500KB are listed in
`skipped` with reason `file_too_large`.

**Parameters**

- `query: str`
- `limit: int = 20`
- `cursor: str | null = null`
- `case_sensitive: bool = false`

**Example response**

```json
{
  "matches": [
    {
      "walnut": "04_Ventures/nova-station",
      "file": "_kernel/log.md",
      "line_number": 42,
      "content": "Discussed MCP server posture with [[ryn-okata]]",
      "context_before": "...",
      "context_after": "..."
    }
  ],
  "next_cursor": null,
  "skipped": []
}
```

### `search_walnut`

Substring-search every searchable file in a single walnut. Same shape
as `search_world`. Use `walnut` as a POSIX-relative path from the
World root.

**Parameters**

- `walnut: str`
- `query: str`
- `limit: int = 20`
- `cursor: str | null = null`
- `case_sensitive: bool = false`

**Example response**

```json
{
  "matches": [
    {
      "walnut": "04_Ventures/nova-station",
      "file": "_kernel/key.md",
      "line_number": 7,
      "content": "goal: first orbital tourism flight by 2030",
      "context_before": "type: venture",
      "context_after": "rhythm: weekly"
    }
  ],
  "next_cursor": null,
  "skipped": []
}
```

### `read_log`

Paginated read of a walnut's log with chapter-aware spanning. Unit is
ENTRIES (one `## <ISO-8601>` heading per entry), not bytes or lines.
Newest-first; `offset=0` returns the newest entry. When `offset+limit`
exceeds the active log, auto-spans into `_kernel/history/chapter-NN.md`
descending.

**Parameters**

- `walnut: str`
- `offset: int = 0`
- `limit: int = 20`

**Example response**

```json
{
  "entries": [
    {
      "timestamp": "2026-04-14T09:12:00Z",
      "walnut": "nova-station",
      "squirrel_id": "2a8c95e9",
      "body": "Shielding shortlist finalized...",
      "signed": true
    }
  ],
  "total_entries": 47,
  "total_chapters": 2,
  "next_offset": 1,
  "chapter_boundary_crossed": false
}
```

### `list_tasks`

List tasks for a walnut or a specific bundle. When `bundle` is omitted,
returns every task from kernel-level `tasks.json` plus each bundle's
`tasks.json`. When `bundle` is supplied, returns only that bundle's
tasks.

**Parameters**

- `walnut: str`
- `bundle: str | null = null`

**Example response**

```json
{
  "tasks": [
    {"id": "t1", "title": "Review vendor proposal", "status": "active"}
  ],
  "counts": {"urgent": 0, "active": 3, "todo": 5, "blocked": 1, "done": 12}
}
```

---

## Resources

alive-mcp exposes each walnut's four kernel files as MCP resources
under the custom `alive://` URI scheme:

```
alive://walnut/{walnut_path}/kernel/{file}
```

- `{walnut_path}` - POSIX relative path from the World root, percent-
  encoded per RFC 3986 path segment rules. Forward slashes are
  preserved as literal separators. Spaces become `%20`. Unicode is
  normalized to NFC before encoding.
- `{file}` - one of `key`, `log`, `insights`, `now` (literal, no
  encoding).

**Examples**

- `alive://walnut/02_Life/people/ben-flint/kernel/log` (markdown)
- `alive://walnut/04_Ventures/supernormal-systems/clients/elite-oceania/kernel/key` (markdown)
- `alive://walnut/04_Ventures/nova-station/kernel/now` (JSON)

### Subscriptions

Resources support subscribe / unsubscribe. A single `watchdog` observer
runs over the resolved World root, filters events to kernel files,
debounces 500ms per `(walnut, file)` key, and emits
`notifications/resources/updated` for any URI with at least one active
subscriber. The `.alive/_mcp/` audit tree is excluded from the observer
to prevent recursion.

### Tools vs resources

Both surfaces expose kernel data. That is deliberate:

- **Resources** - HOST-controlled attach/subscribe workflows (Claude
  Desktop's "attach as context" UI, Cursor's resource picker).
- **Tools** - MODEL-controlled parameterized retrieval (`read_log`
  with offset/limit, `search_world` with query+cursor).

Same bytes on disk, two doors. Each door is authoritative for its use
case.

---

## Security posture

v0.1 is designed around three invariants.

### 1. Read-only

The server writes ONLY to `<world>/.alive/_mcp/` (audit log + rotation).
It never writes to walnut source-of-truth files (`_kernel/*`, bundle
manifests, bundle raw material). The tool set has no capture / save /
mutate surface - that arrives in v0.2 behind explicit per-tool consent
gates modeled on the MCP spec's `destructiveHint` / `openWorldHint`
annotations.

### 2. No phone-home (CI-enforced)

alive-mcp opens no network sockets during normal operation. This is
enforced by a four-layer lock in CI (see
`.github/workflows/ci.yml` and `tests/network_block/sitecustomize.py`):

1. GitHub Actions runner uses `step-security/harden-runner` with
   `egress-policy: block` during the test phase.
2. MCP Inspector is pinned via `package-lock.json` and installed with
   `npm ci` (no `npx` dynamic fetch at test time).
3. A `sitecustomize.py` shim monkeypatches `socket.socket.__init__` in
   every server subprocess spawned by the test suite, raising
   `RuntimeError` if anything tries to open a socket.
4. The Inspector contract snapshot is diffed against a committed
   fixture to catch silent tool-description drift without needing
   network access.

If a dep introduces a phone-home, one of the four layers trips.

### 3. Path safety (CVE-2025-53109 boundary)

Every caller-provided path is `realpath`-normalized on both sides
before checking containment, using `os.path.commonpath` (NOT
`startswith`, which would incorrectly accept `<root>_sibling` as a
child of `<root>`). Symlinks whose targets resolve outside the World
are rejected with `ERR_PATH_ESCAPE`. Case-folding is deferred to v0.2;
paths that differ only in case are treated as distinct in v0.1, which
is safe (rejects accidentally rather than accepts accidentally).

### Audit log

Every tool invocation writes one JSONL entry to
`<world>/.alive/_mcp/audit.log`. Walnut paths, bundle paths, and query
strings are SHA-256 hashed by default (first 16 hex chars) - the
audit log records patterns without leaking user content. Opt-in
verbatim logging for specific walnuts is available via
`ALIVE_MCP_AUDIT_PUBLIC_WALNUT_PATHS` (canonical POSIX paths,
comma-separated). Log rotates at 10MB × 10 files.

See [`docs/error-codes.md`](docs/error-codes.md) for the full error
taxonomy and [`docs/troubleshooting.md`](docs/troubleshooting.md) for
the top install issues.

---

## Troubleshooting

The top five issues in condensed form. The full guide, with diagnostic
steps, is at [`docs/troubleshooting.md`](docs/troubleshooting.md).

1. **`uvx` not found on macOS Claude Desktop** - GUI apps inherit a
   minimal PATH. Use the absolute path from `which uvx` in your
   `command` field.
2. **Server disconnects immediately after connect** - stdout pollution
   from a shell profile banner, a pyenv shim warning, or an older `uv`
   cache message. Move banners into interactive-only shell blocks;
   upgrade `uv`.
3. **`ERR_NO_WORLD`** - set `ALIVE_WORLD_ROOT` in the client config to
   the absolute path of your World root (the directory containing
   `.alive/`).
4. **Python version mismatch** - alive-mcp pins `>=3.10,<3.14`. Python
   3.10, 3.11, 3.12, and 3.13 all work; 3.14 is excluded until the
   upstream `mcp` SDK ships validated CI against it. If your system
   Python is outside this range, install a pinned interpreter with
   `uv python install 3.12` and pass `--python 3.12` in the spawn args.
5. **`ERR_PATH_ESCAPE`** - never construct walnut paths manually. Use
   the `path` field returned by `list_walnuts` verbatim.

---

## Contributing

alive-mcp lives as a sibling package in the
[`alivecontext/alive`](https://github.com/alivecontext/alive) monorepo.
Mainline path: `claude-code/alive-mcp/`. If you clone a release
worktree (e.g. `.worktrees/alive-mcp-v0.1/alive-mcp/`) the commands
below work identically - they are all relative to the package root.

The monorepo's system Python is 3.14 for other plugin work; alive-mcp
contributors use a pinned 3.12 interpreter so the v0.1 pin is enforced
locally:

```bash
cd claude-code/alive-mcp           # or .worktrees/<branch>/alive-mcp
uv python install 3.12
uv venv --python 3.12
uv sync

# Run tests (stdlib unittest, matches the plugin convention)
uv run python -m unittest discover tests

# Run the server against a fixture World
ALIVE_WORLD_ROOT="$PWD/tests/fixtures/world-basic" uv run alive-mcp

# Inspector dev loop (LOCAL only - dynamically fetches latest)
npx @modelcontextprotocol/inspector uv run alive-mcp
```

CI runs the full test suite plus the no-phone-home lock on every PR.
Contract snapshots at `tests/fixtures/contracts/` are the single source
of truth for the tool / resource / error surface; update them with
`scripts/update-snapshots.sh` when intentionally changing the surface.

## References

- Full design: [`.flow/specs/fn-10-60k.md`](https://github.com/alivecontext/alive/blob/main/.flow/specs/fn-10-60k.md)
  in the monorepo root.
- Research bundle: [`alive-mcp-research/`](https://github.com/alivecontext/alive/tree/main/alive-mcp-research) -
  protocol decisions, client capability matrix, SDK evaluation, consent
  and security patterns.
- Strategic context (why MCP matters for ALIVE distribution):
  [`competitor-mempalace/competitor-mempalace-draft-01.md`](https://github.com/alivecontext/alive/blob/main/competitor-mempalace/competitor-mempalace-draft-01.md) -
  the Moat C thesis. MCP is the distribution channel the category is
  picking; without an MCP server, ALIVE is invisible to adjacent agents.

## License

MIT. See [`LICENSE`](LICENSE).
