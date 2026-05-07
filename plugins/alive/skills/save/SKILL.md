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

- `_kernel/now.json` — which bundle is active? What was the context? Which tasks are urgent or active?
- `_kernel/log.md` — first ~100 lines (recent entries — what have previous sessions covered?)
- Active bundle's `context.manifest.yaml` — if `now.json` reports an active bundle, read its manifest

**Do NOT read per-bundle task files directly** — task data lives in `now.json` already (computed projection), or call `tasks.py list --walnut {path}` if you need specific detail. In v3, tasks are stored in `tasks.json` per walnut and per bundle, managed only through the supported task tooling (`alive tasks promote` for batched stash promotion in step 6c, `scripts/tasks.py` for direct edits like done / priority changes). Never edit `tasks.json` from the agent.

**If `_kernel/now.json` does not exist:** suggest running `python3 "$ALIVE_PLUGIN_ROOT/scripts/project.py" --walnut {path}` to generate it.

**Standalone session (no walnut loaded):** If no walnut was opened this session, the squirrel still has a stash to route. Ask: "Which walnut does this session belong to?" If the human names one, load its core files and proceed normally. **If the human cannot name one, abort the save** — on the fn-12 CLI-only save path, `alive log prepend` requires a concrete walnut (it only targets `{walnut}/_kernel/log.md`). Surface a bordered block explaining the save is blocked until a walnut is chosen; the stash stays in conversation and the squirrel YAML at `.alive/_squirrels/` keeps `walnut: null` until the next save. Do NOT attempt to write to a world-level `.alive/log.md` — the CLI does not support that path.

This gives the squirrel the full picture BEFORE it starts routing. It knows which bundle was active, what previous sessions accomplished, and what the task state is. This makes everything that follows smarter — better routing suggestions, better log entries that don't duplicate what's already recorded.

### 2. Pre-Save Scan

"Anything else before I save?"

Then scan back through messages since last save for stash items the squirrel may have missed. Add them.

### 3. Confirm Stash (batched)

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
╰─
```

**Then one AskUserQuestion call with up to 3 questions — skip empty categories:**

| Question slot | Category | Options |
|---|---|---|
| 1 | Decisions | "Confirm all" / "Review list" / "Drop some" |
| 2 | Tasks | "Confirm all" / "Edit or drop" |
| 3 | Notes | "Confirm all" / "Drop some" |

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

### 4. Write Log Entry

**Before writing anything else, prepend a signed entry to `_kernel/log.md`.** This is the primary record of what happened. The log entry uses the standard template:

- What happened (brief narrative)
- Decisions made (with rationale — WHY, not just WHAT)
- Tasks created or completed
- References captured

**The log entry must be written BEFORE any other files. The log is truth. Everything else derives from it.**

**Write the entry via the `alive log prepend` CLI, not via the Edit tool.** The CLI owns the deterministic parts of the entry block (heading, entry-hash marker, signed line, separator) and the frontmatter bump (`entry-count`, `last-entry`, `summary`). Your responsibility is the body prose + summary string.

**Step 4a — Precheck (before composing anything):**

Run the doctor log check. If it fails, **abort the save**, surface the `hint` to the human, and stop — do not attempt to write the log entry any other way.

```bash
"$ALIVE_PLUGIN_ROOT/bin/alive" doctor --check=log --walnut {walnut-path}
```

Python fallback if `bin/alive` is missing:

```bash
"${ALIVE_PYTHON:-python3}" "$ALIVE_PLUGIN_ROOT/scripts/cli.py" doctor --check=log --walnut {walnut-path}
```

Parse the JSON stdout. If `check.status != "ok"`, show the `check.hint` verbatim to the human inside a bordered block and stop. Do NOT fall back to Edit-tool log prepend. The CLI is the only supported write path on this version of the plugin.

**Step 4b — Compose body + summary in memory.**

The body is pure prose — the CLI adds the heading, hash marker, signed line, and separator. Do NOT write `## <date> -- squirrel:...`, `<!-- entry-hash: ... -->`, `signed: squirrel:...`, or a trailing `---` yourself; those come from the CLI.

The summary is a one-line distillation of the entry (what changed, not the full narrative). It lands in the log frontmatter's `summary:` key.

**Step 4c — Invoke the CLI.**

*Single-line summary* — pass via `--summary` CLI arg, body via stdin heredoc:

```bash
"$ALIVE_PLUGIN_ROOT/bin/alive" log prepend \
  --walnut {walnut-path} \
  --entry-file - \
  --summary "{escaped-single-line-summary}" <<'ALIVE_ENTRY_BODY'
{body prose, any markdown, any number of lines}
ALIVE_ENTRY_BODY
```

*Multi-line summary* — write the summary to a real temp file and pass `--summary-file`. **Never use process substitution `<(...)`** — it's shell-fragile in agent-invoked contexts and silently misbehaves under some Bash tool wrappers. Use `trap ... EXIT` so the temp file is removed even when the CLI exits non-zero and the skill aborts the save:

Compose the temp-file path with `mktemp` first, then write the summary to it via the agent's native file-write tool (NOT a shell heredoc — heredocs append a trailing newline, and `--summary-file` is read verbatim with no `rstrip`, so a stray `\n` would land in the frontmatter `summary:`). Use `trap ... EXIT` so the file is removed even when the CLI exits non-zero and the skill aborts the save:

```bash
SUMMARY_FILE=$(mktemp -t alive-summary.XXXXXX)
trap 'rm -f "$SUMMARY_FILE"' EXIT
# Now write the exact multi-line summary text to $SUMMARY_FILE
# via the agent's native file-write tool. Do NOT use a bash
# heredoc (trailing newline) or printf with per-line shell
# quoting (breaks on apostrophes inside the summary).
#
# Then invoke the CLI:
"$ALIVE_PLUGIN_ROOT/bin/alive" log prepend \
  --walnut {walnut-path} \
  --entry-file - \
  --summary-file "$SUMMARY_FILE" <<'ALIVE_ENTRY_BODY'
{body prose}
ALIVE_ENTRY_BODY
rm -f "$SUMMARY_FILE"
trap - EXIT
```

Python fallback — single-line summary (same arg shape; substitute the binary):

```bash
"${ALIVE_PYTHON:-python3}" "$ALIVE_PLUGIN_ROOT/scripts/cli.py" log prepend \
  --walnut {walnut-path} --entry-file - --summary "{escaped}" <<'ALIVE_ENTRY_BODY'
{body}
ALIVE_ENTRY_BODY
```

Python fallback — multi-line summary (same temp-file + trap lifecycle as the primary path):

```bash
SUMMARY_FILE=$(mktemp -t alive-summary.XXXXXX)
trap 'rm -f "$SUMMARY_FILE"' EXIT
# Write summary text to $SUMMARY_FILE via the agent's native
# file-write tool (same no-trailing-newline constraint as the
# primary path).
"${ALIVE_PYTHON:-python3}" "$ALIVE_PLUGIN_ROOT/scripts/cli.py" log prepend \
  --walnut {walnut-path} --entry-file - --summary-file "$SUMMARY_FILE" <<'ALIVE_ENTRY_BODY'
{body}
ALIVE_ENTRY_BODY
rm -f "$SUMMARY_FILE"
trap - EXIT
```

**Step 4d — Parse JSON stdout + surface to the human.**

The CLI emits pure JSON on stdout. Parse it and confirm:

- `success: true`
- `projection_updated: true`
- `index_updated: true` (or you passed `--no-index` intentionally — not the default save path)

Log the `squirrel_id` and `entry_id` in the save summary block so the human has an identifier to grep on later. Surface `entry_count` and any non-empty `projection_stdout` excerpt if the human asked about projection state.

If any of the booleans above are false, or if the CLI exits non-zero, treat the save as failed: surface the JSON `error.code` + `error.message` verbatim inside a bordered block, and include `error.hint` and/or `error.detail` if the CLI emitted them (they're optional fields — not every error path populates them). Stop. Do NOT attempt a second write path.

### 5. Prepare Remaining Content (in memory)

**Re-read `_kernel/log.md` first ~150 lines** to ground the remaining work in the actual written log. This captures the entry just prepended in step 4 plus the previous 3-4 entries. Don't rely on memory of what was read in step 1; the log has changed since then.

Then prepare the content for all remaining files in memory:

- **Active bundle's `context.manifest.yaml`** — update the `context:` field to reflect current state. Merge new information with existing context; don't flatten rich context from a previous deep session.
- **`_kernel/insights.md`** — new evergreen entries (only if confirmed in step 3)
- **Cross-walnut dispatches** — brief log entries for destination walnuts
- **Task promotion** — confirmed task-shaped stash items are promoted in step 6c via a single `alive tasks promote` invocation, not per-item `tasks.py add` calls. No prep is needed here; the CLI reads the stash items off the squirrel YAML written in step 6b. For non-promotion task edits (mark done, change priority on a pre-existing task), plan direct `tasks.py` calls:
  - Mark done: `python3 "$ALIVE_PLUGIN_ROOT/scripts/tasks.py" done --walnut {path} --id t001`
  - Edit: `python3 "$ALIVE_PLUGIN_ROOT/scripts/tasks.py" edit --walnut {path} --id t001 --priority active`

**The agent does NOT write `now.json`.** `alive log prepend` (step 4) invokes `project.py` after the log write, which assembles `now.json` from all source files. Do not prepare now.json content.

### 6. Write Remaining Files (parallel)

Fire all remaining writes as parallel calls in a single message. The content was prepared in step 5. These are independent of each other — they only depend on the log entry existing, which step 4 handled.

Parallel writes:
- Active bundle's `context.manifest.yaml` — context field update
- `_kernel/insights.md` — new evergreen entries (if any confirmed)
- Cross-walnut dispatches — brief log entries to destination walnut logs (if any)
- Cross-walnut task additions — tasks routed to other walnuts (if any)
- Task edits via `tasks.py` Bash calls (mark done, change priority on existing tasks) — can run in parallel with the file writes above. Promotion of new task-shaped stash items is a single post-6b call, see step 6c.

### 6b. Update Squirrel Entry

Write the routed stash to the session's squirrel YAML in `.alive/_squirrels/{session_id}.yaml`. This turns the YAML from a skeleton into an actual session record.

Read the current YAML, then Edit to update:
- `walnut:` — set to the active walnut name (or keep `null` if no walnut opened)
- `stash:` — append the newly routed items to `stash:` (do NOT replace existing entries from prior saves in this session). On the first save the existing value is `[]`, so the operation reads as a fill; on every subsequent save it is a true append. Each item is tagged by type and destination:

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

### 6c. Promote Task-Shaped Stash Items

Confirmed task-shaped stash items land in `tasks.json` via a single `alive tasks promote` call. The CLI reads the squirrel YAML written in step 6b, walks each `type: task` item, and writes both the task row and the `promotion_state` / `task_id` markers back onto the squirrel file under a single flock for crash safety. It also runs a walnut-filtered world-wide sweep that recovers any `pending` markers left behind by an earlier interrupted session for the same walnut.

```bash
"$ALIVE_PLUGIN_ROOT/bin/alive" tasks promote \
  --squirrel "$SESSION_ID" \
  --walnut "$WALNUT_PATH"
```

Python fallback if `bin/alive` is missing:

```bash
"${ALIVE_PYTHON:-python3}" "$ALIVE_PLUGIN_ROOT/scripts/cli.py" tasks promote \
  --squirrel "$SESSION_ID" --walnut "$WALNUT_PATH"
```

Fail-loud if the CLI binary is missing on both paths — do NOT fall back to per-item `tasks.py add` from the agent. The CLI is the only supported promotion path on this version of the plugin.

The CLI emits pure JSON on stdout. Two envelope shapes:

- **Command-level failure** — `{"success": false, "error": {"code": "...", "message": "...", ...}}` with no `status` / `items` keys. Comes from lock timeout, missing walnut, missing world root, usage errors, internal errors. Surface `error.code` + `error.message` verbatim inside a bordered block and stop the save.
- **Run result** — `{"status": "...", "items": [...]}`. Parse and bin items by their per-item `status`:

  - `PROMOTED_BUNDLE` — newly written under `<bundle>/tasks.json`
  - `PROMOTED_UNSCOPED` — newly written under `_kernel/tasks.json` (no bundle resolved; first-class outcome, not a failure)
  - `ALREADY_PROMOTED` — marker said `complete` already; existing `task_id` carried through
  - `RECOVERED_PENDING` — recovered from a prior interrupted session for THIS walnut; `source_squirrel` field names where the marker lived
  - `SKIPPED_CROSS_WALNUT` — `routed:` did not match the active walnut name; no marker, no task
  - `ERROR` — promotion failed for that item; surface the per-item `error` field (a single string of the form `"phase<N>: <ExceptionType>: <message>"`) — there is no per-item `error.code` / `error.message` split; structured code+message only exist on the top-level command-level failure envelope above

Top-level `status` is one of `SUCCEEDED` / `PARTIAL` / `FAILED`. Treat `FAILED` (every item is `ERROR`) as a save failure: surface the JSON inside a bordered block and stop. `PARTIAL` is non-fatal but does NOT imply a fresh promotion happened — it covers any mixed result that isn't all-success-set, including recovery-only runs whose only item is `RECOVERED_PENDING`. Decide what to surface from the per-item statuses, not from the top-level word; the per-status counts land in the save summary block (step 9).

Surface the per-status counts in the save summary block (step 9).

If any stash items require scaffolding new walnuts (new person, new venture/experiment), handle these after the parallel writes. These are heavier operations that may need their own confirmation.

- **New person** → scaffold person walnut in `02_Life/people/`. Legacy person walnuts at `02_Life/people/` are still recognized.
- **New venture/experiment** → scaffold walnut with `_kernel/`

### 8. Integrity Check

Not a vibe check. A concrete checklist. Run through each:

- [ ] **now.json** — project.py will compute this from the log entry and source files. Verify the log entry has enough context for a good projection.
- [ ] **Log entry (CLI)** — `alive log prepend` returned `success: true` AND `projection_updated: true` AND (`index_updated: true` OR `--no-index` used intentionally). If any of those are false the save is NOT complete.
- [ ] **Log entry (content)** — does it capture WHY decisions were made, not just WHAT?
- [ ] **Tasks** — did `alive tasks promote` (step 6c) return top-level `status: SUCCEEDED` or `PARTIAL`? Were any `ERROR` items surfaced? Check by calling `tasks.py list --walnut {path}` if uncertain about the resulting queue.
- [ ] **Bundles** — was any bundle worked on this session? Is its manifest updated (sources, decisions, status)?
- [ ] **References** — was any external content discussed this session that wasn't captured? Any research worth saving? (Route to bundle `raw/` if active bundle exists.)
- [ ] **Insights** — did any standing domain knowledge surface that should be proposed as evergreen?
- [ ] **People** — was anyone mentioned who should have context dispatched to their walnut?
- [ ] **Bundle status** — should any bundle advance? (draft → prototype when it has a visual; prototype → published when shared externally; published → done when outputs graduated). Graduation is a status flip in the manifest.
- [ ] **Bundle shared** — was a bundle shared with someone this session? If so, update the manifest's `shared:` frontmatter (to, method, date, version) and stash a dispatch to the person's walnut.

If anything fails, fix it before completing the save. This is the last gate.

**Post-save note:** `alive log prepend` runs `project.py` → `now.json` and `generate-index.py` → `_index.json` itself, inside the same invocation that wrote the log entry. The agent does not need to trigger these. The JSON response from step 4 reports `projection_updated` and `index_updated` so the Integrity Check can gate on them.

### 9. Continue

Session continues. Stash resets for next checkpoint.

```
╭─ 🐿️ saved — checkpoint 2
│  3 decisions routed to log
│  2 tasks promoted from stash (1 bundle, 1 unscoped)
│  1 dispatch to [[jax-stellara]]
│  ↻ 1 task recovered from prior session
│  zero-context: ✓
│
│  Run alive:system-cleanup? (stale walnuts, orphan refs, stale drafts)
╰─
```

Lines in the saved block:

- **`N tasks promoted from stash`** — N = count of items where the per-item `status` is `PROMOTED_BUNDLE` OR `PROMOTED_UNSCOPED` from the step 6c JSON. Show the bundle / unscoped split in parentheses when both counts are non-zero (e.g. `(1 bundle, 1 unscoped)`); when one count is zero, omit the parenthetical (e.g. just `2 tasks promoted from stash` when both promotions were unscoped). `ALREADY_PROMOTED` and `SKIPPED_CROSS_WALNUT` items do NOT count toward N.
- **`↻ R tasks recovered from prior session`** — conditional. Emit only when R = count of `RECOVERED_PENDING` items > 0. When R = 0, drop the line entirely.
- **`⚠ M tasks failed promotion: t<id>, t<id>, …`** — conditional. Emit only when M = count of `ERROR` items > 0. List the per-item `task_id`s when populated; otherwise list `#<stash_index + 1>` so the number matches the human-numbered stash display in step 3 (the CLI's `stash_index` is 0-based; the step 3 block lists items starting at `1.`). When M = 0, drop the line entirely.

**Save-nudge hook (runtime, do not skip).** Before closing the saved block, run:

```bash
python3 "$ALIVE_PLUGIN_ROOT/scripts/star_prompt.py" save-nudge --world "$ALIVE_WORLD_ROOT" 2>/dev/null
```

If the command produces non-empty output, append the line verbatim as the last line inside the saved block (above the closing `╰─`). If it produces nothing, render the saved block unchanged. Failures of this command are silent (the `2>/dev/null` suppresses any error if the script is missing on partial installs, and the module itself logs internal exceptions to `.alive/logs/star-prompt.log` rather than stdout).

The check suggestion is lightweight — one line. If the human ignores it, no friction. If they say "check" or "yeah", invoke `alive:system-cleanup`.

---

## On Actual Session Exit

When the session truly ends (stop hook, explicit "I'm done done", the human leaves):

- Update the squirrel entry in `.alive/_squirrels/{session_id}.yaml`:
  - Set `ended:` to current timestamp
  - `saves:` is already > 0 from the last save
  - Set `transcript:` — scan `~/.claude/projects/*/` for a JSONL file containing the session ID
- The entry is already saved — this step adds the exit metadata

---

## Empty Save

If nothing was stashed since last save — skip the ceremony.

```
╭─ 🐿️ nothing to save since last checkpoint.
╰─
```

---

## Troubleshooting — staging rollback

If `alive log prepend` fails on your staging install (CLI exits non-zero, or the JSON response has `success: false` + a stack-trace-looking `error.detail`), the fix is to pin the plugin repo to a prior staging commit + reinstall locally:

```bash
cd {path-to-cloned-plugin-repo}
git checkout {prior-staging-sha}   # before the fn-12 cut-over
claude plugin install alive@alivecontext-staging --force  # or your local-install equivalent
```

This applies to the **staging** install only. Public plugin users aren't affected by fn-12 until a separate promote decision — fn-12 is staging-only by design. Surface the CLI's `error.code` + `error.message` verbatim to the human before rolling back (and `error.hint` / `error.detail` if present) so they have the diagnosis in their session history.
