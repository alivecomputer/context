---
name: alive:system-upgrade
description: "Upgrade ALIVE to the current version. Handles v1/v2/v3.x source states, multi-surface aware (alive-mcp / Hermes / Codex), retroactive version detection, partial-failure resume, dry-run previews, and rollback inspection."
user-invocable: true
---

# System Upgrade

Upgrade an ALIVE world from any prior version to the current target. The skill is a thin operator-facing surface; the work happens in the `system_upgrade/` Python package shipped with the plugin (orchestrator + 13 locked phases). Stdlib-only, no PyYAML/ruamel.

If you are reading this on the old monolithic skill (476 lines of inline upgrade logic), that file has been retired in favour of the orchestrator. This file documents what the operator and skill agent need to know to drive it.

---

## When It Fires

- The human runs `/alive:system-upgrade` (any version of the world — v1, v2, v3.0, v3.1, v3.2).
- The session-new hook detects a legacy structure and surfaces the upgrade prompt.
- The human says "upgrade my world", "migrate to the new version", "update alive".
- The human asks to inspect or restore an earlier upgrade tarball (`--rollback`).

---

## Tool version vs world version

Two distinct version concepts; the orchestrator never confuses them.

- **Tool version** — read from `plugins/alive/.claude-plugin/plugin.json` `version`. The version of the migrator currently installed. Used for `--resume` plugin-version-skew validation and the upgrade record's `tool_version_at_run` field. Never feeds world-version inference; never feeds the no-op short-circuit.
- **World version** — derived from world content fingerprints only: path/file existence, bundle schema fingerprints, hook/script content patterns. Three signals, not four. Lowest-version-wins. When zero signals fire, refuse with `--assume-empty-world` as the explicit override.
- **TARGET_WORLD_VERSION** — hardcoded constant in `system_upgrade/__init__.py` (currently `"3.2.0"`). The version the redesign migrates worlds TO. The no-op short-circuit compares the world version (and every per-walnut version) against this constant — never against the tool version. Bumped in lockstep with each plugin minor that introduces world-format changes.

---

## Phases (locked 13)

The orchestrator runs the same 13 steps every invocation. Numbers below are stable contract numbers — they appear in resume markers, runstate logs, and the `phase_reached` envelope field.

For higher-level grouping in operator briefings the orchestrator uses the labels **Setup / Detection block (steps 1–5)** and **Mutation / Verify block (steps 6–13)** — labels are not "macro phases" and never re-use the digit `1` to mean step 1 of a sub-grouping; the locked numbers below are the only phase numbers that matter.

```
1. Preflight        — resolve world_root, .alive symlink + containment check, then UpgradeLock
                       acquire (lock-meta written ONLY after containment validation), dirty stash,
                       Syncthing, half-sync, submodule guards
2. Snapshot         — FileSnapshot pass: both world files + required plugin files. Inputs frozen
                       for the rest of the run.
3. Detect           — consume snapshot; produce DetectionReport (world_version + per-walnut
                       versions + all_signals_raw + tool_version_at_run + walkthrough_eligible_matches).
                       The retired-pattern PRE-SCAN runs here as a read-only pass against the
                       snapshot — phase 7 only renders prompts for matches found here.
4. Probe surfaces   — each surface's --version --json; collect state_paths for sweep exclusion.
                       NO migrator dispatch yet. --surfaces=none skips per-surface probe + dispatch
                       but does NOT skip the prior-record load — load_prior_final_record runs
                       unconditionally so pending retries from a prior run still reach the no-op gate.
5. NO-OP short-circuit — gate predicate: world_version == TARGET_WORLD_VERSION AND every
                       per-walnut version == TARGET AND walkthrough_eligible_matches is empty AND
                       surface_retry_map is empty AND probe_results contains no hard-fail. On pass:
                       write a no-op upgrade record, release lock, exit 0. On fail: continue.
                       --force-run bypasses; --dry-run logs the would-be no-op without writing.
6. Backup           — write .alive/upgrades/pre-upgrade-<iso-ts>.tar.gz (atomic stage → fsync →
                       rename). Stages selected paths into a temp staging dir; excludes
                       .alive/upgrades/, the lock files, and any .alive/.rollback-* dirs to prevent
                       recursive self-inclusion.
7. Walkthrough decide — render prompts for the walkthrough_eligible_matches collected in phase 3;
                       collect y/n decisions. Pure presentation + decision capture; NO writes
                       (dry-run-safe). Phase 7 NEVER re-scans the catalog.
8. Plugin cleanup   — world-root sweep + per-walnut audit. Operates ONLY on retired-pattern
                       catalog entries with cleanup_action == "cleanup". migrate_input entries
                       (`_core/`, `_capsules/`, `now.md`, `tasks.md`, `observations.md`,
                       `_kernel/_generated/`, `03_Inputs/`, `companion.md`) are NOT deleted here
                       — phase 9 consumes them.
9. Plugin migrate   — per-version migrations (v2→v3.0, v3.0→v3.1, v3.1→v3.2). Consumes the
                       walkthrough decisions from phase 7 to apply extension rewrites; consumes
                       cleanup_action=="migrate_input" catalog entries (reads, transforms into
                       the v3 layout, then removes the source paths atomically).
10. Surface dispatch — run each surface's migrator (alive-mcp, Hermes, Codex). Soft-fail per the
                       four-class probe contract. Also consumes the carried-forward needs_retry[]
                       from phase 4's prior-record load.
11. Verify          — live-read verification against post-migration state. Reads disk fresh, NOT
                       the start-of-run snapshot (which would miss failed cleanup/migrate effects).
                       Under --dry-run, verify reads through the virtual post-state overlay.
12. Record          — atomic write .alive/upgrades/<iso-ts>.yaml (final upgrade record).
13. Release lock    — flock release + lock-meta cleanup. Always runs (`finally` block in the CLI
                       handler) even when an earlier phase refused.
```

---

## Skill-invocation pattern

Every ALIVE skill that drives a Python orchestrator follows the same six steps (per `conventions.md § Skill-invocation convention`). System-upgrade is no exception; the steps are spelled out here so future skill authors and the agents reading this skill know exactly how the surface must behave.

1. **Resolve world root.** The skill calls `target_resolver.resolve_target_world(cwd=...)` (the legacy-aware resolver shipped in T1) — it walks up from cwd looking for a high-confidence world marker (`.alive/`, two canonical numbered domain dirs, `.walnut/`, `_core/+companion.md`, `_core/+now.md`, or the `companion+now+tasks` triple). For destructive operations against un-numbered legacy domains the resolver refuses to guess; the operator must pass `--world-root` (or the positional path) explicitly. Other ALIVE skills use `_common.find_world_root_with_strategy()`; system-upgrade uses the legacy-aware path because it is the one command that operates on pre-v3 worlds.
2. **Validate flags + read TTY confirmations.** The skill validates flag combinations (mutex on `<world-path>` vs `--world-root`, `--dry-run` requires `--plan-output` or `--json`, etc.), prompts the operator for type-back when the path-policy gate flags a home/cloud target as confirm-required, and gates `--unsafe-confirm-target` on a real TTY (or `--non-interactive` to bypass).
3. **Invoke the orchestrator via subprocess.run.** The skill calls `python -m system_upgrade.cli <args>` (or, in-process, dispatches through the `bin/alive` `_SUBCOMMANDS` registry) with `subprocess.run(..., shell=False, capture_output=True)`. Never `shell=True`; never process substitution (`<(...)`) — both are shell-fragile in tool contexts and break the deterministic-CLI surface contract.
4. **Stream progress to the agent.** Stream the orchestrator's progress lines to stdout via the agent's tool-output channel. No log-prefixing, no banner — the orchestrator already controls user-facing rendering (the `progress.py` module emits the bordered-block UX inline).
5. **Parse the JSON tail.** On non-zero exit OR `--json` mode, parse the orchestrator's pure-JSON tail (`{ok, exit_code, error_code, error, world_root, phase_reached, noop_short_circuit, ...}`) and surface a structured outcome to the agent. The skill never re-renders the JSON — the agent consumes structured fields directly.
6. **Never auto-retry.** The skill never re-runs the orchestrator on its own. Retry/resume is an explicit operator decision via `--resume` (which reads the most-recent `*-resume.yaml` marker, validates `tool_version_at_run` against current `plugin.json`, refuses on skew without `--force`, refuses staleness >24h without `--force`).

---

## CLI reference

All flags live on `alive system-upgrade` (registered through the `bin/alive` `_SUBCOMMANDS` convention).

| Flag | Purpose |
|---|---|
| `<world-path>` (positional) | Target world to upgrade. May be relative; mutually exclusive with `--world-root`. When neither is supplied, the legacy-aware resolver walks up from cwd. |
| `--world-root <path>` | Explicit target world root. Required for un-numbered legacy domain layouts (auto-detection refuses to guess on destructive ops). |
| `--dry-run` | Read-only after containment, with three narrow allowed transient writes (lock + lock-meta inside `.alive/`, optional `.alive/` dir creation, `--plan-output` plan file). Locks released at phase 13. |
| `--plan-output <path>` | Path for the dry-run plan file. Required when `--dry-run` is supplied without `--json`. |
| `--resume` | Resume a partial-failure run from the most-recent `*-resume.yaml` marker. Refuses on `tool_version` skew (NOT bypassed by `--force`) and on world-state divergence (bypassed by `--force`). |
| `--force` | Bypass world-state divergence on resume. Does NOT bypass `tool_version_at_run` skew. Does NOT bypass any preflight guard. |
| `--force-run` | Bypass the phase-5 no-op short-circuit so already-current worlds re-emit verify + record. Does NOT bypass any preflight guard. |
| `--assume-empty-world` | Phase-3 detection: bypass the `_kernel/` requirement when fingerprint signals are unanimous-empty. |
| `--non-interactive` | Skip every TTY prompt. Combined with `--unsafe-confirm-target` to bypass home/cloud confirm-required gates without a type-back loop. |
| `--ext-migration {skip,backup-only,rewrite,abort}` | Walkthrough user-extension migration policy under `--non-interactive`. Default: `rewrite`. |
| `--surfaces all\|none\|<csv>` | Surface dispatch policy. `all` (default) probes + dispatches every known surface. `none` skips per-surface probe + dispatch (the prior-record `needs_retry[]` load STILL runs). Otherwise a CSV list of surface names. |
| `--rollback [<timestamp>]` | Without an argument: list available pre-upgrade tarballs. With an ISO-8601 timestamp: extract that tarball into `<world>/.alive/.rollback-<ts>/` for inspection. Full automated swap deferred to v3.3. |
| `--force-dirty` | Bypass the dirty-session-stash refusal. |
| `--syncthing-coordinated` | Bypass the Syncthing-active refusal (operator paused sync). |
| `--force-incomplete-sync` | Bypass the half-sync-marker refusal. |
| `--unsafe-confirm-target` | Bypass the home/cloud confirm-required path-policy gate. Combined with TTY type-back in interactive mode OR sufficient alone in `--non-interactive`. NEVER bypasses deny categories. |
| `--keep-tarballs <days>` | Sweep age cutoff in days (default 30). Tarballs older than this are pruned during phase 8 cleanup. |
| `--resume-staleness <hours>` | Resume marker staleness cutoff in hours (default 24). Older markers refuse to resume without `--force`. |
| `--json` | Emit a JSON envelope on stdout for agent consumption. |
| `-v, --verbose` | Increase progress verbosity (`-vv` is step-level). |
| `--plugin-root <path>` | Override the ALIVE plugin root (defaults to `$ALIVE_PLUGIN_ROOT`, then auto-discovery). |

---

## Multi-surface dispatch contract

Phase 4 probes every known surface (`alive-mcp`, `hermes`, `codex`) by invoking `<surface> --version --json`. Each surface MUST respond with the following JSON shape on stdout (and exit 0):

```json
{
  "version": "0.2.0",
  "compatible": true,
  "state_paths": [".alive/_mcp/audit.log"],
  "migrator_argv_prefix": ["alive-mcp", "upgrade"]
}
```

- `version` — semver string. Compared against `version_at_retry` for stale-drop on retry carry-forward.
- `compatible` — boolean. False means the surface understands the contract but refuses this plugin version (orchestrator soft-fails the surface).
- `state_paths` — list of world-relative paths the surface owns. Phase 8 cleanup excludes these from the sweep.
- `migrator_argv_prefix` — list of strings (placeholder-free, no shell expansion). Phase 10 dispatch invokes `<prefix> upgrade --json --world-root <world>` to run the surface's migrator.

The four probe error classes (consumed by phase 5's no-op gate):

- `parse_error` — exit 0 but stdout did not match the contract. Hard fail.
- `non_zero_exit` — subprocess exited non-zero. Hard fail.
- `timeout` — subprocess exceeded its window. Hard fail.
- `missing_binary` — surface executable not on `PATH`. Soft fail (a missing optional surface shouldn't force an upgrade run).
- `migrator_argv_prefix_invalid` — contract violation (e.g. placeholder strings). Surface is treated as `compatible=False`.
- `not_yet_shipped` — Codex stub. Soft signal only.

`alive-mcp` v0.2 is the first conforming implementation (ships AFTER this redesign; soft-fail in the interim — the orchestrator records the dispatch as `skipped` rather than refusing the run).

---

## Resume + rollback flows

Three distinct failure-recovery surfaces; pick the right one.

- **`--resume`** — partial-failure recovery. Re-runs phases 1, 2, 3 fresh, validates `tool_version_at_run` from the marker against the current `plugin.json` (refuses on skew — `--force` does NOT bypass tool-version skew), refuses staleness >24h without `--force`, refuses world-state divergence without `--force`, then resumes from the step after the marker's last completed step. Reads ONLY `*-resume.yaml` markers (NOT `*-runstate.yaml` — runstate is forensic-only).
- **`--rollback [<timestamp>]`** — inspection mode. List available `pre-upgrade-<ts>.tar.gz` tarballs at `.alive/upgrades/`, or extract a specific timestamp's tarball into `<world>/.alive/.rollback-<ts>/` for the operator to inspect. The manual restore procedure is printed with exact paths from the tarball manifest. Full automated swap defers to v3.3.
- **Tool-version-skew refusal vs world-divergence refusal vs staleness refusal** — three separate gates. Skew = the plugin was upgraded between halt and resume (hard refusal, no override). Divergence = the world content moved between halt and resume (`--force` overrides). Staleness = the marker is older than `--resume-staleness` hours (`--force` overrides).

---

## Pre-flight refusals

Every refusal carries a structured `error_code` and exits with the documented code. `--force-*` overrides are scoped — each guard has its own bypass flag.

| Guard | `error_code` | Override |
|---|---|---|
| Path-policy: deny category (system paths, `/`, `/etc`, etc.) | `unsafe_target_deny:<reason>` | none — hard refusal |
| Path-policy: home/cloud confirm-required | `unsafe_target_tty_confirm_required:<reason>` | TTY type-back OR `--unsafe-confirm-target --non-interactive` |
| `.alive/` is a symlink | `boundary_violation:alive_must_be_real_directory` | none — hard refusal |
| Symlink/realpath escapes containment root | `boundary_violation:<reason>` | none — hard refusal |
| Submodule walnut detected | `submodule_mount_refused` | none — surface as `walnut_boundary_skipped[]` |
| Dirty session stash | `dirty_stash` | `--force-dirty` |
| Syncthing active | `syncthing_active` | `--syncthing-coordinated` (operator paused sync) |
| Half-sync marker present | `half_sync_marker` | `--force-incomplete-sync` |
| Upgrade lock contention | `upgrade_lock_busy` (exit 5) | wait + retry; never bypass |
| Missing world / not a directory | `missing_world` (exit 3) | fix the path |
| `--resume` marker missing | `resume_marker_missing` (exit 3) | run without `--resume` for a fresh upgrade |
| `--resume` tool-version skew | `resume_tool_version_skew` | none — hard refusal |
| `--resume` world divergence | `resume_world_diverged` | `--force` |
| `--resume` staleness >24h | `resume_stale` | `--force` |
| `--resume` step not in PHASE_NAMES | `resume_step_unknown` | regenerate marker |
| Empty world detected (zero signals fire) | `detect_empty_world` | `--assume-empty-world` |

---

## Backward-cleanup table

Every prior forward-fix commit (v3.0 → v3.2) maps to a redesign step that handles its backward cleanup. Source-of-truth: the curated commit inventory at `04_Ventures/alive/upgrade-discipline/audit-public-history.md` § per-version retirement events. Deduplicated by `source_commit` — a single commit may be referenced by multiple `RetiredPattern` catalog entries, but the table has one row per commit.

| Source commit | Date | Description | Cleanup step |
|---|---|---|---|
| `7f7bd27` | 2026-03-29 | v1→v2 layout retirement: `_core/` → `_kernel/`, `_capsules/` → `bundles/`, `companion.md` → `context.manifest.yaml`, `now.md` deleted, `tasks.md` distributed, `People/` → `02_Life/people/`. Forward-fix at v2.0.0 release commit. | T9 (v2→v3.0 migration consumes `migrate_input` catalog entries — `companion.md`, `now.md`, `tasks.md`, `_capsules/`, `_core/`) |
| `21ac613` | 2026-04-03 | v2→v3.0 layout retirement: `_kernel/_generated/` flattened, `bundles/` flattened, `03_Inputs/` → `03_Inbox/`, `.walnut/` → `.alive/`, `tasks.md` → `tasks.json`, `observations.md` removed, `_kernel/_generated/`, `bundles/` container removed. Forward-fix at v3.0.0 merge commit. | T9 (consumes 8 `migrate_input` catalog entries) + T4 (3 `verify_only` entries cover the architectural verification rewrite — live-read replaces hardcoded checks) |
| `f66fa16` | 2026-04-03 | v3.0 principle statement: "Plugin IS the runtime. World is data. Scripts ship with the plugin, not copied to user machines." Removed `.alive/scripts/` fallback from post-write hook; subagent brief moved to plugin templates. Forward-fix in v3.0.0. | T1 (skill-architecture redesign establishes the orchestrator as plugin-owned; world contains only state, not scripts) |
| `f565c81` | 2026-04-16 | v3.0→v3.1 `.alive/scripts/` retirement: introduced `ALIVE_PLUGIN_ROOT` env var; replaced broken paths (`plugins/alive/scripts/tasks.py`, `.alive/scripts/*`) with `$ALIVE_PLUGIN_ROOT/scripts/*` across save, system-cleanup, my-context-graph; eliminated the copy-to-world pattern that caused issue #62 (t003 — world-local copies drift from plugin cache). Forward-fix in v3.1 staging. | T5 (cleanup catalog: 6 `cleanup` entries plus 1 `walkthrough_rewrite` for user-authored extensions referencing `.alive/scripts/`) |
| `6a9f629` | 2026-04-29 | v3.1→v3.2 demo-skill retirement of `_stage_outputs/entities/` post-install scaffolding: prevents index double-counting after demo activation. Forward-fix in v3.2 staging. | T5 (cleanup catalog `_stage_outputs/entities/` entry) |
| `407ac86` | 2026-04-29 | v3.2 round-5 demo review v2-compatibility migration cleanup. Architectural / verification-only forward fix; no path pattern. | T4 (verification rewrite — live-read covers the v2-compat surfaces without hardcoded path checks) |

R6 acceptance: every commit SHA in `audit-public-history.md` § retirement events has exactly one row; no commit is missed; no commit appears twice.

---

## Pure-JSON stdout contract

When `--json` is set, the orchestrator emits a single JSON object on stdout (no preamble, no trailing whitespace). Schema:

```json
{
  "ok": true,
  "exit_code": 0,
  "error_code": null,
  "error": null,
  "world_root": "/absolute/path",
  "phase_reached": "release",
  "noop_short_circuit": false,
  "noop_record_path": null,
  "backup_tarball_path": "/absolute/path/.alive/upgrades/pre-upgrade-2026-05-04T12-34-56.tar.gz",
  "rollback_pointer": "→ Rollback available: alive system-upgrade --rollback 2026-05-04T12-34-56"
}
```

`phase_reached` values are drawn from the locked `PHASE_NAMES` set: `preflight`, `snapshot`, `detect`, `probe_surfaces`, `noop_short_circuit`, `backup`, `walkthrough_decide`, `plugin_cleanup`, `plugin_migrate`, `surface_dispatch`, `verify`, `record`, `release`. The CLI guarantees the field is always one of these strings (or `"resume"` / `"rollback_list"` / `"rollback_extract"` for the inspection flows).

`exit_code` mapping:

- `0` — success
- `1` — general failure / preflight refusal / phase refusal
- `2` — usage error (bad flags, mutex violation)
- `3` — not found (missing world, missing resume marker, unknown rollback timestamp)
- `4` — permission (filesystem permission errors)
- `5` — lock contention (`upgrade_lock_busy`)

---

## Canonical paths under `.alive/upgrades/`

Strict regex patterns on basename — the orchestrator, prior-record loader, and rollback all use these classes. NEVER use a `*.yaml` glob; the directory is heterogeneous.

| Filename class | Regex | Owner |
|---|---|---|
| Final upgrade record | `^(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})\.yaml$` | phase 12 RECORD; loaded by phase 4 prior-record load |
| Resume marker | `^(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-resume\.yaml$` | T6 incremental write; loaded ONLY by `--resume` |
| Retroactive synthesized record | `^(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-retroactive\.yaml$` | T9 retroactive synthesis; read ONLY by T9's de-dup check |
| Pre-upgrade tarball | `^pre-upgrade-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})\.tar\.gz$` | T5 backup; read by `--rollback` |
| Run-state log | `^(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-runstate\.yaml$` | T9/T10 forensic-only; NOT consumed by `--resume` |

---

## What this skill does NOT touch

- **Walnut content** — `key.md`, `log.md`, `insights.md`, raw files. Only moved within renames, never edited (except where retired-pattern catalog entries with `walkthrough_rewrite` request a user-confirmed extension rewrite).
- **Git history** — no force pushes, no history rewrites.
- **Plugin cache** — `~/.claude/plugins/` is managed by Claude Code, not this skill.

## What this skill DOES audit (but doesn't auto-fix)

- **Plugin-surface drift in user extensions** — flagged via the retired-pattern catalog's `walkthrough_eligible` entries. Phase 7 surfaces a y/n prompt per match; phase 9 applies only on accepted decisions, with `.bak.<ts>` siblings preserved.
- **Sync scripts referencing retired paths** — surfaced for review; never auto-deleted.
- **External integrations** — MCP servers, email/Slack sync scripts referencing old structure are surfaced.

---

## Manual canary (production validation)

Before each plugin release that touches the upgrade pipeline, the maintainer runs a **manual canary** against their own private world. This is deliberately NOT automated — the maintainer's world is the highest-stakes upgrade target, and the operator must be human-in-the-loop for it.

**Procedure (per release):**

1. **Snapshot the world** — `tar` the world root to a holding location outside the world tree. The pre-upgrade backup phase 6 also writes an atomic tarball under `.alive/upgrades/`, but the manual snapshot is a second line of defence (the operator controls the location, the timing, and the retention).
2. **Dry-run first** — `/alive:system-upgrade --dry-run`. Verify the planned migration plan, walkthrough-eligible matches, and surface-dispatch list match the operator's mental model. If anything surprises the operator, halt and inspect.
3. **Real run** — drop `--dry-run`. Sit with the run; the orchestrator emits one structured-JSON line per phase. The phases that mutate disk (cleanup, migrate, surface-dispatch, record) are the ones to watch.
4. **Verify** — `flowctl` is irrelevant here; the canary's verifier is `/alive:world` + a manual scan of `_kernel/now.json`, `tasks.json`, and `log.md` against the snapshot from step 1. Anything missing or shape-shifted that wasn't in the migration plan is a regression.
5. **Forensic record** — `.alive/upgrades/<ts>.yaml` carries the full run summary including walkthrough decisions and surface-dispatch results. The operator reads this against the dry-run plan from step 2; any divergence is a finding.
6. **Rollback if needed** — `/alive:system-upgrade --rollback` restores the pre-upgrade tarball atomically. Use the manual snapshot (step 1) as a final fallback if the in-tree tarball is itself suspect.

**What the canary catches that automation can't:**

- Walkthrough-eligible prompts whose y/n decisions only an operator who knows the world's history can answer.
- Surface-dispatch failures against the operator's actual MCP / hermes / codex configurations (the test suite mocks these).
- Drift between the dry-run plan and the real-run record — automation would just compare planned vs applied counts; a human notices when the *kind* of change is different.
- Subjective regressions: bundle headers that look correct to the linter but read wrong to the human who wrote them.

**What the canary is NOT:**

- A test pass/fail gate — `pytest -m system_upgrade` is the gate. The canary is operator confidence.
- A substitute for the property-based idempotency tests — those run unattended in CI; the canary runs once per release with attention.
- A staging environment — there is no staging world. The maintainer's world IS the canary target. This is intentional: a staging world would diverge from a real-world history and the test would lose its bite.

---

## What system-upgrade is NOT

- Not `alive:build-extensions` — extensions create new capabilities. Upgrade migrates existing structure.
- Not `alive:system-cleanup` — cleanup fixes broken things in the current version. Upgrade moves between versions.
- Not a fresh install — if the resolver finds no world marker and no override is supplied, the resolver refuses (use `alive:world` for initial setup).

Cleanup fixes. Upgrade transforms.
