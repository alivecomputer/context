---
name: save
description: "Use when the human says to save, checkpoint, wrap up, or route accumulated context — runs the full save protocol: confirms stash items, writes a signed log entry, updates now.md and tasks.md, dispatches cross-walnut notes, and resets the stash so the session can continue."
user-invocable: true
---

# Save

Checkpoint. Route the stash. Update state. Keep working.

Save is NOT a termination. The session continues. Save can happen multiple times. Each save increments the `saves:` counter and updates `last_saved:`. The stop hook only blocks when `saves: 0` (never saved).

---

## Flow

### 1. Read First (understand before acting)

Read these in parallel before presenting the stash or writing anything:

- `_core/now.md` — what was the previous `next:`? What was the context?
- `_core/log.md` — first ~100 lines (recent entries — what have previous sessions covered?)
- `_core/tasks.md` — current task queue

This gives the squirrel the full picture BEFORE it starts routing. It knows what was expected this session, what previous sessions accomplished, and what the task state is. This makes everything that follows smarter — better routing suggestions, better now.md synthesis, better log entries that don't duplicate what's already recorded.

### 2. Pre-Save Scan

"Anything else before I save?"

Then scan back through messages since last save for stash items the squirrel may have missed. Add them.

### 3. Confirm Stash + Next (batched)

Present the full stash visually in a single bordered block for readability, then batch confirmations into as few AskUserQuestion calls as possible.

**Display:**
```
╭─ 🐿️ save checkpoint
│
│  decisions (3)
│   1. Orbital test window confirmed for March 4  → nova-station
│   2. Ryn's team handles all telemetry review  → nova-station
│   3. Festival submission over gallery showing  → glass-cathedral
│
│  tasks (2)
│   4. Book ground control sim for Feb 28  → nova-station
│   5. Submit festival application by Mar 1  → glass-cathedral
│
│  notes (1)
│   6. Jax mentioned new radiation shielding vendor  → [[jax-stellara]]
│
│  next: was "Review telemetry from test window"
╰─
```

**Then one AskUserQuestion call with up to 4 questions — skip empty categories:**

| Question slot | Category | Options |
|---|---|---|
| 1 | Decisions | "Confirm all" / "Review list" / "Drop some" |
| 2 | Tasks | "Confirm all" / "Edit or drop" |
| 3 | Notes | "Confirm all" / "Drop some" |
| 4 | Previous next: | "Completed" / "Move to tasks, new next" / "Still the priority" |

You can select an option OR use "Other" to provide free text — editing items, adding context, changing routing, or explaining what happened. Every question supports elaboration.

**Insight candidates get a separate call** (if any exist) because they require a different decision — commit as evergreen vs just log it:

```
╭─ 🐿️ insight candidate
│   "Orbital test windows only available Tue-Thu due to
│    ISS scheduling conflicts"
│
│   Commit as evergreen insight, or just log it?
╰─
```
→ AskUserQuestion: "Commit as evergreen" / "Just log it"

If previous next: was NOT completed and is being replaced, it moves to tasks.md with context.

### 4. Write Log Entry

**Before writing anything else, prepend a signed entry to log.md.** This is the primary record of what happened. The log entry uses the standard template:

- What happened (brief narrative)
- Decisions made (with rationale — WHY, not just WHAT)
- Tasks created or completed
- References captured
- Next actions identified

**The log entry must be written BEFORE updating now.md. The log is truth. Everything else derives from it.**

### 5. Prepare Remaining Content (in memory)

**Re-read `_core/log.md` first ~150 lines** to ground the now.md synthesis in the actual written log. This captures the entry just prepended in step 4 plus the previous 3-4 entries — enough for a proper 3-5 entry synthesis. Don't rely on memory of what was read in step 1; the log has changed since then.

Then prepare the content for all remaining files in memory:

- **now.md** — full replacement: phase, health, next, updated, squirrel, context paragraph. The context paragraph synthesises the last 3-5 log entries (including the one just written) — what's been happening across sessions, not just this session. A new squirrel reading now.md should understand the full current situation without touching the log.
- **tasks.md** — new tasks added, completed marked, in-progress updated
- **Cross-walnut dispatches** — brief log entries for destination walnuts
- **insights.md** — new evergreen entries (only if confirmed in step 3)

**Protect existing now.md context.** If this session was minor but the existing now.md has rich context from a previous deep session — do NOT flatten it. Merge new information in. The test: is the new now.md MORE informative than the old one? If not, keep what was there and layer the new stuff on top.

### 6. Write Remaining Files (parallel)

Fire all remaining writes as parallel Edit calls in a single message. The content was prepared in step 5. These are independent of each other — they only depend on the log entry existing, which step 4 handled.

Parallel writes:
- `now.md` — full replacement with prepared content
- `tasks.md` — updates with prepared content
- `insights.md` — new evergreen entries (if any confirmed)
- Cross-walnut dispatches — brief log entries to destination walnut logs (if any)
- Cross-walnut task additions — tasks routed to other walnuts (if any)

### 6b. Update Squirrel Entry

Write the routed stash to the session's squirrel YAML in `.alive/_squirrels/{session_id}.yaml`. This turns the YAML from a skeleton into an actual session record.

Read the current YAML, then Edit to update:
- `alive:` — set to the active walnut name (or keep `null` if no walnut opened)
- `stash:` — replace `[]` with the routed items, tagged by type and destination:

```yaml
stash:
  - content: "Orbital test window confirmed for March 4"
    type: decision
    routed: nova-station
  - content: "Book ground control sim for Feb 28"
    type: task
    routed: nova-station
  - content: "Jax mentioned new radiation shielding vendor"
    type: note
    routed: jax-stellara
```

- `working:` — list any `_working/` files created or modified this session
- `saves:` — increment by 1 (was 0 on first save, 1 on second, etc.)
- `last_saved:` — set to current ISO timestamp

This is cumulative across saves. Each save APPENDS new items to `stash:`, it doesn't replace. The YAML becomes the full record of everything routed during the session.

### 7. Route: New Walnuts (if needed)

If any stash items require scaffolding new walnuts (new person, new venture/experiment), handle these after the parallel writes. These are heavier operations that may need their own confirmation.

- **New person** → scaffold person walnut in `02_Life/people/`
- **New venture/experiment** → scaffold walnut with `_core/`

### 8. Integrity Check

Not a vibe check. A concrete checklist. Run through each:

- [ ] **now.md** — does the context paragraph reflect the full current picture (not just this session)?
- [ ] **Log entry** — does it capture WHY decisions were made, not just WHAT?
- [ ] **tasks.md** — are new tasks added, completed tasks marked, nothing stale left as active?
- [ ] **References** — was any external content discussed this session that wasn't captured? Any research worth saving?
- [ ] **Companions** — do all references have companions with `description:` in frontmatter?
- [ ] **Insights** — did any standing domain knowledge surface that should be proposed as evergreen?
- [ ] **People** — was anyone mentioned who should have context dispatched to their walnut?
- [ ] **Working files** — are any drafts ready to promote? Any created this session that need signing?

If anything fails, fix it before completing the save. This is the last gate.

### 9. Continue

Session continues. Stash resets for next checkpoint.

```
╭─ 🐿️ saved — checkpoint 2
│  3 decisions routed to log
│  2 tasks added
│  1 dispatch to [[jax-stellara]]
│  next: updated
│  zero-context: ✓
│
│  Run alive:housekeeping? (stale walnuts, orphan refs, stale drafts)
╰─
```

The check suggestion is lightweight — one line. If the human ignores it, no friction. If they say "check" or "yeah", invoke `alive:housekeeping`.

---

## On Actual Session Exit

When the session truly ends (stop hook, explicit "I'm done done", the human leaves):

- Update the squirrel entry in `.alive/_squirrels/{session_id}.yaml`:
  - Set `ended:` to current timestamp
  - `saves:` is already > 0 from the last save
  - Set `transcript_path:` — scan `~/.claude/projects/*/` for a JSONL file containing the session ID
- Final `now.md` update
- The entry is already saved — this step adds the exit metadata

---

## Empty Save

If nothing was stashed since last save — skip the ceremony.

```
╭─ 🐿️ nothing to save since last checkpoint.
╰─
```
