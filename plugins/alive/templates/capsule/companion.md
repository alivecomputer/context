---
type: capsule
goal: {{goal}}
status: draft
version: v0.1
sensitivity: private
pii: false
created: {{date}}
updated: {{date}}
squirrels: [{{session_id}}]
# active_sessions format when populated:
# - session: a8c95e9
#   engine: claude-opus-4-6
#   started: 2026-03-12T14:00:00
#   working_on: "v0.3 — restructuring intro"
active_sessions: []
sources: []
shared: []
# When populated:
# shared:
#   - to: Name
#     method: email
#     date: YYYY-MM-DD
#     version: capsule-name-draft-01.md
linked_capsules: []
tags: []
---

## Summary
<!-- What this capsule exists to produce. 2-5 sentences. -->

## Current State
<!-- Where things are right now. What's been done, what's next. -->

## Decisions
<!-- Key decisions made during work on this capsule.
     Format: - **Decision** — rationale @session_id -->

## Open Questions
<!-- Unresolved issues that affect this capsule. -->

## Tasks

Capsule-scoped work items. Same syntax as walnut tasks.md.

- [ ] Example task  @session_id

## Changelog

Version history. Every version after v0.1 gets a brief note.

### v0.1 (YYYY-MM-DD, session:xxxxxxxx)
Initial draft.

## Work Log

Append-only. Each session adds at the bottom. Never edit previous entries.

### session:xxxxxxxx (YYYY-MM-DD HH:MM)
engine: model-name
- What was done
- What was changed
- What was flagged
