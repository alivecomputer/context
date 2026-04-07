---
name: alive:load-context
description: "The human mentions a walnut to work on, asks about a specific venture/experiment/project, or wants to check status — not just explicit 'load X'. Load the brief pack (3 files), resolve the people involved, check the active bundle — then surface one observation and ask what to work on. Context loads in tiers: walnut and people are automatic, bundle depth is offered."
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

## Tier 1 — Brief Pack (3 files)

Read these three files. That's it — everything you need to orient.

1. `_kernel/key.md` — full file (identity, people, links, rhythm)
2. `_kernel/now.json` — full file (phase, next action, bundle statuses with task summaries, recent sessions, nested walnut state, blockers, context paragraph)
3. `_kernel/insights.md` — frontmatter only (what domain knowledge sections exist)

**DO NOT read any other files at this stage.** No log.md. No bundle manifests. No tasks files. No squirrel entries. All of that data is already in now.json — the projection script aggregated it. Reading source files at load wastes context window on data you already have.

**Inbox triage (background):** After reading the brief pack, check if `03_Inbox/` has items (`ls 03_Inbox/ 2>/dev/null`). If yes, dispatch a background triage agent (same spec as in the world skill — reads items, tags type/destination/priority, returns structured report). Don't wait for it — continue with people resolution and the Spark. Results arrive while you work.

Show `>` reads as you go:

```
> _kernel/key.md           Lock-in Lab — launching, weekly rhythm, 3 people
> _kernel/now.json          Phase: launching. Bundle: official-launch. Next: Draft PCM essay.
                            Active bundles: 2 (official-launch: 1 urgent, 18 todo; research: 4 todo)
                            Blockers: none. Recent: 3 sessions.
> _kernel/insights.md       4 domain knowledge sections
```

**Backward compat fallback chain:**
1. `_kernel/now.json` (v3)
2. `_kernel/_generated/now.json` (v2)
3. `now.md` at walnut root or `_core/now.md` (v1)

If a legacy format is found, surface the upgrade warning before continuing:
```
╭─ 🐿️ this walnut is on an older version
│  Found v2 state at _kernel/_generated/now.json.
│  The system works but projections, tasks, and world speed are degraded.
│
│  ▸ Run /alive:system-upgrade to migrate.
╰─
```
If NOTHING is found, the walnut has no state — read `_kernel/log.md` as last resort.

### Displaying now.json

Extract and display from now.json's structure:

- **Phase** and **next action** — `next` is an object with `action`, `bundle`, and `why` fields
- **Active bundles** — each bundle entry has task counts and flags for urgent items
- **Blockers** — surface any, or say "none"
- **Recent sessions** — count and brief summary
- **Nested walnuts** — from the `children` field, show any child walnut state worth noting

---

## Tier 2 — People Context (automatic)

After loading the brief pack, resolve `key.md` `people:` to person walnuts. For each person listed, read their person walnut's `_kernel/key.md` **frontmatter only** — name, role, tags, last updated, rhythm. This is lightweight (3-5 small reads) and always happens.

```
> people/ryn-okata/key.md       engineering lead, updated 2 days ago
> people/jax-stellara/key.md    vendor contact, updated 22 days ago !
> people/orion-vex/key.md     systems architect, updated 5 days ago
```

**If any person has relevant recent activity** — a dispatch routed from another session, a stash note tagged to this walnut, or staleness worth flagging — surface it:

```
╭─ 🐿️ people
│  Ryn Okata — engineering lead, updated 2 days ago
│    Dispatch from [[heavy-revive]]: "prefers async comms"
│  Jax Stellara — vendor contact, 22 days ago !
│    Last interaction was pre-testing phase — context may be stale
│  Orion Vex — systems architect, updated 5 days ago
│    3 stash items routed here from session c2f8e7f2
│
│  ▸ Deep load anyone?
│  1. Load Orion's routed stash
│  2. Load all people context (now.json + recent log)
│  3. Just the summary above
╰─
```

**If no relevant activity:** Show the summary inline with the brief pack reads. No separate prompt — keep it lightweight.

**Resolving people to walnuts:** Match `people:` names against `02_Life/people/` folder names (kebab-case). Legacy person walnuts at `02_Life/people/` are still recognized. If no walnut exists for a person, note it but don't flag — not everyone needs a person walnut.

---

## Tier 3 — Bundle Deep-Load (on demand)

If `now.json` has a `bundle:` field pointing to an active bundle, offer to deep-load it. The brief pack already told you the bundle name, status, task counts, and urgency — this tier gives you the full working context.

```
╭─ 🐿️ active bundle: shielding-review
│  Status: draft (v0.3)
│  Goal: Evaluate radiation shielding vendors
│  2 active sessions: squirrel:a8c95e9 (working on v0.3)
│  3 tasks open, 1 in progress
│
│  ▸ Load bundle context?
│  1. Deep load (manifest + live tasks)
│  2. Just the summary above
│  3. Switch to a different bundle
╰─
```

**Deep load reads:**

1. **`{walnut}/{name}/context.manifest.yaml`** — full file (context, changelog, work log, session history). In v3 bundles are flat at the walnut root; for legacy v2 worlds fall back to `bundles/{name}/context.manifest.yaml`.
2. **`tasks.py list --walnut {path} --bundle {name}`** — call the script for the detailed task view. Do NOT read `tasks.json` directly; the script is the interface.
3. **Write `active_sessions:` entry** to the bundle's `context.manifest.yaml` — claim this session so other agents know you're here.

If `active_sessions:` shows another agent is working on this bundle, warn:

```
╭─ 🐿️ heads up
│  squirrel:a8c95e9 is currently working on v0.3 of this bundle.
│  Coordinate or work on something else to avoid conflicts.
╰─
```

---

## Spotted

One observation before asking what to work on. Fires after the load sequence, grounded in the context just loaded.

The brief pack gives you everything: phase, bundles, tasks, blockers, recent sessions, nested walnuts. Find something worth noticing — a blocker that's been sitting, a bundle with no recent sessions, a next action that's overdue, a pattern across task counts.

```
╭─ 🐿️ spotted
│  The official-launch bundle has 1 urgent task but no sessions
│  in 4 days. The PCM essay draft might be blocking everything else.
╰─
```

If there's not enough context for a genuine observation, skip it. An obvious one is worse than none.

---

## Bundle Prompt

After the Spotted observation, prompt with bundle awareness:

```
╭─ 🐿️ nova-station
│  Goal:    Build the first civilian orbital tourism platform
│  Phase:   testing
│  Next:    Review telemetry from test window
│  Bundle:  shielding-review (draft, draft-02)
│
│  ▸ What are you working on?
│  1. Continue from next (review telemetry)
│  2. Continue bundle (shielding-review)
│  3. Start something new (creates bundle)
│  4. Go deeper (log history, linked walnuts, full insights)
│  5. Just chat
```

If the human picks "start something new" -> invoke `alive:bundle` (create operation).

If no active bundle exists, show options 1, 3, 4, 5 only (skip option 2).

---

## Then Ask (legacy — replaced by Bundle Prompt above)

If the Bundle Prompt section is used, skip this. This section remains for backward compatibility with walnuts that don't use bundles.

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

- Stash in conversation (see squirrels.md). No file writes except capture + bundle work.
- Always watching: people updates, bundle progress, capturable content.
- People frontmatter is already loaded — use it. If someone mentioned matches a loaded person, connect the dots.
- When a bundle reaches prototype -> offer to promote to published.

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

## Multi-Walnut Loading

The default is single-walnut focus. But `alive:load-context walnut-a walnut-b` is valid for cross-walnut sessions:

- **First walnut** = primary. Full brief pack + people + bundle offer.
- **Additional walnuts** = secondary. Read `_kernel/key.md` frontmatter + `_kernel/now.json` only. Enough to reference, not enough to distract.

This is rare. Most cross-walnut context comes naturally from the people tier (Tier 2) — loading a venture automatically gives you lightweight context on everyone involved.
