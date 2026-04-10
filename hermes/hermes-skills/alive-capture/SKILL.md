---
name: alive-capture
description: Capture external content into a walnut bundle -- emails, transcripts, screenshots, documents, research
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
triggers:
  - "capture this"
  - "save this to"
  - "store this"
  - "route this"
metadata:
  hermes:
    tags: [ALIVE, context, capture, content]
---

# Capture Context

External content arrives -- email, transcript, file, research output. Route it into the system.

## Detect Content Type

- Pasted text, forwarded message -> extract and store
- File path mentioned -> read and classify
- Research results from a cron or web search -> structure and route
- Screenshot -> store in bundle raw/

## Route Decision

```
This looks like [type]. Route it?
  1. Add to [active bundle] raw/
  2. Create new bundle for it
  3. Route to [suggested walnut]
  4. Just stash it for now
```

## Capture Process

1. **Name the file.** Date-prefixed, descriptive: `2026-04-07-witcheer-feedback.md`
2. **Store in bundle raw/.** Write the content to the active bundle's `raw/` directory.
3. **Update manifest.** Add to `sources:` in `context.manifest.yaml`:

```yaml
sources:
  - path: raw/2026-04-07-witcheer-feedback.md
    description: Telegram feedback on ALIVE x Hermes spec
    type: document
    date: 2026-04-07
```

4. **Extract stash items.** Any decisions, tasks, insights, people mentions, or sharp quotes get stashed.
5. **Rename garbage filenames.** `CleanShot 2026-02-23...` becomes `2026-02-23-descriptive-name.ext`. Preserve original in manifest as `original_filename:`.

## If No Bundle Active

Ask which walnut this belongs to, then offer to create a bundle or add to an existing one.

## If No Walnut Active

Check 03_Inbox/ for unrouted files. Enter inbox scan mode -- present each file with routing suggestions.
