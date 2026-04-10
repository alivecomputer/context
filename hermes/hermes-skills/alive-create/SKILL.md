---
name: alive-create
description: Scaffold a new walnut -- venture, experiment, person, life area, project
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
triggers:
  - "create walnut"
  - "new walnut"
  - "new project"
  - "new experiment"
  - "new person"
metadata:
  hermes:
    tags: [ALIVE, context, create, walnut]
---

# Create Walnut

Something new is emerging. It needs its own walnut.

## Ask

```
What kind of walnut?
  1. Venture (04_Ventures/) -- revenue intent
  2. Experiment (05_Experiments/) -- testing grounds
  3. Person (02_Life/people/) -- someone entering the orbit
  4. Life area (02_Life/) -- personal goal or domain
  5. Project (nested under existing walnut)
```

Then ask:
- **Name:** kebab-case (e.g., `nova-station`)
- **Goal:** one sentence
- **Rhythm:** daily / weekly / biweekly / monthly

## Scaffold

Create via terminal:

```bash
WALNUT_PATH="$ALIVE_WORLD_ROOT/[domain]/[name]"
mkdir -p "$WALNUT_PATH/_kernel"
```

Write three kernel files:

### key.md
```markdown
---
type: [venture/experiment/person/life/project]
goal: [one sentence]
created: [today]
rhythm: [rhythm]
people: []
tags: []
links: []
---

[Brief description of what this walnut is about.]
```

### log.md
```markdown
---
walnut: [name]
created: [today]
last-entry: [today]
entry-count: 1
summary: Walnut created.
---

## [today] -- squirrel:[session_id]

**Type:** creation

Walnut created. [Goal]. [Any initial context from conversation.]

signed: squirrel:[session_id]
```

### insights.md
```markdown
---
walnut: [name]
updated: [today]
squirrel: [session_id]
sections: []
---
```

### tasks.json + completed.json
```bash
echo '{"tasks": []}' > "$WALNUT_PATH/_kernel/tasks.json"
echo '{"completed": []}' > "$WALNUT_PATH/_kernel/completed.json"
```

### Run projection
```bash
python3 "$ALIVE_WORLD_ROOT/.alive/scripts/project.py" --walnut "[relative_path]"
```

## After Creation

- If this walnut is a sub-walnut, add `parent: [[parent]]` to key.md
- Add `[[name]]` to parent's key.md links
- Offer to create the first bundle
