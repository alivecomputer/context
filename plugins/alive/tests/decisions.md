---
epic: fn-7-7cw
created: 2026-04-07
status: locked-pending-review
review_target: ben + will (via consolidation PR)
authority: Epic spec `.flow/specs/fn-7-7cw.md` LD1-LD28 is the source of truth. This file is a navigation aid + nuance capture, not a re-derivation.
divergences_from_recommendations: none — all 9 questions resolved with the gap-analyst recommended defaults. Documented below where the locked spec adds nuance the original recommendation did not call out.
---

# P2P v3 Locked Decisions (LD1–LD28)

Walkthrough of the 9 open questions from task fn-7-7cw.2 against the locked design decisions in the epic spec. Each section: the decision, the rationale, and any nuance Ben should review when the consolidation PR opens. LD references point to the full text in `.flow/specs/fn-7-7cw.md`.

Ben is unavailable for live interview. Per task spec: "If Ben is unavailable for sync: take recommended defaults above, document with rationale, flag in PR body for review." All 9 decisions take the recommended defaults; PR body must surface these for asynchronous review.

---

## Q1 — Atomic swap semantics on receive (LD1, LD18)

**Decision:** Temp-extract → validate → infer/migrate → scope-check → tentative preview → acquire-lock → canonical-dedupe-under-lock → transactional-swap → log-edit → ledger-write → regenerate-now → cleanup. Step ordering is fixed in LD1. The swap is journaled per-bundle for `scope: bundle` (`staging/.alive-receive-journal.json`); for `scope: full` and `scope: snapshot` the target did not exist pre-swap, so rollback degenerates to `shutil.rmtree(target_dir)`.

**Rationale:** Two principles drive the order. First, project.py is invoked AFTER the swap (step 12), never against staging — running project.py over a temp dir would be wasted work, and worse, would corrupt now.json against a path that is about to disappear. Second, the lock is acquired BEFORE the canonical dedupe (step 7 before step 8) so two concurrent receives cannot both decide they need to apply the same bundles based on a stale `imports.json` snapshot. The tentative preview at step 6 is intentionally pre-lock so users see something fast; step 8 re-runs everything authoritatively under lock and aborts with "state changed during preview, re-run the command" if reality moved.

**Open for review:** LD1 step 10/11/12 are NON-FATAL warnings that still allow exit 0 — the walnut is structurally correct even if the log entry, ledger, or now.json regen failed. This is intentional so `--auto-receive` and skill router chains continue working, but it means a silent log-edit failure leaves the walnut without an import entry until the user runs `alive-p2p.py log-import` manually. The `--strict` flag (LD19) escalates these to exit 1 for users who want fail-fast. Worth confirming Ben agrees with the default-permissive posture vs default-strict.

---

## Q2 — Debounce bypass mechanism (LD1 step 12)

**Decision:** `alive-p2p.py receive` invokes `scripts/project.py` directly via `subprocess.run([sys.executable, f"{plugin_root}/scripts/project.py", "--walnut", target], check=False, timeout=30)`. No hook chain. No `alive-post-write.sh` debounce marker manipulation. Plugin root resolution: `os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`.

**Rationale:** The receive process is a CLI subprocess. Its file writes do NOT emit Claude Code `PostToolUse` events, so the `alive-post-write.sh` 5-minute debounce is structurally irrelevant — there's no hook to bypass. Trying to manipulate `/tmp/alive-project-${WALNUT_HASH}` from a subprocess context would be cargo-culting hook behavior into a context where hooks don't fire. Explicit invocation is the canonical path because it's the ONLY path; the hook chain only exists for interactive Claude Code edits.

**Open for review:** Step 12 failures are non-fatal warns (see Q1). The recovery message tells users exactly what to run: `python3 <plugin_root>/scripts/project.py --walnut <path>`. Worth confirming Ben is happy with `timeout=30` for project.py — large walnuts with many bundles could plausibly exceed this.

---

## Q3 — Bundle name collision policy on receive (LD3)

**Decision:** Default REFUSE with an error listing every conflicting bundle name and its exact target path. The optional `--rename` flag enables deterministic suffix chaining: `{name}-imported-{YYYYMMDD}` first, then `-2`, `-3`, ... until a free name is found. Final chosen names are recorded in the ledger entry's `bundle_renames` map. `--merge` is NOT supported (semantic merge is out of scope).

**Rationale:** Refuse-by-default is the safe choice — silently importing over an existing bundle with the same name is a footgun, and the user is the only one who can decide whether the incoming bundle is "the same thing" or "a different thing that happens to share a name." The deterministic chaining (vs random suffix or interactive prompt) makes the behavior predictable in CI and reproducible across receives — important for the idempotency tests in LD2.

**Open for review:** The `--rename` flag is all-or-nothing: it applies to every conflicting bundle in one receive. There's no per-bundle "rename this one, refuse that one." If Ben wants per-bundle granularity, that's a future flag (`--rename-bundle name=newname`); not blocking the epic.

---

## Q4 — Concurrent session locking (LD4, LD28)

**Decision:** Single exclusive lock per walnut. Both share AND receive acquire it — first-come wins. POSIX strategy: `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` against `$HOME/.alive/locks/{sha256_hex(abspath(walnut))[:16]}.lock`. Windows / no-fcntl fallback: `os.makedirs(lock_dir, exist_ok=False)` against `{sha256_hex(...)[:16]}.lock.d/` with a `holder.txt` inside. Stale-lock recovery via `os.kill(pid, 0)` on POSIX, `ctypes.kernel32.OpenProcess` on Windows. Manual unstick via `alive-p2p.py unlock --walnut <path>`.

**Rationale:** Refusing-on-active-squirrel-session was rejected because squirrel sessions are conversational, not transactional — they may be open for hours while the user makes coffee. P2P operations are seconds-to-minutes. Blocking on session presence would make P2P feel broken. A walnut-scoped lock that BOTH share and receive respect prevents the only real race: two concurrent receives writing to the same walnut. The cross-platform split (LD28) is necessary because `fcntl` is Linux/macOS only and `os.kill(pid, 0)` is unreliable on Windows.

**Open for review:** The lock path uses a 16-hex-char hash of the absolute walnut path so two walnuts at different paths can't collide, even if their basenames match. The hash is truncated to 16 chars (64 bits) — collision-resistant for any plausible per-user walnut count. If Ben thinks anyone will hit a birthday-paradox collision at this scale, we can extend to 32 chars; not blocking.

---

## Q5 — v2 crypto compatibility window (LD5)

**Decision:** Transparent receive of legacy v2 packages via openssl CLI fallback chain: try `-md sha256 -pbkdf2 -iter 100000` (v2.1.0 sender) → `-md sha256 -pbkdf2` (early v2) → `-md md5` (v1/pre-v2). On all three failing, hard error: `"Cannot decrypt package — wrong passphrase or unsupported format. Try openssl enc -d manually to debug."` v2.1.0 senders ALWAYS write parameters matching step 1.

**Rationale:** A hard format break would orphan every package already in circulation across users who haven't upgraded. The fallback chain is cheap (three openssl invocations on failure path only, zero cost on the success path) and the `detect_openssl` plumbing already exists. Transparency means users don't need to know which version a package was created with — the receiver figures it out.

**Open for review:** LD6 hard-fails on `format_version: 3.x` packages — receiver only accepts `^2\.\d+(\.\d+)?$`. This means a future v3-format-bump epic must update receivers BEFORE senders. That's intentional (forward-compat is hard, backward-compat is the priority for an installed-base-of-500+ tool) but Ben should be aware that receiver upgrades become a pre-flight for any future format change.

---

## Q6 — `.alive/preferences.yaml` p2p schema (LD17)

**Decision:** Add a top-level `discovery_hints:` key (NEW — not present on main) AND a commented `p2p:` block to `templates/world/preferences.yaml`. Schema:

```yaml
discovery_hints: true   # auto-retires after first successful operation

p2p:
  share_presets:
    internal: { exclude_patterns: [...] }
    external: { exclude_patterns: [...] }
  relay:
    url: null
    token_env: GH_TOKEN
  auto_receive: false
  signing_key_path: "~/.alive/relay/keys/private.pem"
  require_signature: false
```

Per-peer config (relay URL, exclude patterns, accepted state) lives in `$HOME/.alive/relay/relay.json` — NOT in preferences. `relay-probe.py` writes `state.json` (separate file), NEVER touches `relay.json`.

**Rationale:** Two-file split because relay state is fundamentally per-user-machine (your relay URL, your keys, your peer list) while preferences are per-walnut-world (presets, defaults, opt-ins). Bundling them would force users to copy peer keys when they share `.alive/preferences.yaml` between machines. The probe's read-only stance against `relay.json` is enforced by test (LD17 — "test asserts relay.json is byte-identical before/after probe runs") so the boundary can't drift.

**Open for review:** All `p2p.*` keys are commented out by default — opt-in only. Walnuts that don't enable P2P see zero behavior change. `discovery_hints: true` is the only key uncommented, and it's compatible with general (non-P2P) share nudges. Worth confirming with Ben that defaulting `discovery_hints` ON (vs commented-out OFF) matches the wider plugin posture.

---

## Q7 — `plugin.json` version bump target (LD14)

**Decision:** `3.0.0` → `3.1.0` (minor bump). Packages set `min_plugin_version: "3.1.0"` (advisory only — receiver doesn't hard-fail on lower).

**Rationale:** Semver minor bump signals additive new user-visible behavior (P2P share/receive/relay skills). A patch bump (3.0.1) would understate the surface area: this epic adds three new skills, a new subprocess CLI, a new hook, and a new preferences section. A major bump (4.0.0) would overstate it: the v3 architecture itself isn't changing, no breaking removals. 3.1.0 is the honest version.

**Open for review:** `min_plugin_version` is advisory in this epic (warn-only). If Ben wants to make it enforcing in a future release ("refuse packages from receivers below this version"), that's a one-line change in the validation pass. Not enforced now because the installed base is 3.0.0 and we don't want to break interop on day one.

---

## Q8 — Signer identity model (LD20, LD23)

**Decision:** Sender identity is the GitHub handle, resolved from `os.environ.get("GH_USER")` first, then `gh api user --jq .login` as fallback. Hard-fail if neither resolves AND the package is being signed (`--sign`) or RSA-encrypted (`--encrypt rsa`) — those modes structurally need a sender identity. Local signing key + peer public keys stored in `$HOME/.alive/relay/keys/` (LD23 keyring). Per-key identity via 16-hex-char `pubkey_id = sha256(DER-encoded-pubkey)[:16]`. NEVER base64 — hex avoids `+`/`/` copy-paste issues in CLI surfaces.

**Rationale:** GitHub handles are already the routing primitive for the relay (`inbox/<sender-github-user>/<package>.walnut` per LD25), so reusing them as the signer identity keeps the mental model unified. Alternatives were: (a) squirrel `session_id` — too ephemeral, changes every session, doesn't survive process restart; (b) dedicated `~/.alive/relay/identity.json` — yet another piece of state to manage, and we already have GitHub handles in the relay layer. The 16-hex-char `pubkey_id` is a compromise between "long enough to avoid collisions across any plausible peer keyring" and "short enough to paste in error messages and CLI flags."

**Open for review:** `pubkey_id` is computed from the DER form of the public key (via `openssl pkey -pubin -outform DER`), so it's stable across PEM reformatting (line wrapping, whitespace differences). Test asserts this in `test_keyring_pubkey_id_stable`. If Ben ever needs to migrate to longer or different-encoding pubkey_ids, the spec's signature `signed_bytes: "manifest-canonical-json-v1"` literal tag (LD20) lets us version-bump the canonicalization scheme cleanly.

---

## Q9 — Python floor (LD22)

**Decision:** Python 3.9+. Documented in `plugins/alive/tests/README.md`. Code style rules for the entire P2P codebase: type hints from `typing` module (`Optional`, `List`, `Dict`, `Tuple`, `Union`, `Set`, `Any`), NEVER PEP 604 `X | Y` unions or PEP 585 `list[int]` generics. F-strings (3.6+) and walrus operator (3.8+) are fine. Match statements (3.10+) are NOT used. `tarfile.data_filter` (3.12+) is OPTIONAL defense-in-depth only — the pre-validation pass is the AUTHORITATIVE safety mechanism and works on 3.9+.

**Rationale:** Forcing 3.12+ would block local dev for everyone running 3.9 (the gap-analyst confirmed this is the actual dev environment baseline). The pre-validation pass in `safe_extractall` already guarantees "zero writes on reject" without the filter, so 3.9 has the same security posture as 3.12 — the filter is just belt-and-suspenders. Style rules exist so a future contributor doesn't accidentally drop a `dict[str, int]` annotation that breaks 3.9 imports silently.

**Open for review:** A unit test imports `alive-p2p.py` and `walnut_paths` under Python 3.9 specifically (CI-only test if dev environment is newer). If Ben's local Python is 3.10+, this test only runs in CI. If everyone's local moves to 3.10+ in the future, we can drop the version floor in a one-line edit; it's not load-bearing on anything else in the epic.

---

## Walnut-log-authoritative facts (NOT relitigated)

These were locked in earlier P2P epics and are not part of this task. Listed here so future tasks know not to re-open them:

- Crypto: openssl CLI + LD5 fallback chain (NOT Python `cryptography` library)
- Skills delegate to subprocess CLI (NOT `importlib`)
- Relay config at `$HOME/.alive/relay/`
- Sensitivity enum: `open | private | restricted`
- `now.json` is never shipped in packages
- Multi-walnut packages NOT supported
- `COPYFILE_DISABLE=1` on macOS tar
- GitHub Contents API 35 MB pre-flight warn

---

## Cross-reference: Question → LD mapping

| Question | Primary LDs | Secondary LDs |
|---|---|---|
| Q1 atomic swap | LD1 | LD18, LD12 |
| Q2 debounce bypass | LD1 step 12 | — |
| Q3 collision policy | LD3 | LD2 (ledger renames) |
| Q4 concurrent locking | LD4 | LD28 |
| Q5 v2 crypto compat | LD5 | LD6 (format version), LD21 (envelope) |
| Q6 preferences schema | LD17 | LD23 (keyring), LD25 (relay wire) |
| Q7 version bump | LD14 | — |
| Q8 signer identity | LD20, LD23 | LD25 |
| Q9 Python floor | LD22 | — |

For full text and rationale, read the corresponding section in `.flow/specs/fn-7-7cw.md`. This document is a navigation aid, not a substitute.

---

## Review hold-position log (fn-7-7cw.5, 2026-04-07)

Two rounds of `flowctl codex impl-review` against task .5 returned NEEDS_WORK with the same five findings. After re-checking each against the LD6/LD20 contracts and the orchestrating brief, all five were judged non-substantive. Per the brief's 2-round hygiene rule (`Hold position on LD6/LD20 decisions ... Document and proceed if review findings are non-substantive after 2 rounds`), the task is being completed as scoped.

| Finding | Reviewer position | Held position | Authority |
|---|---|---|---|
| `_cli` placeholder + missing `create_package` | Critical, blocks acceptance | Out of scope for .5 — deferred to .7 | Brief: "DO NOT yet add: `create_package` ... `_cli` entry point (task .7 onwards)" |
| `encryption: "none"` vs `encrypted: false` | Major, breaks v2 helper | Spec is definitive | Epic line 1387: `encryption: "none"  # none \| passphrase \| rsa — LD21`. Cross-task gap with v2 `_update_manifest_encrypted` documented inline; .7 will rewrite the encrypt pipeline. |
| `validate_manifest` requires `source` + `payload_sha256` | Major, weakens v2 compat | Brief enumerates 6 required fields | Brief explicit list. LD20 (lines 1357-1361, 1375) makes both fields mandatory in the v3 schema. .5 task spec's older 4-field bullet predates LD20 finalization. |
| 3.x error string mismatch | Minor wording | Spec is verbatim authoritative | Epic line 241: exact string matches my impl byte-for-byte. Reviewer's suggested string is shorter than the LD6 contract. |
| `_MANIFEST_FIELD_ORDER` omits `relay`, includes `encryption` | Minor field ordering | Matches LD20 schema | LD20 (lines 1349-1393) defines the v3 on-disk schema with `encryption`, no `relay` at top level. `relay` metadata lives in `rsa-envelope-v1.json` per LD21, not in `manifest.yaml`. |

The reviewer's anchor was the older `.flow/tasks/fn-7-7cw.5.md` acceptance text, which predates the LD20 finalization in the same epic. Where the .5 task spec and the LD6/LD20 epic body differ, the epic body wins. Where the .5 task spec and the orchestrating brief differ, the brief wins (the brief is the most recent refinement, written specifically to scope the work narrowly to manifest generation/validation/YAML I/O, deferring CLI and `create_package` to .7).

Round 1 receipt: codex session `019d66fc-84e5-7492-a561-4461e1aab015` (2026-04-07T08:18Z).
Round 2 receipt: codex session `019d670b-3198-7ae0-8949-e927c7aeb56f` (2026-04-07T08:31Z).
Both stored at `/tmp/impl-review-receipt-fn7-7cw-5.json` (overwritten by round 2).

The defensive doc comment in `generate_manifest` (commit `a0f3e25`) acknowledges the encryption-field cross-task gap so .7 picks it up cleanly.
