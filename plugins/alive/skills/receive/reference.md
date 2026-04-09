# Receive — Reference

The full LD1 pipeline broken down step by step. Each step's purpose,
inputs, outputs, failure semantics, and recovery commands. Use this when
the router in `SKILL.md` is not enough.

The receive pipeline is implemented in `plugins/alive/scripts/alive-p2p.py`
as the function `receive_package`. The CLI subcommand `receive` is a thin
wrapper that translates exceptions to exit codes.

```
INPUT  → 1. extract → 2. validate → 3. dedupe-check → 4. infer-layout
       → 5. scope-check → 6. migrate → 7. preview → 8. acquire-lock
       → 9. transact-swap → 10. log-edit → 11. ledger-write
       → 12. regenerate-now → 13. cleanup-and-release
OUTPUT
```

Steps 1-8 are pre-swap: a failure aborts cleanly without touching the
target. Step 9 is the atomic swap. Steps 10-12 are post-swap and NON-FATAL
(unless `--strict`). Step 13 always runs via `try/finally`.

---

## Step 1 — extract

**Purpose:** detect the envelope, decrypt if needed, and extract the
plaintext payload into a staging directory on the SAME filesystem as the
target. Same-filesystem matters because step 9 uses `shutil.move` which is
atomic across same-filesystem renames but a copy+delete across filesystems.

**Envelope detection (LD21):**
- First two bytes `1F 8B` → unencrypted gzipped tarball.
- First eight bytes `Salted__` → OpenSSL passphrase envelope.
- Otherwise, opens the file as a tar archive and looks for either
  `rsa-envelope-v1.json` (LD21 canonical RSA hybrid, lands in .11) or
  `payload.key` + `payload.enc` (legacy v2 RSA hybrid).
- Anything else raises `ValueError("Unknown package format")`.

**Decryption:**
- gzip → no decrypt; the package path itself is fed to `safe_tar_extract`.
- passphrase → `openssl enc -d -aes-256-cbc` with the LD5 fallback chain
  (pbkdf2 iter=600000, iter=100000, no iter, md5). The output must look
  like a gzip file or the next fallback runs. All four failing raises
  `RuntimeError` with last-error context.
- rsa → `NotImplementedError("RSA hybrid decryption lands in task .11")`.

**Staging creation:**
```python
staging = tempfile.mkdtemp(
    prefix=".alive-receive-",
    dir=os.path.dirname(os.path.abspath(target_path)),
)
```

**Extraction:**
- Calls `safe_tar_extract` (LD22 wrapper) which pre-validates EVERY tar
  member before any disk write. Symlinks, hardlinks, absolute paths, path
  traversals, device files, and oversized payloads are rejected before any
  file is created in staging.
- After extraction, strips any `.alive/`, `.walnut/`, or `__MACOSX/`
  directories that may have made it through (defense in depth).

**Failure modes:**
- Tar safety violation → `ValueError("Package tar failed safety check")`.
  Staging may exist but contains zero files. Cleaned up automatically.
- Decryption failure → `RuntimeError` with the last-known openssl error.
- RSA hybrid → `NotImplementedError`.

**Recovery:** none required; failure is pre-target-mutation.

---

## Step 2 — validate

**Purpose:** confirm the package is well-formed before doing anything else.

**Operations:**
1. Read `staging/manifest.yaml` via `read_manifest_yaml` (the LD20
   stdlib-only YAML reader in `alive-p2p.py`).
2. Run `validate_manifest` (the LD6 schema validator). Hard-fails on
   missing required fields, format_version 3.x, malformed scope, malformed
   files[] entries.
3. Run `verify_checksums` -- recomputes sha256 of every file listed in
   `manifest.files[]` and compares against the recorded value. Mismatch
   → `ValueError("Checksum verification failed: ...")`.
4. Recompute `payload_sha256` from the files[] list using
   `compute_payload_sha256` and compare against the manifest field. Catches
   manifest-vs-files divergence.
5. If a `signature` block is present and `--verify-signature` is set,
   record a warning -- the keyring lookup defers to task .11.

**Failure modes:** any validation error raises `ValueError` with the
detailed reason. Pre-swap, no target mutation.

---

## Step 3 — dedupe-check

**Purpose:** apply LD2 subset-of-union dedupe against the target's existing
import ledger.

**Operations:**
1. Read `{target}/_kernel/imports.json` via `_read_imports_ledger` (returns
   an empty ledger if the file or target doesn't exist yet).
2. Compute `import_id = sha256_hex(canonical_manifest_bytes(manifest))`.
3. Compute the union of all `applied_bundles` across every prior ledger
   entry whose `import_id` matches this one.
4. If the requested bundle set is a subset of that union → STRICT NO-OP.
   Cleanup staging, return `{"status": "noop"}` with the ledger context.
5. Otherwise compute `effective_to_apply = requested - prior_applied` and
   continue.

**Subset-of-union semantics example:**
- Receive #1 imports `[A, B]`, ledger entry records `applied_bundles=[A, B]`.
- Receive #2 of the same package with `--bundle C` adds `C`, ledger entry
  records `applied_bundles=[C]`.
- Receive #3 of the same package with `--bundle A --bundle B` is a NO-OP
  because the union `{A, B, C}` already covers `{A, B}`.

**Failure modes:** none -- a corrupted ledger is treated as empty so the
receive can proceed and rebuild it.

---

## Step 4 — infer-layout

**Purpose:** determine whether the staging tree is `v2` (legacy
`bundles/` container + `_kernel/_generated/`) or `v3` (flat) so the LD8
migration step can normalize it.

**Precedence (LD7):**
1. `--source-layout` flag if provided AND `ALIVE_P2P_TESTING=1` env var
   is set (testing-only override).
2. `manifest.source_layout` field if it equals `v2` or `v3`.
3. Structural inspection of immediate children only:
   - `staging/bundles/*/context.manifest.yaml` exists → `v2`
   - `staging/_kernel/_generated/` exists → `v2`
   - any non-`_kernel`/non-`bundles` child has
     `context.manifest.yaml` at its root → `v3`
   - only `_kernel/` exists → `agnostic` (snapshot scope)
4. Otherwise: `ValueError("Cannot infer source layout. ...")`.

**Failure modes:** unparseable layout → `ValueError`. Pre-swap, no target
mutation. Recovery: ask the sender to add a `source_layout` field to the
manifest.

---

## Step 5 — scope-check

**Purpose:** apply LD18 target preconditions. Different scopes have
different rules about target existence, parent dir presence, and walnut
identity.

**Full / snapshot scope:**
- Target path MUST NOT exist (refuses on even an empty dir).
- Parent directory MUST exist and be writable.
- Rationale: rollback on swap failure is `shutil.rmtree(target_dir)` which
  is only safe if we created it fresh.

**Bundle scope:**
- Target walnut MUST exist with a valid `_kernel/key.md`.
- Walnut identity check: byte-compare the package's `_kernel/key.md` to
  the target's. If they differ → refuse with cross-walnut grafting error.
  Override via `ALIVE_P2P_ALLOW_CROSS_WALNUT=1` env var.
- Pre-swap log validation: target's `_kernel/log.md` MUST have valid YAML
  frontmatter. If missing or malformed, abort here (safe abort, no swap).

**Failure modes:** any precondition violation → `ValueError` with the
exact rule that failed. Pre-swap, no target mutation.

---

## Step 6 — migrate

**Purpose:** if the inferred layout is `v2`, run `migrate_v2_layout` on the
staging tree to reshape it into v3 form. The migration function is
documented in `migration.md`.

**Operations:**
1. If `inferred_layout == "v2"` → call `migrate_v2_layout(staging)`.
2. The function returns a result dict with `actions`, `bundles_migrated`,
   `tasks_converted`, `warnings`, `errors`.
3. If `errors` is non-empty → `ValueError("v2 -> v3 staging migration
   failed: ...")`.

**Idempotency:** running migrate against an already-v3 staging tree is a
no-op (returns a single "no-op" action).

**Failure modes:** migration errors → `ValueError`. Recovery: see
`migration.md` troubleshooting section.

---

## Step 7 — preview

**Purpose:** print a human-readable summary of what is about to happen and
require explicit confirmation via `--yes`.

**Preview block content:**
```
=== receive preview ===
scope:        bundle
bundles:      shielding-review, launch-checklist
to apply:     launch-checklist
already applied: shielding-review
file count:   24
package size: 12345 bytes
encryption:   passphrase
signer:       a1b2c3d4e5f67890
sensitivity:  private
renames:
  launch-checklist -> launch-checklist-imported-20260407
=======================
```

**Behaviour:**
- If `--yes` is passed, the pipeline proceeds immediately after printing
  the preview.
- If `--yes` is NOT passed, the pipeline raises `ValueError` after the
  preview is printed. The skill router or interactive caller is expected
  to surface the preview, get the human's confirmation, and re-run with
  `--yes`.

**Failure modes:** missing `--yes` is a deliberate "abort and ask"; no
target mutation.

---

## Step 8 — acquire-lock

**Purpose:** prevent concurrent receive/share operations on the same
walnut. LD4/LD28 cross-platform locking.

**Lock path:**
```
~/.alive/locks/{sha256_hex(abspath(target))[:16]}.lock
```

**Strategy:**
- POSIX (fcntl available): file lock via `os.open` + `fcntl.flock LOCK_EX
  | LOCK_NB`. Holder PID + timestamp written to the file. Released via
  `fcntl.flock LOCK_UN` + close + unlink.
- Fallback (no fcntl): directory lock via
  `os.makedirs(lock_path + ".d", exist_ok=False)`. Holder PID + timestamp
  written to `holder.txt` inside the dir. Released via `shutil.rmtree`.

**Stale-PID recovery:**
- On acquire failure, read the holder PID from the lock artifact.
- POSIX: `os.kill(pid, 0)` raises `ProcessLookupError` if dead.
- If dead → remove the lock artifact, retry once.
- If alive → refuse with actionable error including the holder PID and
  the `unlock` recovery command.

**Failure modes:** lock held by live process → `RuntimeError("busy: ...")`.
Pre-swap, no target mutation.

**Recovery:** `alive-p2p.py unlock --walnut <path>`.

---

## Step 9 — transact-swap

**Purpose:** apply the staged changes atomically. The exact mechanism
depends on scope.

**Full / snapshot scope:**
- Strip the package's `manifest.yaml` from staging (it's a packaging
  artifact, not walnut content).
- `shutil.move(staging, target_path)` -- atomic on the same filesystem.
- For snapshot scope: bootstrap missing `_kernel/{tasks.json,
  completed.json}` with empty placeholders if the package didn't include
  them.
- Rollback on failure: `shutil.rmtree(target_path)` (the target was just
  created, no pre-existing content at risk).

**Bundle scope (journaled move):**
1. Build the operations list: one `move` op per bundle to apply, with
   src=`staging/{leaf}`, dst=`target/{leaf-or-renamed}`, status=`pending`.
2. Write the journal to `staging/.alive-receive-journal.json` BEFORE any
   target mutation.
3. For each op:
   - Mark `committing` and rewrite the journal.
   - `shutil.move(src, dst)`.
   - Mark `done` and rewrite the journal.
4. On failure mid-loop:
   - Read the journal, find ops marked `done`.
   - Reverse-rollback: `shutil.move(dst, src)` for each, in reverse order.
   - Mark each `rolled_back` (or `rollback_failed`).
   - PRESERVE staging by renaming to
     `.alive-receive-incomplete-{iso_timestamp}` next to the target.
   - Print the preserved staging path to stderr.
   - Raise `RuntimeError("swap failed (bundle scope): ...")`.
5. On success: continue to step 10.

**Failure modes:** OS-level move failure → `RuntimeError` with the cause.
For bundle scope, the journal drives rollback and staging is preserved
for diagnosis.

---

## Step 10 — log-edit (NON-FATAL post-swap)

**Purpose:** insert an import entry into `{target}/_kernel/log.md` after
the YAML frontmatter, before any existing entries (LD12).

**Operations:**
1. Read `{target}/_kernel/log.md`.
2. Parse the frontmatter (uses regex to find the second `---` line).
3. Build the import entry from the canonical template:
   ```
   ## {iso_timestamp} - squirrel:{session_id}

   Imported package from {sender} via P2P.
   - Scope: {scope}
   - Bundles: {bundle_list or 'n/a'}
   - source_layout: {layout}
   - import_id: {import_id[:16]}

   signed: squirrel:{session_id}
   ```
4. Update frontmatter `last-entry` to the new timestamp and increment
   `entry-count`.
5. Atomic write: `tempfile.mkstemp` in the same dir + `os.replace`.

**Edge cases:**
- For full/snapshot scope: log.md is brand-new, the function creates it
  with canonical frontmatter + the entry. Always succeeds.
- For bundle scope: log.md must already exist with valid frontmatter
  (validated in step 5). If somehow it doesn't, this step warns and the
  caller appends the entry to the warnings list.

**Failure modes (bundle scope):** any IOError or parse failure → WARN,
not fatal. The walnut is structurally correct but missing the log entry.
Recovery: `alive-p2p.py log-import --walnut <path> --import-id <id>`.

---

## Step 11 — ledger-write (NON-FATAL post-swap)

**Purpose:** append a new entry to `{target}/_kernel/imports.json` so
future receives can dedupe.

**Entry schema:**
```json
{
  "import_id": "sha256-hex",
  "format_version": "2.1.0",
  "source_layout": "v3",
  "scope": "bundle",
  "package_bundles": ["a", "b"],
  "applied_bundles": ["a"],
  "bundle_renames": {"a": "a-imported-20260407"},
  "sender": "patrickSupernormal",
  "created": "2026-04-07T10:15:00Z",
  "received_at": "2026-04-07T10:16:00Z"
}
```

**Operations:**
1. Re-read the ledger (in case it changed during step 9).
2. Append the new entry to `imports[]`.
3. Atomic write via `tempfile.mkstemp` + `os.replace`.

**Failure modes:** WARN, not fatal. The walnut is structurally correct
but future duplicate receives will not dedupe. Recovery: manually append
the entry shape above to `_kernel/imports.json`.

---

## Step 12 — regenerate-now (NON-FATAL)

**Purpose:** regenerate `_kernel/now.json` so the walnut's projected
state matches the new content. This is the LD1 explicit-subprocess path:
the receive pipeline does NOT rely on hook chains.

**Operations:**
1. Resolve plugin root: `os.environ.get("CLAUDE_PLUGIN_ROOT")` or
   `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`.
2. Subprocess: `[sys.executable, f"{plugin_root}/scripts/project.py",
   "--walnut", target]` with `check=False`, `timeout=30`.

**Failure modes:** WARN, not fatal. The walnut's `_kernel/now.json` is
stale. Recovery:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/project.py --walnut <target>
```

The receive skill skips this step entirely if `ALIVE_P2P_SKIP_REGEN` is
set in the environment (used by tests).

---

## Step 13 — cleanup-and-release (ALWAYS RUNS)

**Purpose:** release the lock and clean up staging artifacts. Wrapped in
`try/finally` so it ALWAYS runs, even when steps 10/11/12 hit warnings.

**Operations:**
1. Release the lock acquired in step 8 (close fd + unlink for fcntl, or
   rmtree for the mkdir fallback).
2. If swap succeeded AND steps 10/11 both succeeded: delete staging dir
   and journal file.
3. If swap succeeded but steps 10 OR 11 warned: rename staging to
   `.alive-receive-incomplete-{iso_timestamp}` next to the target so the
   journal can be inspected. The walnut itself is structurally correct.
4. Always clean any decrypt temp dirs created in step 1.

**Exit codes (CLI wrapper):**
- 0 on success
- 0 on no-op (`status == "noop"`)
- 0 on swap-success-with-warnings UNLESS `--strict` is set
- 1 on `--strict` + warnings
- 1 on pre-swap or swap failure (`ValueError`, `RuntimeError`,
  `NotImplementedError`)
- 2 on `FileNotFoundError` (package missing, parent dir missing, etc.)

---

## Exit code matrix

| Scenario                                              | Exit |
| ----------------------------------------------------- | ---- |
| Receive completed cleanly                             | 0    |
| Dedupe no-op                                          | 0    |
| Swap succeeded, log/ledger/regen warned (no --strict) | 0    |
| Swap succeeded, log/ledger/regen warned (--strict)    | 1    |
| Tar safety violation, decrypt error, schema invalid   | 1    |
| Bundle collision without --rename                     | 1    |
| Lock held by live process                             | 1    |
| RSA hybrid envelope (deferred to .11)                 | 1    |
| Package not found                                     | 2    |
| Parent dir of target not found                        | 2    |

---

## Recovery commands cheat sheet

```bash
# Walnut got into a stale-lock state.
python3 .../scripts/alive-p2p.py unlock --walnut /path

# Step 10 (log edit) failed; replay it manually.
python3 .../scripts/alive-p2p.py log-import \
    --walnut /path --import-id <id> --sender X --scope full

# Step 12 (project.py) failed; regenerate now.json.
python3 .../scripts/project.py --walnut /path

# Inspect the prior import ledger.
cat /path/_kernel/imports.json | python3 -m json.tool

# Inspect a partially-rolled-back staging dir after a failed swap.
ls /parent/.alive-receive-incomplete-*/
cat /parent/.alive-receive-incomplete-*/.alive-receive-journal.json
```

## See also

- `SKILL.md` -- the user-facing router with entry points and decision tree.
- `migration.md` -- v2 → v3 layout migration documentation.
