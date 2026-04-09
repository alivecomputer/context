#!/usr/bin/env python3
"""Unit tests for ``plugins/alive/hooks/hooks.json`` (LD15 + LD16, fn-7-7cw).

Pins the metadata invariants the manual hook count drift used to violate.
Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_hooks_json -v

Stdlib only -- the hooks.json file is plain JSON so this is a thin layer
over ``json.load`` plus a regex sanity check on the description string.
"""

import json
import os
import re
import unittest


_HERE = os.path.dirname(os.path.abspath(__file__))
_HOOKS_JSON = os.path.normpath(
    os.path.join(_HERE, "..", "hooks", "hooks.json")
)
_RELAY_CHECK_BASENAME = "alive-relay-check.sh"
_HARDCODED_COUNT_RE = re.compile(r"\d+\s+hooks?", re.IGNORECASE)


def _load_hooks():
    # type: () -> dict
    """Read hooks.json once per test (cheap; ~120 lines)."""
    with open(_HOOKS_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def _all_command_strings(hooks):
    # type: (dict) -> list
    """Return every ``command`` string across every event/matcher.

    Used by both the relay-check registration test and the duplicate test.
    """
    out = []
    events = hooks.get("hooks", {})
    for _event, matchers in events.items():
        if not isinstance(matchers, list):
            continue
        for matcher in matchers:
            if not isinstance(matcher, dict):
                continue
            for h in matcher.get("hooks", []):
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command", "")
                if cmd:
                    out.append(cmd)
    return out


def _commands_for_matcher(hooks, event, matcher_value):
    # type: (dict, str, str) -> list
    """Return command strings for a specific event + matcher combo."""
    out = []
    for matcher in hooks.get("hooks", {}).get(event, []) or []:
        if matcher.get("matcher") != matcher_value:
            continue
        for h in matcher.get("hooks", []) or []:
            cmd = h.get("command", "")
            if cmd:
                out.append(cmd)
    return out


class HooksJsonStructureTests(unittest.TestCase):
    """The file is valid JSON and parses to the expected shape."""

    def test_file_exists(self):
        self.assertTrue(
            os.path.exists(_HOOKS_JSON),
            "hooks.json missing at {0}".format(_HOOKS_JSON),
        )

    def test_file_is_valid_json(self):
        # Will raise on bad JSON.
        data = _load_hooks()
        self.assertIsInstance(data, dict)

    def test_top_level_keys(self):
        data = _load_hooks()
        self.assertIn("description", data)
        self.assertIn("hooks", data)
        self.assertIsInstance(data["hooks"], dict)

    def test_session_start_present(self):
        data = _load_hooks()
        self.assertIn("SessionStart", data["hooks"])
        ss = data["hooks"]["SessionStart"]
        self.assertIsInstance(ss, list)
        self.assertGreater(len(ss), 0, "SessionStart must have at least one matcher")


class LD15DescriptionTests(unittest.TestCase):
    """LD15 -- description must NOT contain a hardcoded hook count."""

    def test_description_has_no_hardcoded_count(self):
        data = _load_hooks()
        description = data.get("description", "")
        self.assertIsInstance(description, str)
        match = _HARDCODED_COUNT_RE.search(description)
        self.assertIsNone(
            match,
            "hooks.json description must not contain a hardcoded hook count "
            "(matched: {0!r}). Description: {1!r}".format(
                match.group(0) if match else None, description
            ),
        )

    def test_description_is_canonical_string(self):
        # The exact LD15 string. If the description ever drifts to something
        # else this fails loudly so the change is intentional.
        data = _load_hooks()
        expected = (
            "ALIVE Context System hooks. Session hooks read/write "
            ".alive/_squirrels/. All read stdin JSON for session_id."
        )
        self.assertEqual(data.get("description"), expected)


class LD16RelayCheckRegistrationTests(unittest.TestCase):
    """LD16 -- alive-relay-check.sh must be registered on startup + resume."""

    def test_alive_relay_check_registered_startup(self):
        hooks = _load_hooks()
        cmds = _commands_for_matcher(hooks, "SessionStart", "startup")
        self.assertTrue(
            any(_RELAY_CHECK_BASENAME in c for c in cmds),
            "{0} not registered on SessionStart.startup. Got: {1}".format(
                _RELAY_CHECK_BASENAME, cmds
            ),
        )

    def test_alive_relay_check_registered_resume(self):
        hooks = _load_hooks()
        cmds = _commands_for_matcher(hooks, "SessionStart", "resume")
        self.assertTrue(
            any(_RELAY_CHECK_BASENAME in c for c in cmds),
            "{0} not registered on SessionStart.resume. Got: {1}".format(
                _RELAY_CHECK_BASENAME, cmds
            ),
        )

    def test_relay_check_uses_bash_command(self):
        # Defensive: the command must be a bash invocation, not a raw script
        # path. Claude Code expects "bash <path>" so the hook chain works on
        # Windows + macOS without a shebang round-trip.
        hooks = _load_hooks()
        cmds = _commands_for_matcher(hooks, "SessionStart", "startup")
        relay_cmds = [c for c in cmds if _RELAY_CHECK_BASENAME in c]
        self.assertTrue(relay_cmds, "no relay-check command found on startup")
        for c in relay_cmds:
            self.assertTrue(
                c.startswith("bash "),
                "relay-check command must start with 'bash ': {0!r}".format(c),
            )


class HookDuplicationTests(unittest.TestCase):
    """No hook script should be registered twice within the SAME matcher.

    Cross-matcher reuse is fine (relay-check fires on BOTH startup AND
    resume by design). The constraint is per-matcher: registering the same
    script twice in one matcher list would run it twice on every fire.
    """

    def test_no_duplicate_hook_entries_within_matcher(self):
        hooks = _load_hooks()
        events = hooks.get("hooks", {})
        for event, matchers in events.items():
            if not isinstance(matchers, list):
                continue
            for matcher in matchers:
                if not isinstance(matcher, dict):
                    continue
                cmds = []
                for h in matcher.get("hooks", []) or []:
                    cmd = h.get("command", "")
                    if cmd:
                        cmds.append(cmd)
                seen = set()
                for c in cmds:
                    self.assertNotIn(
                        c,
                        seen,
                        "duplicate hook command in {0}/{1}: {2!r}".format(
                            event, matcher.get("matcher", "<no-matcher>"), c
                        ),
                    )
                    seen.add(c)


if __name__ == "__main__":
    unittest.main()
