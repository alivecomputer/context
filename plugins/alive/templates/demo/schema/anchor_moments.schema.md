# Stage 1 Anchor Moments envelope -- schema (v0.1)

Stage 1 of the `/alive:demo` generation pipeline confirms (or rewrites)
the anchor moments the spine proposed and writes a single JSON envelope
to `<partial>/_stage_outputs/anchor_moments.json`. That envelope is
read by Stages 3 + 4: Stage 3's timeline cites the confirmed moments,
Stage 4's insights ground recurring patterns against them.

This document is the **human-readable canonical schema description**.
There is no separate `.json` Draft 2020-12 descriptor for the envelope
because Stage 1 is UX-only -- the validator
(`stages/stage3._validate_anchor_envelope`,
`stages/stage4.load_anchors`) reads the envelope directly via stdlib
parsing and rejects malformed shapes with explicit errors.

**Stdlib-only validation** is the locked epic policy
(`.flow/specs/fn-2-2zz.md` § "Why stdlib-only validation"). The
envelope is parsed via `json.loads` only.

## Top-level shape

```json
{
  "schema_version": "0.1",
  "confirmed": [ ... ],
  "frozen": true,
  "frozen_at": "2026-04-29T12:00:00Z"
}
```

* `schema_version` (string, required) -- always exactly `"0.1"` for
  v3.2 demo. Mismatch produces a fatal validation error.
* `confirmed` (array, required) -- list of confirmed anchor-moment
  objects (shape below). The list is non-empty when `frozen=true`.
* `frozen` (bool, required) -- `true` iff the human confirmed the
  anchors and Stage 1 is done. Stages 3 + 4 refuse to dispatch unless
  this is `true`.
* `frozen_at` (`string | null`, optional) -- ISO 8601 UTC timestamp
  of the freeze. Set to a string when `frozen=true`; set to `null`
  while the envelope is in the in-progress draft state. The key
  itself may be omitted entirely on draft envelopes; the validator
  treats missing and `null` identically.

## Per-moment shape

Each `confirmed[i]` is an object with the same key set as
`spine.anchor_moments[i]` (Stage 0). The same closed key set applies
(`additionalProperties: false`). Required fields:

* `slug` (string) -- matches `^[a-z0-9]+(-[a-z0-9]+)*$`. Unique within
  `confirmed`.
* `name` (string) -- non-empty display title.
* `date` (string) -- ISO 8601 `YYYY-MM-DD`, zero-padded. Must fall
  within the spine's `time_span.start..end`.
* `summary` (string) -- non-empty 1-3 sentence description.
* `walnut_slugs` (array of slug) -- the walnuts the moment touches.
  Every slug must resolve to a real `walnut_roster[*].slug`.
* `people_slugs` (array of slug) -- the people the moment involves.
  Every slug must resolve to a real `people_roster[*].slug`.

## Coherence (cross-stage)

These rules are NOT enforced by the per-stage validators in isolation
(Stage 1 is UX, Stage 0 only validates the spine's draft anchors). They
are enforced by `skills/demo/validate.py`'s
`_validate_stage_0` cross-reference layer:

* Every `confirmed[i].date` lies in `[time_span.start, time_span.end]`
  (lex compare on YYYY-MM-DD).
* Every `walnut_slugs` / `people_slugs` resolves into the spine
  rosters.

## Failure classification

* **Fatal**: `schema_version` mismatch, missing/invalid top-level
  shape, `confirmed` not an array.
* **Retryable**: any per-moment shape error, any cross-reference
  mismatch (the dispatcher can re-prompt the LLM).

## Notes

* `frozen=false` is a normal in-progress state during Stage 1 UX. No
  validator treats it as an error; only the Stage 3 + 4 dispatchers
  refuse to advance. The `_validate_stage_0` cross-reference layer
  reads anchors from `spine.anchor_moments` (the un-confirmed draft
  set) before Stage 1 runs, so the layer works against either source.
