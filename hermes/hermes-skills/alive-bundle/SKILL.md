---
name: alive-bundle
description: Create, manage, and graduate bundles -- the unit of focused work within a walnut
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
triggers:
  - "create bundle"
  - "new bundle"
  - "bundle"
  - "start a bundle"
  - "graduate bundle"
metadata:
  hermes:
    tags: [ALIVE, context, bundle, work]
---

# Bundle Management

Bundles are self-contained units of work inside a walnut. Any folder with a `context.manifest.yaml` is a bundle.

## Create

```
What's this bundle for?
  1. Outcome -- produces something specific (deliverable, has a done state)
  2. Evergreen -- accumulates context over time (collection, ongoing)
```

Ask for:
- **Name:** kebab-case (e.g., `website-rebuild`)
- **Goal:** one sentence describing the deliverable or collection

Scaffold:

```bash
BUNDLE_PATH="$WALNUT_PATH/[bundle-name]"
mkdir -p "$BUNDLE_PATH/raw"
```

Write `context.manifest.yaml`:

```yaml
goal: "[goal]"
status: draft
version: v0.1
sensitivity: private
pii: false

created: [today]
updated: [today]

context: |
  Bundle created. [Initial context from conversation.]

sources: []
linked_bundles: []
tags: []

squirrels: [[session_id]]
active_sessions:
  - session: [session_id]
    engine: hermes-agent
    started: [now]
    working_on: "Initial setup"
```

Write `tasks.json`:
```json
{"tasks": []}
```

## Status Lifecycle

```
draft -> prototype -> published -> done
```

- **draft** -- actively worked on, markdown only
- **prototype** -- has a visual (HTML), maybe shared with 1-2 people
- **published** -- shared externally
- **done** -- outputs complete, bundle stays as historical record

## Graduate

When a `*-v1.md` or `*-v1.html` file exists:

```
v1 exists. Graduate this bundle?
  1. Yes, mark as done
  2. Yes, mark as published
  3. Not yet
```

Flip `status:` in manifest. Bundle folder stays where it is.

## Routing

When content arrives:
- Same goal -> same bundle
- Related but different goal -> new bundle, link to existing
- Ambiguous -> ask once
