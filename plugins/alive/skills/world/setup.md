---
name: setup
description: First-time world creation. Triggered automatically when alive:world detects no existing ALIVE structure.
internal: true
---

# Setup — Three Paths to a World

First time. No `.alive/` folder exists. You just installed the ALIVE Context System. Make it feel like something just came alive.

All three paths produce the same result: a fully scaffolded ALIVE world with domain folders, `.alive/` config, and at least one walnut. The only difference is how we collect the information.

---

## Shared World-Location Prelude (P1 + P2 run before path branch)

The prelude resolves a single `$WORLD_ROOT_TARGET` that every path then uses. Some prelude steps run BEFORE the Path A/B/C dispatch (the shared steps); others run INSIDE individual paths (the path-specific steps).

**Order of operations (locked).**

1. **P1** (skip-or-resolve) — runs FIRST, BEFORE the A/B/C dispatch. May set `$WORLD_ROOT_TARGET` and jump straight to **P5**.
2. **P2** (stale-config recovery) — runs SECOND, BEFORE the A/B/C dispatch (only when P1 did not set the target).
3. Run **Detection Logic** below to decide which path (A / B / C).
4. **P3** (fresh-install location prompt) and **P4** (validate the choice) — these are PATH-SPECIFIC steps that run INSIDE the interactive paths only:
   - Path A skips P3/P4 and runs **A1'** instead (non-interactive: `AskUserQuestion` is unavailable).
   - Path B runs **B0** which invokes the canonical 3-option P3 + the full P4 type-back loop.
   - Path C runs **C0** which collapses to a 2-option P3 prompt + the full P4 loop.
5. **P5** (lexical normalize + export + scaffolding-convergence) — convergence point that every path arrives at with a resolved `$WORLD_ROOT_TARGET`.

The phrase "before path branch" in this section header refers to **P1 + P2 only**; P3 and P4 are intentionally per-path and described in this prelude section as a single source of truth that the per-path steps reference.

### P1. Skip-or-resolve

Skip the rest of the prelude (and use the existing path) when **either** condition is true:

1. `$ALIVE_WORLD_ROOT_OVERRIDE` is set in the environment. Run `validate_path_choice` against it as a **deny-only** non-interactive guard (no prompts) — even an explicit override cannot point at a hard-deny system root like `/` or `/etc`:
   ```bash
   source "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/alive-common.sh"
   if [ -n "${ALIVE_WORLD_ROOT_OVERRIDE:-}" ]; then
     ov_result="$(validate_path_choice "$ALIVE_WORLD_ROOT_OVERRIDE")"
     ov_decision="${ov_result%%$'\t'*}"
     if [ "$ov_decision" = "deny" ]; then
       printf 'ALIVE_WORLD_ROOT_OVERRIDE rejected: %s\n' "$ov_result" >&2
       exit 1
     fi
     WORLD_ROOT_TARGET="$ALIVE_WORLD_ROOT_OVERRIDE"
     # confirm_required is intentionally accepted here (the user set
     # the env var explicitly; that IS the explicit confirmation).
     # Continue to step P5.
   fi
   ```
2. `~/.config/alive/world-root` exists and validates `OK` via the canonical helper:
   ```bash
   if existing="$(read_world_root_file 2>/dev/null)"; then
     if [ "$(validate_world_root "$existing")" = "ok" ]; then
       WORLD_ROOT_TARGET="$existing"
       # Skip ahead to step P5 (resolve absolute path).
     fi
   fi
   ```

If either branch fired, jump to **P5**.

### P2. Existing-install branch (stale-config recovery)

`read_world_root_file` is the canonical helper for the happy path, but for stale entries it **returns non-zero with no stdout in bash** (and `None` in Python) — so P2 cannot use it to retrieve the stale path's value. Instead, read the config file directly and feed the result through `validate_world_root` to derive `<path>` and `<reason>`:

```bash
# Assumes alive-common.sh was sourced in P1 (the prelude sources it
# once at the top of the skill). Re-source defensively if you split
# the snippet out:
#   source "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/alive-common.sh"

CONFIG_FILE="${HOME}/.config/alive/world-root"
[ -f "$CONFIG_FILE" ] || skip_p2=1   # nothing to recover from

if [ -z "$skip_p2" ]; then
  # Read + lexically-normalize the persisted line. The canonical T1
  # helper strips whitespace, validates the single-line shape, and
  # rejects corrupt content (empty, multi-line, ascend-past-root,
  # tab/newline/CR).
  if stale_path="$(_alive_parse_persisted_world_root_file "$CONFIG_FILE" 2>/dev/null)"; then
    stale_reason="$(validate_world_root "$stale_path")"
  else
    # Corrupt content -- surface a distinct reason so the recovery
    # copy makes sense (the file's path is shown rather than a path
    # we can't extract).
    stale_path="$CONFIG_FILE"
    stale_reason="corrupt_config_file"
  fi

  if [ "$stale_reason" = "ok" ]; then
    skip_p2=1   # not actually stale; P1 should have caught this
  fi
fi
```

If `skip_p2` is unset and `stale_reason != "ok"`, surface:

> `AskUserQuestion`: "Existing config points at `$stale_path`, but it's `$stale_reason` (`missing_dir` / `missing_marker` / `unmounted_volume` / `corrupt_config_file`). What now?"
>
> Options (header `Recovery`, max 12 chars):
> 1. **New here** — set up a new world at the current location
> 2. **Repair** — run `alive doctor --fix --world-root <path>`
> 3. **Cancel** — exit setup and leave the config untouched

Branch:
- **New here** → fall through to **P3** (fresh-install prompt). P3's option labels stay as locked (`~/alive/` is the recommended option); the `$PWD`-based "Here at `{{PWD}}`" option remains the natural picker for users repairing in-place.
- **Repair** → display the exact command. For `missing_dir` / `missing_marker` / `unmounted_volume`: `alive doctor --fix --world-root "$stale_path"`. For `corrupt_config_file` there is no usable path to recover, so instead display `rm "$CONFIG_FILE"` and instruct the user to re-run setup. End the skill; do **not** scaffold.
- **Cancel** → end the skill.

### P3. Fresh-install location prompt (interactive paths only)

When a `world-seed.md` is present in `$PWD`, **skip P3 entirely** — Path A is non-interactive and resolves the target from the seed (see Path A1' below). Otherwise prompt:

> `AskUserQuestion`: "Where should your ALIVE world live?"
>
> Options (header `Location`):
> 1. **`~/alive/`** (recommended)
> 2. **Here at `{{PWD}}`**
> 3. **Custom path**

If **Custom path**, free-text follow-up:

> `AskUserQuestion`: "Type the absolute path. `~` is expanded; relative paths are rejected."

Set `WORLD_ROOT_CANDIDATE` to the chosen value (expanded for `~/alive/` to `${HOME}/alive`, raw for the others).

### P4. Validate the choice

Run the system-path policy validator (Python sibling at `scripts/_world_root_io.py:validate_path_choice`, bash sibling in `alive-common.sh`):

```bash
source "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/alive-common.sh"
result="$(validate_path_choice "$WORLD_ROOT_CANDIDATE")"
decision="${result%%$'\t'*}"
rest="${result#*$'\t'}"
category="${rest%%$'\t'*}"
message="${rest#*$'\t'}"
```

Branch on `decision`:

- **`allow`** → set `WORLD_ROOT_TARGET="$WORLD_ROOT_CANDIDATE"`. Continue to P5.
- **`deny`** → display `message` and re-loop to P3. Hard-deny is never overridable in setup; the user must pick a different path.
- **`confirm_required`** → enter the **type-back loop**:

  First, compute the canonical display string from the candidate:

  ```bash
  CANDIDATE_DISPLAY="$(lexical_normalize_path "$WORLD_ROOT_CANDIDATE")"
  ```

  Display that string verbatim and prompt:

  > `AskUserQuestion`: "`message`. To confirm, type back this exact string: `<CANDIDATE_DISPLAY>`. Or pick a different location."
  >
  > Options:
  > 1. **Type back** (free text — must equal `<CANDIDATE_DISPLAY>` byte-for-byte)
  > 2. **Pick different** — re-loop to P3

  Accept iff `typed_input == "$CANDIDATE_DISPLAY"` (literal string compare, no normalization on the typed input — that would let `~`-shorthand satisfy a `$HOME`-rooted path and undermine the friction the type-back is intended to add). Otherwise re-loop the type-back prompt up to two more times, then re-loop to P3.

### P5. Resolve absolute path

Lexically normalize and store the final value:

```bash
WORLD_ROOT_TARGET="$(lexical_normalize_path "$WORLD_ROOT_TARGET")" || exit 1
export WORLD_ROOT_TARGET
```

Every Path A/B/C step from here on uses `$WORLD_ROOT_TARGET` instead of `$PWD` for the world root. **Scaffolding convergence (locked):** before the **Scaffolding Procedure** runs, the path executes `mkdir -p "$WORLD_ROOT_TARGET" && cd "$WORLD_ROOT_TARGET"` so that the relative-path `mkdir -p 01_Archive/`, `02_Life/`, `.alive/`, etc. directives in **Step 1** of Scaffolding Procedure resolve under the resolved target rather than `$PWD`. This is the single convergence point — paths must NOT silently fall back to `$PWD` mid-flow.

The corresponding `~/.config/alive/world-root` pointer write is the final scaffolding step — see **Scaffolding Procedure → Step 8** below.

### Non-interactive surfaces (locked)

`AskUserQuestion` is unavailable in subagents and MCP. Path A (with seed), MCP first-run, and CI invocations MUST default to `~/alive/` silently and skip every interactive step above. Path A's own seed-file handling (see A1' below) carries the explicit-target case.

---

## Detection Logic

`alive:world` checks for `01_Archive/`, `02_Life/`, etc. If none found, this fires.

Check two things at the top:

### 1. Is there a world-seed.md in PWD?

The session-new hook will have injected additionalContext containing `"World seed: found at /path/to/world-seed.md"` if one exists. Alternatively, check `$PWD/world-seed.md` directly.

**If world-seed.md exists** → go straight to **Path A**. No menu. No questions.

### 2. No world-seed.md → present the choice

```
╭─ welcome
│
│  No world found here. Let's build one.
│
│  Three ways to start:
│
│   1. Quick start — name + one walnut, 30 seconds
│   2. Terminal setup — guided questions, 3 minutes
│   3. World builder — open the questionnaire in your browser,
│      fill it out, drag the export here, and run /alive:world again
│
│  → Pick 1, 2, or 3
╰─
```

Wait for user input (numbered selection or free text that maps to one).

- **1** → Path C (Minimal Quick Start)
- **2** → Path B (In-Terminal Survey)
- **3** → Open the HTML questionnaire

For **option 3**: The session-new hook injects `"Onboarding questionnaire: /path/to/world-builder.html"` in additionalContext. The path points to the plugin's bundled HTML file.

Run:
```bash
open /path/to/world-builder.html
```

Then display:
```
╭─ questionnaire opened
│
│  The world builder just opened in your browser.
│
│  Fill it out, hit "Export", and save world-seed.md
│  to this directory:
│    {{PWD}}
│
│  Then run /alive:world again. I'll pick it up automatically.
╰─
```

End the skill here. The user will come back.

---

## Path A: World Seed (from HTML questionnaire)

### Trigger
`world-seed.md` exists in PWD (detected by hook or direct check).

### Steps

#### A1'. Resolve `$WORLD_ROOT_TARGET` from the seed (non-interactive)

Path A never blocks on `AskUserQuestion`. Before any scaffolding:

1. If the parsed seed contains an explicit `world_root:` field at the top level, run `validate_path_choice` on it. On `allow` set `WORLD_ROOT_TARGET` to the lexically-normalized value. On `confirm_required` or `deny`, abort the path silently with a stash entry and fall back to default (next step) — Path A cannot prompt.
2. Otherwise default `WORLD_ROOT_TARGET="${HOME}/alive"` silently. Log the choice.
3. Lexically normalize `$WORLD_ROOT_TARGET` before continuing.

Every subsequent A-step writes into `$WORLD_ROOT_TARGET`, not `$PWD`.

#### A1. Read and parse world-seed.md

Read the file. It contains structured sections with YAML-like data:

```markdown
---
type: world-seed
version: 1.0.1-beta
created: 2026-03-10T12:00:00Z
generator: world-builder-html
---

# World Seed

## Identity
name: Alex Chen
description: Builder shipping AI-native tools
timezone: America/Los_Angeles

## Walnuts

### nova-station
type: venture
goal: Build the first civilian orbital platform
rhythm: daily

### glass-cathedral
type: experiment
goal: Interactive fiction prototype
rhythm: weekly

## People

### ryn-okata
name: Ryn Okata
role: Engineering lead
walnuts: nova-station

### mira-solaris
name: Mira Solaris
role: Co-founder

## Context Sources

gmail:
  type: mcp_live
  status: available
chatgpt:
  type: static_export
  path: ~/exports/chatgpt/
  status: available

## Preferences
spark: true
show_reads: true
health_nudges: true
stash_checkpoint: true
always_watching: true
save_prompt: true

## Voice
character: [direct, warm, technical]
blend: 70% sage, 30% rebel
```

Parse each section. All sections are optional except Identity (which must have at least `name`).

#### A2. Show what's coming

```
╭─ world seed found
│
│  Found world-seed.md with:
│    Name: {{name}}
│    Walnuts: {{count}} ({{list of names}})
│    People: {{count}} ({{list of names}})
│    Context sources: {{count}}
│
│  Building your world now...
╰─
```

#### A3. Scaffold the world

Execute the scaffolding sequence (see **Scaffolding Procedure** below) using all parsed data.

#### A4. Copy the seed file into the world

**COPY (do not move)** `$PWD/world-seed.md` to `${WORLD_ROOT_TARGET}/.alive/world-seed.md`. Leave the original in place.

```bash
if [ "$PWD" != "$WORLD_ROOT_TARGET" ]; then
  cp "$PWD/world-seed.md" "${WORLD_ROOT_TARGET}/.alive/world-seed.md"
fi
# When PWD == WORLD_ROOT_TARGET the file is already where it needs to
# be (scaffold step's mkdir created .alive/ alongside it); the copy
# would be a same-path no-op so we skip it.
```

Rationale: the original seed might live in a directory the user wants to keep (a downloads folder, a shared drive); silently moving it across volumes also has cross-device failure modes. The `${WORLD_ROOT_TARGET}/.alive/world-seed.md` copy is the canonical reference for the world's identity going forward; the original is preserved at its discovery location for the user to delete manually.

#### A5. Present the completed world

Show the **After Setup** display (see below). Then offer:

```
→ Say "open {{first-walnut-name}}" to start working.
```

---

## Path B: In-Terminal Survey

### Trigger
User chose option 2 from the menu.

### Steps

Use `AskUserQuestion` for each step. These are real form-style questions, not numbered menus.

#### B0. Resolve `$WORLD_ROOT_TARGET` (prelude P3 + P4)

Before B1, run **P3** (fresh-install location prompt) and **P4** (validate) from the prelude above. This sets `$WORLD_ROOT_TARGET` for the rest of Path B. Path B is the canonical interactive flow, so the full three-option prompt + type-back loop applies.

#### B1. Name

> AskUserQuestion: "What's your name?"

Store as `name`. This goes into `.alive/key.md` frontmatter and body.

#### B2. Identity (optional)

> AskUserQuestion: "One sentence about yourself — what are you building? (press enter to skip)"

Store as `description`. If skipped, leave the description section in key.md as a comment placeholder.

#### B3. First walnut

> AskUserQuestion: "What's the most important thing you're working on right now? Give it a name."

Store as first walnut `name`.

> AskUserQuestion: "Describe it in a sentence — what's the goal?"

Store as first walnut `goal` and `description`.

> AskUserQuestion: "Is that a venture (revenue-focused), experiment (testing something), or life goal? (venture/experiment/life)"

Store as first walnut `type`. Map to domain:
- `venture` → `04_Ventures/`
- `experiment` → `05_Experiments/`
- `life` → `02_Life/goals/`

> AskUserQuestion: "How often do you work on this? (daily/weekly/monthly)"

Store as first walnut `rhythm`. Default to `weekly` if skipped.

#### B4. Additional walnuts (up to 3 more)

> AskUserQuestion: "Want to add another walnut? (yes/no)"

If yes, repeat the name/goal/type/rhythm questions. Allow up to 3 additional walnuts (4 total). After each, ask again until they say no or hit 3.

#### B5. People (optional, up to 5)

> AskUserQuestion: "Who matters most in your world right now? Give me a name and their role — like 'Ryn - engineering lead' or 'Jake - co-founder'. (press enter to skip)"

If they provide a person, store `name` and `role`. Then ask:

> AskUserQuestion: "Anyone else? (name - role, or press enter to finish)"

Repeat until they skip or hit 5 people.

#### B6. Context sources (optional)

> AskUserQuestion: "Where does your existing context live? Pick all that apply (comma-separated numbers, or press enter to skip):
> 1. Gmail (MCP)
> 2. Slack (sync script)
> 3. ChatGPT export
> 4. Claude Desktop export
> 5. Fathom/Otter transcripts
> 6. Apple Notes
> 7. Notion
> 8. Obsidian vault
> 9. GitHub (MCP)"

Parse their selection. Map each to a context source entry with the appropriate type:
- Gmail → `mcp_live`
- Slack → `sync_script`
- ChatGPT → `static_export`
- Claude Desktop → `static_export`
- Fathom/Otter → `static_export`
- Apple Notes → `static_export`
- Notion → `mcp_live`
- Obsidian → `markdown_vault`
- GitHub → `mcp_live`

All sources start with `status: available` unless they're MCP-based and the MCP server is already connected, in which case use `status: active`.

Do NOT ask about voice or preferences in the terminal flow. Defaults are fine. The human can customize later via `/alive:settings`.

#### B6b. Credential storage (optional)

> AskUserQuestion: "Where do you keep API keys and tokens? (default: ~/.env)"

Record the path for the `## Credentials` section in `.alive/key.md`. If they press enter or skip, use `~/.env`.

#### B7. Scaffold

Execute the scaffolding sequence (see **Scaffolding Procedure** below) using collected data.

#### B8. Present the completed world

Show the **After Setup** display (see below).

---

## Path C: Minimal Quick Start

### Trigger
User chose option 1 from the menu, or said something like "just set it up", "quick", "minimal".

### Steps

#### C0. Confirm location (single prompt — quick-start UX)

The shared prelude (P3) prompts with three choices. Quick-start collapses that to a single confirm because the whole point is "fewest choices possible":

> `AskUserQuestion`: "Set up your world at `~/alive/`?"
>
> Options (header `Location`):
> 1. **Yes** — use `~/alive/`
> 2. **Custom** — type a path

If **Custom**, free-text the path and run `validate_path_choice` per **P4**. The same UX rules apply as in P4: `deny` re-prompts, `confirm_required` enters the type-back loop. Path C does NOT short-circuit to `~/alive/` on a non-`allow` decision — silently overriding a user-typed Custom path with the recommended default would either drop the user's chosen location without warning (Yes-fallback feels deceptive) or apply system-path policy inconsistently across surfaces. The only divergence from Path B is the entry prompt: 2 options instead of 3, recognizing that the most common quick-start outcome is the recommended default.

#### C1. Name

> AskUserQuestion: "What's your name?"

#### C2. First walnut

> AskUserQuestion: "Name the most important thing you're working on right now."

Store as walnut name.

> AskUserQuestion: "Is that a venture, experiment, or life goal? (venture/experiment/life)"

Store as walnut type. Default to `venture` if unclear.

#### C3. Scaffold

Execute the scaffolding sequence with:
- `name` from C1
- `description`: empty (comment placeholder)
- `goal`: empty
- `timezone`: detect from system (`date +%Z` or similar)
- One walnut from C2 with `rhythm: weekly`, goal set to the walnut name
- No people
- No context sources
- All preferences as defaults (commented out in preferences.yaml)

#### C4. Present the completed world

Show the **After Setup** display (see below).

---

## Scaffolding Procedure

This is the shared build sequence. All three paths call this with their collected data.

### Input Data Shape

```
world:
  name: string (required)
  goal: string (optional, defaults to "")
  description: string (optional, defaults to "")
  timezone: string (optional, detect from system)

walnuts: array of:
  - name: string
    type: venture | experiment | life
    goal: string
    description: string (optional)
    rhythm: daily | weekly | monthly (default: weekly)

people: array of:
  - name: string
    role: string
    context: string (optional)

context_sources: object (optional)
  key: { type: string, status: string }

preferences: object (optional)
  key: value pairs for preferences.yaml

voice: object (optional)
  character: array of strings
  blend: string
  never_say: array of strings
```

### Execution Steps

Show progress as each item is created:

```
╭─ building your world...
│
```

#### Step 1: Domain folders

Create these directories (use `mkdir -p`):

```
01_Archive/
02_Life/
02_Life/goals/
03_Inbox/
04_Ventures/
05_Experiments/
02_Life/people/
.alive/
.alive/_squirrels/
```

Show:
```
│  ▸ 01_Archive/
│  ▸ 02_Life/
│  ▸ 02_Life/goals/
│  ▸ 03_Inbox/
│  ▸ 04_Ventures/
│  ▸ 05_Experiments/
│  ▸ 02_Life/people/
```

#### Step 2: World identity — .alive/key.md

Read the template from the plugin: `templates/world/key.md`

Replace template variables:
- `{{name}}` → world name
- `{{goal}}` → world goal (or empty string)
- `{{date}}` → today's date in YYYY-MM-DD format
- `{{timezone}}` → detected or provided timezone
- `{{description}}` → world description (or empty string)

If people were provided, fill in the `## Key People` section with entries like:
```
- **{{person.name}}** — {{person.role}}. [[{{person-name-slugified}}]]
```

And fill in the `## Connections` section with entries like:
```
- [[{{walnut-name-slugified}}]] — {{walnut.goal}}
```

If the human provided a credential storage path, fill in the `## Credentials` section:
```
env_file: {{env_file_path}}
```
If not provided, leave it as the template default (`~/.env`).

Write to `.alive/key.md`.

Show:
```
│  ▸ .alive/key.md (your identity)
```

#### Step 3: Preferences — .alive/preferences.yaml

Read the template from the plugin: `templates/world/preferences.yaml`

If preferences were provided (Path A only), uncomment the relevant lines and set values.

If voice config was provided (Path A only), uncomment the voice section and fill values.

If context sources were provided, uncomment the `context_sources:` section and add each source:
```yaml
context_sources:
  gmail:
    type: mcp_live
    status: available
    walnuts: all
```

For paths B and C with no explicit preferences, write the template as-is (all commented out = defaults).

Write to `.alive/preferences.yaml`.

Show:
```
│  ▸ .alive/preferences.yaml (defaults)
```

#### Step 4: Overrides — .alive/overrides.md

Read the template from the plugin: `templates/world/overrides.md`

Write as-is. No variable replacement needed.

Show:
```
│  ▸ .alive/overrides.md (your customizations)
```

#### Step 5: Create each walnut

For each walnut in the list:

**Determine the folder path:**
- `venture` → `04_Ventures/{{walnut-name-slugified}}/`
- `experiment` → `05_Experiments/{{walnut-name-slugified}}/`
- `life` → `02_Life/goals/{{walnut-name-slugified}}/`

**Slugify the name:** lowercase, spaces to hyphens, strip non-alphanumeric except hyphens. Examples: "Nova Station" → "nova-station", "Glass Cathedral" → "glass-cathedral".

**Create the directory structure:**
```
{{domain}}/{{slug}}/
{{domain}}/{{slug}}/_kernel/
```

**Create walnut files from templates:**

For each file in `templates/walnut/` (key.md, log.md, insights.md):

Read the template. Replace variables:
- `{{name}}` → walnut display name (original casing)
- `{{type}}` → walnut type (venture/experiment/life)
- `{{goal}}` → walnut goal
- `{{description}}` → walnut description (or goal repeated if no separate description)
- `{{date}}` → today's date in YYYY-MM-DD format
- `{{session_id}}` → current session ID (from stdin JSON or "setup")
- `{{next}}` → "Define first outcomes and tasks"

For key.md specifically:
- Set `rhythm:` to the walnut's rhythm value
- If people are associated with this walnut, fill the `## Key People` section

Write each file to `{{domain}}/{{slug}}/_kernel/{{filename}}`. Do NOT create `_kernel/_generated/` — v3 kernels are flat. Do NOT create `bundles/` — v3 bundles live flat at the walnut root and are created on demand by `/alive:bundle`. The `tasks.json` + `completed.json` + `now.json` files are created lazily by `scripts/tasks.py` and `scripts/project.py` on first use.

Show:
```
│  ▸ {{domain}}/{{slug}}/
│  ▸   _kernel/key.md — "{{goal}}"
│  ▸   _kernel/log.md — first entry signed
│  ▸   _kernel/insights.md — empty, ready
```

#### Step 6: Create people walnuts

For each person in the list:

**Slugify the name:** "Ryn Okata" → "ryn-okata", "Mira Solaris" → "mira-solaris"

**Create the directory structure:**
```
02_Life/people/{{slug}}/
02_Life/people/{{slug}}/_kernel/
```

**Create walnut files from templates:**

Use the same `templates/walnut/` templates with:
- `{{name}}` → person's display name
- `{{type}}` → `person`
- `{{goal}}` → person's role
- `{{description}}` → person's context (or role if no context)
- `{{date}}` → today
- `{{session_id}}` → current session ID or "setup"
- `{{next}}` → ""

Show:
```
│  ▸ 02_Life/people/{{slug}}/
│  ▸   _kernel/key.md — "{{role}}"
```

#### Step 7: Persist `~/.config/alive/world-root` (config-file contract)

This is the FINAL scaffolding step before the progress box closes. It runs AFTER every domain dir and `.alive/` are on disk so that re-running setup over the same target re-writes the same bytes (atomic no-op) and a target switch overwrites atomically. The write goes through T1's `write_world_root_file`, which lexically normalizes, validates the input is absolute, and uses `_atomic_io.atomic_write_text` (mode 0600, parent dir 0700) — never an inline `echo > file`. Doing the write BEFORE the closing "Done." line means the user only sees "Done" iff every step (including this one) succeeded.

```bash
source "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/alive-common.sh"
if ! write_world_root_file "$WORLD_ROOT_TARGET"; then
  printf 'alive setup: failed to persist world-root pointer at %s\n' \
    "${HOME}/.config/alive/world-root" >&2
  exit 1
fi
```

Locked properties:

- **Sole-emitter contract.** Setup is one of only three writers of `~/.config/alive/world-root` (the others are `alive doctor --fix` (T7) and `.walnut` import (T8)). No other surface writes this file.
- **Atomic.** `write_world_root_file` writes to a sibling tempfile, `chmod 0600`, then `mv -f` — there is no half-written file at any moment, even if the process is killed.
- **Idempotent same-target.** Re-running setup over an existing valid world with the same `$WORLD_ROOT_TARGET` writes byte-identical content; the atomic rename is observably a no-op.
- **Atomic different-target.** Re-running setup with a different `$WORLD_ROOT_TARGET` (e.g. user moves their world) replaces the pointer atomically; readers see either the old path or the new path, never garbage.
- **Ordering.** This step runs LAST among scaffolding writes, after Step 1 (domain dirs + `.alive/`) and Steps 2–6 (kernel + walnut files), and BEFORE Step 8 closes the progress box. Writing the pointer earlier would briefly leave the file pointing at a target that `validate_world_root` would diagnose as `MISSING_MARKER`; writing it after the "Done." line would let users see "Done" before the contract is actually established.

#### Step 8: Close the progress box

```
│
│  Done. Five domains. {{walnut_count}} walnuts. Your world is alive.
╰─
```

---

## After Setup (all paths converge here)

Display this summary. Fill in actual values for every placeholder.

```
╭─ your world is alive
│
│  World: {{WORLD_ROOT_TARGET}}
│  Config: ~/.config/alive/world-root → {{WORLD_ROOT_TARGET}}
│  Walnuts: {{comma-separated list of walnut names with their domain}}
│  People: {{comma-separated list of people names, or "none yet"}}
│  Context sources: {{comma-separated list, or "none yet"}}
│
│  12 skills ready:
│    world · load · save · capture · find · create · tidy · tune · history · mine · extend · map
│
│  Say "load {{first-walnut-name}}" to start working.
│  Say "world" anytime to see everything.
│
│  → Build your world.
╰─
```

---

## What Setup Creates

| Path | Purpose |
|------|---------|
| `01_Archive/` | Graduated walnuts |
| `02_Life/people/` | Person walnuts |
| `02_Life/goals/` | Life goals |
| `03_Inbox/` | Buffer — content arrives, gets routed out within 48h |
| `04_Ventures/` | Revenue intent |
| `05_Experiments/` | Testing grounds |
| `.alive/key.md` | World identity (name, goal, timezone, people, connections) |
| `.alive/preferences.yaml` | Toggles, context sources, voice config |
| `.alive/overrides.md` | User rule customizations (never overwritten by updates) |
| `.alive/_squirrels/` | Centralized session entries |
| `[walnut]/_kernel/key.md` | Walnut identity and standing context |
| `[walnut]/_kernel/now.json` | Current state projection (computed by `scripts/project.py`) |
| `[walnut]/_kernel/log.md` | Prepend-only event spine |
| `[walnut]/_kernel/tasks.json` | Walnut-scoped task queue (managed by `scripts/tasks.py`) |
| `[walnut]/_kernel/insights.md` | Evergreen domain knowledge |
| `[walnut]/{bundle-name}/` | Self-contained units of work (flat v3 layout, at walnut root) |

## What Setup Does NOT Do

- Import or index existing context (use `/alive:mine-for-context` after setup)
- Configure MCP integrations (use `/alive:settings`)
- Set up voice customization in terminal paths (use `/alive:settings`)
- Create the walnut.world link (use `/alive:settings`)
- Symlink rules or agents.md (handled by session-new hook, not setup)
