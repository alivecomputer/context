---
name: alive:save
description: "The human wants to checkpoint. Or: the stash has grown heavy — 5+ items, 30+ minutes, a natural pause in the work. The squirrel doesn't decide when to save. It surfaces the need and lets the human pull the trigger. Runs the full save protocol: confirms stash, writes log, updates state, generates projections, dispatches, resets."
user-invocable: true
---

# Save

Checkpoint. Route the stash. Update state. Generate projections. Keep working.

Save is NOT a termination. The session continues. Save can happen multiple times. Each save increments the `saves:` counter and updates `last_saved:`. The stop hook only blocks when `saves: 0` (never saved).

---

## Flow

### 1. Read First (understand before acting)

Read these in parallel before presenting the stash or writing anything:

- `_kernel/now.json` — what was the previous `next:`? What bundle is active? What was the context?
- `_kernel/log.md` — first ~100 lines (recent entries — what have previous sessions covered?)
- Active bundle's `context.manifest.yaml` — if `now.json` has a `next.bundle` value, read that bundle's manifest

**Do NOT read `bundles/*/tasks.md`** — task data lives in `now.json` already, or call `tasks.py list --walnut {path}` if you need specific detail.

**Backward compat:** If `_kernel/now.json` does not exist, check `_kernel/_generated/now.json` as a fallback.

**Standalone session (no walnut loaded):** If no walnut was opened this session, the squirrel still has a stash to route. Ask: "Which walnut does this session belong to?" If the human names one, load its core files and proceed normally. If truly walnut-less (system maintenance, cross-walnut work, one-off task), write the log entry to `.alive/log.md` instead of a walnut log. Same format, same signing. The squirrel YAML at `.alive/_squirrels/` keeps `walnut: null`.

This gives the squirrel the full picture BEFORE it starts routing. It knows what was expected this session, which bundle was active, what previous sessions accomplished, and what the task state is. This makes everything that follows smarter — better routing suggestions, better log entries that don't duplicate what's already recorded.

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

If previous next: was NOT completed and is being replaced, it gets routed as a task via `tasks.py add` to the relevant bundle with context.

### 4. Write Log Entry

**Before writing anything else, prepend a signed entry to `_kernel/log.md`.** This is the primary record of what happened. The log entry uses the standard template:

- What happened (brief narrative)
- Decisions made (with rationale — WHY, not just WHAT)
- Tasks created or completed
- References captured
- Next actions identified

**The log entry must be written BEFORE any other files. The log is truth. Everything else derives from it.**

### 5. Prepare Remaining Content (in memory)

**Re-read `_kernel/log.md` first ~150 lines** to ground the remaining work in the actual written log. This captures the entry just prepended in step 4 plus the previous 3-4 entries. Don't rely on memory of what was read in step 1; the log has changed since then.

Then prepare the content for all remaining files in memory:

- **Active bundle's `context.manifest.yaml`** — update the `context:` field to reflect current state. Merge new information with existing context; don't flatten rich context from a previous deep session.
- **`_kernel/insights.md`** — new evergreen entries (only if confirmed in step 3)
- **Cross-walnut dispatches** — brief log entries for destination walnuts
- **Tasks via `tasks.py`** — plan the calls:
  - New task: `python3 plugins/alive/scripts/tasks.py add --walnut {path} --title "..." --bundle {name} --priority urgent`
  - Mark done: `python3 plugins/alive/scripts/tasks.py done --walnut {path} --id t001`
  - Edit: `python3 plugins/alive/scripts/tasks.py edit --walnut {path} --id t001 --priority active`

**The agent does NOT write `now.json`.** The post-write hook runs `project.py` automatically after `log.md` is written, which assembles `now.json` from all source files. Do not prepare now.json content.

### 6. Write Remaining Files (parallel)

Fire all remaining writes as parallel calls in a single message. The content was prepared in step 5. These are independent of each other — they only depend on the log entry existing, which step 4 handled.

Parallel writes:
- Active bundle's `context.manifest.yaml` — context field update
- `_kernel/insights.md` — new evergreen entries (if any confirmed)
- Cross-walnut dispatches — brief log entries to destination walnut logs (if any)
- Cross-walnut task additions — tasks routed to other walnuts (if any)
- Tasks via `tasks.py` Bash calls — can run in parallel with the file writes above

### 6b. Update Squirrel Entry

Write the routed stash to the session's squirrel YAML in `.alive/_squirrels/{session_id}.yaml`. This turns the YAML from a skeleton into an actual session record.

Read the current YAML, then Edit to update:
- `walnut:` — set to the active walnut name (or keep `null` if no walnut opened)
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

- `working:` — list any working files created or modified this session
- `saves:` — increment by 1 (was 0 on first save, 1 on second, etc.)
- `last_saved:` — set to current ISO timestamp

This is cumulative across saves. Each save APPENDS new items to `stash:`, it doesn't replace. The YAML becomes the full record of everything routed during the session.

### 7. Route: New Walnuts (if needed)

If any stash items require scaffolding new walnuts (new person, new venture/experiment), handle these after the parallel writes. These are heavier operations that may need their own confirmation.

- **New person** → scaffold person walnut in `People/`. Legacy person walnuts at `02_Life/people/` are still recognized.
- **New venture/experiment** → scaffold walnut with `_kernel/`

### 8. Integrity Check

Not a vibe check. A concrete checklist. Run through each:

- [ ] **now.json** — project.py will compute this from the log entry and source files. Verify the log entry has enough context for a good projection.
- [ ] **Log entry** — does it capture WHY decisions were made, not just WHAT?
- [ ] **Tasks** — were tasks routed via `tasks.py`? Check by calling `tasks.py list --walnut {path}` if uncertain.
- [ ] **Bundles** — was any bundle worked on this session? Is its manifest updated (sources, decisions, status)?
- [ ] **References** — was any external content discussed this session that wasn't captured? Any research worth saving? (Route to bundle `raw/` if active bundle exists.)
- [ ] **Insights** — did any standing domain knowledge surface that should be proposed as evergreen?
- [ ] **People** — was anyone mentioned who should have context dispatched to their walnut?
- [ ] **Bundle status** — should any bundle advance? (draft → prototype when it has a visual; prototype → published when shared externally; published → done when outputs graduated). Graduation is a status flip in the manifest.
- [ ] **Bundle shared** — was a bundle shared with someone this session? If so, update the manifest's `shared:` frontmatter (to, method, date, version) and stash a dispatch to the person's walnut.

If anything fails, fix it before completing the save. This is the last gate.

**Post-save note:** After `log.md` is written, the post-write hook automatically runs `project.py` → `now.json`, then `generate-index.py` → `_index.json`. The agent does not need to trigger these.

### 9. Continue

Session continues. Stash resets for next checkpoint.

```
╭─ 🐿️ saved — checkpoint 2
│  3 decisions routed to log
│  2 tasks added via tasks.py
│  1 dispatch to [[jax-stellara]]
│  next: updated
│  zero-context: ✓
│
│  Run alive:system-cleanup? (stale walnuts, orphan refs, stale drafts)
╰─
```

The check suggestion is lightweight — one line. If the human ignores it, no friction. If they say "check" or "yeah", invoke `alive:system-cleanup`.

---

## On Actual Session Exit

When the session truly ends (stop hook, explicit "I'm done done", the human leaves):

- Update the squirrel entry in `.alive/_squirrels/{session_id}.yaml`:
  - Set `ended:` to current timestamp
  - `saves:` is already > 0 from the last save
  - Set `transcript_path:` — scan `~/.claude/projects/*/` for a JSONL file containing the session ID
- The entry is already saved — this step adds the exit metadata

---

## Empty Save

If nothing was stashed since last save — skip the ceremony.

```
╭─ 🐿️ nothing to save since last checkpoint.
╰─
```
