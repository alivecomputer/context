#!/usr/bin/env python3
"""ALIVE Context System -- relay state probe.

Read-only scan of every peer in ``~/.alive/relay/relay.json``. Writes
results to ``~/.alive/relay/state.json`` (separate file -- per LD17 the
probe NEVER mutates ``relay.json``). Invoked by the SessionStart hook
(``alive-relay-check.sh``) under a 10-minute cooldown and on demand by
``/alive:relay status``.

Per LD17 / LD25 of epic fn-7-7cw the canonical CLI surface is::

    relay-probe.py probe [--all-peers | --peer NAME]
                         [--output PATH]
                         [--timeout SECONDS]

There is intentionally no ``--info`` flag and no other subcommands -- those
were superseded drafts.

Exit codes (LD16 / LD17):
    0  -- state.json written successfully (even if some peers were
          unreachable; peer-level failures are recorded as data inside the
          state.json ``peers`` map)
    1  -- hard local failure: cannot read relay.json, cannot write
          state.json, gh CLI missing, etc. The hook script translates this
          into a notification but never blocks the session-start chain.

Stdlib only. Python 3.9+ floor. Tests mock ``gh_client.repo_exists`` and
``gh_client.list_inbox_files`` so no real network calls fire.
"""

import argparse
import datetime
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional

# Make gh_client importable when this script lives in the same directory
# (the plugins/alive/scripts/ tree). Mirrors the pattern used by alive-p2p.py
# for walnut_paths.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import gh_client  # noqa: E402  -- import after sys.path mutation


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_RELAY_DIR = os.path.expanduser("~/.alive/relay")
DEFAULT_RELAY_JSON = os.path.join(DEFAULT_RELAY_DIR, "relay.json")
DEFAULT_STATE_JSON = os.path.join(DEFAULT_RELAY_DIR, "state.json")
DEFAULT_TIMEOUT = 10
STATE_VERSION = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso_now():
    # type: () -> str
    """Return current UTC time as ``YYYY-MM-DDTHH:MM:SSZ`` (no microseconds)."""
    now = datetime.datetime.utcnow().replace(microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_relay_json(path):
    # type: (str) -> Dict[str, Any]
    """Read ``relay.json`` and return the parsed dict.

    Raises:
        FileNotFoundError: relay.json does not exist (caller surfaces as
            "relay not configured" -- exit 1 from a probe perspective is
            still appropriate; the HOOK script handles the "not configured
            is OK" semantics).
        ValueError: relay.json exists but cannot be parsed.
    """
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError("relay.json malformed: {0}".format(exc))
    if not isinstance(data, dict):
        raise ValueError("relay.json must be a JSON object")
    return data


def _peers_from_relay(relay):
    # type: (Dict[str, Any]) -> Dict[str, Dict[str, Any]]
    """Extract the ``peers`` map from a parsed relay.json. Always a dict."""
    peers = relay.get("peers", {})
    if not isinstance(peers, dict):
        return {}
    return peers


def _parse_repo_url(url):
    # type: (str) -> Optional[tuple]
    """Parse a GitHub repo URL into ``(owner, repo)`` tuple.

    Accepts the canonical forms ``https://github.com/<owner>/<repo>`` and
    ``https://github.com/<owner>/<repo>.git`` (trailing ``.git`` stripped).
    Returns ``None`` if the URL is empty or unparseable -- the probe records
    that as ``reachable: false`` with an actionable error string.
    """
    if not url or not isinstance(url, str):
        return None
    # Trim whitespace + trailing slash
    u = url.strip().rstrip("/")
    # Strip github.com/ prefix variants
    for prefix in (
        "https://github.com/",
        "http://github.com/",
        "git@github.com:",
        "github.com/",
    ):
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    else:
        return None
    if u.endswith(".git"):
        u = u[:-4]
    parts = u.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return (parts[0], parts[1])


def _atomic_write_json(path, data):
    # type: (str, Dict[str, Any]) -> None
    """Write JSON to ``path`` atomically via tempfile + os.replace.

    Cross-platform safe (os.replace is atomic on POSIX and Windows).
    Creates the parent directory if missing -- the relay state lives under
    ``~/.alive/relay/`` which the user may not have created yet on first
    probe.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=parent or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup; re-raise so caller exits 1.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_existing_state(path):
    # type: (str) -> Dict[str, Any]
    """Load existing state.json or return a fresh empty skeleton.

    Used by ``probe --peer NAME`` so a single-peer probe merges into the
    existing peer map rather than wiping it. ``probe --all-peers`` always
    overwrites with a fresh peers dict (no merge needed).
    """
    if not os.path.exists(path):
        return {"version": STATE_VERSION, "last_probe": None, "peers": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"version": STATE_VERSION, "last_probe": None, "peers": {}}
    if not isinstance(data, dict):
        return {"version": STATE_VERSION, "last_probe": None, "peers": {}}
    data.setdefault("version", STATE_VERSION)
    data.setdefault("last_probe", None)
    if not isinstance(data.get("peers"), dict):
        data["peers"] = {}
    return data


# ---------------------------------------------------------------------------
# Probe core
# ---------------------------------------------------------------------------


def _probe_peer(peer_name, peer_cfg, timeout):
    # type: (str, Dict[str, Any], int) -> Dict[str, Any]
    """Probe a single peer and return its state.json entry.

    Always returns a dict with the LD17 schema:
        {reachable, last_probe, pending_packages, error}

    Errors are recorded as DATA, never raised. The probe overall succeeds
    even if every peer is unreachable -- that information is what the user
    is asking for.
    """
    now = _utc_iso_now()
    entry = {
        "reachable": False,
        "last_probe": now,
        "pending_packages": 0,
        "error": None,
    }

    url = peer_cfg.get("url") if isinstance(peer_cfg, dict) else None
    parsed = _parse_repo_url(url or "")
    if parsed is None:
        entry["error"] = "invalid or missing peer url"
        return entry
    owner, repo = parsed

    # Stage 1: does the relay repo exist + are we authorised to read it?
    try:
        exists = gh_client.repo_exists(owner, repo, timeout=timeout)
    except FileNotFoundError:
        # gh CLI missing -- bubble up so the caller exits 1.
        raise
    except Exception as exc:  # gh_client raises generic errors on 5xx etc.
        entry["error"] = "repo_exists failed: {0}".format(exc)
        return entry
    if not exists:
        entry["error"] = "repo not found or no access: {0}/{1}".format(owner, repo)
        return entry

    # Stage 2: count pending packages in inbox/<peer>/.
    try:
        files = gh_client.list_inbox_files(owner, repo, peer_name, timeout=timeout)
    except FileNotFoundError:
        raise
    except gh_client.GhClientError as exc:
        # Inbox dir missing or empty is the common case for a fresh peer --
        # treat as reachable with 0 packages but record the error so the
        # user can see WHY if they're expecting deliveries.
        entry["reachable"] = True
        entry["pending_packages"] = 0
        entry["error"] = "list_inbox_files failed: {0}".format(exc)
        return entry
    except Exception as exc:
        entry["error"] = "list_inbox_files unexpected: {0}".format(exc)
        return entry

    entry["reachable"] = True
    entry["pending_packages"] = len(files)
    return entry


def probe_all(relay_path, output_path, timeout):
    # type: (str, str, int) -> int
    """Probe every peer in relay.json. Returns process exit code."""
    try:
        relay = _load_relay_json(relay_path)
    except FileNotFoundError:
        sys.stderr.write("relay-probe: relay.json not found at {0}\n".format(relay_path))
        return 1
    except (OSError, ValueError) as exc:
        sys.stderr.write("relay-probe: cannot read relay.json: {0}\n".format(exc))
        return 1

    peers = _peers_from_relay(relay)
    state = {
        "version": STATE_VERSION,
        "last_probe": _utc_iso_now(),
        "peers": {},
    }

    for name, cfg in sorted(peers.items()):
        try:
            state["peers"][name] = _probe_peer(name, cfg, timeout)
        except FileNotFoundError:
            sys.stderr.write("relay-probe: gh CLI not found on PATH\n")
            return 1

    try:
        _atomic_write_json(output_path, state)
    except OSError as exc:
        sys.stderr.write("relay-probe: cannot write {0}: {1}\n".format(output_path, exc))
        return 1
    return 0


def probe_one(relay_path, output_path, peer_name, timeout):
    # type: (str, str, str, int) -> int
    """Probe a single peer, merge into existing state.json."""
    try:
        relay = _load_relay_json(relay_path)
    except FileNotFoundError:
        sys.stderr.write("relay-probe: relay.json not found at {0}\n".format(relay_path))
        return 1
    except (OSError, ValueError) as exc:
        sys.stderr.write("relay-probe: cannot read relay.json: {0}\n".format(exc))
        return 1

    peers = _peers_from_relay(relay)
    if peer_name not in peers:
        sys.stderr.write("relay-probe: peer not in relay.json: {0}\n".format(peer_name))
        return 1

    state = _load_existing_state(output_path)
    try:
        state["peers"][peer_name] = _probe_peer(peer_name, peers[peer_name], timeout)
    except FileNotFoundError:
        sys.stderr.write("relay-probe: gh CLI not found on PATH\n")
        return 1
    state["last_probe"] = _utc_iso_now()
    state["version"] = STATE_VERSION

    try:
        _atomic_write_json(output_path, state)
    except OSError as exc:
        sys.stderr.write("relay-probe: cannot write {0}: {1}\n".format(output_path, exc))
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser():
    # type: () -> argparse.ArgumentParser
    """Build the argparse parser. Single ``probe`` subcommand only.

    LD17 forbids any other subcommands. The single ``probe`` shape exists
    so future subcommands can be added without breaking the canonical
    invocation.
    """
    parser = argparse.ArgumentParser(
        prog="relay-probe.py",
        description=(
            "ALIVE relay probe -- read-only scan of peer relay state. "
            "Writes ~/.alive/relay/state.json. Never mutates relay.json."
        ),
    )
    sub = parser.add_subparsers(dest="cmd")

    p_probe = sub.add_parser(
        "probe",
        help="Probe relay peers and write state.json",
        description=(
            "Probe one or all peers in ~/.alive/relay/relay.json and write "
            "the result to ~/.alive/relay/state.json (or --output PATH). "
            "Exit 0 even if some peers are unreachable -- those failures "
            "are recorded as data, not script-level errors."
        ),
    )
    target = p_probe.add_mutually_exclusive_group()
    target.add_argument(
        "--all-peers",
        action="store_true",
        help="Probe every peer in relay.json (default if no --peer given)",
    )
    target.add_argument(
        "--peer",
        metavar="NAME",
        help="Probe only the named peer; merge into existing state.json",
    )
    p_probe.add_argument(
        "--output",
        metavar="PATH",
        default=DEFAULT_STATE_JSON,
        help="Override state.json output path (default: {0})".format(DEFAULT_STATE_JSON),
    )
    p_probe.add_argument(
        "--relay-config",
        metavar="PATH",
        default=DEFAULT_RELAY_JSON,
        help="Override relay.json input path (default: {0})".format(DEFAULT_RELAY_JSON),
    )
    p_probe.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="Per-peer GitHub API timeout in seconds (default: {0})".format(DEFAULT_TIMEOUT),
    )
    return parser


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd != "probe":
        parser.print_help()
        return 1

    if args.peer:
        return probe_one(
            relay_path=args.relay_config,
            output_path=args.output,
            peer_name=args.peer,
            timeout=args.timeout,
        )
    # Default to --all-peers if neither flag explicitly set.
    return probe_all(
        relay_path=args.relay_config,
        output_path=args.output,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
