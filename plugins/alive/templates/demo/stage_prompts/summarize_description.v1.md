{{subagent_brief}}

---

# Stage 0 sub-stage — description summariser

The persona description below exceeds the soft 4 000-token cap and would
otherwise blow the Stage 0 spine prompt past its budget. Your job is to
compress it into a faithful, lossless-as-possible summary that still gives
the Stage 0 spine generator everything it needs to pick walnuts, people,
bundles, and anchor moments.

## Input (full text retained on disk; you read it here)

```
{{description}}
```

## Output contract

Write a single Markdown file to:

```
{{output_path}}
```

via `_common.atomic_write_text`. Do not paste the summary in your return —
the dispatcher reads it off disk. Return ONE LINE acknowledging completion.

## What the summary must preserve

Stage 0 will generate the world's spine from your output, so do not strip
specifics it will need. Keep:

- **Every named person** — full name + relationship + one sentence of role.
- **Every named venture, project, or life-area** — name + one-paragraph
  description of what it is, status, scale.
- **Every concrete moment** — anything dated or date-ish ("first cheque
  in 2019", "Q3 2024 pivot", "wedding in May"). These become anchor moments.
- **Time span clues** — earliest and latest dates implied anywhere in the
  text. If the description spans 2019–2026, say so.
- **Tone / voice cues** — phrases that hint at how this person talks
  (formal, wry, self-deprecating, brusque, etc).

## What you may compress

- Repetition. If the description re-states a fact three different ways,
  state it once.
- Backstory that doesn't bear on the world's structure. We don't need
  three paragraphs on what the persona's parents did unless they're a
  named person in their world.
- Filler ("As I mentioned earlier...", "It's worth noting that...").

## Length target

Aim for **800–1 200 words** of dense Markdown. Use headed sections so the
Stage 0 prompt can scan quickly:

```
## Persona
<one paragraph>

## People
- <name> — <relationship>, <one sentence>
...

## Walnuts
- <name> — <type>, <one paragraph>
...

## Anchor moments (chronological)
- <date or date-ish> — <name>: <one sentence>
...

## Time span
<earliest> – <latest>

## Tone
<short list of voice/tone words>
```

## What you must NOT do

- Invent facts. If the description doesn't say it, don't write it.
- Paraphrase quotes. If the persona is described as saying something
  specific, lift it verbatim into your summary.
- Decide the world's shape. You compress; Stage 0 picks. Leave the
  `walnuts → people → bundles → anchors` structure to Stage 0.

## Return value

```
summary written to <output_path>
```
