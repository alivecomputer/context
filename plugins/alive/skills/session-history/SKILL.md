---
name: alive:session-history
description: "Revive sessions (quick or heavy), browse, and search — 'what happened recently?', 'find the session where we discussed X', 'revive yesterday's session'. For single-session recall and multi-session browsing. If the human needs to merge multiple sessions into one working context or detect conflicts between parallel sessions, use alive:session-context-rebuild instead."
user-invocable: true
---

# History

Session timeline and recent activity. The system's short-term memory.

Not a search (that's find — searches content across walnuts). History searches SESSIONS — what squirrels did, what was discussed, what was decided. For deep context extraction from source material, use `alive:mine-for-context`.

---

## How It Works

Two tiers of data. Always start with tier 1. Go to tier 2 via revive.

**Tier 1 — Squirrel Entries** (`.alive/_squirrels/*.yaml`, with fallback to per-walnut `_kernel/_squirrels/*.yaml`)
Structured, fast, indexed. Session ID, walnut, model, timestamps, stash items, transcript path. Every squirrel leaves one of these.

**Tier 2 — Session Transcripts** (path stored in squirrel YAML `transcript:`)
The full conversation. Every message, every agent response, every tool call, every result. This is the deep context — the actual thinking, the back-and-forth, the nuance that didn't make it into the stash.

---

## Modes

### Browse (no args)

Show recent sessions across all walnuts. **Default filter:** only show sessions with `saves > 0` or non-empty stash. Cap at 10 results. Add a summary line: "N empty shells not shown." Use `history --shells` to include them.

**Data source:** prefer `.alive/_index.yaml` `recent_sessions:` as primary data. Fall back to direct `.alive/_squirrels/` YAML walk only if the index is missing or returns fewer results than requested.

```
╭─ squirrel history — recent sessions
│
│   1. 2a8c95e9  nova-station     today       opus-4-6
│      System architecture, telemetry pipeline, 8 modules configured, shipped v0.1-beta
│
│   2. a44d04aa  nova-station     yesterday   opus-4-6
│      Supply chain mapped, vendor shortlist locked, timeline set
│
│   3. 5551126e  nova-station     Feb 22      opus-4-6
│      Companion app, test harness, integration v0.1-beta released
│
│   4. fb6ec273  nova-station     Feb 21      opus-4-6
│      Transcript extraction, dashboard built, requirements captured
│
│   5. 224e54bb  stellarforge  Feb 22      opus-4-6
│      stellarforge infrastructure, telemetry, comms, access system
│
│  number to dive in, or describe what you're looking for.
╰─
```

#### Unsigned Sessions

After listing sessions, check for unsaved entries (`saves: 0`) in `.alive/_squirrels/`. These represent sessions that weren't properly closed — the human may have lost stash items.

Surface them with a visual marker:

```
╭─ squirrel history — recent sessions
│
│   1. 2a8c95e9  nova-station  today       opus-4-6
│      System architecture, 8 skills built
│
│   2. a44d04aa  nova-station     yesterday   opus-4-6
│      Website rebuild, brand locked
│
│   unsaved c48b658d  (unsaved)    today       opus-4-6
│     Session was not properly closed. Stash may be unrouted.
│     -> Review and sign off?
│
│  number to dive in, or describe what you're looking for.
╰─
```

If the human selects an unsaved session, present the stash items from the YAML and offer to route them now (invoke save flow for those items) or dismiss (dismiss the entry by setting `ended:` to current time and `saves:` to -1 (dismissed)).

### Query (by walnut, date, topic, person)

"What sessions touched nova-station this month?"
"Find the session where we discussed shielding vendors"
"Show me all sessions with Jax"
"What happened last week?"

Search tier 1 first (squirrel YAML frontmatter + stash content). If no match, offer tier 2 search (full transcript grep).

#### Temporal Queries

"What happened last week" -> filter squirrel entries + log entries by date range, show across all active walnuts.

"What changed since Tuesday" -> scan `_kernel/now.json` updated timestamps + recent log entries.

"History of nova-station" -> show `_kernel/log.md` frontmatter (entry count, summary) + offer to load recent entries.

### Revive (single session)

The human selects a session. Show the tier 1 metadata, then offer the choice:

```
╭─ squirrel history — session:{id}
│
│  Walnut: {walnut}
│  Date: {date}, {duration}
│  Model: {engine}
│  Stash: {count} items
│
│  -> Quick revive or heavy revive?
╰─
```

Use AskUserQuestion with two options:
- **Quick revive** — "Structured briefing. One agent reads the full transcript and returns a handoff document covering what happened, why, and what comes next. ~2 minutes."
- **Heavy revive** — "Full context transplant. Five parallel agents each extract a different dimension — narrative arc, decisions, verbatim quotes, technical substance, and open threads. Reconstructs the session's awareness in the current context window. ~5 minutes."

Before dispatching either mode, resolve the transcript path (see Transcript Discovery below). If no transcript is found, tell the human and offer a tier-1-only summary from the YAML stash data instead.

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
session about? What walnut was being worked on? What was the human trying to accomplish?
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

Be specific. "Modified the config" is useless. "/path/to/config.yaml — added
retry logic with 3 attempts and exponential backoff because the API was rate-limiting" is
useful.]

## Why

[Every decision made during the session, with:
- The decision itself
- The rationale — why this choice over alternatives
- What alternatives were considered and why they were rejected
- Constraints that drove the decision (technical, time, preference)
- Preferences expressed (explicitly or implicitly)
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
╭─ squirrel revive — quick
│  [agent output]
╰─
```

#### Heavy Revive

Full context transplant. Five parallel agents each extract a different dimension from the transcript — narrative arc, decisions, verbatim quotes, technical substance, and open threads.

When the human selects heavy revive, read `heavy-revive.md` from this skill's directory (same folder as this file) using the Read tool. That file contains the full agent prompts and dispatch instructions. Do NOT attempt to run heavy revive without reading that file first.

### Combine (multiple sessions -> one context pack)

The power move. Pick 2-5 sessions and merge their context.

"Give me everything from the last 3 nova-station sessions"
"Combine the shielding research session with yesterday's vendor call"

```
╭─ squirrel history — combining 3 sessions
│
│  Loading:
│   > 2a8c95e9 — system architecture (today)
│   > a44d04aa — supply chain mapping (yesterday)
│   > 5551126e — integration shipping (Feb 22)
│
│  Combined context: 23 decisions, 8 tasks, 4 insights
│  Full transcripts available (142,000 tokens total)
│
│  What do you need this context for?
╰─
```

That last question is key: **the handoff is targeted.** Not just dumping everything. The system builds the context pack around WHY it's needed. "I need to write the investor deck" gets different context emphasis than "I need to debug the hook scripts."

### Handoff (context -> new session)

Generate a context briefing that a new squirrel can load to continue the work. This is the output — a structured document that captures:

- Decisions and rationale (from log entries + stash)
- Current state (from `_kernel/now.json`)
- Open questions (from stash + conversation)
- Bundle drafts in progress
- Key relationships and people context
- The specific reason for the handoff

Written to `_inbox/raw/recall-[date]-[topic].md` and optionally loaded directly into a new session.

```
╭─ squirrel handoff ready
│
│  _inbox/raw/recall-2026-02-24-investor-deck.md
│
│  Context from 3 sessions, focused on: investor deck preparation
│  12 decisions, 4 open questions, 3 bundles referenced
│
│  Start a new session with this loaded?
╰─
```

---

## Mining Escalation

When history finds sessions with rich, unmined context — long sessions, many stash items, transcripts with extensive research or decision-making — suggest escalation to `alive:mine-for-context`:

```
╭─ squirrel these sessions look rich
│  3 sessions from last week have 45+ stash items and long transcripts.
│  Want to deep-mine them for people, decisions, and patterns?
│
│  -> Mine them (alive:mine-for-context)
│  -> Just the summary is fine
╰─
```

This is the bridge between "what happened?" (history) and "what's buried in there?" (mine).

---

## Transcript Discovery

Different platforms store session data in different places. Resolve the transcript path before dispatching revive agents.

**Resolution order:**

1. Check `transcript:` in the squirrel YAML entry
2. **Fallback:** Scan `~/.claude/projects/*/` for `{session_id}*.jsonl` — Claude Code names transcripts using the session UUID
3. If no transcript found: tell the human, offer tier-1-only summary from the YAML stash data (no revive possible without a transcript)

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

**Privacy note:** Transcripts live on the human's machine in Claude Code's project directory. They never leave. History reads them locally.

---

## What History Is NOT

- Not `alive:search-world` — search-world searches content across walnuts. History searches sessions.
- Not `alive:system-cleanup` — system-cleanup surfaces broken things. History surfaces past context.
- Not `alive:mine-for-context` — mine-for-context does deep extraction from source material. History is the lighter "what happened recently?"
- Not a log viewer — `_kernel/log.md` has the signed record. History has the full session timeline and can revive transcripts.

The log tells you WHAT was decided. History tells you WHEN, and can revive the WHY from transcripts.
