---
walnut: {{name}}
created: {{date}}
last-entry: {{date}}
entry-count: 1
summary: {{name}} created.
---

<!-- LOG — Where it's been. The event spine. Prepend only.

     ENTRY STRUCTURE (use as many sections as relevant, omit empty ones):

     ## YYYY-MM-DDTHH:MM:SS — squirrel:session_id

     [2-4 sentence narrative of what happened and why it matters]

     ### Decisions
     - **Decision name** — rationale (WHY this was chosen, what was considered)

     ### Work Done
     - What was built, written, edited, shipped (concrete outputs with file paths)

     ### Tasks
     - [ ] New task created
     - [x] Task completed (date)
     - Reprioritised tasks with context

     ### References Captured
     - type: description — companion path

     ### Quotes
     - "Verbatim quote worth keeping" — attribution

     ### Next
     - What the next squirrel should pick up (feeds into now.md next:)

     signed: squirrel:session_id

     RULES:
     - Prepend only. Newest at top.
     - Every entry signed.
     - Wrong entry? Correction above, never edit.
     - At 50 entries or phase close → chapter.
     - A future squirrel should reconstruct the session from the entry alone.
-->

## {{date}} — squirrel:{{session_id}}

Walnut created. {{goal}}

signed: squirrel:{{session_id}}
