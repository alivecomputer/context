# walnut 🐿️

structured context for ai agents. plain files on your machine.

---

```bash
# claude code
claude plugin install walnut@stackwalnuts

# everything else (hermes, claude desktop, cursor, windsurf, cline)
npx @lock-in-lab/walnut setup
```

---

## why

we spent 7 months trying to fix the mess tiago fortes brilliant PARA framework got us into. drowning in the context we did capture, losing most of what we didnt. then ai came along and fixed retrieval — it can find anything if you structure the system right.

the next job is context capture.

your life is a series of decisions. remembering the ones you made and the ones you still have to make. thats it. thats the whole system. but your world of context cant be condensed into one monolithic MEMORY.md. each meaningful thing in your life needs its own space — your startup, your people, your health, your side project. each one has goals that dont change often, tasks that change every day, a history of decisions that compounds, and domain knowledge you uncover slowly over time.

so we give each meaningful thing a walnut.

---

## what happens when you use ai with context

youve had those conversations. where every word the model spits out is on point. where the output changes the scope of your project entirely. writes the copy perfectly. smashes the architecture. you get out exactly what you wanted or sometimes so much more.

thats what good context does.

and when that stuff happens, you need a log of the decisions and tasks that led to it. where it came from. where its going. thats what a context system gives you — not just memory, but structured memory. project-scoped. decision-aware. persistent across sessions and models.

---

## the walnut

a walnut is a unit of context. lifes got a bunch. each walnut has five system files inside `_core/`:

```
my-startup/_core/
  key.md       what it is (identity, people, links — rarely changes)
  now.md       where it is right now (rebuilt every save)
  log.md       where its been (prepend-only, immutable)
  insights.md  what it knows (evergreen domain knowledge)
  tasks.md     what needs doing (the work queue)
```

the inside of a walnut is shaped like a brain. and just like a brain, it has bundles that feed it — bundles of emails, research, quotes, files, anything related to doing a certain thing. they grow over time, get archived, or get shared.

your agent reads these files before speaking about your project. any ai model can parse them because theyre just markdown with frontmatter.

---

## the ALIVE framework

five folders. the letters are the folders. the file system IS the methodology.

```
01_Archive/       everything that was
02_Life/          personal foundation — goals, people, health
03_Inputs/        buffer only — arrives here, gets routed out
04_Ventures/      revenue intent — businesses, clients, products
05_Experiments/   testing grounds — might become a venture, might get archived
```

we discovered it accidentally while trying to rearrange PARA for ai retrieval. turns out the old hierarchy breaks down when your agent can search everything — what you need instead is a system designed around capture, routing, and structured persistence.

---

## how it works

1. **your agent reads your project state before responding.** not guessing from a flat memory file. reading the actual system files — identity, current state, tasks, recent decisions.

2. **decisions get caught mid-conversation.** the stash runs silently. when you say "lets go with react native for the mobile app" — thats a decision. it gets tagged, routed, and logged at the next save checkpoint.

3. **next session picks up exactly where you left off.** the brief pack loads in 30 seconds. your agent knows your project, your people, your last decision, and what needs doing next. no re-explaining.

---

## platforms

```
claude code       claude plugin install walnut@stackwalnuts
claude desktop    npx @lock-in-lab/walnut setup
cursor            npx @lock-in-lab/walnut setup
windsurf          npx @lock-in-lab/walnut setup
hermes            npx @lock-in-lab/walnut setup
cline             npx @lock-in-lab/walnut setup
openclaw          openclaw plugins install @lock-in-lab/walnut-openclaw
```

`setup` auto-detects which platforms you have installed and configures them. one command.

---

## whats in this repo

```
plugins/walnut/          claude code plugin (12 skills, hooks, rules)
plugins/walnut-cowork/   claude co-work plugin (coming soon)
src/                     mcp server (5 tools for any mcp client)
skills/walnuts/          hermes skill pack
```

the claude code plugin is the full experience — 12 skills including the save protocol, stash mechanic, world dashboard, context mining, and capsule lifecycle. the mcp server gives any mcp-compatible client the core read/write operations.

---

## the squirrel

context operations show up as bordered blocks with 🐿️

```
╭─ 🐿️ +1 stash (3)
│  react native for mobile app → my-startup
│  → drop?
╰─
```

this visually separates context operations from regular conversation. you always know whats the system and whats your agent talking. opt-out in preferences if you dont want it.

the squirrel isnt a personality. its a ui convention. your agent keeps its own voice.

---

## context as property

nothing phones home. your context lives on your machine as plain files. switch models — claude to gpt to local ollama — your walnuts come with you. switch platforms — claude code to cursor to hermes — same files, same context.

your context belongs to you.

---

## links

- [walnut.world](https://walnut.world) — the platform
- [openclaw plugin](https://github.com/stackwalnuts/walnut-openclaw) — for openclaw users
- [lock-in lab](https://walnut.world) — who built this

props to anthropic for making killer tools. and to the openclaw and hermes communities for building open ecosystems around open source ideas. this is the future of platforms — distributed, open, community-driven.

communities are built around their context. and so are lives. this thing is built around that.

---

built by [lock-in lab](https://walnut.world). mit license.
