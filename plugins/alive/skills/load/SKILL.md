---
description: "The human has chosen a walnut to focus on (prev. open). They're ready to work. Load the brief pack, resolve the people involved, check the active capsule — then surface one observation and ask what to work on. Context loads in tiers: walnut and people are automatic, capsule depth is offered."
user-invocable: true
---

# Load

Load a walnut. See where things are. Work.

Default: single-walnut focus. But people involved are loaded automatically (frontmatter only) — you can't work on a venture without knowing who's in it.

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
│  ▸ Number to load, or name one.
╰─
```

---

## Tier 1 — Walnut Brief Pack (automatic)

Read in order (show `▸` reads):

1. `_core/key.md` — what this walnut is (identity, people list, links, rhythm)
2. `_core/now.md` — where it is right now (phase, active capsule, next action)
3. `_core/insights.md` — frontmatter scan (what domain knowledge sections exist)
4. `_core/tasks.md` — current task queue
5. `.alive/_squirrels/` — any unsaved entries with stash for this walnut?
6. `_core/_capsules/` — **companion frontmatter only** (scan what capsules exist, their status and goal — don't read full companions yet)

**Backward compat:** Check `_core/` first for system files, fall back to walnut root. If `_core/_capsules/` doesn't exist, fall back to scanning `_core/_working/` and `_core/_references/` instead.

**Update squirrel YAML immediately:** Find the current session's YAML in `.alive/_squirrels/` (most recently created file, or match `ALIVE_SESSION_ID` env var). Set `walnut:` to the loaded walnut name.

```
▸ _core/key.md        Nova Station — orbital tourism, weekly rhythm, 3 people
▸ _core/now.md        Phase: testing. Capsule: shielding-review. Next: review telemetry.
▸ _core/insights.md   3 sections (engineering, regulatory, partners)
▸ _core/tasks.md      2 active, 1 urgent, 4 to do
▸ .alive/_squirrels/  1 unsaved entry (empty — safe to clear)
▸ _core/_capsules/    3 capsules (shielding-review: draft, launch-checklist: prototype, safety-brief: done)
```

---

## Tier 2 — People Context (automatic)

After loading the brief pack, resolve `key.md` `people:` to person walnuts. For each person listed, read their person walnut's `_core/key.md` **frontmatter only** — name, role, tags, last updated, rhythm. This is lightweight (3-5 small reads) and always happens.

```
▸ people/ryn-okata/key.md       engineering lead, updated 2 days ago
▸ people/jax-stellara/key.md    vendor contact, updated 22 days ago ⚠
▸ people/attila-mora/key.md     modes architect, updated 5 days ago
```

**If any person has relevant recent activity** — a dispatch routed from another session, a stash note tagged to this walnut, or staleness worth flagging — surface it:

```
╭─ 🐿️ people
│  Ryn Okata — engineering lead, updated 2 days ago
│    Dispatch from [[heavy-revive]]: "prefers async comms"
│  Jax Stellara — vendor contact, 22 days ago ⚠
│    Last interaction was pre-testing phase — context may be stale
│  Attila Mora — modes architect, updated 5 days ago
│    3 stash items routed here from session c2f8e7f2
│
│  ▸ Deep load anyone?
│  1. Load Attila's routed stash
│  2. Load all people context (now.md + recent log)
│  3. Just the summary above
╰─
```

**If no relevant activity:** Show the summary inline with the brief pack reads. No separate prompt — keep it lightweight.

**Resolving people to walnuts:** Match `people:` names against `02_Life/people/` folder names (kebab-case). If no walnut exists for a person, note it but don't flag — not everyone needs a person walnut.

---

## Tier 3 — Active Capsule (offered)

If `now.md` has a `capsule:` field pointing to an active capsule, offer to deep-load it. This reads the capsule's companion body (not just frontmatter), tasks, changelog, and work log.

```
╭─ 🐿️ active capsule: shielding-review
│  Status: draft (v0.3)
│  Goal: Evaluate radiation shielding vendors
│  2 active sessions: squirrel:a8c95e9 (working on v0.3)
│  3 tasks open, 1 in progress
│
│  ▸ Load capsule context?
│  1. Deep load (companion + tasks + work log)
│  2. Just the summary above
│  3. Switch to a different capsule
╰─
```

If the human picks "deep load" — read the full companion.md body, which includes:
- `## Context` — what the capsule is about
- `## Tasks` — capsule-scoped work items
- `## Changelog` — version history
- `## Work Log` — what happened in previous sessions

If `active_sessions:` shows another agent is working on this capsule, warn:

```
╭─ 🐿️ heads up
│  squirrel:a8c95e9 is currently working on v0.3 of this capsule.
│  Coordinate or work on something else to avoid conflicts.
╰─
```

---

## Spotted

One observation before asking what to work on. Fires after the load sequence, grounded in the context just loaded.

```
╭─ 🐿️ spotted
│  Ryn hasn't been mentioned in 8 days but there are 2 telemetry
│  reports from her team sitting in email. Might be test results.
╰─
```

If there's not enough context for a genuine observation, skip it. An obvious one is worse than none.

---

## Capsule Prompt

After the Spotted observation, prompt with capsule awareness:

```
╭─ 🐿️ nova-station
│  Goal:    Build the first civilian orbital tourism platform
│  Phase:   testing
│  Next:    Review telemetry from test window
│  Capsule: shielding-review (draft, draft-02)
│
│  ▸ What are you working on?
│  1. Continue from next (review telemetry)
│  2. Continue capsule (shielding-review)
│  3. Start something new (creates capsule)
│  4. Load full context (log entries, linked walnuts)
│  5. Just chat
```

If the human picks "start something new" → invoke `alive:capsule` (create operation).

If no active capsule exists, show options 1, 3, 4, 5 only (skip option 2).

**Graduation check:** When scanning `_core/_capsules/` companion frontmatter (Tier 1), also check for files matching `*-v1.md` in any capsule folder. If found and the capsule is still in `_core/_capsules/`:

```
╭─ 🐿️ graduation ready
│  shielding-review has a v1. Graduate to walnut root?
│
│  ▸ Graduate?
│  1. Yes — move to walnut root
│  2. Not yet
╰─
```

If yes → invoke `alive:capsule` (graduate operation).

---

## Then Ask (legacy — replaced by Capsule Prompt above)

If the Capsule Prompt section is used, skip this. This section remains for backward compatibility with walnuts that don't use capsules.

```
╭─ 🐿️ nova-station
│  Goal:    Build the first civilian orbital tourism platform
│  Phase:   testing
│  Next:    Review telemetry from test window
│
│  ▸ What to work on?
│  1. Continue from next (review telemetry)
│  2. Load full context (log entries, linked walnuts)
│  3. Just chat
╰─
```

"Continue from next" — jump straight into the next action.
"Load full context" — reads log frontmatter, recent entries, expands linked walnuts.
"Just chat" — freestyle, the squirrel loads more later if needed.

---

## During Work

- Stash in conversation (see squirrels.md). No file writes except capture + capsule work.
- Always watching: people updates, capsule progress, capturable content.
- People frontmatter is already loaded — use it. If someone mentioned matches a loaded person, connect the dots.
- When a capsule reaches prototype → offer to promote to published.

---

## Cross-Loading

If another walnut becomes relevant during work ("this references [[glass-cathedral]]"), ask before loading it. The primary walnut stays focused.

```
╭─ 🐿️ cross-reference
│  This mentions [[glass-cathedral]]. Load its context?
│
│  ▸ How much?
│  1. Frontmatter only (quick scan)
│  2. Full brief pack
│  3. Skip
╰─
```

---

## Unsigned Entry Recovery

If `.alive/_squirrels/` has an unsaved entry with stash items from a previous session:

```
╭─ 🐿️ previous session had 6 stash items that were never saved.
│
│  ▸ Review before we start?
│  1. Yeah, show me
│  2. Clear and move on
╰─
```

If yes: present the previous stash for routing. If no: clear and move on.

---

## Multi-Walnut Loading

The default is single-walnut focus. But `alive:load walnut-a walnut-b` is valid for cross-walnut sessions:

- **First walnut** = primary. Full brief pack + people + capsule offer.
- **Additional walnuts** = secondary. Frontmatter only (key.md + now.md frontmatter). Enough to reference, not enough to distract.

This is rare. Most cross-walnut context comes naturally from the people tier (Tier 2) — loading a venture automatically gives you lightweight context on everyone involved.
