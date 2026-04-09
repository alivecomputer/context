# Relay -- reference

The full step-by-step flow for each operation in `SKILL.md`. Headings
match the router section names. Read SKILL.md first for the decision
tree; come here when you are actually executing one of the commands and
need the exact `gh` calls, error messages, and edge cases.

## Setup

First-time setup for a new relay. Run once per user, not per walnut.

### Preflight

Before any state mutation, verify the toolchain:

1. `command -v gh` -- if missing, hard-fail with:

   > `relay setup: gh CLI not found. Install via 'brew install gh' or see https://cli.github.com/`

2. `gh auth status` -- if exit non-zero, hard-fail with:

   > `relay setup: not authenticated. Run 'gh auth login' first.`

3. `command -v openssl` -- the keypair generation uses the system
   `openssl` binary, not Python `cryptography`, per LD5 of the epic. If
   missing, hard-fail.

4. `command -v python3` -- needed by `relay-probe.py`.

5. Check `~/.alive/relay/relay.json` -- if it already exists, refuse:

   > `relay setup: ~/.alive/relay/relay.json already exists. Delete it first if you want to start over (this will orphan your existing relay repo).`

### Repo creation

Pick a name. Default is `<github-user>-relay` -- short, unambiguous, and
lines up with the LD25 wire spec which uses the username as the inbox
discriminator. Allow override via `--repo-name` for users who want a
custom name.

```bash
gh repo create "$REPO_NAME" --private --add-readme \
  --description "ALIVE relay -- private peer-to-peer .walnut delivery"
```

If `gh repo create` fails:

- **422 quota exceeded**: surface the GitHub plan settings link
  (<https://github.com/settings/billing>) and abort.
- **422 name taken**: ask the user for a different name and retry.
- **Network error**: retry once, then abort with the raw `gh` stderr.

### Keypair generation

Use the system `openssl` binary -- ALIVE never depends on the Python
`cryptography` package (LD5).

```bash
mkdir -p ~/.alive/relay/keys
chmod 700 ~/.alive/relay
chmod 700 ~/.alive/relay/keys

openssl genrsa -out ~/.alive/relay/keys/private.pem 4096
chmod 600 ~/.alive/relay/keys/private.pem

openssl rsa -in ~/.alive/relay/keys/private.pem \
  -pubout -out ~/.alive/relay/keys/public.pem
chmod 644 ~/.alive/relay/keys/public.pem
```

Compute `pubkey_id` per LD23 (sha256 of the DER-encoded public key, first
16 hex chars). The id is recorded in the local relay.json so other tools
can refer to the key without re-computing.

### Push the public key

Sparse-clone the relay repo and seed the layout:

```bash
WORK=$(mktemp -d)
cd "$WORK"
gh repo clone "$OWNER/$REPO" .
mkdir -p keys keys/peers inbox .alive-relay
cp ~/.alive/relay/keys/public.pem "keys/${OWNER}.pem"
cat > .alive-relay/relay.json <<JSON
{
  "version": 1,
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
git add -A
git commit -m "alive-relay: initial layout"
git push origin HEAD
cd / && rm -rf "$WORK"
```

### Local config write

```bash
mkdir -p ~/.alive/relay
cat > ~/.alive/relay/relay.json <<JSON
{
  "version": 1,
  "relay": {
    "url": "https://github.com/$OWNER/$REPO",
    "username": "$OWNER",
    "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  },
  "peers": {}
}
JSON
chmod 600 ~/.alive/relay/relay.json
```

Atomic write semantics: write to a `.tmp` sibling and `mv` into place
(POSIX atomic on the same FS) so a SIGINT does not leave a partial
config.

### Confirmation

Print the success block:

```
╭─ relay setup complete
│  url:      https://github.com/$OWNER/$REPO
│  username: $OWNER
│  pubkey:   $PUBKEY_ID
│
│  ▸ Next: invite a peer
│  /alive:relay invite <peer-github-user>
╰─
```

## Invite peer

### Preflight

1. `~/.alive/relay/relay.json` exists with a non-empty `relay.url`. If
   not: surface "Run /alive:relay setup first".
2. `<peer-github-user>` is provided -- if missing, prompt interactively.
3. The peer is not already in `peers.<name>` with `accepted: true` -- if
   they are, refuse with "Peer already accepted; nothing to do".
4. `gh auth status` exit 0.

### Add collaborator

```bash
gh api --method PUT \
  "repos/${OWNER}/${REPO}/collaborators/${PEER}" \
  -f permission=push
```

Possible responses:

- **201 / 204**: invitation sent (or peer auto-accepted because they had
  prior collaborator access).
- **403 forbidden**: usually means the peer is blocked or the repo is
  archived. Surface the gh stderr verbatim.
- **404 not found**: peer username does not exist. Suggest the user
  double-check.
- **422 already a collaborator**: idempotent; treat as success and skip
  to the inbox creation.

### Create inbox subdir

The peer needs `inbox/<peer>/` to exist before they can push to it. We
create it via a sparse clone + commit (an empty directory cannot be
committed in git, so seed it with a `.gitkeep`).

```bash
WORK=$(mktemp -d)
cd "$WORK"
gh repo clone "$OWNER/$REPO" . -- --depth=1
mkdir -p "inbox/${PEER}"
touch "inbox/${PEER}/.gitkeep"
git add "inbox/${PEER}/.gitkeep"
git commit -m "alive-relay: prep inbox for ${PEER}"
git push origin HEAD
cd / && rm -rf "$WORK"
```

### Update local relay.json

Add the peer to `peers.<name>` with `accepted: false`. The flag flips to
`true` only when the peer runs `accept` -- the invite side has no way to
detect their decision (GitHub's collaborator-accepted webhook is not in
scope). The probe DOES NOT touch this field per LD17.

```python
import json, datetime, os
RELAY_JSON = os.path.expanduser("~/.alive/relay/relay.json")
with open(RELAY_JSON, "r", encoding="utf-8") as f:
    cfg = json.load(f)
cfg.setdefault("peers", {})[PEER] = {
    "url": None,  # peer's relay url, learned at /alive:relay accept time
    "added_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    "accepted": False,
    "exclude_patterns": [],
}
tmp = RELAY_JSON + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, sort_keys=True)
    f.write("\n")
os.replace(tmp, RELAY_JSON)
```

Note that this peer record has `url: null` until the peer accepts and
gives you back their own relay url. You can push packages to them (via
their relay) only after they have set up their own relay AND you have
discovered the URL -- typically by them running `/alive:relay accept`
against YOUR url and then sharing theirs back over a side channel.

### Confirmation

```
╭─ invitation sent
│  peer:    $PEER
│  status:  waiting for accept
│
│  ▸ Tell them:
│  /alive:relay accept https://github.com/$OWNER/$REPO
╰─
```

## Accept invitation

### Preflight

1. Your own relay must be set up. The accept flow needs your local
   keyring to register the owner's public key.
2. `<relay-url>` is provided. Validate the form
   `https://github.com/<owner>/<repo>` -- reject other URLs.
3. `gh auth status` exit 0.

### Sparse clone

We need only two paths from the relay: `keys/<owner>.pem` (so we can
trust the owner's public key) and `inbox/<you>/` (so we can verify push
access works).

```bash
WORK=$(mktemp -d)
cd "$WORK"
git init -q
git remote add origin "$RELAY_URL"
git config core.sparseCheckout true
mkdir -p .git/info
cat > .git/info/sparse-checkout <<EOF
keys/${OWNER}.pem
inbox/${YOU}/
EOF
git pull --depth=1 origin HEAD
```

If `git pull` fails:

- **403**: GitHub has not yet propagated the collaborator invitation.
  Tell the user to accept via GitHub web first
  (<https://github.com/notifications>) and retry.
- **404**: relay url is wrong, or the repo was deleted.

### Read the public key

```bash
PUB=$(cat "keys/${OWNER}.pem")
```

If the file is missing, the owner did not run setup. Refuse with
"Relay layout incomplete; ask the owner to re-run /alive:relay setup".

### Add to local keyring

Per LD23, the keyring lives in `~/.alive/relay/keys/peers/`. Each peer
gets one PEM file plus a JSON metadata sidecar.

```bash
mkdir -p ~/.alive/relay/keys/peers
cp "keys/${OWNER}.pem" "${HOME}/.alive/relay/keys/peers/${OWNER}.pem"
chmod 644 "${HOME}/.alive/relay/keys/peers/${OWNER}.pem"

# Compute pubkey_id (16 hex of sha256 of DER bytes) -- delegated to
# alive-p2p.py keyring helpers in task .11.
```

### Update local relay.json

Set `peers.<owner>.url` and flip `accepted: true`. If the owner is not
in `peers` yet (you accepted before they invited, e.g. they sent the URL
manually), create a fresh entry.

```python
import json, datetime, os
RELAY_JSON = os.path.expanduser("~/.alive/relay/relay.json")
with open(RELAY_JSON, "r", encoding="utf-8") as f:
    cfg = json.load(f)
peer = cfg.setdefault("peers", {}).setdefault(OWNER, {
    "url": None,
    "added_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    "accepted": False,
    "exclude_patterns": [],
})
peer["url"] = RELAY_URL
peer["accepted"] = True
tmp = RELAY_JSON + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, sort_keys=True)
    f.write("\n")
os.replace(tmp, RELAY_JSON)
```

### Cleanup

```bash
cd / && rm -rf "$WORK"
```

### Confirmation

```
╭─ relay accepted
│  owner:   $OWNER
│  url:     $RELAY_URL
│  pubkey:  added to local keyring
│
│  ▸ You can now push to them:
│  /alive:share --to $OWNER
╰─
```

## Push to peer relay

This is invoked by `/alive:share --to <peer>` -- the user does not
typically call `/alive:relay push` directly. Documented here for
completeness and for the testing harness.

### Preflight

1. `~/.alive/relay/relay.json` exists with a `peers.<name>` entry where
   `accepted: true` AND `url` is not null.
2. The package file exists and ends in `.walnut`.
3. The peer's public key is in your local keyring (added during accept).
4. `gh auth status` exit 0.

### Sparse clone target

Clone the peer's relay sparsely -- only `keys/<peer>.pem` (so we can
encrypt against THEIR key, not ours) and `inbox/<you>/` (so we can write
into our own slot under their inbox).

```bash
WORK=$(mktemp -d)
cd "$WORK"
git init -q
git remote add origin "$PEER_RELAY_URL"
git config core.sparseCheckout true
mkdir -p .git/info
cat > .git/info/sparse-checkout <<EOF
keys/${PEER}.pem
inbox/${YOU}/
EOF
git pull --depth=1 origin HEAD
```

### Encrypt + copy

The actual RSA-hybrid encryption is `alive-p2p.py` internals (task
fn-7-7cw.11 in this epic). At a high level: read `keys/${PEER}.pem`,
generate an AES-256 session key, encrypt the payload tar with AES, wrap
the AES key with RSA-OAEP-SHA256, package as `rsa-envelope-v1.json` +
`payload.enc`, tar them together as the outer `.walnut`.

The destination filename includes a random 8-char suffix to dodge
timestamp collisions per LD25:

```bash
TS=$(date -u +%Y%m%d-%H%M%S)
RAND=$(python3 -c 'import secrets; print(secrets.token_hex(4))')
DEST="inbox/${YOU}/${WALNUT}-${TS}-${RAND}.walnut"
cp "$PACKAGE_PATH" "$DEST"
```

### Commit + push

```bash
git add -A
git commit -m "deposit: ${WALNUT} ${TS}"

# Retry once on push race -- another sender may have pushed concurrently.
if ! git push origin HEAD; then
  git pull --rebase origin HEAD
  git push origin HEAD
fi
```

If the second push fails too, abort with the raw git stderr and tell the
user to retry manually.

### Cleanup

```bash
cd / && rm -rf "$WORK"
```

## Pull from own relay

Invoked by `/alive:receive --from-relay`. Pulls every pending package
from your own relay's inbox subdirectories.

### Preflight

1. `~/.alive/relay/relay.json` exists.
2. `gh auth status` exit 0.

### Sparse clone own relay

```bash
WORK=$(mktemp -d)
cd "$WORK"
git init -q
git remote add origin "$RELAY_URL"
git config core.sparseCheckout true
mkdir -p .git/info
echo 'inbox/' > .git/info/sparse-checkout
git pull --depth=1 origin HEAD
```

### List pending packages

```bash
find inbox -mindepth 2 -maxdepth 2 -name '*.walnut' -type f
```

Each path looks like `inbox/<sender>/<walnut>-<ts>-<rand>.walnut`. Group
by sender for the user-facing presentation:

```
╭─ relay inbox
│  benflint    2 packages
│    1. nova-station-20260407-141200-a1b2c3d4.walnut    14.2 KB
│    2. nova-station-20260407-153012-e5f6a7b8.walnut    14.4 KB
│  willsupernormal  1 package
│    3. glass-cathedral-20260407-152100-deadbeef.walnut  8.1 KB
│
│  ▸ Pull which?
│  1. All
│  2. Specific numbers (e.g. 1,3)
│  3. Cancel
╰─
```

### Copy + receive

For each selected file:

```bash
DEST="${WORLD_ROOT}/03_Inbox/$(basename "$pkg")"
cp "$pkg" "$DEST"
python3 plugins/alive/scripts/alive-p2p.py receive "$DEST" \
  --target "$DEFAULT_TARGET"
```

The receive pipeline does the heavy lifting (decryption, integrity,
scope validation, ledger). On success, return code 0; on failure,
preserve the local `03_Inbox/` copy and stop.

### Cleanup the relay

For successful receives, `git rm` the package from the cloned relay so
the sender knows it landed:

```bash
git rm "$pkg"
git commit -m "received: $(basename "$pkg")"
git push origin HEAD
```

Failed receives are NOT removed -- the user needs to investigate; the
package stays so they can retry from a clean state.

### Cleanup

```bash
cd / && rm -rf "$WORK"
```

## Probe (relay-probe.py)

Read-only scan of every peer in `~/.alive/relay/relay.json`. Runs
automatically on session start under a 10-minute cooldown via
`alive-relay-check.sh`. You can also invoke it manually for fresh
numbers.

### CLI surface

```bash
python3 plugins/alive/scripts/relay-probe.py probe \
  [--all-peers | --peer NAME] \
  [--output PATH] \
  [--relay-config PATH] \
  [--timeout SECONDS]
```

There is intentionally no `--info` flag and no other subcommands. The
canonical invocation is `probe`. Defaults:

- `--all-peers` is implicit when neither `--all-peers` nor `--peer` is
  given.
- `--output` defaults to `~/.alive/relay/state.json`.
- `--relay-config` defaults to `~/.alive/relay/relay.json`.
- `--timeout` defaults to 10 seconds per peer.

### What it does

For each peer in `relay.json`:

1. Parse `peers.<name>.url` into `(owner, repo)`. If unparseable, record
   `reachable: false` with an actionable error and continue.
2. Call `gh_client.repo_exists(owner, repo)` -- abstracted via
   `gh_client.py` so tests can mock it.
3. Call `gh_client.list_inbox_files(owner, repo, peer_name)` to count
   pending `.walnut` packages in `inbox/<peer>/`.
4. Build the per-peer entry per LD17 schema:
   ```json
   {
     "reachable": true,
     "last_probe": "2026-04-07T10:00:00Z",
     "pending_packages": 0,
     "error": null
   }
   ```
5. Write the merged state.json atomically (tempfile + os.replace).

### Exit codes

- **0**: state.json was written. This includes the case where every
  peer was unreachable -- per-peer failures are DATA, not script-level
  errors. Per LD16 the SessionStart hook needs this so peer outages
  never block session start.
- **1**: hard local failure: `relay.json` not found, `relay.json`
  malformed, cannot write state.json, `gh` CLI missing.

### Test contracts

`tests/test_relay_probe.py` enforces:

- `test_probe_writes_state_json` -- mocked `gh_client`, state.json
  exists with the LD17 schema.
- `test_probe_never_writes_relay_json` -- snapshot the bytes of
  `relay.json` before and after the probe; assert byte-identical.
- `test_probe_handles_unreachable_peer` -- mock returns failure; the
  peer entry in state.json has `reachable: false` and a non-null
  `error`.
- `test_probe_handles_missing_gh_cli` -- patch `gh_client.repo_exists`
  to raise `FileNotFoundError`; probe exits 1.
- `test_probe_single_peer` -- `probe --peer foo` only probes `foo`.
- `test_probe_updates_last_probe_timestamp` -- top-level `last_probe`
  is fresh after each run.

### Cooldown semantics

The session-start hook reads the top-level `last_probe` field from
state.json and compares it to `now - 10 min`. If state.json is fresher
than 10 minutes, the hook skips the probe entirely (exit 0). On a cold
machine with no state.json, the probe always runs.

The 10-minute window is the SAME concept as the prior `last_sync` field
on fork branches; LD17 renamed it to `last_probe` so the field name
matches the operation that updates it.

## Key files map

| File | Owner | When written | Schema source |
| --- | --- | --- | --- |
| `~/.alive/relay/relay.json` | skill (setup, invite, accept) | mutation only via skill flows | LD17 |
| `~/.alive/relay/state.json` | `relay-probe.py` | every probe (atomic os.replace) | LD17 |
| `~/.alive/relay/keys/private.pem` | setup | once at first setup | OpenSSL RSA-4096 |
| `~/.alive/relay/keys/public.pem` | setup | once at first setup | OpenSSL RSA-4096 |
| `~/.alive/relay/keys/peers/<name>.pem` | accept | once per accepted peer | LD23 |

## Schema reference: relay.json

```json
{
  "version": 1,
  "relay": {
    "url": "https://github.com/<user>/<user>-relay",
    "username": "<user>",
    "created_at": "2026-04-07T10:00:00Z"
  },
  "peers": {
    "<peer>": {
      "url": "https://github.com/<peer>/<peer>-relay",
      "added_at": "2026-04-07T10:05:00Z",
      "accepted": true,
      "exclude_patterns": []
    }
  }
}
```

Required peer fields: `url`, `added_at`, `accepted`.
Optional peer fields: `exclude_patterns` (default `[]`).

## Schema reference: state.json

```json
{
  "version": 1,
  "last_probe": "2026-04-07T10:00:00Z",
  "peers": {
    "<peer>": {
      "reachable": true,
      "last_probe": "2026-04-07T10:00:00Z",
      "pending_packages": 0,
      "error": null
    }
  }
}
```

`error` is `null` on success, otherwise a short human-readable string.
The probe never raises -- failures land here.
