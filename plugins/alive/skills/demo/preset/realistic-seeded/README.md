# Realistic-Seeded Preset (Nova Station)

Lean, deterministic preset world for sandbox-testing the `/alive:demo` skill end-to-end without firing the LLM pipeline. Voiced in ALIVE narrative tone (vivid, specific, present-tense for current state, second-person for log entries, no closure, no em-dashes).

## Provenance

- Inspired by the older `sandbox-environment v0.1` bundle but NOT copied. fn-2-2zz.11 spec called for a LEAN REIMPL; depth lives only where skill mechanics need exercising.
- Pre-validated at author time. The preset content does NOT pass through the coherence-invariant validators (`validate.py`); the trade-off is intentional, the preset is for DEVS testing the skill, not for investor demos.
- Synthetic surnames (Okata, Stellara) carry over from prior preset work in the demo skill's persona allowlist.

## Used for

- Skill development (iterate on stage code without burning LLM budget on regenerating worlds).
- Regression testing (the test suite asserts the preset round-trips through `preset.run_preset` cleanly).
- Internal "want a demo world fast for WIP test" scenarios.

## NOT used for

- Investor demos. Use the custom persona path (`/alive:demo create` then choose `Custom`) for narrative-rich worlds.
- Coherence-invariant validation. The preset does not exercise the Stage 4 citation resolver or the anchor-or-pattern rule.

## Shape

```
realistic-seeded/
  _world_meta.json                          # walnut + bundle + session manifest
  README.md                                 # this file
  04_Ventures/
    nova-station/
      _kernel/
        key.md                              # venture key (phase: testing)
        log.md                              # 5 log entries across Q1 2026
        insights.md                         # 7 insights with citations
        tasks.json                          # 3 open tasks
        completed.json                      # 7 backdated completions
      shielding-review/
        context.manifest.yaml               # bundle 1 (closed)
        tasks.json
      launch-readiness/
        context.manifest.yaml               # bundle 2 (in-flight)
        tasks.json
  02_Life/
    people/
      ryn-okata/_kernel/{key.md, log.md, insights.md}
      jax-stellara/_kernel/{key.md, log.md, insights.md}
```

## Regen workflow

Edit files directly in this directory. Verify the preset still round-trips through `preset.run_preset` by running:

```bash
plugins/alive/.venv-test/bin/pytest plugins/alive/tests/test_demo_preset.py -x
```

The smoke test asserts:
- All preset files parse and resolve.
- Wikilinks (`[[name]]`) reference real walnuts in the preset.
- Log entry headers match the canonical `## YYYY-MM-DD -- squirrel:<16-hex>` shape.
- No em-dashes anywhere in `*.md`, `*.json`, or `*.yaml`.

## Voice rules

- ALIVE narrative tone: vivid, specific, no closure.
- Second-person voice in log-entry bodies (`You walked the launch-readiness bundle ...`).
- Decision-WHY rule: every `### Decisions` bullet states the rationale and what alternative was considered.
- No em-dashes. Use `--` (double-hyphen) in entry headers per the canonical pattern in `scaffold._ENTRY_HEADER_RE`.
- Citation format in insights: `(YYYY-MM-DD, squirrel:<8-hex>)` (8-hex prefix of the squirrel-id).

## Design decisions

- `nova-station` is the only venture walnut. Two people walnuts (Ryn Okata, Jax Stellara) ground the cross-walnut wikilinks. Two bundles under the venture exercise the bundle-projection path in `project.py` without bloating the tree.
- Squirrel-IDs are deterministic 16-hex strings, hand-picked for stability so the squirrel YAMLs in `.alive/_squirrels/` carry the same session identifiers and shape on every run. They are NOT byte-identical across runs because the YAML embeds `cwd: <world_path>` and the world path includes a fresh ULID per activation.
- `completed.json` ships pre-baked rather than synthesized at run-time; this skips the 80/20 split logic in `scaffold.step_5_completed_json` (which is itself preserved for the custom path).
- The preset's persona has `persona_first_name: "sandbox"` in `_world_meta.json`; the shared `scaffold.step_3_preferences` helper composes `squirrel_name: "sandbox's squirrel"` from the first-name token (the same composition rule the custom path uses, so the preset path never diverges from Stage 5's preferences contract).
