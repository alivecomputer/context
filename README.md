<p align="center">
  <a href="https://github.com/alivecontext/alive/releases"><img src="https://img.shields.io/badge/version-3.2.0-F97316?style=flat" alt="Version"></a>
  <a href="https://github.com/alivecontext/alive/stargazers"><img src="https://img.shields.io/github/stars/alivecontext/alive?style=flat&color=F97316&label=Stars" alt="GitHub Stars"></a>
  <a href="https://github.com/alivecontext/alive/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <a href="https://x.com/ALIVE_context"><img src="https://img.shields.io/badge/𝕏-@ALIVE_context-000000?logo=x&logoColor=white" alt="@ALIVE_context"></a>
  <a href="https://discord.gg/mJvDsU9ApN"><img src="https://img.shields.io/discord/1485396210486612100?label=Discord&logo=discord&color=5865F2" alt="Discord"></a>
</p>

```
                 █████╗  ██╗     ██╗██╗   ██╗███████╗
                ██╔══██╗ ██║     ██║██║   ██║██╔════╝
                ███████║ ██║     ██║██║   ██║█████╗
                ██╔══██║ ██║     ██║╚██╗ ██╔╝██╔══╝
                ██║  ██║ ███████╗██║ ╚████╔╝ ███████╗
                ╚═╝  ╚═╝ ╚══════╝╚═╝  ╚═══╝  ╚══════╝
```

<h3 align="center">Personal Context Manager for Claude Code</h3>

<p align="center">
  <strong>The agent is replaceable. The runtime is portable. The walnut is permanent.</strong>
</p>

---

```bash
claude plugin install alive@alivecontext
```

Or from the Claude Code UI: `/plugins` → add marketplace `alivecontext/alive` → install `alive`

---

<p align="center"><img src="assets/hero-v3.gif" alt="ALIVE session demo" width="700"></p>

## How It Works

Open Claude Code at your `~/world`. The runtime is already loaded.

ALIVE structures your context into plain files on your machine. Agents read them at session start and save what matters at session end. Everything in between — the decisions, the research, the people, the knowledge — gets structure instead of dying with the session.

**Orient.** Before you say a word, the runtime has already oriented the agent — a lightweight index of your world, your preferences, and the behavioural rules that make the agent yours. When you load a walnut — a venture, an experiment, a person, a life area — three files give the agent its identity, current state, and standing knowledge. No re-explaining. No pasting context. The agent knows what you're building, who's involved, and what happened last time.

**Work.** Every AI session is a context event. You're injecting context — files, transcripts, research, screenshots. You're generating context — decisions, architecture choices, domain knowledge. You're discovering context — connections between people, patterns across projects, insights that only surface mid-conversation. Right now, all of that is ephemeral. It dies when the window closes. ALIVE catches it as it happens.

```
╭─ 🐿️ +4 stash (8)
│   Decided: React Native for mobile app              → my-startup
│   Task: Chase Jake for API specs by Friday           → my-startup
│   Note: Jake prefers async comms, hates standups     → [[jake-chen]]
│   Action: Connected ElevenLabs API for voiceover     → my-startup
│   → drop?
╰─
```

Decisions route to the log. Tasks route to the queue. People updates route to their person walnut. Actions get tracked. Nothing gets lost.

**Save.** When you checkpoint, everything routes to where it belongs — decisions to the log, tasks to the queue, knowledge to insights, people updates to their own files. External content gets captured into bundles. Scripts compute a fresh snapshot. The ephemeral becomes structural.

**Compound.** Next session inherits everything the last one produced. And the one before that. Context accumulates. Sessions build on each other instead of starting from zero.

---

## Two Units

### Walnut — unit of context

Each meaningful thing in your life gets a walnut — your startup, your people, your health, your side project.

A walnut has a kernel — three source files that move at different speeds:

```
my-startup/
  _kernel/
    key.md        → What it is (identity, people, links — rarely changes)
    log.md        → Where it's been (prepend-only, signed decisions)
    insights.md   → What it knows (evergreen domain knowledge)
    tasks.json    → What needs doing (script-operated)
    now.json      → Current state (generated on save, never hand-written)
```

The agent writes to source files. Scripts compute the projection. Judgment and aggregation stay separate.

### Bundle — unit of work

Work gets done inside bundles. Each bundle has a goal and owns its own tasks.

```yaml
# context.manifest.yaml
goal: "Ship the website from strategy deck to production"
status: prototype         # draft | prototype | published | done
```

Two species: **outcome bundles** ship a deliverable and graduate. **Evergreen bundles** accumulate context over time — meeting notes, research, reference material.

Bundles are shareable — your workflow becomes a context product anyone can install.

---

## The Runtime

The squirrel is the agent runtime — rules, hooks, skills, and policies that any AI agent inhabits when working inside your world. Why squirrel? 🐿️ Squirrels scatter-hoard — bury context now, retrieve by value later. That's the mechanic.

```
┌─────────────────────────────────────────────────┐
│          squirrel.core@3.0 runtime               │
│                                                  │
│  ┌───────────┐   ┌───────────┐   ┌───────────┐  │
│  │   Rules   │   │   Skills  │   │   Hooks   │  │
│  │ 6 files   │   │ 15 skills │   │ 13 hooks  │  │
│  └───────────┘   └───────────┘   └───────────┘  │
│                                                  │
│  ┌─────────────────────────────────────────────┐ │
│  │              PERSONA LAYER                  │ │
│  │   Named squirrel · voice · instincts        │ │
│  └─────────────────────────────────────────────┘ │
│                                                  │
│  ┌─────────────────────────────────────────────┐ │
│  │           AGENT INSTANCE                    │ │
│  │   Claude, GPT, local — interchangeable      │ │
│  └─────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────┤
│              YOUR WORLD  (v3)                    │
│                                                  │
│  Archive · Life · Inbox · Ventures · Experiments │
│     └── walnuts ──┐                              │
│                   ├── _kernel/ (identity, state)  │
│                   └── bundles  (units of work)   │
│                                                  │
│  Scripts compute projections. Agents read them.  │
│  Plain files. Your machine. Nothing phones home. │
└─────────────────────────────────────────────────┘
```

You name your squirrel. It persists across sessions — same identity, same context awareness, regardless of which model is running underneath. The runtime loads your world, the agent does the work, the save makes it permanent.

See your full world at session start with `/alive:world`. Visualise it with `/alive:my-context-graph`.

---

## The ALIVE Framework

Five domains. The file system is the methodology.

```

01_Archive/       → A — Everything that was
02_Life/          → L — Personal foundation
03_Inbox/        → I — Buffer only — arrives, gets routed out
04_Ventures/      → V — Revenue intent
05_Experiments/   → E — Testing grounds
```

---

## Session Flow

```
START ──→ Hook creates session, loads preferences
  │
  ▼
OPEN ──→ Read key.md → now.json → insights.md
  │       Agent is oriented. One observation.
  │
  ▼
WORK ──→ Stash in conversation. Capture to disk.
  │       Everything else waits for save.
  │
  ▼
SAVE ──→ Confirm stash → route to destinations
  │       Write log → update bundle → compute projection
  │       Stash resets. Back to WORK or EXIT.
  │
  ▼
EXIT ──→ Sign session. Final projection.
```

---

## Install

```bash
claude plugin install alive@alivecontext
```

Requires [Claude Code](https://docs.anthropic.com/en/docs/claude-code) + Python 3. Works on macOS, Linux, Windows (WSL).

15 skills, 13 hooks, 6 rule files, templates, and a statusline.

### Skills

| Skill | What it does |
|-------|-------------|
| `/alive:world` | See your world — dashboard, health, routing |
| `/alive:load-context` | Load a walnut — identity, state, active bundle |
| `/alive:save` | Checkpoint — route stash, generate projections |
| `/alive:capture-context` | Store external content, route to bundles |
| `/alive:bundle` | Create, share, graduate bundles |
| `/alive:search-world` | Search across walnuts, bundles, people, logs |
| `/alive:create-walnut` | Scaffold a new walnut |
| `/alive:system-cleanup` | Maintenance — stale bundles, orphan files |
| `/alive:settings` | Preferences, permissions, voice |
| `/alive:session-history` | Session timeline, squirrel activity |
| `/alive:mine-for-context` | Deep context extraction from source material |
| `/alive:build-extensions` | Create skills, rules, hooks for your world |
| `/alive:my-context-graph` | Render the world graph |
| `/alive:session-context-rebuild` | Rebuild context from past sessions |
| `/alive:system-upgrade` | Upgrade from any previous version |

### Upgrading

```bash
claude plugin install alive@alivecontext

/alive:system-upgrade --dry-run --plan-output /tmp/upgrade-plan.txt
/alive:system-upgrade
```

The upgrade skill works against any prior version (v1, v2, v3.0, v3.1, v3.2). It detects what's there via content fingerprints, shows you a plan first (`--dry-run`), backs everything up to a versioned tarball, and migrates only what needs migrating. Multi-surface aware — if you run `alive-mcp` or Hermes alongside the plugin, their migrators are dispatched too. Partial-failure resume via `--resume`. Inspection-mode rollback via `--rollback`. Your old plugin keeps working — nothing breaks.

---

## What People Are Saying

<table>
<tr>
<td width="50%" valign="top">
<br>
<p align="center"><em>"most cracked thing I've seen for AI in 2025."</em></p>
<p align="center"><strong><a href="https://linkedin.com/in/louka-ewington-pitsos-2a92b21a0">Louka Ewington-Pitsos</a></strong><br><sub>AI Researcher · Parsewave</sub></p>
</td>
<td width="50%" valign="top">
<br>
<p align="center"><em>"two AI systems, one context layer."</em></p>
<p align="center"><strong><a href="https://x.com/witcheer">witcheer ☯︎</a></strong> · <a href="https://t.me/witcheergrimoire"><sub>Telegram</sub></a><br><sub>Hermes integration pioneer</sub></p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<br>
<p align="center"><em>"You're gonna smoke everyone with this."</em></p>
<p align="center"><strong>Athon Millane</strong><br><sub>AI Researcher · VC-backed · SF</sub></p>
</td>
<td width="50%" valign="top">
<br>
<p align="center"><em>"context quality > context quantity."</em></p>
<p align="center"><strong><a href="https://x.com/mawensx">Marcus</a></strong><br><sub><a href="https://x.com/mawensx/status/2036050610420650243">original tweet</a></sub></p>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<br>
<p align="center"><em>"best thing ive ever used. this is fucked."</em></p>
<p align="center"><strong><a href="https://instagram.com/caspartremlett">Caspar Tremlett</a></strong><br><sub>Brand Business Coach · Bali/Australia</sub></p>
</td>
<td width="50%" valign="top">
<br>
<p align="center"><em>"Bro. ALIVE is legendary."</em></p>
<p align="center"><strong><a href="https://instagram.com/roland.bernath.official">Roland Bernath</a></strong><br><sub>Growth Strategist · 6K followers</sub></p>
</td>
</tr>
</table>

---

## Context as Property

Your context lives on your machine as plain files. Switch models — Claude to GPT to local — your walnuts come with you. Switch platforms — your walnuts are yours.

No cloud. No account. No vendor lock-in. Git-track it if you want version history. Or don't. Your context is your property.

---

## Roadmap

- Bundle marketplace — share and discover context products
- Obsidian plugin — walnut dashboards and context graph
- MCP server — read and manage walnuts from any MCP client
- Hermes plugin — persistent context for autonomous agents
- OpenClaw context engine — ALIVE as a context slot
- Integrations registry — track every API, service, and connection across your world

---

## A small ask

ALIVE is open source. As you use it, the plugin will surface a small invitation a handful of times — two soft inline lines and three full asks across your first sixty sessions, plus an occasional one-line nudge after `/alive:save`. After that it goes quiet, whether you star or not. The star is fired through your own `gh` CLI (browser fallback if `gh` isn't authenticated); nothing leaves your machine except the star itself. Silence it any time by setting `github_star_ask: false` in `.alive/preferences.yaml`.

## Contributing

[Open an issue](https://github.com/alivecontext/alive/issues) · [Discussions](https://github.com/alivecontext/alive/discussions) · [Contributing guide](CONTRIBUTING.md)

---

<p align="center">
  <br>
  <a href="https://alivecontext.com"><img src="https://img.shields.io/badge/🐿️_ALIVE_Context_System-alivecontext.com-F97316?style=for-the-badge&labelColor=0a0a0a" alt="ALIVE Context System"></a>
  &nbsp;&nbsp;
  <a href="https://github.com/alivecontext/alive"><img src="https://img.shields.io/github/stars/alivecontext/alive?style=for-the-badge&logo=github&labelColor=0a0a0a&color=F97316" alt="Star on GitHub"></a>
  &nbsp;&nbsp;
  <a href="https://x.com/ALIVE_context"><img src="https://img.shields.io/badge/𝕏-@ALIVE_context-F97316?style=for-the-badge&logo=x&logoColor=white&labelColor=0a0a0a" alt="Follow @ALIVE_context"></a>
  <br><br>
</p>

<p align="center">
  Built by <a href="https://lockinlab.ai">Lock-in Lab</a> · <a href="https://x.com/benslockedin">@benslockedin</a> · MIT License
</p>
