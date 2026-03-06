---
name: housekeeping
description: "Use when the human asks for a system check, mentions anything stale or broken, or wants maintenance — scans squirrel activity across all walnuts then surfaces unsigned entries, stale walnuts past their rhythm, orphan references, stale drafts, overdue tasks, and unrouted inputs one issue at a time."
user-invocable: true
---

# Housekeeping

System housekeeping. Starts by scanning what squirrels have been up to, then surfaces issues one at a time with recommended fixes.

Not a dashboard (that's world). Not a search (that's find). Not session recall (that's recall). Pure maintenance.

---

## Step 1: Squirrel Scan

Before checking for problems, get the lay of the land. Scan `_core/_squirrels/` across all walnuts (or the current walnut if one is open).

```
╭─ 🐿️ housekeeping — recent squirrel activity
│
│  orbit-lab       5 sessions this week (3 signed, 2 unsigned)
│  nova-station    1 session (signed)
│  glass-cathedral 0 sessions in 12 days
│
│  2 unsigned entries need attention. Starting there.
╰─
```

---

## Step 2: Issues (one at a time, with recommended fix)

Each issue surfaces with context AND a recommended action. The human picks.

### 2a. Unsigned Squirrel Entries

```
╭─ 🐿️ housekeeping — unsigned session
│  nova-station / squirrel:a3f7b2c1 — started Feb 21, never signed
│  Has 4 stash items that were never saved.
│
│  Recommended: review the stash — it may contain unrouted decisions.
│  → review stash / clear entry / skip
╰─
```

### 2b. Stale Walnuts Past Rhythm

| Rhythm | Quiet at | Waiting at |
|--------|----------|-----------|
| daily | 2 days | 4+ days |
| weekly | 2 weeks | 4+ weeks |
| fortnightly | 3 weeks | 6+ weeks |
| monthly | 6 weeks | 3+ months |

```
╭─ 🐿️ housekeeping — stale walnut
│  midnight-frequency has been quiet for 18 days (rhythm: weekly)
│  Last entry: Feb 5 — "locked episode 11 structure"
│
│  Recommended: open it and check if it's still active, or change rhythm.
│  → open it / archive it / change rhythm / skip
╰─
```

### 2c. References Without Companions

```
╭─ 🐿️ housekeeping — orphan reference
│  nova-station / _references/documents/raw/2026-02-15-vendor-proposal.pdf
│  No companion file. Can't scan without loading the full PDF.
│
│  Recommended: create a companion with description + summary.
│  → create companion now / skip
╰─
```

### 2d. Working Files Older Than 30 Days

```
╭─ 🐿️ housekeeping — stale draft
│  glass-cathedral / _working/submission-draft-v0.1.md
│  Last modified: Jan 15 — 39 days ago.
│
│  Recommended: if it's done, promote to v1. If it's dead, archive it.
│  → promote to v1 / archive / delete / skip
╰─
```

### 2e. Tasks Overdue or Stale

```
╭─ 🐿️ housekeeping — stale task
│  nova-station / "Book ground control sim" — added Feb 10, no progress
│
│  Recommended: check if it's still relevant. If blocked, note what's blocking it.
│  → still relevant / remove / reprioritize / blocked (note why) / skip
╰─
```

### 2f. Inputs Buffer (> 48 hours)

```
╭─ 🐿️ housekeeping — unrouted input
│  03_Inputs/ has 2 items older than 48 hours:
│   - vendor-brochure.pdf (3 days)
│   - meeting-notes-feb20.md (4 days)
│
│  Recommended: route these via alive:capture — they may contain
│  decisions or context that affects active walnuts.
│  → route them / skip
╰─
```

### 2g. now.md Stale or Thin

If now.md context paragraph is empty, hasn't been updated in 2+ weeks, or doesn't reflect the recent log entries.

```
╭─ 🐿️ housekeeping — thin now.md
│  nova-station / now.md context paragraph is 1 sentence.
│  Last 3 log entries cover: test window, shielding vendor, telemetry review.
│
│  Recommended: rewrite now.md to synthesise recent sessions.
│  → rewrite now / skip
╰─
```

---

## Presentation

**One at a time.** Surface the highest priority issue with a recommended fix. Let the human deal with it. Then surface the next.

```
╭─ 🐿️ housekeeping complete
│  5 issues found, 4 resolved, 1 skipped
│  World is healthy.
╰─
```
