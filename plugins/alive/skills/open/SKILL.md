---
name: open
description: "Use when the human names a walnut, says 'let's work on', 'open', 'switch to', 'resume', or 'where was I' — loads that walnut's core files in sequence, surfaces a spark, and establishes single-walnut focus for the session."
user-invocable: true
---

# Open

Single-walnut focus. Load one walnut. See where things are. Work.

---

## If No Walnut Named

Show available walnuts as a numbered list grouped by domain:

```
╭─ 🐿️ pick a walnut
│
│  Life
│   1. identity         active    Mars visa application
│   2. health           quiet     Sleep study results
│
│  Ventures
│   3. nova-station      active   Orbital test window
│   4. paper-lantern     quiet    Menu redesign
│
│  Experiments
│   5. midnight-frequency active  Episode 12 edit
│   6. glass-cathedral   waiting  Decide: gallery or festival
│
│  number to open, or name one.
╰─
```

## Load Sequence

Read in order (show `▸` reads):

1. `_core/key.md` — what this walnut is
2. `_core/now.md` — where it is right now
3. `_core/insights.md` — frontmatter scan (what domain knowledge exists)
4. `_core/tasks.md` — current task queue
5. `_core/_squirrels/` — any unsigned entries?
6. `_core/_working/` — **frontmatter only** (scan what drafts exist, don't read their full content)
7. `_core/_references/` — **frontmatter only** (scan what's been captured, not the full companions)

```
▸ key.md       Nova Station — orbital tourism platform, weekly rhythm
▸ now.md       Phase: testing. Next: review telemetry from test window.
▸ insights     3 sections (engineering, regulatory, partners)
▸ tasks        2 active, 1 urgent, 4 to do
▸ _squirrels/  1 unsigned entry (empty — safe to clear)
▸ _working/    2 drafts (launch-checklist-v0.2, safety-brief-v0.1)
▸ _references/ 8 companions (3 transcripts, 3 emails, 2 research)
```

## The Spark

One observation the human might not have noticed. A connection, a question, a nudge.

```
╭─ 🐿️ spark
│  Ryn hasn't been mentioned in 8 days but there are 2 telemetry
│  reports from her team sitting in email. Might be test results.
╰─
```

If there's not enough context for a genuine spark, skip it. An obvious one is worse than none.

## Then Ask

```
╭─ 🐿️ nova-station
│  Goal:    Build the first civilian orbital tourism platform
│  Phase:   testing
│  Next:    Review telemetry from test window
│
│  Load full context, or just chat?
╰─
```

"Load context" reads log frontmatter, recent entries, linked walnuts.
"Just chat" starts freestyle — the squirrel loads more later if needed.

## During Work

- Stash in conversation (see squirrels.md). No file writes except capture + _working/.
- Always watching: people updates, _working/ fits, capturable content.
- When a _working/ file looks shareable → offer to promote to v1.

## Cross-Loading

If another walnut becomes relevant during work ("this references [[ryn-okata]]"), ask before loading it. One walnut, one focus.

```
╭─ 🐿️ cross-reference
│  This mentions [[ryn-okata]]. Load her context?
╰─
```

## Unsigned Entry Recovery

If `_squirrels/` has an unsigned entry with stash items from a previous session:

```
╭─ 🐿️ previous session had 6 stash items that were never saved.
│  Review before we start?
╰─
```

If yes: present the previous stash for routing. If no: clear and move on.
