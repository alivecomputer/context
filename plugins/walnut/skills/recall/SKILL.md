---
description: Rebuild context from previous sessions. Browse, query, combine, and revive squirrel sessions — including full conversation transcripts.
user-invocable: true
triggers:
  # Direct
  - "walnut:recall"
  - "recall"
  # Resume
  - "pick up where I left off"
  - "what was I working on"
  - "continue that session"
  - "resume"
  - "revive"
  # Browse
  - "what have I been doing"
  - "show me recent sessions"
  - "what happened this week"
  - "what did we do on"
  - "session history"
  # Query
  - "that creative session where"
  - "the session about"
  - "when did I last work on"
  - "find that conversation"
  # Combine
  - "combine those sessions"
  - "merge the context from"
  - "give me everything about"
  - "I need the full picture on"
  # Handoff
  - "handoff"
  - "hand off"
  - "context transfer"
  - "brief me on"
---

# Recall

Rebuild context from previous sessions. The system's memory.

Not a search (that's find — searches content). Recall searches SESSIONS — what squirrels did, what was discussed, what was decided, what context existed in those conversations.

---

## How It Works

Two tiers of data. Always start with tier 1. Go to tier 2 via revive.

**Tier 1 — Squirrel Entries** (`.home/_squirrels/*.yaml`, with fallback to per-walnut `_core/_squirrels/*.yaml`)
Structured, fast, indexed. Session ID, walnut, model, timestamps, stash items, working files, transcript path. Every squirrel leaves one of these.

**Tier 2 — Session Transcripts** (path stored in squirrel YAML `transcript_path:`)
The full conversation. Every human message, every agent response, every tool call, every result. This is the deep context — the actual thinking, the back-and-forth, the nuance that didn't make it into the stash.

---

## Modes

### Browse (no args)

Show recent sessions across all walnuts.

```
╭─ 🐿️ recall — recent sessions
│
│   1. 2a8c95e9  alive-gtm     today       opus-4-6
│      System architecture, blueprint, 8 skills built, shipped v0.1-beta
│
│   2. a44d04aa  alive-gtm     yesterday   opus-4-6
│      alivecomputer.com rebuilt, whitepaper v0.3, brand locked
│
│   3. 5551126e  alive-gtm     Feb 22      opus-4-6
│      Companion app, web installer, plugin v0.1-beta released
│
│   4. fb6ec273  alive-gtm     Feb 21      opus-4-6
│      Otter transcript extraction, website built, manifesto captured
│
│   5. 224e54bb  walnut-world  Feb 22      opus-4-6
│      walnut.world infrastructure, KV, Blob, DNS, keyphrase system
│
│  number to dive in, or describe what you're looking for.
╰─
```

#### Unsigned Sessions

After listing sessions, check for unsigned entries (`signed: false`) in `.home/_squirrels/`. These represent sessions that weren't properly closed — the conductor may have lost stash items.

Surface them with a visual marker:

```
╭─ 🐿️ recall — recent sessions
│
│   1. 2a8c95e9  nova-station  today       opus-4-6
│      System architecture, 8 skills built
│
│   2. a44d04aa  alive-gtm     yesterday   opus-4-6
│      Website rebuild, brand locked
│
│   ⚠ c48b658d  (unsigned)    today       opus-4-6
│     Session was not properly closed. Stash may be unrouted.
│     → Review and sign off?
│
│  number to dive in, or describe what you're looking for.
╰─
```

If the conductor selects an unsigned session, present the stash items from the YAML and offer to route them now (invoke save flow for those items) or dismiss (sign the entry as-is with `ended:` set to current time).

### Query (by walnut, date, topic)

"What sessions touched nova-station this month?"
"Find the session where we discussed shielding vendors"
"Show me all creative sessions" (matches by tags or stash content)

Searches tier 1 first (squirrel YAML frontmatter + stash content). If no match, offers tier 2 search (full transcript grep).

### Revive (single session)

The conductor selects a session. Show the tier 1 metadata, then offer the choice:

```
╭─ 🐿️ recall — session:{id}
│
│  Walnut: {walnut}
│  Date: {date}, {duration}
│  Model: {engine}
│  Stash: {count} items
│  Working: {files}
│
│  → Quick revive or heavy revive?
╰─
```

Use AskUserQuestion with two options:
- **Quick revive** — "Structured briefing. One agent reads the full transcript and returns a handoff document covering what happened, why, and what comes next. ~2 minutes."
- **Heavy revive** — "Full context transplant. Five parallel agents each extract a different dimension — narrative arc, decisions, verbatim quotes, technical substance, and open threads. Reconstructs the session's awareness in the current context window. ~5 minutes."

Before dispatching either mode, resolve the transcript path (see Transcript Discovery below). If no transcript is found, tell the conductor and offer a tier-1-only summary from the YAML stash data instead.

#### Quick Revive

One agent. Reads the full JSONL transcript. Returns a structured handoff directly into the conversation.

Dispatch a single Agent tool call with `subagent_type: "general-purpose"`. Use this prompt, substituting `{transcript_path}` with the resolved path:

```
You are reconstructing a previous work session from its full transcript.

Read the JSONL transcript at: {transcript_path}

The transcript is JSONL — one JSON object per line. Each line has a "type" field
(user, assistant, tool_use, tool_result, progress, file-history-snapshot, etc.)
and typically a "message" field with content. Focus on "user" and "assistant" types
for the conversation, and "tool_use"/"tool_result" for understanding what work was done.

For tool_use entries, note what tools were called (Read, Edit, Write, Bash, Grep, Glob,
Agent, etc.) and what they operated on — this tells you what files were touched and what
commands were run. For Agent tool calls, read the prompt to understand what subagents were
dispatched and why.

Produce a structured handoff using EXACTLY this format:

# Session Revive: {Brief Description — one line}

## What You Need to Know

[1-2 paragraphs. Write this for a squirrel with ZERO memory of this session. What was the
session about? What walnut was being worked on? What was the conductor trying to accomplish?
What state were things in when the session ended? This section alone should give enough
context to have a useful conversation about this work.]

---

## What Happened

[Full narrative of the session. Not a bullet list — a story. Cover:
- What problems were being solved and how they were approached
- Files created or modified — FULL PATHS, and what changed in each
- Code patterns, architectures, or designs established
- Tools and commands run that produced significant results
- Dead ends attempted — what was tried and why it didn't work (so the next session
  doesn't repeat mistakes)
- Subagents dispatched — what they were asked to do and what they found
- The chronological progression: what happened first, what came next, how the work evolved

Be specific. "Modified the config" is useless. "/Users/will/project/config.yaml — added
retry logic with 3 attempts and exponential backoff because the API was rate-limiting" is
useful.]

## Why

[Every decision made during the session, with:
- The decision itself
- The rationale — why this choice over alternatives
- What alternatives were considered and why they were rejected
- Constraints that drove the decision (technical, time, preference)
- User preferences expressed (explicitly or implicitly)
- Pivots — moments where the approach changed, and what triggered the change
- Principles established — rules or patterns decided that should carry forward

This section is critical. The log tells you WHAT was decided. This tells you WHY.]

## What Comes Next

[Exact numbered next steps, ordered by priority:
1. Step one — with enough detail to execute without re-reading the transcript
2. Step two — etc.

Also include:
- Unfinished work and its exact state (what's done, what remains)
- Gotchas and warnings — things that will bite the next session if forgotten
- Dependencies — things that need to happen before other things can proceed
- Context that exists nowhere else — information that was discussed but not written
  to any file, and would be lost without this extraction]

Be thorough. Read the ENTIRE transcript. Do not stop early. If the session spanned
multiple topics or phases, cover all of them.
```

Present the agent's output in a bordered block:

```
╭─ 🐿️ revive — quick
│  [agent output]
╰─
```

#### Heavy Revive

Full context transplant. Five parallel agents each extract a different dimension from the transcript — narrative arc, decisions, verbatim quotes, technical substance, and open threads.

When the conductor selects heavy revive, invoke the `walnut:heavy-revive` skill via the Skill tool, passing the resolved `{transcript_path}`. That skill contains the full agent prompts and dispatch instructions. Do NOT attempt to run heavy revive without loading that skill first.

### Combine (multiple sessions → one context pack)

The power move. Pick 2-5 sessions and merge their context.

"Give me everything from the last 3 alive-gtm sessions"
"Combine the shielding research session with yesterday's vendor call"

```
╭─ 🐿️ recall — combining 3 sessions
│
│  Loading:
│   ▸ 2a8c95e9 — system architecture (today)
│   ▸ a44d04aa — website rebuild (yesterday)
│   ▸ 5551126e — plugin shipping (Feb 22)
│
│  Combined context: 23 decisions, 8 tasks, 4 insights
│  Full transcripts available (142,000 tokens total)
│
│  What do you need this context for?
╰─
```

That last question is key: **the handoff is targeted.** You're not just dumping everything. The system builds the context pack around WHY you need it. "I need to write the investor deck" gets different context emphasis than "I need to debug the hook scripts."

### Handoff (context → new session)

Generate a context briefing that a new squirrel can load to continue the work. This is the output — a structured document that captures:

- Decisions and rationale (from log entries + stash)
- Current state (from now.md)
- Open questions (from stash + conversation)
- Working files in progress
- Key relationships and people context
- The specific reason for the handoff

Written to `_core/_working/recall-[date]-[topic].md` and optionally loaded directly into a new session.

```
╭─ 🐿️ handoff ready
│
│  _core/_working/recall-2026-02-24-investor-deck.md
│
│  Context from 3 sessions, focused on: investor deck preparation
│  12 decisions, 4 open questions, 3 working files referenced
│
│  Start a new session with this loaded?
╰─
```

---

## Transcript Discovery

Different platforms store session data in different places. The squirrel resolves the transcript path before dispatching revive agents.

**Resolution order:**

1. Check `transcript_path:` in the squirrel YAML entry
2. **Fallback:** Scan `~/.claude/projects/*/` for `{session_id}*.jsonl` — Claude Code names transcripts using the session UUID
3. If no transcript found: tell the conductor, offer tier-1-only summary from the YAML stash data (no revive possible without a transcript)

**Known platforms:**

| Platform | Transcript path | Format |
|----------|----------------|--------|
| Claude Code | `~/.claude/projects/<hash>/<session>.jsonl` | JSONL (messages + tool calls) |
| Cursor | `~/.cursor/workspaceStorage/*/state.vscdb` | SQLite |
| Windsurf | `~/.windsurf/` | Varies |
| Codex | Platform-dependent | Varies |
| ChatGPT | Export only | JSON archive |
| Local models | No standard | Agent-dependent |

**The squirrel entry is the universal layer.** It works across every platform. Transcripts are a bonus when the platform supports them. The system never breaks if transcripts aren't available — it just has less depth.

**Privacy note:** Transcripts live on the conductor's machine in Claude Code's project directory. They never leave. Recall reads them locally.

---

## What Recall Is NOT

- Not `walnut:find` — find searches content across walnuts. Recall searches sessions.
- Not `walnut:housekeeping` — check surfaces broken things. Recall surfaces past context.
- Not a log viewer — log.md has the signed record. Recall has the full conversation.

The log tells you WHAT was decided. Recall tells you WHY, in context, with every exchange that led to it.
