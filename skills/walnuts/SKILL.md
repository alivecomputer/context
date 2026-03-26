---
name: walnuts
description: Load ALIVE walnut context — structured project memory across sessions. Read priorities, save decisions, manage tasks across your world.
version: 1.0.0
platforms: [macos, linux]
metadata:
  hermes:
    tags: [walnut, context, planning, priorities, memory]
---

# Walnuts — behavioral protocol for walnut MCP tools

## What This Does

A walnut is a self-contained unit of context — a project, person, or life area — with identity, state, history, tasks, and knowledge. Plain markdown files on the human's machine. You interact with them through five MCP tools:

| Tool | Purpose |
|------|---------|
| `walnut_read` | Load a walnut's brief pack (key, now, tasks, insights, log, capsules) |
| `walnut_list` | List all walnuts with domain, goal, phase, and health |
| `walnut_save` | Save progress: prepend log entry, update state, manage tasks |
| `walnut_capture` | Capture content into a capsule or the world's Inputs folder |
| `walnut_create` | Create a new walnut with full directory structure and core files |

The tools handle file operations. This skill tells you WHEN and HOW to use them.

---

## Read Before Speaking

This is non-negotiable.

Before responding about any project, person, or life area:

1. **Know which walnut** — if you're unsure, call `walnut_list` first to see what exists.
2. **Load the walnut** — call `walnut_read` with the walnut name. This returns the brief pack: identity, current state, recent history, open tasks, capsules, and insights.
3. **Reference specifics** — after loading, use concrete details from the brief pack. Mention the current phase, open tasks, recent log entries. Show that you know where things stand.
4. **Never guess** — if you haven't read it this session, you don't know the current state. Read it.

If the human mentions something and you're not sure which walnut it belongs to, say so and offer to check: "Let me pull up your walnuts to find where that lives."

---

## The Stash (interactive sessions)

When working with a human in conversation, maintain a running mental list of things that need saving. Three types:

- **Decision** — the human decided something ("Let's go with Stripe for payments")
- **Task** — something to do was identified ("Need to write the onboarding email")
- **Note** — context worth preserving ("The API rate limit is 1000/hr on free tier")

### How to stash

- When you spot a decision, task, or note: track it mentally with the destination walnut.
- Surface the add briefly so the human knows it's captured:

```
╭─ 🐿️ stash
│  Decision: Go with Stripe for payments → payments-walnut
╰─
```

- Don't ask permission to stash. Just note it and move on.
- If you're unsure which walnut something belongs to, flag it: "This could go to [walnut-a] or [walnut-b] — I'll route it at save."

### Stash is temporary

The stash lives only in conversation memory. It becomes real when you save. If the session ends without saving, the stash is lost. This is why the save protocol exists.

---

## Save Protocol

### When to save

Surface the need when any of these are true:

- Stash has 5 or more items
- 30+ minutes have passed since last save
- There's a natural pause in conversation
- The human is wrapping up
- The human asks to save

### How to save

1. **Surface the stash** — show the human what you've collected:

```
╭─ 🐿️ save checkpoint (7 items)
│
│  Decisions:
│  1. Go with Stripe for payments
│  2. Launch date moved to April 15
│
│  Tasks:
│  3. [+] Write onboarding email
│  4. [+] Set up Stripe test account
│  5. [✓] Fix the auth bug (done in this session)
│
│  Notes:
│  6. API rate limit is 1000/hr on free tier
│  7. Will wants to review the design before launch
│
│  Saving to: payments-walnut
╰─
```

2. **Get confirmation** — "Save this?" One question, not a negotiation.

3. **Call `walnut_save`** with:
   - `walnut`: the target walnut name
   - `logEntry`: a narrative paragraph of what happened. Include decisions and their rationale, not just what was decided but why. Write it so a future agent (or the human in 3 months) can understand the full context.
   - `phase` / `next` / `capsule`: update these if they changed during the session. Don't touch them if they didn't.
   - `addTasks`: new tasks identified (array of strings)
   - `completeTasks`: tasks finished (array of strings matching existing task text)

4. **Reset the stash** — after a successful save, the stash is empty.

### Multi-walnut saves

If stash items route to different walnuts, make separate `walnut_save` calls for each. Surface the routing before saving so the human can correct it.

---

## Cron Jobs (autonomous mode)

When running as a scheduled task with no human present:

1. **Start** — call `walnut_read` for the relevant walnut. Understand current priorities, open tasks, and recent context before doing any work.
2. **Do the work** — research, analysis, monitoring, whatever the job is.
3. **End** — call `walnut_save` with:
   - A clear log entry summarizing what was found or done
   - Task updates if applicable
   - Phase/next updates if the findings change the trajectory

No stash needed in cron mode. The work is self-contained — just read at start, save at end.

Route findings to the correct walnut based on subject matter, not which walnut triggered the job. If research for walnut-a reveals something relevant to walnut-b, save to both.

---

## Capture

When content arrives that should be preserved — pasted text, research results, transcripts, images, links:

1. Call `walnut_capture` with:
   - `walnut`: the target walnut
   - `content`: the full content to save
   - `filename`: a descriptive name (e.g., `competitor-pricing-march-2026.md`)
   - `description`: one-line summary of what this is
   - `capsule` (optional): if you know which capsule it belongs to, specify it

2. If you don't know the capsule, omit it — the content routes to `03_Inputs/` for later sorting.

3. If the human pastes something without context, ask: "Which walnut does this belong to?" before capturing.

---

## Creating New Walnuts

When the human mentions a project, person, or area that doesn't have a walnut:

1. **Recognize it** — if they're talking about something new that has ongoing context, it probably needs a walnut.
2. **Surface it, don't assume**:

```
╭─ 🐿️ new walnut?
│  "Side project with James" doesn't have a walnut yet.
│  Want me to create one?
╰─
```

3. **On confirmation**, call `walnut_create` with:
   - `name`: short, lowercase, hyphenated (e.g., `james-side-project`)
   - `domain`: which life area (work, personal, health, finance, learning, etc.)
   - `goal`: one sentence — what is this walnut for?
   - `type`: project, person, or area

4. After creation, immediately `walnut_read` the new walnut to confirm it's set up, then continue the conversation with it loaded.

---

## Visual Conventions

For context operations — stash adds, save summaries, spotted observations — use bordered blocks to visually separate them from regular conversation:

```
╭─ 🐿️ [type]
│  [content]
╰─
```

Types: `stash`, `save checkpoint`, `new walnut?`, `spotted`, `loaded`

Use `spotted` when you notice something in conversation that connects to a loaded walnut's priorities or open tasks:

```
╭─ 🐿️ spotted
│  "Reduce onboarding time" is an open task in growth-walnut.
│  The approach you just described would close it.
╰─
```

These blocks are opt-in visual sugar. If the client doesn't render them well, drop them and use plain text.

---

## Zero-Context Standard

Every log entry you write via `walnut_save` must pass this test:

> If a brand new agent loaded this walnut cold — no prior conversation history, no memory of this session — would it have everything it needs to understand what happened and continue the work?

This means:
- **Name people** — "Will reviewed the design" not "he reviewed it"
- **State rationale** — "Chose Stripe because it handles international payments and Will has experience with it" not "Chose Stripe"
- **Include numbers** — "API rate limit is 1000 requests/hour on free tier" not "there's a rate limit"
- **Reference artifacts** — "Captured competitor pricing in competitor-pricing-march-2026.md" not "saved the research"

Err on the side of too much context. Storage is cheap. Lost context is expensive.

---

## One Walnut, One Focus

Don't mix context from multiple walnuts in the same thread of work. If you have one walnut loaded and the conversation drifts to another topic:

1. **Recognize the drift** — "This sounds like it's about [other-walnut], not [current-walnut]."
2. **Surface it**:

```
╭─ 🐿️ spotted
│  This relates to marketing-walnut, not the payments-walnut we have loaded.
│  Want me to load it?
╰─
```

3. **Save first if needed** — if there's stash for the current walnut, offer to save before switching.
4. **Load the new walnut** — call `walnut_read` for the relevant one.

It's fine to reference other walnuts in passing. The rule is about active work — don't write log entries or tasks to the wrong walnut.

---

## Quick Reference

| Situation | Action |
|-----------|--------|
| Human mentions a project | `walnut_read` before responding |
| Don't know which walnut | `walnut_list` to find it |
| Human makes a decision | Stash it with destination |
| Human identifies a task | Stash it with destination |
| 5+ stash items or 30+ min | Surface save checkpoint |
| Content to preserve | `walnut_capture` to right walnut |
| New project/person/area | Offer `walnut_create` |
| Conversation drifts | Surface the drift, offer to switch |
| Cron job starts | `walnut_read` first, `walnut_save` at end |
| Writing a log entry | Apply zero-context standard |
