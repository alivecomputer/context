---
name: alive:share
description: "Package walnut context into a portable .walnut file for sharing via any channel -- email, AirDrop, Slack, USB. Supports three scopes (full, bundle, snapshot), sensitivity gating, optional passphrase encryption, and relay push for automatic delivery to peers."
user-invocable: true
---

# Share

Package walnut context and send it. Nine steps: scope, pick, gate, confirm, encrypt, create, verify, record, push.

Output is a `.walnut` file on the Desktop. Optionally encrypted (passphrase for manual transfer, RSA for relay). Relay push delivers directly to a peer's inbox via GitHub.

---

## Prerequisites

Before starting, determine the active walnut:

```bash
WALNUT_ROOT="$(pwd)"
WALNUT_NAME="$(basename "$WALNUT_ROOT")"
```

Verify `_kernel/key.md` exists. If not, stop: "No walnut loaded. Open one first with /alive:load."

---

## Step 1: Scope Selection

```
╭─ 🐿️ share
│
│  Walnut: <walnut-name>
│
│  ▸ what scope?
│  1. Full -- everything (_kernel, bundles, live context)
│  2. Bundle -- one or more specific bundles + key.md
│  3. Snapshot -- key.md + insights.md only (lightweight intro)
╰─
```

Wait for selection. Map: 1 = `full`, 2 = `bundle`, 3 = `snapshot`.

If `full` or `snapshot`, skip to Step 3.

---

## Step 2: Bundle Picker (bundle scope only)

List bundles from `bundles/*/context.manifest.yaml`. For each, read frontmatter to extract `name`, `phase`, `sensitivity`, `pii`, and `description`.

```bash
for manifest in "$WALNUT_ROOT"/bundles/*/context.manifest.yaml; do
  echo "---"
  echo "path: $manifest"
  python3 -c "
import sys
with open('$manifest') as f:
    content = f.read()
# Read YAML frontmatter (before first ---)
lines = content.strip().split('\n')
for line in lines:
    if line.strip() == '---':
        break
    print(line)
"
done
```

Present as numbered list:

```
╭─ 🐿️ bundles
│
│  1. p2p-design         phase: published   sensitivity: open     P2P sharing architecture
│  2. funding-pitch      phase: draft       sensitivity: private  Series A investor deck
│  3. user-research      phase: prototype   sensitivity: restricted (pii)  Interview transcripts
│
│  ▸ which bundles? (comma-separated, e.g. 1,3)
╰─
```

Collect selected bundle names. These are used in Step 6.

---

## Step 3: Sensitivity Gate

For each bundle in scope (all bundles for full scope, selected for bundle scope), read `sensitivity:` and `pii:` from its `context.manifest.yaml` frontmatter.

For snapshot scope, skip this step (key.md and insights.md don't carry bundle sensitivity).

**Gate logic:**

| sensitivity | pii | Action |
|---|---|---|
| `open` | `false` / absent | Pass. No warning. |
| `open` | `true` | Warn: "This bundle is marked open but contains PII. Confirm before sharing." Block until confirmed. |
| `private` | any | Note: "This bundle is marked private. A note will be added to the package metadata." Continue. |
| `restricted` | `false` / absent | Warn + confirm: |
| `restricted` | `true` | Block until confirmed: |

For `restricted` without PII:

```
╭─ 🐿️ sensitivity check
│
│  Bundle "<name>" is marked restricted.
│
│  ▸ continue with share?
│  1. Yes, I understand the sensitivity
│  2. Cancel
╰─
```

For `restricted` with PII, or `open` with PII:

```
╭─ 🐿️ sensitivity check
│
│  Bundle "<name>" contains personally identifiable information (pii: true).
│  Sharing PII outside your system requires explicit confirmation.
│
│  ▸ confirm PII share?
│  1. Yes, the recipient is authorized to receive this data
│  2. Cancel -- do not share
╰─
```

If any bundle fails the gate and the user cancels, abort the entire share flow.

---

## Step 4: Scope Confirmation

Count files and estimate package size before creating:

```bash
# For bundle scope -- count files in selected bundles
TOTAL_FILES=0
TOTAL_SIZE=0
for BUNDLE in <selected-bundles>; do
  BUNDLE_DIR="$WALNUT_ROOT/bundles/$BUNDLE"
  COUNT=$(find "$BUNDLE_DIR" -type f | wc -l)
  SIZE=$(find "$BUNDLE_DIR" -type f -exec stat -f%z {} + 2>/dev/null | paste -sd+ | bc)
  TOTAL_FILES=$((TOTAL_FILES + COUNT))
  TOTAL_SIZE=$((TOTAL_SIZE + SIZE))
done

# For full scope -- count everything staged
find "$WALNUT_ROOT" -type f \
  -not -path '*/.alive/*' \
  -not -path '*/.git/*' \
  -not -path '*/__pycache__/*' \
  -not -path '*/node_modules/*' \
  -not -name '.DS_Store' \
  | wc -l
```

Present summary:

```
╭─ 🐿️ pre-flight
│
│  Scope: bundle (p2p-design, funding-pitch)
│  Files: 47
│  Estimated size: 2.3 MB
│
│  ▸ proceed?
│  1. Yes, create package
│  2. Change scope
│  3. Cancel
╰─
```

**Size warning** (> 35 MB):

```
╭─ 🐿️ size warning
│
│  Estimated package size is ~52 MB.
│  GitHub relay limit is ~50 MB (Contents API with base64 overhead).
│  Manual transfer (AirDrop, email) will still work.
│
│  ▸ proceed anyway?
│  1. Yes, create package (relay push may fail)
│  2. Reduce scope
│  3. Cancel
╰─
```

If "Change scope" or "Reduce scope", return to Step 1.

---

## Step 5: Encryption Prompt

Check whether a relay is configured:

```bash
[ -f "$HOME/.alive/relay/relay.json" ] && echo "relay:configured" || echo "relay:none"
```

If relay is configured, also check for accepted peers:

```bash
python3 -c "
import json
with open('$HOME/.alive/relay/relay.json') as f:
    config = json.load(f)
accepted = [p for p in config.get('peers', []) if p.get('status') == 'accepted']
print(f'accepted_peers:{len(accepted)}')
"
```

**Present options based on relay state:**

No relay or no accepted peers:

```
╭─ 🐿️ encryption
│
│  ▸ encrypt the package?
│  1. Passphrase -- AES-256, you share the passphrase separately
│  2. No encryption -- plaintext .walnut file
╰─
```

Relay with accepted peers:

```
╭─ 🐿️ encryption
│
│  ▸ encrypt the package?
│  1. Passphrase -- AES-256, you share the passphrase separately
│  2. No encryption -- plaintext .walnut file
│  3. Relay -- RSA-encrypt per peer, push to relay inbox
╰─
```

**If passphrase (option 1):**

```
╭─ 🐿️ passphrase
│
│  Enter a passphrase for the package. Share it with the recipient
│  through a separate channel (not alongside the .walnut file).
│
│  ▸ passphrase:
╰─
```

Store the passphrase in an environment variable. **Never** pass it as a CLI argument. **Never** write it to disk.

```bash
export WALNUT_PASSPHRASE="<user-provided-passphrase>"
```

**If relay (option 3):** Encryption happens per-peer in Step 9. Create the unencrypted package first in Step 6.

Record the encryption choice: `ENCRYPT_MODE` = `passphrase`, `none`, or `relay`.

---

## Step 6: Package Creation

Call alive-p2p.py to create the .walnut package:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/alive-p2p.py" create \
  --scope <scope> \
  --walnut "$WALNUT_ROOT" \
  --bundle <bundle-name> \
  --output "$HOME/Desktop/<walnut>-<scope>-<date>.walnut" \
  --description "<user-facing description>"
```

For bundle scope, pass `--bundle <name>` once per selected bundle.
For full/snapshot scope, omit `--bundle`.
If output path is omitted, alive-p2p.py defaults to `~/Desktop/`.

**If passphrase encryption was selected,** encrypt immediately after creation:

```bash
export WALNUT_PASSPHRASE="<passphrase>"
python3 -c "
import sys
sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/scripts')
from alive_p2p import encrypt_package
result = encrypt_package(
    package_path='<path-from-create>',
    mode='passphrase'
)
print(result)
"
```

The encrypt_package function reads `WALNUT_PASSPHRASE` from the environment. It produces a new `.walnut` file with encrypted payload. The original unencrypted file is replaced.

**Unset the passphrase immediately after encryption:**

```bash
unset WALNUT_PASSPHRASE
```

Capture the output path and size from the result.

---

## Step 7: Output Confirmation

```
╭─ 🐿️ package created
│
│  Path: ~/Desktop/stackwalnuts-bundle-p2p-design-2026-04-01.walnut
│  Size: 2.3 MB
│  Scope: bundle (p2p-design)
│  Encryption: AES-256-CBC (passphrase)
│  Files: 47
│
│  Ready to send via email, AirDrop, Slack, USB -- whatever works.
│  Recipient imports with /alive:receive.
╰─
```

If unencrypted:

```
╭─ 🐿️ package created
│
│  Path: ~/Desktop/stackwalnuts-bundle-p2p-design-2026-04-01.walnut
│  Size: 2.3 MB
│  Scope: bundle (p2p-design)
│  Encryption: none
│  Files: 47
│
│  This package is not encrypted. Anyone with the file can read it.
│  Send via a trusted channel, or re-share with /alive:share and pick passphrase.
╰─
```

If relay encryption was selected, note that the local copy is unencrypted but the relay push will RSA-encrypt per peer:

```
╭─ 🐿️ package created
│
│  Path: ~/Desktop/stackwalnuts-bundle-p2p-design-2026-04-01.walnut
│  Size: 2.3 MB (local copy, unencrypted)
│  Scope: bundle (p2p-design)
│
│  Relay push will RSA-encrypt per peer in the next step.
╰─
```

---

## Step 8: Metadata Update

For each bundle in scope, update its `context.manifest.yaml` `shared:` array. Use inline python3 for safe YAML manipulation:

```bash
python3 -c "
import datetime, sys

manifest_path = '$WALNUT_ROOT/bundles/<bundle-name>/context.manifest.yaml'
with open(manifest_path, 'r') as f:
    content = f.read()

# Find the shared: line or add it
# Simple append-to-YAML approach -- add shared entry
entry = '''  - method: alive:share
    scope: <scope>
    date: $(date -u +%Y-%m-%dT%H:%M:%SZ)
    encrypted: <true|false>
    package: <filename>'''

# Read existing content, find or create shared: section
lines = content.rstrip().split('\n')
shared_idx = None
for i, line in enumerate(lines):
    if line.startswith('shared:'):
        shared_idx = i
        break

if shared_idx is not None:
    if lines[shared_idx].strip() == 'shared: []':
        lines[shared_idx] = 'shared:'
    lines.insert(shared_idx + 1, entry)
else:
    lines.append('shared:')
    lines.append(entry)

with open(manifest_path, 'w') as f:
    f.write('\n'.join(lines) + '\n')
"
```

This records the share event in the bundle's manifest. When the relay push delivers to specific peers, update the entry with `to: <github-username>`.

---

## Step 9: Relay Push (conditional)

Only runs when `ENCRYPT_MODE` is `relay`. If the user chose passphrase or no encryption, the flow ends at Step 8.

### Step 9a: Read Peer Reachability

```bash
python3 -c "
import json
with open('$HOME/.alive/relay/state.json') as f:
    state = json.load(f)
with open('$HOME/.alive/relay/relay.json') as f:
    config = json.load(f)

accepted = [p for p in config.get('peers', []) if p.get('status') == 'accepted']
reachability = state.get('peer_reachability', {})

for p in accepted:
    gh = p['github']
    name = p.get('name') or gh
    reach = reachability.get(gh, {})
    status = reach.get('status', 'unknown')
    print(f\"{gh}\t{name}\t{status}\")
"
```

### Step 9b: Present Peer Selection

```
╭─ 🐿️ relay push
│
│  ▸ send to which peers?
│  1. benflint (Ben Flint) -- reachable
│  2. janedoe (janedoe) -- unreachable
│  3. All reachable peers
│
│  Unreachable peers are shown but may fail. The package stays
│  in their inbox for pickup when they come back online.
╰─
```

### Step 9c: User Selects

Wait for numbered selection.

### Step 9d: Read Selected Peer Details

For each selected peer, read their relay repo and key path from relay.json:

```bash
python3 -c "
import json
with open('$HOME/.alive/relay/relay.json') as f:
    config = json.load(f)
peer = [p for p in config['peers'] if p['github'] == '<selected-github>'][0]
print(f\"relay:{peer['relay']}\")
print(f\"github:{peer['github']}\")
"
```

### Step 9e: Confirmation Before Push

**This is a confirmation gate for an external action.** The external guard hook does not catch Bash tool calls. This prompt is the gate.

```
╭─ 🐿️ relay push confirmation
│
│  This will push an RSA-encrypted package to:
│
│  Peer: benflint (Ben Flint)
│  Relay: benflint/walnut-relay
│  Inbox: inbox/benflint/<filename>.walnut
│
│  The package will be encrypted with their RSA public key.
│  Only they can decrypt it with their private key.
│
│  ▸ push to relay?
│  1. Yes, push now
│  2. Cancel
╰─
```

**Wait for confirmation.** Do not proceed without explicit "yes" or "1".

If multiple peers are selected, confirm once listing all peers, not once per peer.

### Step 9f: RSA-Encrypt Per Peer

For each selected peer, encrypt the package with their public key:

```bash
python3 -c "
import sys
sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/scripts')
from alive_p2p import encrypt_package

result = encrypt_package(
    package_path='<unencrypted-package-path>',
    output_path='/tmp/<peer>-<filename>.walnut',
    mode='rsa',
    recipient_pubkey='$HOME/.alive/relay/keys/peers/<peer>.pem'
)
print(result)
"
```

### Step 9g: Push to Peer's Inbox

Read the RSA-encrypted package, base64-encode, and push via GitHub Contents API:

```bash
PEER="<peer-github>"
PEER_RELAY="<peer-relay-repo>"
PACKAGE_NAME="<filename>.walnut"
MY_USER=$(python3 -c "import json; print(json.load(open('$HOME/.alive/relay/relay.json'))['github_username'])")

# Base64-encode the encrypted package (cross-platform, no line breaks)
CONTENT=$(openssl base64 -A -in "/tmp/${PEER}-${PACKAGE_NAME}")

# Check if file already exists (need SHA for update)
EXISTING_SHA=$(gh api "repos/${PEER_RELAY}/contents/inbox/${MY_USER}/${PACKAGE_NAME}" \
  --jq '.sha' 2>/dev/null || echo "")

if [ -n "$EXISTING_SHA" ]; then
  # Update existing file
  gh api "repos/${PEER_RELAY}/contents/inbox/${MY_USER}/${PACKAGE_NAME}" \
    --method PUT \
    --field message="Package from ${MY_USER}" \
    --field content="$CONTENT" \
    --field sha="$EXISTING_SHA"
else
  # Create new file
  gh api "repos/${PEER_RELAY}/contents/inbox/${MY_USER}/${PACKAGE_NAME}" \
    --method PUT \
    --field message="Package from ${MY_USER}" \
    --field content="$CONTENT"
fi
```

### Step 9h: Clean Up + Confirm Delivery

```bash
# Clean up temporary encrypted copies
rm -f "/tmp/${PEER}-${PACKAGE_NAME}"
```

Update the shared entry in the bundle manifest with peer info (update Step 8's entry):

```bash
python3 -c "
import datetime
manifest_path = '$WALNUT_ROOT/bundles/<bundle>/context.manifest.yaml'
with open(manifest_path, 'r') as f:
    content = f.read()
# Append to: field to the most recent shared entry
content = content.rstrip()
# Find last '    package:' line and add 'to:' after it
lines = content.split('\n')
for i in range(len(lines) - 1, -1, -1):
    if '    package:' in lines[i]:
        lines.insert(i + 1, '    to: <peer-github>')
        break
with open(manifest_path, 'w') as f:
    f.write('\n'.join(lines) + '\n')
"
```

Present delivery confirmation:

```
╭─ 🐿️ delivered
│
│  Package pushed to relay:
│  - benflint/walnut-relay/inbox/benflint/<package>.walnut (RSA-encrypted)
│
│  They'll see a notification at next session start (alive-relay-check hook)
│  or can pull manually with /alive:receive --relay.
╰─
```

If push failed for any peer:

```
╭─ 🐿️ relay push failed
│
│  Could not push to benflint/walnut-relay:
│  <error message>
│
│  The unencrypted package is still at:
│  ~/Desktop/<package>.walnut
│
│  You can send it manually or retry with /alive:share.
╰─
```

---

## Account Routing

Apply platform routing from platforms.md. For GitHub API calls in relay push:

```bash
GH_TOKEN=$(gh auth token --user <github-username>) gh api ...
```

The `github_username` in relay.json determines which account to use.

---

## Error Handling

| Error | Message |
|---|---|
| No walnut loaded | "No walnut loaded. Open one with /alive:load first." |
| No bundles found | "No bundles in this walnut. Create one with /alive:bundle first." |
| Bundle not found | "Bundle '<name>' not found. Available: <list>." |
| alive-p2p.py fails | Show stderr, suggest checking the walnut structure |
| Encryption fails (LibreSSL) | "OpenSSL doesn't support PBKDF2. Upgrade LibreSSL to >= 3.1 or install OpenSSL >= 1.1.1." |
| Relay push 403 | "Permission denied on <repo>. Check collaborator access with /alive:relay status." |
| Relay push 422 | "File too large for GitHub Contents API (~50 MB limit). Use manual transfer instead." |
| Package > 50 MB for relay | "Package exceeds GitHub API limit. Created locally at ~/Desktop/ -- send manually." |
| Peer key missing | "No public key cached for <peer>. They may need to accept the relay invitation first." |
| state.json missing | Run probe first: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/relay-probe.py" --config "$HOME/.alive/relay/relay.json" --state "$HOME/.alive/relay/state.json"` |

---

## Confirmation Gate Rules

Every external action MUST have a confirmation prompt. The external guard hook only catches `mcp__` tools, not Bash. This skill is the gate.

**Requires confirmation:** relay push (Step 9e). This is the only external write action in the share flow.

**No confirmation needed:** reading config/state files, local package creation, local encryption, writing to bundle manifest, file counting, size estimation.

Pattern: present what will happen, numbered options, wait for choice. Never fire-and-forget on external actions.
