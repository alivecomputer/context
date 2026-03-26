# walnut 🐿️

Your AI forgets you. Walnut fixes that.

---

Every AI conversation starts cold. Your agent doesn't know your projects, your decisions, or where you left off. Walnut gives AI agents structured context — decisions, tasks, state — that persists across sessions. Plain markdown files on your machine. Any model can read them.

## Install

```bash
# Claude Code
claude plugin install walnut@stackwalnuts

# Any MCP client (Hermes, Claude Desktop, Cursor, Windsurf, Cline)
npx @lock-in-lab/walnut setup
```

That's it. `setup` auto-detects your platforms and configures everything.

## What happens

- Your agent reads your project state before responding. Not guessing from memory — reading the files.
- Decisions, tasks, and context get caught mid-conversation and routed to the right project.
- Next session picks up exactly where you left off. No re-explaining.

## What's a walnut

A walnut is a self-contained unit of context — a project, person, or life area. Five markdown files inside `_core/`:

```
my-startup/_core/
  key.md       what it is
  now.md       where it is right now
  log.md       where it's been
  insights.md  what's known
  tasks.md     what needs doing
```

Your agent reads these before speaking about your project. Any AI model can parse them because they're just markdown with frontmatter.

## Platforms

```
Platform          Install                                              Status
────────          ───────                                              ──────
Claude Code       claude plugin install walnut@stackwalnuts            ✓
Claude Desktop    npx @lock-in-lab/walnut setup                        ✓
Cursor            npx @lock-in-lab/walnut setup                        ✓
Windsurf          npx @lock-in-lab/walnut setup                        ✓
Hermes            npx @lock-in-lab/walnut setup                        ✓
Cline             npx @lock-in-lab/walnut setup                        ✓
OpenClaw          openclaw plugins install @lock-in-lab/walnut-openclaw
```

## What's in this repo

```
plugins/walnut/          Claude Code plugin (12 skills, hooks, rules)
plugins/walnut-cowork/   Claude Co-Work plugin (coming soon)
src/                     MCP server (5 tools for any MCP client)
skills/walnuts/          Hermes skill pack
```

## The squirrel

Context operations show up as bordered blocks with 🐿️. It visually separates system operations from conversation so you always know what's walnut and what's the agent talking. Opt-out via preferences. Not a personality — a UI convention.

## Links

- [walnut.world](https://walnut.world) — the platform
- [OpenClaw plugin](https://github.com/stackwalnuts/walnut-openclaw) — for OpenClaw users
- [Lock-in Lab](https://walnut.world) — who built this

---

Built by Lock-in Lab. MIT License.
