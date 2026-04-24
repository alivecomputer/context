---
name: alive:feedback
description: "Report a bug, request a feature, or send general feedback to the ALIVE team. Collects safe system metadata, shows a preview, and creates a GitHub Issue. Nothing personal leaves the machine, only what you type and anonymous system info."
user-invocable: true
---

# Feedback

Send feedback to the ALIVE team. Bug reports, feature requests, general thoughts — whatever is on your mind.

Nothing personal leaves your machine. The only content in the issue is what you type plus anonymous system metadata (plugin version, model, OS). No walnut data, no file contents, no conversation history.

---

## Flow

### 1. Ask Type

```
╭─ 🐿️ feedback
│
│  ▸ What kind?
│  1. Bug report
│  2. Feature request
│  3. General feedback
╰─
```

### 2. Collect Description

Based on type, prompt with guidance that encourages detail:

- **Bug:** "What went wrong? What did you expect to happen? Include any error messages you saw — the more detail, the better our chance of fixing it."
- **Feature:** "What would you like to see? How would you use it? Help us understand the problem you're solving."
- **General:** "What's on your mind? The more context you give, the more useful it is for us."

The human types their message. Free text, no template.

**For bugs only**, ask one follow-up: "What did you expect to happen instead?"

### 3. Detect Skill Context

Check if an `/alive:*` skill was invoked earlier in this session. If so, offer to include it:

```
╭─ 🐿️ context
│  Looks like you were using alive:save
│
│  ▸ Include that as context?
│  1. Yes
│  2. No
╰─
```

If no skill was recently invoked, skip this step silently.

### 4. Optional Attach

Surface available diagnostic context one item at a time. Each requires individual consent.

**Error output (any type):** If recent tool errors or hook failures occurred in this session, offer regardless of feedback type:

```
╭─ 🐿️ diagnostics
│  There were errors earlier in this session.
│
│  ▸ Include the error output? (helps us diagnose)
│  1. Yes, include it
│  2. No, skip
╰─
```

**Walnut name (bugs only):** If a walnut is loaded and the type is bug, offer:

```
╭─ 🐿️ diagnostics
│  You're working in a walnut right now.
│
│  ▸ Include the walnut name? (just the name, nothing else)
│  1. Yes
│  2. No
╰─
```

If no errors occurred in the session and the type isn't a bug, skip this step entirely.

### 5. Collect System Metadata

Gather these automatically — no user interaction needed:

| Field | How to get it |
|-------|--------------|
| Plugin version | Read `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json` → `version` field. Fallback: use CLAUDE.md frontmatter version. |
| Engine/model | You know which model you are — state it (e.g., `claude-opus-4-7`) |
| OS/platform | Run: `uname -s` via Bash |
| Context usage % | Read `.alive/.context_pct` if it exists (written by statusline). Otherwise: "unavailable" |
| Session duration | Read `.alive/.session_id` (written by statusline). Then open `.alive/_squirrels/{session_id}.yaml` and calculate minutes elapsed from its `started:` field to now. **Do not guess the session ID from directory contents** — if `.session_id` is missing, report "unavailable". |

**Never collect:** walnut contents, file paths, conversation history, PII, credentials, `.alive/key.md`.

### 6. Preview

Show the complete issue in a bordered block. This is exactly what will be posted — nothing hidden.

```
╭─ 🐿️ feedback preview
│
│  Title: [Bug] Save skill dropped stash items
│
│  Body:
│  ### Description
│  [their message]
│
│  ### Expected Behaviour
│  [their answer — bug only]
│
│  ### Context
│  - Skill: alive:save
│  - Error output: [if opted in]
│  - Walnut: [name, if opted in]
│
│  ### System
│  - Plugin: 3.0.0
│  - Engine: claude-opus-4-7
│  - OS: Darwin
│  - Context: 45%
│  - Session: ~32min
│
│  Labels: feedback, bug
│
│  → this will be posted publicly to github.com/alivecontext/alive
│
│  ▸ Send?
│  1. Send
│  2. Edit description
│  3. Cancel
╰─
```

**Title format:** `[Bug] first ~60 chars of description` / `[Feature] ...` / `[Feedback] ...`. Keep under 70 chars total.

If the human picks "Edit" — ask what they want to change, update the preview, show it again.

### 7. Send

First, verify `gh` is available:

```bash
which gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1
```

If either check fails, skip straight to the fallback in Step 8.

If `gh` is ready, create the issue. Use a heredoc for the body to handle multiline content and special characters:

```bash
gh issue create \
  --repo alivecontext/alive \
  --title "[Bug] Save skill dropped stash items" \
  --label "feedback,bug" \
  --body "$(cat <<'ALIVE_FEEDBACK_EOF'
### Description

The save skill lost 3 of my stash items during checkpoint...

### Expected Behaviour

All stash items should persist through save...

### Context

- Skill: alive:save

### System

- Plugin: 3.0.0
- Engine: claude-opus-4-7
- OS: Darwin
- Context: 45%
- Session: ~32min

---
*Sent via `/alive:feedback`*
ALIVE_FEEDBACK_EOF
)"
```

**Label mapping:**
- Bug report → `--label "feedback,bug"`
- Feature request → `--label "feedback,enhancement"`
- General feedback → `--label "feedback"`

### 8. Confirm or Handle Failure

**On success** (`gh` returns a URL):

```
╭─ 🐿️ sent
│  Feedback submitted: https://github.com/alivecontext/alive/issues/42
│  Thanks — back to work.
╰─
```

**On failure** (`gh` not installed, not authenticated, or network error):

```
╭─ 🐿️ couldn't send
│  [error message]
│
│  Two options:
│  1. Run `gh auth login` in your terminal, then try /alive:feedback again
│  2. Copy the issue below and paste it at:
│     github.com/alivecontext/alive/issues/new
│
│  ---
│  [show the full formatted issue body for copy-paste]
╰─
```

---

## Privacy Boundary — Hard Rules

These are non-negotiable. The skill MUST NOT:

- Read any walnut file contents (key.md, now.json, log.md, insights.md, tasks.json)
- Include file paths from the world
- Include conversation history or stash items
- Include people names or any PII from walnut data
- Include `.alive/key.md` or world identity
- Include API keys, tokens, or credentials
- Collect anything not listed in the metadata table above

The walnut name is the only walnut-related data that CAN be included, and only with explicit per-item consent for bug reports.

---

## Nudge Behaviour

The squirrel can suggest `/alive:feedback` when it notices problems — but only if `feedback_nudges` is enabled in `.alive/preferences.yaml` (default: true).

**When to nudge:**
- After a hook failure or tool error
- After a save that hit integrity check failures

**How to nudge:**

```
╭─ 🐿️ that hook failed
│  Want to report it? /alive:feedback
╰─
```

**Frequency cap:** Maximum one nudge per session. If you've already nudged once this session, don't show another.

This is a squirrel instinct, not a hook. The squirrel notices errors as part of its always-watching behaviour and surfaces the nudge when `feedback_nudges` is on.
