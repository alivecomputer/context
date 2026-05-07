# /alive:demo

Scaffold a believable, lived-in ALIVE world from a one-paragraph persona description, then activate it as your live world.

## Purpose

`/alive:demo` produces a demo world you can drive the squirrel against. Two paths:

- **Preset (sandbox-testing).** Hand-authored Nova Station world. No LLM. ~10 seconds. Used for skill development, regression tests, and quick smoke runs.
- **Custom (persona-driven).** A 5-stage subagent pipeline reads a one-paragraph persona description and writes a full kernel: walnuts, people, bundles, log entries, insights, anchor moments. ~90 seconds to ~5 minutes depending on persona size.

Audiences:

- ALIVE developers who need a non-empty world to iterate against without polluting their live one.
- Anyone running an investor or onboarding walkthrough who wants to show what ALIVE looks like after weeks of scatter-hoarding rather than starting from an empty `_kernel/`.
- Future contributors evaluating the system end to end.

How it works at 1000 ft: stages 0 through 4 are LLM subagents that write JSON and markdown to a partial directory on disk. Stage 5 is deterministic Python: it promotes the partial to a real world, generates the projection cache, and atomically flips `~/.config/alive/world-root` to the new path. Failure at any earlier step leaves your live pointer untouched.

Privacy posture: the preset path is fully local and deterministic; nothing crosses the network. The custom path dispatches LLM subagents through your configured Claude Code runtime, so persona text reaches the model the same way any other Claude Code prompt does. The skill itself does not phone home and does not write persona text to any disk location outside the partial directory on your machine.

## Quick start

Preset path (no LLM, deterministic):

```
alive demo preset run --preset realistic-seeded
```

Wait ~10 seconds, restart Claude Code (Cmd+Q + relaunch), then run `/alive:world` to see Nova Station.

Custom path (persona-driven): run `/alive:demo`, pick `Custom (persona-driven)` at the no-args router prompt, then follow the persona intake block in `create.md`. The orchestrator walks Stage 0 (spine) -> Stage 1 (anchor confirmation, via `anchor_confirm.md`) -> Stage 2 (entity prose, parallel subagents) -> Stage 3 (timeline, single subagent) -> Stage 4 (insights, single subagent) -> Stage 5 (deterministic activation transaction) end-to-end. Each stage validates on disk before the next stage dispatches; on a double-validation-failure the orchestrator surfaces a 3-option block (accept partial, retry full, cancel). Activation flips `~/.config/alive/world-root` atomically as the single commit point. After activation, restart Claude Code to pick up the new world index.

## Example invocations

### 1. Preset: sandbox-testing (Nova Station)

Use when iterating on demo skill code itself, or running pytest against the preset.

```
alive demo preset run --preset realistic-seeded
```

```
╭─ activated, restart Claude Code
│  Demo world activated:
│    wld_01j5h... , sandbox-realistic-seeded
│    /Users/<you>/.alive-demos/wld_01j5h...
│
│  Restart Claude Code to pick up the new world index.
╰─
```

On disk: one venture walnut (`nova-station`), two people walnuts (`ryn-okata`, `jax-stellara`), two bundles, 5 log entries, 7 insights, pre-baked `completed.json`. Provenance and authoring rules in `preset/realistic-seeded/README.md`.

### 2. Custom small: web agency

Single venture, two collaborators, no family side. Total runtime ~90 seconds. Stage 2 fans out 4 entity subagents.

Persona input:

```
Maya runs a two-person web agency in Brisbane. She handles strategy
and account work; her partner Theo writes code. They have one anchor
client (ExampleCorp) on retainer and three smaller projects. Maya
wants to track decisions and follow-ups across all four engagements
without a CRM. She codes in the mornings and reviews work in the
afternoons.
```

After Stage 1 confirmation, Stage 5 promotes:

```
~/.alive-demos/wld_01j5h.../
├── 04_Ventures/maya-web-agency/
│   ├── _kernel/{key,log,insights}.md, tasks.json, now.json
│   └── examplecorp-retainer/         # bundle
└── 02_Life/people/{theo-okumura, maya-oraio}/
```

Activated world has 8 to 12 log entries spanning Q4 2025 to Q1 2026.

### 3. Custom medium: the angel investor

The hero example. Multi-walnut: two ventures, one angel-portfolio rolling tracker, family side, partner, two kids, dog.

Persona snippet:

```
An angel investor in their early forties runs a diversified
early-stage portfolio across consumer brands and climate tech.
They led the seed round in ClientA; they are also an early
backer of ClientB. The rest of their angel work lives in a
single rolling tracker called the Angel Portfolio.
```

Anchor moments confirmed in Stage 1 typically include:

- The day ClientA closed its seed.
- The day ClientB first hit break-even on a single SKU.
- The Saturday last spring when the dog learned to swim.
- The day the family signed the lease on the small house.

Total runtime ~3 minutes. Stage 3 (timeline) is the longest single pass.

Proof point after activation: the squirrel can answer "what was your last move on ClientA?" by citing a real log entry from the scaffolded world rather than guessing.

### 4. Custom large: indie game studio

Full team of seven to ten people, two ventures (the studio and a side experiment), dense bundle distribution. Stage 2 fans out 12+ entity subagents in batches of 6. Total runtime ~5 minutes.

Persona input describes the studio lead, two co-founders, four contractors, and a publisher relationship; Stage 0 produces a spine with 9 to 12 anchor moments covering hiring, a near-miss launch slip, and the publisher pivot.

```
╭─ stage 2 dispatch
│  14 entities to generate (8 persons, 2 walnuts, 4 bundles)
│  batched 6 calls per assistant turn
│
│  Output: _stage_outputs/entities/<slug>/
╰─
```

Watch this tier carefully on first run; persona text over ~600 words is summarised by Stage 0 before being fed into the spine generator.

### 5. Resume from partial after Ctrl+C

If Stage 3 dies mid-pass (timeout, validator double-failure, or an interrupted session), the partial directory survives at `~/.alive-demos/wld_<ulid>.partial/` and demo-state.json carries the failure marker.

```
alive demo resume
```

CLI lists every resumable partial:

```
╭─ multiple resumable partials
│  2 resumable partial generation(s):
│
│    1. angel-investor-portfolio , wld_01h... (failed at 3_timeline,
│       reason: validation_double_failure)
│    2. nova-station-experiment , wld_01g... (failed at 5_promote,
│       reason: projection_failure)
│
│  Pick one and re-run:
│    alive demo resume <ulid>
╰─
```

Pick one to see its retry plan. The squirrel re-fires the failing stage's subagent dispatch; the per-stage freeze step clears the failure marker on success. Atomic-write failures (stage 9, mode 15c) leave demo-state.json intact and require the human to resolve disk or permission cause first.

## Subcommand reference

| Subcommand | What it does |
| --- | --- |
| `/alive:demo` (no args) | Router prompt: preset vs. custom. On `Preset`, runs the realistic-seeded sandbox path. On `Custom`, hands control to `create.md`, which walks Stage 0 through Stage 5 end-to-end with bordered blocks at every user decision point. |
| `alive demo list` | 6-column table of every promoted demo world plus in-flight partials. Active world flagged `*active`. |
| `alive demo activate <ref>` | Switch to a previous demo. `<ref>` is a label, a ULID prefix (>=3 chars), or a full `wld_<ulid>`. |
| `alive demo deactivate` | Restore the cached previous world-root pointer; clear demo state. |
| `alive demo delete <ref>` | Remove a demo world from disk. Refuses on the active world. Two-step confirmation (CLI returns `needs_confirmation` first; squirrel re-runs with `--confirm`). |
| `alive demo status` | 5- to 7-line block summarising the active demo + cached previous pointer. |
| `alive demo resume [partial_id]` | List or retry a partial that failed mid-pipeline. |
| `/alive:demo reset` | Rebuild `~/.config/alive/demo-state.json` from the live world-root pointer (recovery from schema mismatch). |

Each non-create subcommand's prose lives in a sibling `.md` next to this README (`list.md`, `activate.md`, `deactivate.md`, `delete.md`, `status.md`, `resume.md`, `anchor_confirm.md`).

## Edge-case FAQ

**What happens to my live world when I activate a demo?**

The pointer at `~/.config/alive/world-root` flips atomically to the new demo path. Your live world stays intact on disk. `alive demo deactivate` flips the pointer back to the cached `previous_world_root`.

**Why do I have to restart Claude Code after activation?**

The squirrel resolves world-root once at session start (the WORLD_INDEX injection). The pointer flip needs a fresh session to take effect. `Cmd+Q` and relaunch, or run `/alive:world` to re-render against the new pointer in the same session.

**Can I run multiple demos in parallel?**

One demo is active at a time. `alive demo list` shows everything you have scaffolded and which one (if any) the world-root currently points at.

**What if a stage fails?**

Stages 0, 2, 3, and 4 each ship one auto-retry with feedback. On a second failure you get a bordered block with three options: accept partial (proceed with current outputs), retry full (re-dispatch the whole stage), or cancel (stops the orchestrator; the partial stays on disk as a resumable handle for `alive demo resume <ref>`, or run `alive demo delete <ref>` to destroy it explicitly). Stage 5 is deterministic Python; failures at any of its first nine steps leave the world-root pointer untouched.

**How long does the custom path take?**

Small persona ~90 seconds. Medium (angel-investor shape) ~3 minutes. Large (full studio team) ~5 minutes. Stage 3 (timeline materialisation) is the single longest pass.

**Is the world I scaffold real?**

No. Names are synthetic; surnames come from an allowlist (`templates/demo/synthetic_surnames.txt`). The preset path is fully local. The custom path sends persona text through your configured Claude Code runtime as part of subagent dispatch (the same path any other Claude Code prompt takes); the skill itself does not phone home and does not write persona text outside the partial directory on your machine.

**Can I edit the scaffolded world after activation?**

Yes. Once activated it is a normal ALIVE world. The squirrel stash + save loop, `/alive:capture-context`, and all other skills work on it.

**How do I share a demo with someone else?**

Outside this skill's scope. The world is on your filesystem. P2P share + receive for demo worlds is on the v4 roadmap.

## Walkthrough recording

A 5-7 minute walkthrough of the Custom-path angel-investor scenario (persona paste, anchor confirmation, stage 2 through 5, the activated world, and a context-grounded query proof moment) is captured by the maintainer outside the plugin tree before the v3.2 public PR opens. The artifact lives in the sibling `alive-demo/raw/` directory of the same project; it is not committed to this repository, and this README does not assert its presence at any point in time.
