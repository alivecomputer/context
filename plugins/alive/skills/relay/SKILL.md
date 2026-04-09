---
name: alive:relay
version: 3.1.0
user-invocable: true
description: "Set up and manage a private GitHub relay for automatic .walnut package delivery between peers. Handles relay creation (private repo + RSA keypair), peer invitations, invitation acceptance, push, pull, and probe."
---

# Relay

Set up and manage a private GitHub relay for automatic `.walnut` package
delivery between peers. The relay is the optional transport layer that
extends `/alive:share` and `/alive:receive` -- without it, sharing still
works (file in, file out), but you have to hand the package over yourself.

This file is the router. Each command below points at a section in
`reference.md` with the full step-by-step flow, the exact `gh` calls, and
the error paths.

## When to use

- Sharing context with the same peers repeatedly (a co-worker, a partner,
  a co-founder) and you want it to feel like email rather than a file
  drop.
- Receiving packages without scheduling a sync call -- the next session
  start picks up whatever landed since you last checked.
- Multiple machines: your laptop and your iPad both pull from the same
  relay so you do not have to copy `.walnut` files between them.

## Prerequisites

- `gh` CLI installed and authenticated (`gh auth status` exits 0). If not
  installed: `brew install gh` or see <https://cli.github.com/>.
- A GitHub account that can create private repositories. Free plans are
  fine -- private repos are unlimited.
- `python3` available on `PATH`. The probe and key generation are stdlib
  Python; `openssl` from the system is used for the RSA keypair.
- Per-user state lives at `~/.alive/relay/` -- not in any walnut. The
  relay belongs to YOU, not to a project.

## Decision tree

```
╭─ alive:relay
│
│  ▸ What do you need?
│  1. Set up my relay (first time)             → setup
│  2. Invite a peer to my relay                → invite
│  3. Accept someone else's relay invitation   → accept
│  4. Push a package to a peer's relay         → push
│  5. Pull packages from my relay              → pull
│  6. Check what is waiting (read-only probe)  → probe
│  7. See current relay status                 → status
╰─
```

Pick the matching command. Each section below is a thin pointer; the
real flow with errors and `gh` calls is in `reference.md` under the
matching heading.

## Command: setup

Create your private relay repo, generate an RSA-4096 keypair, push the
public key, and write `~/.alive/relay/relay.json`.

```bash
/alive:relay setup
```

What it does, in order:

1. Verify `gh` CLI present and authenticated.
2. Pick a repo name (default: `<github-user>-relay`).
3. `gh repo create <name> --private --add-readme`.
4. Generate keypair via `openssl genrsa -out private.pem 4096` +
   `openssl rsa -in private.pem -pubout -out public.pem`. Stored at
   `~/.alive/relay/keys/{private,public}.pem` with mode 0600 / 0644.
5. Push the public key to `keys/<github-user>.pem` in the relay repo.
6. Initialise the directory layout: `keys/peers/`, `inbox/`,
   `.alive-relay/relay.json` (minimal repo metadata).
7. Write the local `~/.alive/relay/relay.json` with the relay URL,
   username, and timestamp.

Full flow with retry semantics: see `reference.md` -> "Setup".

## Command: invite

Invite a peer to write to your relay (so they can deposit packages for
you).

```bash
/alive:relay invite <peer-github-user>
```

What it does:

1. Verify your relay is set up (`~/.alive/relay/relay.json` exists with a
   `relay.url`).
2. Add the peer as a collaborator on the relay repo via
   `gh api --method PUT /repos/<owner>/<repo>/collaborators/<peer>`.
3. Create the `inbox/<peer>/` subdirectory in a sparse clone, commit, and
   push.
4. Record the invite in your local `relay.json` under
   `peers.<peer>` with `accepted: false` -- the field flips to `true` only
   after the peer runs `accept`.
5. GitHub sends the invitation email automatically; tell the peer to run
   `/alive:relay accept <your-relay-url>` once they accept.

See `reference.md` -> "Invite peer" for the rate-limit edge case and
"already a collaborator" path.

## Command: accept

Accept an invitation from someone else's relay so you can push packages
TO them and pull keys / trust their public key.

```bash
/alive:relay accept <relay-url>
```

What it does:

1. Verify your own relay is set up (you need a public key to receive).
2. Sparse-clone `<relay-url>` -- only `keys/<owner>.pem` and your own
   `inbox/<you>/` directory.
3. Read the owner's public key, add it to your local keyring per LD23
   with `added_by: "relay-accept"`.
4. Update your local `~/.alive/relay/relay.json` with
   `peers.<owner>.url = <relay-url>`, `accepted: true`,
   `added_at: <iso>`.
5. Cleanup the temporary clone.

See `reference.md` -> "Accept invitation".

## Command: push

Push a `.walnut` package to a peer's relay (called by `/alive:share` when
the user picks the relay transport).

```bash
/alive:relay push --peer <name> --package <path/to/file.walnut>
```

What it does:

1. Look up `peers.<name>.url` in your local `relay.json`.
2. Sparse-clone the peer's relay (only `keys/<peer>.pem` and
   `inbox/<you>/`).
3. Read the peer's public key from the cloned `keys/<peer>.pem` and
   RSA-encrypt the package against it (delegated to `alive-p2p.py`
   internals; this is task fn-7-7cw.11).
4. Copy the encrypted package to
   `inbox/<you>/<walnut>-<yyyymmdd-hhmmss>-<rand8>.walnut`.
5. `git add -A && git commit -m "deposit: <walnut> <ts>" && git push`.
6. Cleanup the clone.

See `reference.md` -> "Push to peer relay" for the conflict-retry
semantics.

## Command: pull

Pull pending packages from your own relay inbox.

```bash
/alive:relay pull            # interactive: list and pick
/alive:relay pull --all      # pull every pending package
```

What it does:

1. Sparse-clone your own relay (`inbox/*/` only).
2. List `inbox/*/*.walnut` files.
3. Interactive: present them to the user, get a selection.
4. Copy selected files to `03_Inbox/` in the active world.
5. Hand off to `/alive:receive` (one invocation per package).
6. On successful receive, `git rm` the package from the relay and push
   the cleanup commit so the sender knows it landed.

See `reference.md` -> "Pull from own relay".

## Command: probe

Refresh `~/.alive/relay/state.json` by hitting every peer's relay via
`gh api`. Read-only -- never mutates `relay.json`.

```bash
/alive:relay probe                 # default: all peers
/alive:relay probe --peer <name>   # single peer
```

The session-start hook runs the same probe in the background under a
10-minute cooldown -- you only need to invoke this manually if you want
fresh numbers immediately. Errors per peer are recorded as data inside
`state.json`, never raised.

Direct CLI form (used by the hook):

```bash
python3 plugins/alive/scripts/relay-probe.py probe --all-peers
```

See `reference.md` -> "Probe (relay-probe.py)".

## Command: status

Show what your relay looks like right now -- peers, accepted state,
pending packages -- without making any network calls.

```bash
/alive:relay status
```

Reads:

- `~/.alive/relay/relay.json` for the peer list and `accepted` flags.
- `~/.alive/relay/state.json` for `last_probe` + `peers.<>.pending_packages`.

If `state.json` is older than 10 minutes, suggest running `probe` first.
If a peer is `accepted: false`, surface "waiting for peer to accept".

## Files

| Path | Owner | Purpose |
| --- | --- | --- |
| `~/.alive/relay/relay.json` | user (skill) | peer config + relay url |
| `~/.alive/relay/state.json` | probe (read-only of relay.json) | peer reachability + pending counts |
| `~/.alive/relay/keys/private.pem` | user | RSA-4096 private key (mode 0600) |
| `~/.alive/relay/keys/public.pem` | user | matching public key |
| `~/.alive/relay/keys/peers/<name>.pem` | user (via accept) | peer public keys |

`relay.json` is mutated ONLY by `setup`, `invite`, and `accept`.
`relay-probe.py` and `state.json` NEVER touch it (verified in
`tests/test_relay_probe.py::test_probe_never_writes_relay_json`).

## Hook integration

`plugins/alive/hooks/scripts/alive-relay-check.sh` runs at SessionStart
(matchers: `startup`, `resume`). It reads the cooldown, fires the probe in
the background if stale, and exits 0 in all "expected" paths (not
configured, within cooldown, peer-level failures). Exit 1 only on hard
local failures (gh missing, can't write state.json). Never exit 2 -- this
is a notification hook, not a guard.

## Errors

Common error paths and the user-facing message:

- **`gh` not installed**: hard error at setup with brew install link.
- **Not authenticated**: hard error at setup, point to `gh auth login`.
- **Repo creation quota exceeded**: hard error with link to GitHub plan
  settings.
- **Peer relay URL unreachable** (network, 404, 403): recorded in
  `state.json`, surfaced by `status` -- not fatal at the skill level.
- **Push rejected (race, permission)**: retry once, then warn.

## Sharing context

This relay is YOUR relay. You can write to it freely; peers can deposit
to `inbox/<their-username>/` because they were added as collaborators.
You DO NOT push to peers' relays directly -- you push to a sparse clone
of theirs. The git transport is the trust boundary.

## Next steps

After setup:

1. Run `/alive:relay invite <co-worker>` for each peer.
2. Tell each peer to run `/alive:relay accept <your-relay-url>`.
3. Check `/alive:relay status` -- accepted peers show `accepted: true`.
4. Use `/alive:share --to <peer>` to send your first package.
