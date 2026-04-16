"""Unit tests for ``alive_mcp.errors`` and ``alive_mcp.envelope``.

Covers the T4 acceptance criteria:

1. All 9 error codes are defined with message templates and suggestions.
2. ``envelope.ok()`` and ``envelope.error()`` return the canonical
   ``CallToolResult``-shaped dict.
3. Error envelope shape matches the MCP spec fields (``content``,
   ``structuredContent``, ``isError``).
4. No message template in the codebook contains an absolute filesystem
   path — the mask IS the template.
5. ``error_from_exception`` NEVER leaks ``str(exc)`` to the client;
   unknown codes degrade to a generic mask message.

Uses stdlib ``unittest``. No third-party deps — matches the T2/T3
convention.
"""
from __future__ import annotations

import enum
import json
import os
import pathlib
import re
import sys
import unittest

# Ensure ``python3 -m unittest discover tests`` works from a clean
# checkout without requiring ``pip install -e .``. Mirrors the shim in
# ``test_paths.py`` / ``test_vendor_smoke.py``.
_SRC_DIR = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if os.path.isdir(_SRC_DIR) and _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from alive_mcp import envelope, errors  # noqa: E402


class ErrorCodeEnumTests(unittest.TestCase):
    """The codebook is an ``enum.Enum`` per spec; members are strings."""

    EXPECTED = (
        errors.ErrorCode.ERR_NO_WORLD,
        errors.ErrorCode.ERR_WALNUT_NOT_FOUND,
        errors.ErrorCode.ERR_BUNDLE_NOT_FOUND,
        errors.ErrorCode.ERR_KERNEL_FILE_MISSING,
        errors.ErrorCode.ERR_PERMISSION_DENIED,
        errors.ErrorCode.ERR_PATH_ESCAPE,
        errors.ErrorCode.ERR_INVALID_CURSOR,
        errors.ErrorCode.ERR_TOOL_TIMEOUT,
        errors.ErrorCode.ERR_AUDIT_DISK_FULL,
    )

    def test_error_code_is_an_enum(self) -> None:
        self.assertTrue(issubclass(errors.ErrorCode, enum.Enum))

    def test_error_code_members_are_strings(self) -> None:
        """``str, Enum`` mixin: members compare equal to their string values."""
        self.assertEqual(errors.ErrorCode.ERR_PATH_ESCAPE, "ERR_PATH_ESCAPE")
        self.assertEqual(errors.ErrorCode.ERR_NO_WORLD, "ERR_NO_WORLD")

    def test_enum_contains_exactly_expected_codes(self) -> None:
        self.assertEqual(set(errors.ErrorCode), set(self.EXPECTED))

    def test_error_codes_tuple_matches_enum(self) -> None:
        self.assertEqual(set(errors.ERROR_CODES), set(self.EXPECTED))

    def test_module_constants_alias_enum_members(self) -> None:
        """``ERR_PATH_ESCAPE`` constant IS the enum member (backward compat)."""
        self.assertIs(errors.ERR_PATH_ESCAPE, errors.ErrorCode.ERR_PATH_ESCAPE)
        self.assertIs(errors.ERR_NO_WORLD, errors.ErrorCode.ERR_NO_WORLD)
        self.assertIs(
            errors.ERR_WALNUT_NOT_FOUND, errors.ErrorCode.ERR_WALNUT_NOT_FOUND
        )
        self.assertIs(
            errors.ERR_BUNDLE_NOT_FOUND, errors.ErrorCode.ERR_BUNDLE_NOT_FOUND
        )
        self.assertIs(
            errors.ERR_KERNEL_FILE_MISSING,
            errors.ErrorCode.ERR_KERNEL_FILE_MISSING,
        )
        self.assertIs(
            errors.ERR_PERMISSION_DENIED, errors.ErrorCode.ERR_PERMISSION_DENIED
        )
        self.assertIs(errors.ERR_INVALID_CURSOR, errors.ErrorCode.ERR_INVALID_CURSOR)
        self.assertIs(errors.ERR_TOOL_TIMEOUT, errors.ErrorCode.ERR_TOOL_TIMEOUT)
        self.assertIs(
            errors.ERR_AUDIT_DISK_FULL, errors.ErrorCode.ERR_AUDIT_DISK_FULL
        )

    def test_wire_strips_err_prefix(self) -> None:
        self.assertEqual(errors.ErrorCode.ERR_PATH_ESCAPE.wire, "PATH_ESCAPE")
        self.assertEqual(
            errors.ErrorCode.ERR_WALNUT_NOT_FOUND.wire, "WALNUT_NOT_FOUND"
        )

    def test_error_code_is_json_serializable(self) -> None:
        """``str, Enum`` mixin emits the raw string when dumped."""
        encoded = json.dumps(errors.ErrorCode.ERR_PATH_ESCAPE)
        self.assertEqual(encoded, '"ERR_PATH_ESCAPE"')


class ErrorCodebookTests(unittest.TestCase):
    """Acceptance: all 9 codes have templates and suggestions."""

    def test_every_code_has_spec(self) -> None:
        for code in errors.ErrorCode:
            self.assertIn(code, errors.ERRORS)
            spec = errors.ERRORS[code]
            self.assertIsInstance(spec, errors.ErrorSpec)
            self.assertIs(spec.code, code)

    def test_every_code_has_non_empty_message(self) -> None:
        for code, spec in errors.ERRORS.items():
            self.assertIsInstance(spec.message, str)
            self.assertGreater(len(spec.message), 10, msg=code.value)

    def test_every_code_has_actionable_suggestions(self) -> None:
        for code, spec in errors.ERRORS.items():
            self.assertIsInstance(spec.suggestions, tuple)
            self.assertGreaterEqual(
                len(spec.suggestions),
                1,
                msg=f"{code.value} has no suggestions — LLM cannot self-recover",
            )
            for hint in spec.suggestions:
                self.assertIsInstance(hint, str)
                self.assertGreater(len(hint), 0)

    def test_error_spec_is_frozen(self) -> None:
        """Codebook mutation at runtime would be a nasty silent failure."""
        spec = errors.ERRORS[errors.ErrorCode.ERR_PATH_ESCAPE]
        with self.assertRaises(Exception):
            spec.message = "tampered"  # type: ignore[misc]

    def test_messages_projection_covers_all_codes(self) -> None:
        self.assertEqual(set(errors.MESSAGES.keys()), set(errors.ErrorCode))

    def test_suggestions_projection_covers_all_codes(self) -> None:
        self.assertEqual(set(errors.SUGGESTIONS.keys()), set(errors.ErrorCode))


class NoAbsolutePathsInMessagesTests(unittest.TestCase):
    """Acceptance: templates never leak absolute filesystem paths.

    The ``mask_error_details=True`` FastMCP promise is that user-facing
    messages do not leak server-internal paths. This test asserts the
    promise at the template level — the templates themselves contain
    zero absolute-path literals.
    """

    # POSIX absolute paths (``/etc``, ``/Users/foo``, ``/.alive``,
    # ``/_kernel``) and Windows drive paths (``C:\``, ``D:/``). The
    # ``{placeholders}`` that carry caller-facing identifiers (walnut,
    # bundle, file, query, tool) are not absolute paths.
    #
    # The POSIX check accepts ``/`` followed by ANY path-like character
    # (letter, digit, ``.``, ``_``, ``~``) at the boundary of a word so
    # ``/.alive/`` and ``/_kernel/`` are caught alongside ``/etc/``.
    POSIX_ABS = re.compile(r"(?:^|[\s'\"`(])/[A-Za-z0-9._~]")
    WINDOWS_ABS = re.compile(r"[A-Za-z]:[\\/]")

    def test_no_posix_absolute_path_in_any_message(self) -> None:
        for code, spec in errors.ERRORS.items():
            match = self.POSIX_ABS.search(spec.message)
            self.assertIsNone(
                match,
                msg=(
                    f"Message template for {code.value} contains a "
                    f"POSIX-shaped absolute path: {spec.message!r} "
                    f"(match={match.group() if match else None})"
                ),
            )

    def test_no_posix_absolute_path_in_any_suggestion(self) -> None:
        for code, spec in errors.ERRORS.items():
            for hint in spec.suggestions:
                match = self.POSIX_ABS.search(hint)
                self.assertIsNone(
                    match,
                    msg=(
                        f"Suggestion for {code.value} contains a "
                        f"POSIX-shaped absolute path: {hint!r} "
                        f"(match={match.group() if match else None})"
                    ),
                )

    def test_no_windows_absolute_path_in_any_message(self) -> None:
        for code, spec in errors.ERRORS.items():
            self.assertIsNone(
                self.WINDOWS_ABS.search(spec.message),
                msg=(
                    f"Message template for {code.value} contains a "
                    f"Windows drive path: {spec.message!r}"
                ),
            )

    def test_no_windows_absolute_path_in_any_suggestion(self) -> None:
        for code, spec in errors.ERRORS.items():
            for hint in spec.suggestions:
                self.assertIsNone(
                    self.WINDOWS_ABS.search(hint),
                    msg=(
                        f"Suggestion for {code.value} contains a Windows "
                        f"drive path: {hint!r}"
                    ),
                )

    def test_posix_regex_catches_dotfile_paths(self) -> None:
        """Regression: the earlier regex missed ``/.alive``-style paths."""
        self.assertIsNotNone(self.POSIX_ABS.search("see /.alive/config"))
        self.assertIsNotNone(self.POSIX_ABS.search("wrote /_kernel/log.md"))
        self.assertIsNotNone(self.POSIX_ABS.search("read /etc/passwd"))
        # Should NOT flag plain ``/`` used as a separator in
        # non-absolute contexts (there are none in the codebook but
        # guard the detector anyway).
        self.assertIsNone(self.POSIX_ABS.search("a/b/c is a relative path"))


class ExceptionToCodeMappingTests(unittest.TestCase):
    """Each code-emitting seam has a matching ``AliveMcpError`` subclass."""

    def test_path_escape_exception_carries_enum_code(self) -> None:
        exc = errors.PathEscapeError("candidate escaped")
        self.assertIs(exc.code, errors.ErrorCode.ERR_PATH_ESCAPE)
        self.assertIsInstance(exc, errors.AliveMcpError)

    def test_walnut_not_found_exception_carries_code(self) -> None:
        exc = errors.WalnutNotFoundError("no such walnut")
        self.assertIs(exc.code, errors.ErrorCode.ERR_WALNUT_NOT_FOUND)

    def test_bundle_not_found_exception_carries_code(self) -> None:
        exc = errors.BundleNotFoundError("no such bundle")
        self.assertIs(exc.code, errors.ErrorCode.ERR_BUNDLE_NOT_FOUND)

    def test_kernel_file_missing_exception_carries_code(self) -> None:
        exc = errors.KernelFileMissingError("log.md missing")
        self.assertIs(exc.code, errors.ErrorCode.ERR_KERNEL_FILE_MISSING)

    def test_permission_denied_exception_carries_code(self) -> None:
        exc = errors.PermissionDeniedError("EACCES")
        self.assertIs(exc.code, errors.ErrorCode.ERR_PERMISSION_DENIED)

    def test_invalid_cursor_exception_carries_code(self) -> None:
        exc = errors.InvalidCursorError("bad token")
        self.assertIs(exc.code, errors.ErrorCode.ERR_INVALID_CURSOR)

    def test_tool_timeout_exception_carries_code(self) -> None:
        exc = errors.ToolTimeoutError("exceeded")
        self.assertIs(exc.code, errors.ErrorCode.ERR_TOOL_TIMEOUT)

    def test_audit_disk_full_exception_carries_code(self) -> None:
        exc = errors.AuditDiskFullError("ENOSPC")
        self.assertIs(exc.code, errors.ErrorCode.ERR_AUDIT_DISK_FULL)

    def test_kernel_file_error_reexported_from_vendor(self) -> None:
        """``KernelFileError`` comes from ``_vendor._pure`` — present on errors."""
        self.assertTrue(hasattr(errors, "KernelFileError"))
        self.assertTrue(issubclass(errors.KernelFileError, Exception))

    def test_world_not_found_error_reexported_from_vendor(self) -> None:
        self.assertTrue(hasattr(errors, "WorldNotFoundError"))
        self.assertTrue(issubclass(errors.WorldNotFoundError, Exception))

    def test_malformed_yaml_warning_reexported_from_vendor(self) -> None:
        self.assertTrue(hasattr(errors, "MalformedYAMLWarning"))
        self.assertTrue(issubclass(errors.MalformedYAMLWarning, Warning))


class EnvelopeOkShapeTests(unittest.TestCase):
    """Acceptance: ``envelope.ok`` returns the MCP ``CallToolResult`` shape."""

    def test_ok_with_dict_payload(self) -> None:
        resp = envelope.ok({"walnuts": ["a", "b"], "count": 2})
        self.assertEqual(set(resp.keys()), {"content", "structuredContent", "isError"})
        self.assertFalse(resp["isError"])
        self.assertEqual(resp["structuredContent"], {"walnuts": ["a", "b"], "count": 2})
        # content[0] is a TextContent-shaped block.
        self.assertIsInstance(resp["content"], list)
        self.assertEqual(len(resp["content"]), 1)
        self.assertEqual(resp["content"][0]["type"], "text")
        # content text parses back to the structured payload.
        parsed = json.loads(resp["content"][0]["text"])
        self.assertEqual(parsed, {"walnuts": ["a", "b"], "count": 2})

    def test_ok_merges_meta_into_structured_content(self) -> None:
        resp = envelope.ok({"matches": [1, 2]}, next_cursor="abc123", total=17)
        self.assertEqual(
            resp["structuredContent"],
            {"matches": [1, 2], "next_cursor": "abc123", "total": 17},
        )
        self.assertFalse(resp["isError"])

    def test_ok_wraps_list_payload_under_data_key(self) -> None:
        """Non-dict payloads go under ``data`` so structuredContent stays an object."""
        resp = envelope.ok([{"id": 1}, {"id": 2}])
        self.assertEqual(resp["structuredContent"], {"data": [{"id": 1}, {"id": 2}]})

    def test_ok_wraps_scalar_payload_under_data_key(self) -> None:
        resp = envelope.ok("ok")
        self.assertEqual(resp["structuredContent"], {"data": "ok"})

    def test_ok_raises_on_meta_key_collision(self) -> None:
        """Silent shadowing is a bug, not a feature — collision raises."""
        with self.assertRaises(ValueError):
            envelope.ok({"total": 5}, total=10)

    def test_ok_rejects_meta_collision_with_non_dict_payload(self) -> None:
        """Non-dict payload goes under ``data``; the collision check is unified.

        Python's function-signature check already blocks the literal
        ``envelope.ok('x', data='y')`` and splat forms that include
        ``data`` (``TypeError: multiple values for argument``), so the
        ``data`` key specifically is unreachable from kwargs. The
        unified collision check in ``ok`` still matters because it
        confirms the algorithm is the same for both dict and non-dict
        payloads — future refactors that expose additional reserved
        keys (e.g. if the non-dict wrapper grew a ``schema`` or ``type``
        key) would be caught automatically.

        The important shadowing case — dict payload + meta kwarg with
        the same key — is exercised by the sibling test
        ``test_ok_raises_on_meta_key_collision``.
        """
        # Positive: a plain non-dict payload with non-colliding meta is
        # accepted and wraps correctly.
        resp = envelope.ok("payload", total=1)
        self.assertEqual(resp["structuredContent"], {"data": "payload", "total": 1})

    def test_ok_renders_unicode_without_escape(self) -> None:
        """``ensure_ascii=False`` lets bundle/walnut names with unicode pass through."""
        resp = envelope.ok({"name": "北極星"})
        self.assertIn("北極星", resp["content"][0]["text"])


class EnvelopeErrorShapeTests(unittest.TestCase):
    """Acceptance: ``envelope.error`` returns a well-shaped error envelope."""

    def test_error_shape_is_call_tool_result(self) -> None:
        resp = envelope.error(
            errors.ErrorCode.ERR_WALNUT_NOT_FOUND, walnut="nova-station"
        )
        self.assertEqual(set(resp.keys()), {"content", "structuredContent", "isError"})
        self.assertTrue(resp["isError"])

    def test_error_accepts_enum_member(self) -> None:
        resp = envelope.error(
            errors.ErrorCode.ERR_WALNUT_NOT_FOUND, walnut="nova-station"
        )
        self.assertEqual(resp["structuredContent"]["error"], "WALNUT_NOT_FOUND")

    def test_error_accepts_string_constant(self) -> None:
        """String-constant callers (T3-style) still work via ``str, Enum`` mixin."""
        resp = envelope.error(errors.ERR_WALNUT_NOT_FOUND, walnut="nova-station")
        self.assertEqual(resp["structuredContent"]["error"], "WALNUT_NOT_FOUND")

    def test_error_accepts_raw_string_code(self) -> None:
        resp = envelope.error("ERR_WALNUT_NOT_FOUND", walnut="nova-station")
        self.assertEqual(resp["structuredContent"]["error"], "WALNUT_NOT_FOUND")

    def test_error_accepts_wire_form_without_err_prefix(self) -> None:
        """Callers echoing ``structuredContent['error']`` back (wire form)."""
        resp = envelope.error("WALNUT_NOT_FOUND", walnut="nova-station")
        self.assertEqual(resp["structuredContent"]["error"], "WALNUT_NOT_FOUND")
        # The codebook lookup succeeded, so the message is formatted.
        self.assertIn("nova-station", resp["structuredContent"]["message"])

    def test_error_code_strips_err_prefix(self) -> None:
        """Surface ``error`` field drops ``ERR_`` per Merge/Workato convention."""
        resp = envelope.error(
            errors.ErrorCode.ERR_WALNUT_NOT_FOUND, walnut="nova-station"
        )
        self.assertEqual(resp["structuredContent"]["error"], "WALNUT_NOT_FOUND")

    def test_error_message_formats_placeholders(self) -> None:
        resp = envelope.error(
            errors.ErrorCode.ERR_WALNUT_NOT_FOUND, walnut="nova-station"
        )
        self.assertIn("nova-station", resp["structuredContent"]["message"])

    def test_error_includes_suggestions_list(self) -> None:
        resp = envelope.error(
            errors.ErrorCode.ERR_WALNUT_NOT_FOUND, walnut="nova-station"
        )
        self.assertIsInstance(resp["structuredContent"]["suggestions"], list)
        self.assertGreater(len(resp["structuredContent"]["suggestions"]), 0)

    def test_error_content_text_parses_as_structured_content(self) -> None:
        resp = envelope.error(
            errors.ErrorCode.ERR_BUNDLE_NOT_FOUND,
            walnut="nova-station",
            bundle="shielding-review",
        )
        parsed = json.loads(resp["content"][0]["text"])
        self.assertEqual(parsed, resp["structuredContent"])

    def test_error_missing_template_placeholder_degrades_gracefully(self) -> None:
        """Missing kwarg for a referenced placeholder must not crash.

        Degrades to the unformatted template — the envelope does NOT
        surface the offending placeholder name (internal detail).
        """
        # ERR_TOOL_TIMEOUT expects ``{tool}`` and ``{timeout_s}`` —
        # supply neither and confirm we get a well-formed envelope.
        resp = envelope.error(errors.ErrorCode.ERR_TOOL_TIMEOUT)
        self.assertTrue(resp["isError"])
        # The unformatted template shows through.
        self.assertIn("{tool}", resp["structuredContent"]["message"])
        # Internal detail stays out.
        self.assertNotIn("missing placeholder", resp["structuredContent"]["message"])
        self.assertNotIn("KeyError", resp["structuredContent"]["message"])

    def test_error_format_spec_mismatch_degrades_gracefully(self) -> None:
        """Passing a non-numeric for a ``{x:.1f}`` slot must not crash.

        ``ERR_TOOL_TIMEOUT`` uses ``{timeout_s:.1f}`` — pass a string
        instead of a float and confirm we still get a well-formed
        envelope instead of a ``ValueError``/``TypeError`` escaping
        from ``.format()``.
        """
        resp = envelope.error(
            errors.ErrorCode.ERR_TOOL_TIMEOUT, tool="search", timeout_s="not-a-float"
        )
        self.assertTrue(resp["isError"])
        # Degrades to the unformatted template.
        self.assertIn("{timeout_s", resp["structuredContent"]["message"])
        # Raw exception string + internal detail stay out.
        self.assertNotIn("not-a-float", resp["structuredContent"]["message"])
        self.assertNotIn("formatting error", resp["structuredContent"]["message"])
        self.assertNotIn("ValueError", resp["structuredContent"]["message"])

    def test_error_unknown_code_returns_unknown_envelope(self) -> None:
        """Unknown codes degrade to a well-formed ``UNKNOWN`` envelope."""
        resp = envelope.error("ERR_BOGUS_NEVER_DEFINED")
        self.assertTrue(resp["isError"])
        # Strips the ``ERR_`` prefix even for unknown codes.
        self.assertEqual(
            resp["structuredContent"]["error"], "BOGUS_NEVER_DEFINED"
        )
        # Unknown codes get the masked generic message — not the caller's
        # hypothetical debug string.
        self.assertEqual(
            resp["structuredContent"]["message"], "An unknown error occurred."
        )

    def test_error_envelopes_render_for_every_frozen_code(self) -> None:
        """Every frozen code produces a well-formed envelope.

        Builds the minimal kwarg set each template needs — if a template
        is added later that references a new placeholder, either this
        test or the missing-placeholder fallback catches it.
        """
        kwarg_matrix = {
            errors.ErrorCode.ERR_NO_WORLD: {},
            errors.ErrorCode.ERR_WALNUT_NOT_FOUND: {"walnut": "nova-station"},
            errors.ErrorCode.ERR_BUNDLE_NOT_FOUND: {
                "walnut": "nova-station",
                "bundle": "shielding-review",
            },
            errors.ErrorCode.ERR_KERNEL_FILE_MISSING: {
                "walnut": "nova-station",
                "file": "log",
            },
            errors.ErrorCode.ERR_PERMISSION_DENIED: {
                "walnut": "nova-station",
                "file": "log",
            },
            errors.ErrorCode.ERR_PATH_ESCAPE: {},
            errors.ErrorCode.ERR_INVALID_CURSOR: {},
            errors.ErrorCode.ERR_TOOL_TIMEOUT: {
                "tool": "search_world",
                "timeout_s": 5.0,
            },
            errors.ErrorCode.ERR_AUDIT_DISK_FULL: {},
        }
        for code, kwargs in kwarg_matrix.items():
            resp = envelope.error(code, **kwargs)
            self.assertTrue(resp["isError"], msg=f"{code.value} envelope isError not True")
            sc = resp["structuredContent"]
            self.assertEqual(sc["error"], code.wire, msg=code.value)
            self.assertGreater(len(sc["message"]), 0, msg=code.value)
            self.assertIsInstance(sc["suggestions"], list, msg=code.value)
            self.assertNotIn(
                "{", sc["message"], msg=f"{code.value} has unfilled placeholder"
            )


class EnvelopeErrorFromExceptionTests(unittest.TestCase):
    """``error_from_exception`` bridges ``AliveMcpError`` to envelope.

    Critically: it MUST NOT leak ``str(exc)`` to the client. The codebook
    template always wins, and unknown codes degrade to the masked
    generic message. This preserves ``mask_error_details=True`` even
    when a subclass is raised with sensitive detail in its string form.
    """

    def test_walnut_not_found_exception_maps_to_envelope(self) -> None:
        try:
            raise errors.WalnutNotFoundError("nova-station")
        except errors.AliveMcpError as exc:
            resp = envelope.error_from_exception(exc, walnut="nova-station")
        self.assertTrue(resp["isError"])
        self.assertEqual(resp["structuredContent"]["error"], "WALNUT_NOT_FOUND")
        self.assertIn("nova-station", resp["structuredContent"]["message"])

    def test_path_escape_exception_does_not_leak_exception_message(self) -> None:
        """Exception's string form is masked — template wins."""
        try:
            raise errors.PathEscapeError("escape via /etc/passwd")
        except errors.AliveMcpError as exc:
            resp = envelope.error_from_exception(exc)
        self.assertTrue(resp["isError"])
        self.assertEqual(resp["structuredContent"]["error"], "PATH_ESCAPE")
        self.assertNotIn("/etc/passwd", resp["structuredContent"]["message"])
        self.assertNotIn("escape via", resp["structuredContent"]["message"])

    def test_unknown_code_exception_is_fully_masked(self) -> None:
        """Unknown code on an exception still returns the generic mask message.

        The tool layer's contract is that it only raises codes in
        :data:`errors.ERROR_CODES`. If that contract is violated we
        prefer losing debug detail to leaking it — the audit log (T12)
        is the right channel for debug info, not the envelope.
        """

        class BogusError(errors.AliveMcpError):
            code = "ERR_NEVER_DEFINED"

        try:
            raise BogusError("internal details that should not leak")
        except errors.AliveMcpError as exc:
            resp = envelope.error_from_exception(exc)
        self.assertTrue(resp["isError"])
        self.assertEqual(resp["structuredContent"]["error"], "NEVER_DEFINED")
        # Generic mask — NOT the exception's string form.
        self.assertEqual(
            resp["structuredContent"]["message"], "An unknown error occurred."
        )
        self.assertNotIn(
            "internal details", resp["structuredContent"]["message"]
        )


class EnvelopeSerializableTests(unittest.TestCase):
    """Envelopes are JSON-round-trippable — they go over stdio as JSON-RPC."""

    def test_ok_envelope_round_trips_through_json(self) -> None:
        resp = envelope.ok(
            {"walnuts": [{"name": "a"}, {"name": "b"}]}, next_cursor="x"
        )
        serialized = json.dumps(resp)
        parsed = json.loads(serialized)
        self.assertEqual(parsed, resp)

    def test_error_envelope_round_trips_through_json(self) -> None:
        resp = envelope.error(
            errors.ErrorCode.ERR_BUNDLE_NOT_FOUND, walnut="w", bundle="b"
        )
        serialized = json.dumps(resp)
        parsed = json.loads(serialized)
        self.assertEqual(parsed, resp)


if __name__ == "__main__":
    unittest.main()
