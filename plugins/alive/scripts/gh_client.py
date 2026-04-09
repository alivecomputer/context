#!/usr/bin/env python3
"""ALIVE Context System -- ``gh`` CLI wrapper for the relay layer.

Stdlib-only abstraction over ``gh api`` and ``gh auth status`` calls used by
the relay probe and the relay skill flows. The wrapper exists for testability
per LD17 of epic fn-7-7cw: tests mock the public functions in this module via
``unittest.mock.patch('gh_client.<fn>', ...)`` so the relay-probe round-trip
runs against deterministic fixtures with no real network traffic.

Design constraints (LD17 + script-builder specialist):

* **Stdlib only.** No ``requests``, no ``PyYAML``, no third-party deps. The
  module must import cleanly on a stock Python 3.9+ install.
* **Subprocess wrapping.** Every public call shells out to ``gh`` via
  ``subprocess.run`` with explicit ``timeout=`` and ``check=False``. Errors
  are caught and surfaced as Python exceptions or ``False`` returns; the
  caller decides whether the failure is fatal.
* **No mutation of relay.json.** Functions in this module are READ-only with
  respect to local state (they only invoke ``gh`` over the network or read
  ``gh auth status`` -- they never touch ``~/.alive/relay/relay.json``).
* **Public surface.** The exported callables are:
    - ``check_auth() -> bool``
    - ``repo_exists(owner, repo, timeout=10) -> bool``
    - ``list_inbox_files(owner, repo, peer, timeout=10) -> List[Dict]``
    - ``fetch_public_key(owner, repo, peer, timeout=10) -> str``
  Tests patch these by name (``gh_client.repo_exists``).

Python floor: 3.9. Type hints use the ``typing`` module to match
``alive-p2p.py`` conventions (no PEP 604 unions, no PEP 585 builtin
generics).
"""

import json
import subprocess
from typing import Any, Dict, List


class GhClientError(RuntimeError):
    """Raised when the ``gh`` CLI call fails in a way the caller cares about.

    The probe layer catches these and records ``reachable: false`` for the
    affected peer; the relay skill surfaces them to the user verbatim.
    """


# ---------------------------------------------------------------------------
# Internal subprocess helper
# ---------------------------------------------------------------------------


def _run_gh(args, timeout):
    # type: (List[str], int) -> subprocess.CompletedProcess
    """Run ``gh <args>`` and return the completed process.

    Wraps ``subprocess.run`` with a fixed contract:

    * ``check=False`` -- the caller inspects ``returncode`` rather than
      catching ``CalledProcessError``. This keeps the error surface uniform
      across the four public callables.
    * ``capture_output=True`` -- both stdout and stderr are captured. ``gh``
      writes JSON payloads to stdout and human-readable errors to stderr.
    * ``text=True`` -- decoded as UTF-8 strings (``gh`` always outputs
      UTF-8). Bytes are not needed at this layer.
    * ``timeout=`` -- per-call timeout passed by the caller. Network
      operations get the default 10s; tests can override.

    Raises ``FileNotFoundError`` if the ``gh`` binary is missing -- callers
    catch this and surface it as a hard local failure (LD16 exit 1 path).
    Raises ``subprocess.TimeoutExpired`` on timeout -- callers convert this
    to a peer-level error in state.json (LD16 exit 0 path).
    """
    return subprocess.run(
        ["gh"] + list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_auth():
    # type: () -> bool
    """Return True if ``gh auth status`` reports an authenticated account.

    Used by the relay skill setup flow and by ``relay-probe.py`` as a
    pre-flight before iterating peers. ``gh auth status`` exits 0 when at
    least one host has a stored token, 1 otherwise.

    Returns False (rather than raising) on FileNotFoundError so the relay
    skill can surface "install gh" as a friendly error rather than a Python
    traceback.
    """
    try:
        result = _run_gh(["auth", "status"], timeout=5)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def repo_exists(owner, repo, timeout=10):
    # type: (str, str, int) -> bool
    """Return True if the GitHub repo ``<owner>/<repo>`` exists and is visible.

    Uses ``gh api repos/<owner>/<repo>`` -- a 200 response means the
    authenticated user can read the repo (public OR collaborator on private).
    A 404 means the repo does not exist OR the user has no access (gh
    intentionally collapses both for security).

    The relay setup flow uses this to verify the relay repo exists before
    pushing the public key; the probe uses it to mark a peer reachable
    before listing inbox contents.
    """
    result = _run_gh(
        ["api", "repos/{0}/{1}".format(owner, repo)],
        timeout=timeout,
    )
    return result.returncode == 0


def list_inbox_files(owner, repo, peer, timeout=10):
    # type: (str, str, str, int) -> List[Dict[str, Any]]
    """List ``.walnut`` files under ``inbox/<peer>/`` in the relay repo.

    Calls ``gh api repos/<owner>/<repo>/contents/inbox/<peer>`` which
    returns a JSON array of GitHub content objects (each with ``name``,
    ``sha``, ``size``, ``path``, ``download_url``, ...). Filters to entries
    whose ``name`` ends in ``.walnut`` -- README files, key.pem, and other
    non-package artifacts are ignored.

    Returns a list of plain dicts with the keys the caller cares about
    (``name``, ``sha``, ``size``, ``path``). The probe uses ``len()`` of
    the result; the receive flow consumes the full dict so it can dispatch
    a download via the same ``sha``.

    Raises:
        GhClientError: when ``gh`` returns a non-zero exit code (404 missing
            inbox dir, 403 permission, 5xx upstream). The probe catches this
            and records ``pending_packages: 0`` plus the error string. The
            receive flow surfaces it directly to the user.
    """
    path = "repos/{0}/{1}/contents/inbox/{2}".format(owner, repo, peer)
    result = _run_gh(["api", path], timeout=timeout)
    if result.returncode != 0:
        # gh writes the error JSON or text to stderr; surface it.
        msg = (result.stderr or result.stdout or "unknown error").strip()
        raise GhClientError("list_inbox_files {0}: {1}".format(path, msg))

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GhClientError("list_inbox_files {0}: bad JSON ({1})".format(path, exc))

    if not isinstance(payload, list):
        # ``gh api`` returns a dict {message, ...} on errors that escape the
        # returncode check (rare). Treat as empty inbox -- safer than crash.
        return []

    out = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        if not name.endswith(".walnut"):
            continue
        out.append({
            "name": name,
            "sha": entry.get("sha", ""),
            "size": entry.get("size", 0),
            "path": entry.get("path", ""),
        })
    return out


def fetch_public_key(owner, repo, peer, timeout=10):
    # type: (str, str, str, int) -> str
    """Fetch the peer's public key PEM from the relay repo.

    GitHub returns file contents as a base64 blob in JSON when ``gh api``
    asks for ``repos/<owner>/<repo>/contents/keys/<peer>.pem``. This helper
    decodes and returns the PEM as a UTF-8 string.

    Used by ``/alive:relay accept`` (peer side) to read the OWNER's public
    key from the OWNER's relay so the peer can encrypt outbound packages
    against it. The keyring write happens in the skill flow, not here.

    Raises:
        GhClientError: on any failure (gh non-zero, missing key file, JSON
            decode error, base64 decode error). The skill catches this and
            tells the user the key file is missing or unreadable.
    """
    path = "repos/{0}/{1}/contents/keys/{2}.pem".format(owner, repo, peer)
    result = _run_gh(["api", path], timeout=timeout)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "unknown error").strip()
        raise GhClientError("fetch_public_key {0}: {1}".format(path, msg))

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GhClientError("fetch_public_key {0}: bad JSON ({1})".format(path, exc))

    if not isinstance(payload, dict) or "content" not in payload:
        raise GhClientError("fetch_public_key {0}: missing content field".format(path))

    encoding = payload.get("encoding", "base64")
    raw = payload.get("content", "")
    if encoding == "base64":
        import base64
        try:
            decoded = base64.b64decode(raw)
        except (ValueError, TypeError) as exc:
            raise GhClientError("fetch_public_key {0}: base64 decode failed ({1})".format(path, exc))
        try:
            return decoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GhClientError("fetch_public_key {0}: not utf-8 ({1})".format(path, exc))
    # Already-decoded content (rare; gh sometimes returns raw text).
    return str(raw)
