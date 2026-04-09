---
type: reference
description: "Share preset + per-peer exclusion configuration for /alive:share. Loaded on demand from SKILL.md."
---

# Share presets and per-peer exclusions

Share presets and per-peer exclusions live OUTSIDE the share skill itself --
they're configured in the human's world via `.alive/preferences.yaml` and
`~/.alive/relay/relay.json`. The skill loads them at share time. This
reference doc covers the schema, the merge order, and the discovery hints.

---

## Share preset schema

Presets live under `p2p.share_presets` in `.alive/preferences.yaml`. Each
preset is a named bag of `exclude_patterns` (glob strings) that get applied
to the staging tree before the package is generated.

```yaml
# .alive/preferences.yaml

# Top-level discovery hints (used by the share skill at first run).
discovery_hints: true

p2p:
  share_presets:
    internal:
      exclude_patterns:
        - "**/observations.md"

    external:
      exclude_patterns:
        - "**/observations.md"
        - "**/pricing*"
        - "**/invoice*"
        - "**/salary*"
        - "**/strategy*"
        - "_kernel/log.md"
        - "_kernel/insights.md"

  relay:
    url: null                 # GitHub repo URL for relay
    token_env: GH_TOKEN       # env var holding the GitHub token

  auto_receive: false         # Auto-import .walnut files in 03_Inbox/
  signing_key_path: "~/.alive/relay/keys/private.pem"
  require_signature: false    # Refuse unsigned packages on receive
```

The two named presets shown above are the **suggested defaults** -- the
human can rename them, drop one, or add their own. The share skill enumerates
whatever is configured under `share_presets` and presents the names in the
preset picker.

`exclude_patterns` use the LD27 glob syntax:

| Pattern               | Meaning                                         |
|-----------------------|-------------------------------------------------|
| `*.tmp`               | Any `.tmp` file at any depth                    |
| `**/observations.md`  | Any `observations.md` at any depth              |
| `_kernel/log.md`      | EXACTLY `_kernel/log.md` at the package root    |
| `bundles/*`           | Single segment under `bundles/`                 |
| `bundles/**`          | Recursive subtree under `bundles/`              |
| `**/pricing*`         | Any file/dir whose name starts with `pricing`   |

`?` matches one character within a segment. `[abc]` matches one character
from the set. `**` matches zero or more path segments.

---

## LD26 protected paths (cannot be excluded)

Exclusion patterns are silently ignored when they would match these paths:

- All scopes: `manifest.yaml`
- Full scope: `_kernel/key.md`, `_kernel/log.md`, `_kernel/insights.md`,
  `_kernel/tasks.json`, `_kernel/completed.json`
- Bundle scope: `_kernel/key.md`
- Snapshot scope: `_kernel/key.md`, `_kernel/insights.md`

Required files always make it into the package. The `external` preset
above includes `_kernel/log.md` and `_kernel/insights.md` for clarity --
those entries are no-ops because the LD9 baseline stubbing logic already
substitutes them with placeholder content. The preset is harmless to keep
since the protected-path rule means it cannot accidentally remove key.md.

---

## Per-peer exclusions

Per-peer exclusions live in the relay config at `~/.alive/relay/relay.json`,
NOT in the world preferences. This keeps them user-scoped instead of
walnut-scoped -- the same peer gets the same per-peer treatment regardless
of which walnut you're sharing.

```json
{
  "version": 1,
  "relay": {
    "url": "https://github.com/patrickSupernormal/patrickSupernormal-relay",
    "username": "patrickSupernormal",
    "created_at": "2026-04-07T10:00:00Z"
  },
  "peers": {
    "benflint": {
      "url": "https://github.com/benflint/benflint-relay",
      "added_at": "2026-04-07T10:05:00Z",
      "accepted": true,
      "exclude_patterns": [
        "**/strategy*",
        "engineering/"
      ]
    },
    "willsupernormal": {
      "url": "https://github.com/willsupernormal/willsupernormal-relay",
      "added_at": "2026-04-07T10:10:00Z",
      "accepted": false,
      "exclude_patterns": []
    }
  }
}
```

Required `peers.<name>` fields: `url`, `added_at`, `accepted`. Optional:
`exclude_patterns` (defaults to empty list).

When the human picks `--exclude-from <peer>` in the share flow, the share
CLI reads `peers.<peer>.exclude_patterns` and merges them additively with
the preset and any explicit `--exclude` flags.

`accepted` is managed exclusively by `/alive:relay` -- the share skill
NEVER touches it. A peer with `accepted: false` is still listed in the
picker (so the human can see they're queued) but cannot be the target of
a relay push until they accept the invitation.

---

## Exclusion merge order

Per LD26, the share CLI applies exclusions in this order:

1. Collect candidate file set (per scope rules)
2. Separate REQUIRED files from exclude-eligible files
3. Apply `--preset <name>` exclusions (from `share_presets.<name>`)
4. Apply `--exclude <glob>` flags (additive)
5. Apply `--exclude-from <peer>` exclusions (additive from
   `peers.<name>.exclude_patterns`)
6. Build `manifest.files[]` from the surviving set + required files
   (as stubs where applicable)
7. Warn if any pattern matched zero files

The audit trail in `manifest.exclusions_applied` records the merged
pattern list (deduplicated, insertion-ordered).

---

## Discovery hints

`discovery_hints: true` (top-level in `preferences.yaml`, NOT under
`p2p:`) controls whether the share skill drops first-run hints to the
human. There are three places hints fire:

1. **At share start, no relay configured:**
   ```
   Tip: set up a private GitHub relay via /alive:relay so peers can pull
   packages automatically. Skip this step if you're sharing via file
   transfer.
   ```

2. **At preset picker, no presets configured:**
   ```
   Tip: configure share presets in .alive/preferences.yaml under
   p2p.share_presets to drop sensitive files automatically.
   ```

3. **At signature picker, no signing key configured:**
   ```
   Tip: set p2p.signing_key_path in .alive/preferences.yaml to sign your
   shared packages. Recipients can verify it came from you.
   ```

Each hint fires AT MOST once per session. The hints auto-retire when
the corresponding feature is configured (e.g., once relay.json exists,
hint #1 stops appearing). Set `discovery_hints: false` to silence them
entirely.

---

## LD17 safe defaults

When the preferences file or section is missing entirely, the share CLI
falls back to:

- `share_presets`: `{}` (no presets, baseline stubs only)
- `relay`: `{url: null, token_env: "GH_TOKEN"}`
- `auto_receive`: `false`
- `signing_key_path`: `""` (signing refused with actionable error)
- `require_signature`: `false`
- `discovery_hints`: `true`

A warning surfaces in the share output:

```
warnings:
  - No p2p preferences found; using baseline stubs only.
```

The baseline stub behaviour (LD9) is INDEPENDENT of preferences -- it
always applies unless `--include-full-history` is passed. Presets layer
ADDITIONAL exclusions on top but can never override the baseline.

---

## Editing presets

The human edits presets directly in `.alive/preferences.yaml`. The share
skill does NOT write to this file. To add a new preset:

```yaml
p2p:
  share_presets:
    legal:
      exclude_patterns:
        - "**/contract*"
        - "**/NDA*"
        - "engineering/private/"
```

Then use it via `--preset legal` or pick it from the share preset picker.
There's no validation step -- the preset name is free-form and the patterns
are validated lazily at share time (a malformed glob pattern fails the
share with a clear error message).

---

## See also

- `SKILL.md` — the share skill router (decision tree + quick commands).
- `reference.md` — the full 9-step interactive flow.
- `/alive:relay` — set up the GitHub relay and manage peer keys.
- `~/.alive/relay/keys/peers/` — peer public keys for RSA hybrid
  encryption.
