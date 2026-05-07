#!/usr/bin/env python3
"""ALIVE GitHub star prompt -- runtime module.

Stdlib-only. Public CLI:
    star_prompt.py session-start --world <path>
    star_prompt.py respond --world <path> --choice {yes,maybe,never}
    star_prompt.py save-nudge --world <path>
"""
import argparse
import json
import subprocess
import sys
import webbrowser
from datetime import date
from pathlib import Path
from typing import Any, Dict

from _atomic_io import atomic_write_text


REPO = "alivecontext/alive"
REPO_URL = "https://github.com/alivecontext/alive"


def open_in_browser():
    # type: () -> bool
    """Open the repo URL in the default browser. Returns False on failure."""
    try:
        return bool(webbrowser.open(REPO_URL))
    except Exception:
        return False


def state_path(world_root):
    # type: (Path) -> Path
    return Path(world_root) / ".alive" / "_generated" / "github-star.json"


DEFAULT_STATE = {
    "starred": None,
    "session_count": 0,
    "session_nudges_shown": [],
    "saves_since_last_nudge": 0,
    "save_nudges_shown": 0,
    "last_prompt": None,
}


def load_state(world_root):
    # type: (Path) -> Dict[str, Any]
    """Load runtime state, returning defaults if file missing or corrupt."""
    path = state_path(world_root)
    if not path.exists():
        return dict(DEFAULT_STATE)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_STATE)
    merged = dict(DEFAULT_STATE)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_STATE})
    return merged


SURFACES = {
    "invitation": "  If ALIVE has earned a star · github.com/alivecontext/alive",
    "save_nudge": "│  ☆ github.com/alivecontext/alive",
    "ceremony": (
        "╭─ 🐿️ one small ask\n"
        "│  ALIVE is open source. If it's earned its place\n"
        "│  in your week, a star helps more than you'd think.\n"
        "│\n"
        "│  Uses your own gh login. Nothing leaves your\n"
        "│  machine except the star itself.\n"
        "│\n"
        "│  ▸ Star alivecontext/alive?\n"
        "│  1. Yes\n"
        "│  2. Maybe later\n"
        "│  3. Already starred / never ask\n"
        "╰─"
    ),
}


def render_surface(name):
    # type: (str) -> str
    return SURFACES.get(name, "")


def star_via_gh():
    # type: () -> str
    """Attempt to star the repo via the user's gh CLI.

    Returns 'success' if gh exits 0, 'fallback' for any other outcome
    (gh missing, unauth, network, timeout). The caller decides what to do
    with 'fallback'.
    """
    try:
        result = subprocess.run(
            ["gh", "repo", "star", REPO],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "fallback"
    if result.returncode == 0:
        return "success"
    return "fallback"


SESSION_THRESHOLDS = {
    5:  "invitation",
    10: "invitation",
    20: "ceremony",
    40: "ceremony",
    60: "ceremony",
}
SAVE_NUDGE_INTERVAL = 7
SAVE_NUDGE_LIFETIME_CAP = 3


def which_session_surface(state):
    # type: (Dict[str, Any]) -> Any
    """Return surface name for the current session, or None.

    Caller is responsible for the ask-flag short-circuit and for appending
    the count to session_nudges_shown after rendering.
    """
    if state.get("starred") is True:
        return None
    count = state.get("session_count", 0)
    surface = SESSION_THRESHOLDS.get(count)
    if surface is None:
        return None
    if count in state.get("session_nudges_shown", []):
        return None
    return surface


def should_show_save_nudge(state):
    # type: (Dict[str, Any]) -> bool
    if state.get("starred") is True:
        return False
    if state.get("save_nudges_shown", 0) >= SAVE_NUDGE_LIFETIME_CAP:
        return False
    return state.get("saves_since_last_nudge", 0) >= SAVE_NUDGE_INTERVAL


def read_ask_flag(world_root):
    # type: (Path) -> bool
    """Read the github_star_ask flat toggle from preferences.yaml.

    Defaults to True if the file is missing or the key is absent. Mirrors
    the line-level parsing pattern in alive-resolve-preferences.sh -- only
    flat top-level scalar keys are recognised.
    """
    prefs = Path(world_root) / ".alive" / "preferences.yaml"
    if not prefs.exists():
        return True
    try:
        for line in prefs.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            if key.strip() == "github_star_ask":
                return value.strip().lower() not in ("false", "off", "no")
    except OSError:
        return True
    return True


CEREMONY_AGENT_INSTRUCTIONS = (
    "\n[ALIVE -- agent: when the user picks 1, 2, or 3 above, run "
    "`python3 \"$ALIVE_PLUGIN_ROOT/scripts/star_prompt.py\" respond "
    "--world \"$ALIVE_WORLD_ROOT\" --choice {yes|maybe|never}` "
    "(map 1->yes, 2->maybe, 3->never). Render no other output for the choice.]\n"
)

CONFIRM_STARRED = (
    "╭─ 🐿️ starred\n"
    "│  Thanks. That actually does help.\n"
    "╰─"
)

CONFIRM_BROWSER = (
    "╭─ 🐿️ opened in your browser\n"
    "│  Couldn't action the star locally, so I opened the repo.\n"
    "│  Hit the star button at the top right if ALIVE has earned it.\n"
    "╰─"
)


def cmd_session_start(world_root):
    # type: (Path) -> str
    """Bump the session counter and decide which surface (if any) to emit."""
    state = load_state(world_root)
    state["session_count"] = int(state.get("session_count", 0)) + 1
    save_state(world_root, state)

    if not read_ask_flag(world_root):
        return ""

    surface = which_session_surface(state)
    if surface is None:
        return ""

    text = render_surface(surface)
    state["session_nudges_shown"] = sorted(
        set(state.get("session_nudges_shown", [])) | {state["session_count"]}
    )
    save_state(world_root, state)

    if surface == "ceremony":
        return text + CEREMONY_AGENT_INSTRUCTIONS
    return text


def cmd_save_nudge(world_root):
    # type: (Path) -> str
    state = load_state(world_root)
    state["saves_since_last_nudge"] = int(
        state.get("saves_since_last_nudge", 0)
    ) + 1

    if not read_ask_flag(world_root) or state.get("starred") is True:
        save_state(world_root, state)
        return ""

    if not should_show_save_nudge(state):
        save_state(world_root, state)
        return ""

    state["saves_since_last_nudge"] = 0
    state["save_nudges_shown"] = int(state.get("save_nudges_shown", 0)) + 1
    save_state(world_root, state)
    return render_surface("save_nudge")


def cmd_respond(world_root, choice):
    # type: (Path, str) -> str
    outcome = handle_response(world_root, choice)
    if outcome == "starred":
        return CONFIRM_STARRED
    if outcome == "browser":
        return CONFIRM_BROWSER
    return ""


def handle_response(world_root, choice):
    # type: (Path, str) -> str
    """Apply the ceremony response to state and side effects.

    Returns the surface to render in the agent context after the action:
        'starred' / 'browser' / 'silent'
    Unknown choices are no-ops returning 'silent'.
    """
    state = load_state(world_root)
    if choice == "yes":
        outcome = star_via_gh()
        if outcome == "success":
            state["starred"] = True
            save_state(world_root, state)
            disable_ask(world_root)
            return "starred"
        open_in_browser()
        save_state(world_root, state)
        disable_ask(world_root)
        return "browser"
    if choice == "maybe":
        state["last_prompt"] = date.today().isoformat()
        save_state(world_root, state)
        return "silent"
    if choice == "never":
        disable_ask(world_root)
        return "silent"
    return "silent"


def disable_ask(world_root):
    # type: (Path) -> None
    """Persist 'never ask' by writing github_star_ask: false into preferences.yaml.

    If the key already exists (any value), the line is rewritten in place.
    If absent, a new line is appended. Comments and ordering of other lines
    are preserved. Writes go through the in-house atomic helper so a crash
    mid-write cannot leave a half-truncated preferences file.
    """
    prefs = Path(world_root) / ".alive" / "preferences.yaml"
    new_line = "github_star_ask: false"
    if not prefs.exists():
        atomic_write_text(str(prefs), new_line + "\n", mode=0o600, parent_mode=0o700)
        return
    lines = prefs.read_text(encoding="utf-8").splitlines()
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("github_star_ask:"):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    payload = "\n".join(lines).rstrip() + "\n"
    atomic_write_text(str(prefs), payload, mode=0o600, parent_mode=0o700)


def save_state(world_root, state):
    # type: (Path, Dict[str, Any]) -> None
    """Atomic write to .alive/_generated/github-star.json via the
    in-house atomic_write_text primitive."""
    path = state_path(world_root)
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    atomic_write_text(str(path), payload, mode=0o600, parent_mode=0o700)


def _parse_args(argv):
    # type: (Any) -> argparse.Namespace
    parser = argparse.ArgumentParser(prog="star_prompt")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_session = sub.add_parser("session-start")
    p_session.add_argument("--world", required=True, type=Path)

    p_respond = sub.add_parser("respond")
    p_respond.add_argument("--world", required=True, type=Path)
    p_respond.add_argument(
        "--choice", required=True, choices=["yes", "maybe", "never"]
    )

    p_save = sub.add_parser("save-nudge")
    p_save.add_argument("--world", required=True, type=Path)

    return parser.parse_args(argv)


def _log_failure(world_root, exc):
    # type: (Path, Exception) -> None
    try:
        log_dir = Path(world_root) / ".alive" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "star-prompt.log").open("a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (date.today().isoformat(), exc))
    except Exception:
        pass


def main(argv=None):
    # type: (Any) -> int
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        if args.cmd == "session-start":
            sys.stdout.write(cmd_session_start(args.world))
        elif args.cmd == "respond":
            sys.stdout.write(cmd_respond(args.world, args.choice))
        elif args.cmd == "save-nudge":
            sys.stdout.write(cmd_save_nudge(args.world))
    except Exception as exc:  # the hook never blocks user-visibly
        _log_failure(args.world, exc)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
