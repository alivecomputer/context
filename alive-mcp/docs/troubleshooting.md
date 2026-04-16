# Troubleshooting

The four issues that cover ~95% of v0.1 install reports, plus a few
adjacent ones that come up on specific platforms.

If you hit something not listed here, check the audit log at
`<your-world>/.alive/_mcp/audit.log` (one JSONL entry per tool call;
see `docs/error-codes.md` for the code taxonomy), then open an issue.

---

## 1. `uv`/`uvx` not found (macOS Claude Desktop)

**Symptom.** Claude Desktop shows the server as "failed to connect" or
"spawn uvx ENOENT" in its logs. Running `uvx alive-mcp@0.1.0` from the
Terminal works fine.

**Cause.** GUI apps on macOS inherit a minimal `PATH`
(`/usr/bin:/bin:/usr/sbin:/sbin`) that does not include `~/.local/bin`
or `~/.cargo/bin` where `uv` typically lives. Claude Desktop spawns the
server with that minimal PATH, so the `uvx` binary cannot be found even
though the shell can find it.

**Fix.** Give Claude Desktop the absolute path to `uvx`:

```bash
which uvx
# /Users/you/.local/bin/uvx
```

Then edit your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "alive": {
      "command": "/Users/you/.local/bin/uvx",
      "args": ["alive-mcp@0.1.0"],
      "env": {
        "ALIVE_WORLD_ROOT": "/absolute/path/to/your/world"
      }
    }
  }
}
```

Restart Claude Desktop. The server should connect on first try.

Cursor, Codex CLI, Gemini CLI, and Continue.dev all inherit your shell
PATH when they spawn MCP servers, so they do not hit this. Claude
Desktop is the only client that routinely trips on it.

---

## 2. Server connects, then immediately disconnects (stdout pollution)

**Symptom.** Tools list briefly, then vanish. Client logs show "JSON
parse error" or "unexpected token" near server startup.

**Cause.** Something the server spawn inherits is writing to stdout.
MCP stdio transport treats stdout as the JSON-RPC channel; any `print()`
output from a hook, a plugin, or `~/.pyenv/shims/python` shim corrupts
the frame and the client disconnects.

**Fix.** Check these, in order:

1. **Shell profile.** If `~/.bashrc` / `~/.zshrc` / `~/.profile` prints
   anything on login ("Welcome, patrick" style banners, `neofetch`,
   custom prompts that write to stdout rather than setting `PS1`) it
   will leak into the server's stdout. Move those to a function guarded
   on `$PS1` being set, or to the interactive-only block.
2. **Python shims.** `pyenv` sometimes prints shim-resolution warnings
   on stdout. Pin the interpreter instead:
   ```json
   "args": ["--python", "3.12", "alive-mcp@0.1.0"]
   ```
3. **`uv` cache warnings.** Older `uv` versions printed "updating
   cache" to stdout in some install flows. Upgrade `uv`:
   `curl -LsSf https://astral.sh/uv/install.sh | sh`.

Quick diagnostic: run
`uvx alive-mcp@0.1.0 < /dev/null > /tmp/stdout.log 2> /tmp/stderr.log`
for two seconds, Ctrl-C, then check `/tmp/stdout.log`. The first bytes
must be `{` (JSON-RPC). Anything else is the culprit.

---

## 3. `ERR_NO_WORLD` on startup

**Symptom.** Tools return
`{"error": "NO_WORLD", "message": "No ALIVE World could be located..."}`
on every call.

**Cause.** The server could not resolve your World root. One of:

- The client did not send MCP Roots AND `ALIVE_WORLD_ROOT` is not set
  in the server's env.
- The Root the client sent is ABOVE a World rather than AT or INSIDE it
  (e.g. Root = `$HOME` when your World is at `$HOME/world/`). v0.1 does
  not walk DOWN into Roots - that would be invasive and introduce
  multi-world ambiguity.
- The configured path does not satisfy the World predicate: it needs
  either `.alive/` OR the legacy `01_Archive/` + `02_Life/` pair.

**Fix.** Use the env fallback. Every config snippet in `docs/configs/`
has an `ALIVE_WORLD_ROOT` slot - fill it with the absolute path to your
World root:

```json
"env": {
  "ALIVE_WORLD_ROOT": "/Users/you/world"
}
```

Verify the path satisfies the predicate:

```bash
ls /Users/you/world/.alive                           # should exist
# or, for legacy worlds:
ls -d /Users/you/world/01_Archive /Users/you/world/02_Life
```

If the path resolves but you still get `ERR_NO_WORLD`, check the
server's stderr (Claude Desktop: `~/Library/Logs/Claude/mcp-server-alive.log`).
The server logs the discovery decision and the reason each candidate
was rejected.

---

## 4. Python version errors on install

**Symptom.**
`uvx alive-mcp@0.1.0` fails with either
"No solution found when resolving tool dependencies: requires
Python >=3.10,<3.14, your system is 3.14.0", or transient wheel-
availability / source-build errors on Python 3.13.

**Cause.** alive-mcp v0.1 pins `>=3.10,<3.14`. Python 3.10, 3.11, 3.12,
and 3.13 are all supported by the package. CI validates on 3.12 (the
recommended interpreter for this version). 3.13 can hit platform-
specific dependency-resolution issues on some combinations (missing
wheels triggering source builds); pinning to 3.12 sidesteps these. 3.14
is excluded until the upstream `mcp` SDK ships validated CI against it.

**Fix.** Point `uv` at a pinned 3.12 interpreter:

```bash
uv python install 3.12
```

Then pass `--python 3.12` to the spawn:

```json
"args": ["--python", "3.12", "alive-mcp@0.1.0"]
```

`uv` resolves the interpreter without touching your system Python.

---

## 5. `ERR_PATH_ESCAPE` on walnut paths

**Symptom.** A tool call with a walnut path you copied from somewhere
returns
`{"error": "PATH_ESCAPE", "message": "The requested path is outside the authorized World root..."}`.

**Cause.** alive-mcp resolves symlinks BOTH sides before comparing, and
rejects anything that resolves outside the World root. Common triggers:

- A walnut path that starts with `..` or `/`. All paths are POSIX-
  relative from the World root.
- A walnut is a symlink whose target is outside the World (e.g. you
  symlinked `~/world/04_Ventures/client` to `~/Documents/client`). v0.1
  rejects that as an escape.

**Fix.** Call `list_walnuts` and use the returned `path` field verbatim
in subsequent calls. Do not construct paths manually. If you genuinely
want a walnut outside the World, move it inside - alive-mcp is scoped
to exactly one World per process by design.

---

## 6. Audit log growing too fast

**Symptom.** `<world>/.alive/_mcp/audit.log` is rotating aggressively
(10 backups of 10MB each = 100MB cap). You want smaller or larger.

**Cause.** Expected behavior. alive-mcp v0.1 does not expose a size
knob; the default is tuned for month-scale retention of typical usage
(~5000-10000 tool calls).

**Fix.** Manually rotate or truncate the log. alive-mcp re-opens the
file on next write:

```bash
: > <your-world>/.alive/_mcp/audit.log
```

If you want the log disabled entirely, v0.1 does not offer that flag.
The audit log is a safety invariant (every invocation recorded); a
"disable audit" flag is on the v0.2 roadmap behind a loud consent gate.

---

## 7. Resource subscriptions not firing

**Symptom.** You subscribed to
`alive://walnut/04_Ventures/alive/kernel/log` and edited `log.md`, but
the client did not receive a `resources/updated` notification.

**Cause.** One of:

- Your client does not implement resource subscriptions (capability
  matrix varies across clients; Cursor and Claude Desktop do, some
  others do not).
- You edited the file inside `.alive/_mcp/` - the watchdog observer
  deliberately excludes that path to prevent recursion (the audit log
  itself lives there).
- The edit was a rename/atomic-replace pattern and the observer saw
  the create/delete as separate events. v0.1 debounces 500ms per
  `(walnut, file)` key; back-to-back renames inside that window
  collapse into one notification.

**Fix.** Verify the client advertises
`capabilities.resources.subscribe: true` at initialize time. If it
does and you still see no events, check the server's stderr for
watchdog errors. On Linux, you may be hitting inotify's per-user watch
limit (`fs.inotify.max_user_watches`); raise it with `sysctl`.

---

## Still stuck

Open an issue at https://github.com/alivecontext/alive/issues with:

- Client name and version
- OS and version
- The full stderr from the server spawn (Claude Desktop: `~/Library/Logs/Claude/mcp-server-alive.log`; Cursor: developer tools console)
- A minimal config that reproduces the failure
- The relevant JSONL entries from `<your-world>/.alive/_mcp/audit.log`
  (walnut/bundle names are hashed by default; copy and paste freely)

The audit log is the most useful artifact - it records tool names,
argument shapes, durations, and error codes with no sensitive data
leaked.
