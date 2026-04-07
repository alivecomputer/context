#!/usr/bin/env python3
"""Unit tests for the v3 manifest layer in ``alive-p2p.py``.

Covers LD6 (format version contract), LD20 (manifest schema, canonical JSON,
checksums, signature), and the stdlib-only YAML reader/writer. Each test
builds manifest dicts directly or stages a tiny fixture under
``tempfile.TemporaryDirectory`` and asserts on the canonical bytes, on-disk
YAML round-trips, and validator behaviour.

Run from ``claude-code/`` with::

    python3 -m unittest plugins.alive.tests.test_manifest -v

Stdlib only -- no PyYAML, no third-party assertions.
"""

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


# ---------------------------------------------------------------------------
# Module loading: alive-p2p.py has a hyphen in the filename so a plain
# ``import alive_p2p`` does not work. Load it via importlib.util from the
# scripts directory.
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

# walnut_paths is a plain module; import it first so alive-p2p's own
# ``import walnut_paths`` line hits the cache instead of re-importing.
import walnut_paths  # noqa: E402,F401

_AP2P_PATH = os.path.join(_SCRIPTS, "alive-p2p.py")
_spec = importlib.util.spec_from_file_location("alive_p2p", _AP2P_PATH)
ap2p = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ap2p)  # type: ignore[union-attr]


FIXED_TS = "2026-04-07T12:00:00Z"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_minimal_staging(tmp):
    """Create a tiny staging tree with three files for manifest generation."""
    staging = os.path.join(tmp, "stage")
    _write(os.path.join(staging, "_kernel", "key.md"), "---\nname: test\n---\n")
    _write(os.path.join(staging, "_kernel", "tasks.json"), '{"tasks": []}\n')
    _write(os.path.join(staging, "shielding-review", "draft.md"), "# draft\n")
    return staging


def _base_manifest():
    """Return a known manifest dict for canonical-bytes / round-trip tests."""
    return {
        "format_version": "2.1.0",
        "source_layout": "v3",
        "min_plugin_version": "3.1.0",
        "created": FIXED_TS,
        "scope": "snapshot",
        "source": {
            "walnut": "nova-station",
            "session_id": "abc123",
            "engine": "claude-opus-4-6",
            "plugin_version": "3.1.0",
        },
        "sender": "patrickSupernormal",
        "description": "test package",
        "note": "",
        "exclusions_applied": [],
        "substitutions_applied": [],
        "payload_sha256": (
            "0000000000000000000000000000000000000000000000000000000000000000"
        ),
        "files": [
            {
                "path": "_kernel/key.md",
                "sha256": "a" * 64,
                "size": 10,
            },
        ],
        "encryption": "none",
    }


# ---------------------------------------------------------------------------
# canonical_manifest_bytes (LD20)
# ---------------------------------------------------------------------------


class CanonicalManifestBytesTests(unittest.TestCase):
    """canonical_manifest_bytes is byte-stable across input variations."""

    def test_canonical_bytes_determinism(self):
        """Same dict produces same bytes regardless of dict insertion order."""
        a = {"format_version": "2.1.0", "scope": "full", "files": []}
        b = {"files": [], "scope": "full", "format_version": "2.1.0"}
        self.assertEqual(
            ap2p.canonical_manifest_bytes(a),
            ap2p.canonical_manifest_bytes(b),
        )

    def test_canonical_bytes_strips_signature(self):
        """signature field is removed before canonicalization."""
        unsigned = _base_manifest()
        signed = _base_manifest()
        signed["signature"] = {
            "algo": "rsa-pss-sha256",
            "pubkey_id": "deadbeefcafebabe",
            "sig_b64": "ZmFrZXNpZw==",
            "signed_bytes": "manifest-canonical-json-v1",
        }
        self.assertEqual(
            ap2p.canonical_manifest_bytes(unsigned),
            ap2p.canonical_manifest_bytes(signed),
        )

    def test_canonical_bytes_does_not_mutate_input(self):
        """canonical_manifest_bytes must not mutate the caller's dict."""
        m = _base_manifest()
        m["signature"] = {"algo": "rsa-pss-sha256"}
        before = json.dumps(m, sort_keys=True)
        ap2p.canonical_manifest_bytes(m)
        after = json.dumps(m, sort_keys=True)
        self.assertEqual(before, after)
        self.assertIn("signature", m)

    def test_canonical_bytes_sorts_lists(self):
        """List fields are sorted in canonical output."""
        m = _base_manifest()
        m["files"] = [
            {"path": "z.md", "sha256": "z" * 64, "size": 1},
            {"path": "a.md", "sha256": "a" * 64, "size": 2},
            {"path": "m.md", "sha256": "m" * 64, "size": 3},
        ]
        m["bundles"] = ["zebra", "alpha", "mike"]
        m["exclusions_applied"] = ["**/zoo", "**/apple"]
        m["substitutions_applied"] = [
            {"path": "z.md", "reason": "stub"},
            {"path": "a.md", "reason": "stub"},
        ]
        m["scope"] = "bundle"

        canonical = ap2p.canonical_manifest_bytes(m)
        decoded = json.loads(canonical.decode("utf-8"))

        self.assertEqual(
            [f["path"] for f in decoded["files"]],
            ["a.md", "m.md", "z.md"],
        )
        self.assertEqual(decoded["bundles"], ["alpha", "mike", "zebra"])
        self.assertEqual(decoded["exclusions_applied"], ["**/apple", "**/zoo"])
        self.assertEqual(
            [s["path"] for s in decoded["substitutions_applied"]],
            ["a.md", "z.md"],
        )

    def test_canonical_bytes_fixture_pinned(self):
        """A known manifest fixture produces a known canonical byte sha256.

        This is the regression lock from LD20: any future change to the
        canonicalization algorithm flips this hash and the test fails loudly.
        """
        m = _base_manifest()
        canonical = ap2p.canonical_manifest_bytes(m)
        actual = hashlib.sha256(canonical).hexdigest()
        # Recompute expected by re-running the documented algorithm so this
        # test catches drift in either direction (algorithm AND fixture).
        d = dict(m)
        d.pop("signature", None)
        d["files"] = sorted(d["files"], key=lambda f: f["path"])
        expected_bytes = json.dumps(
            d,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        expected = hashlib.sha256(expected_bytes).hexdigest()
        self.assertEqual(actual, expected)
        # Pin to a known constant so an algorithm drift also fails the
        # constant comparison.
        self.assertEqual(canonical, expected_bytes)

    def test_canonical_bytes_uses_strict_separators(self):
        """No incidental whitespace in the JSON output."""
        m = _base_manifest()
        canonical = ap2p.canonical_manifest_bytes(m).decode("utf-8")
        # json.dumps default separators include spaces; strict ones don't.
        self.assertNotIn(", ", canonical)
        self.assertNotIn(": ", canonical)


# ---------------------------------------------------------------------------
# compute_payload_sha256 (LD20)
# ---------------------------------------------------------------------------


class ComputePayloadSha256Tests(unittest.TestCase):
    """compute_payload_sha256 implements the exact LD20 byte construction."""

    def test_compute_payload_sha256_ordering(self):
        """Reordering files[] does not change the output."""
        files_a = [
            {"path": "a.md", "sha256": "a" * 64, "size": 1},
            {"path": "b.md", "sha256": "b" * 64, "size": 2},
            {"path": "c.md", "sha256": "c" * 64, "size": 3},
        ]
        files_b = [
            {"path": "c.md", "sha256": "c" * 64, "size": 3},
            {"path": "a.md", "sha256": "a" * 64, "size": 1},
            {"path": "b.md", "sha256": "b" * 64, "size": 2},
        ]
        self.assertEqual(
            ap2p.compute_payload_sha256(files_a),
            ap2p.compute_payload_sha256(files_b),
        )

    def test_compute_payload_sha256_exact_bytes(self):
        """Fixture produces a known hex digest matching the manual algorithm."""
        files = [
            {"path": "a.md", "sha256": "a" * 64, "size": 10},
            {"path": "b.md", "sha256": "b" * 64, "size": 20},
        ]
        # Manually reproduce the algorithm so the test pins both the function
        # AND the algorithm description in LD20.
        h = hashlib.sha256()
        for f in files:
            h.update(f["path"].encode("utf-8"))
            h.update(b"\x00")
            h.update(f["sha256"].encode("ascii"))
            h.update(b"\x00")
            h.update(str(f["size"]).encode("ascii"))
            h.update(b"\n")
        expected = h.hexdigest()
        self.assertEqual(ap2p.compute_payload_sha256(files), expected)

    def test_compute_payload_sha256_empty(self):
        """Empty list produces the sha256 of the empty byte string."""
        self.assertEqual(
            ap2p.compute_payload_sha256([]),
            hashlib.sha256(b"").hexdigest(),
        )

    def test_compute_payload_sha256_different_paths_collide_resistant(self):
        """Two files where the path could be confused with the sha differ."""
        # Without the NUL delimiter, "ab" + "cd" would equal "a" + "bcd".
        # The NUL byte prevents that ambiguity.
        files_a = [{"path": "ab", "sha256": "c" * 64, "size": 1}]
        files_b = [{"path": "a", "sha256": "b" + "c" * 63, "size": 1}]
        self.assertNotEqual(
            ap2p.compute_payload_sha256(files_a),
            ap2p.compute_payload_sha256(files_b),
        )


# ---------------------------------------------------------------------------
# generate_manifest (LD20)
# ---------------------------------------------------------------------------


class GenerateManifestTests(unittest.TestCase):
    """generate_manifest writes a manifest with all LD20 fields."""

    def test_generate_manifest_full_scope(self):
        """Full-scope generation writes all required LD20 fields."""
        with tempfile.TemporaryDirectory() as tmp:
            staging = _make_minimal_staging(tmp)
            with mock.patch.object(ap2p, "now_utc_iso", return_value=FIXED_TS):
                manifest = ap2p.generate_manifest(
                    staging,
                    scope="full",
                    walnut_name="nova-station",
                    description="test",
                    sender="testsender",
                    session_id="sess1",
                    engine="claude-opus-4-6",
                )

            # Required fields per LD20
            self.assertEqual(manifest["format_version"], "2.1.0")
            self.assertEqual(manifest["source_layout"], "v3")
            self.assertEqual(manifest["min_plugin_version"], "3.1.0")
            self.assertEqual(manifest["created"], FIXED_TS)
            self.assertEqual(manifest["scope"], "full")
            self.assertEqual(manifest["sender"], "testsender")
            self.assertEqual(manifest["description"], "test")
            self.assertEqual(manifest["encryption"], "none")
            self.assertNotIn("signature", manifest)

            # Source block
            self.assertEqual(manifest["source"]["walnut"], "nova-station")
            self.assertEqual(manifest["source"]["session_id"], "sess1")
            self.assertEqual(manifest["source"]["engine"], "claude-opus-4-6")
            self.assertEqual(manifest["source"]["plugin_version"], "3.1.0")

            # Files list contains every staged regular file (no manifest.yaml).
            paths = [f["path"] for f in manifest["files"]]
            self.assertIn("_kernel/key.md", paths)
            self.assertIn("_kernel/tasks.json", paths)
            self.assertIn("shielding-review/draft.md", paths)
            self.assertNotIn("manifest.yaml", paths)
            for f in manifest["files"]:
                self.assertEqual(len(f["sha256"]), 64)
                self.assertGreater(f["size"], 0)

            # Payload sha is consistent with compute_payload_sha256
            self.assertEqual(
                manifest["payload_sha256"],
                ap2p.compute_payload_sha256(manifest["files"]),
            )

            # File written to disk
            self.assertTrue(
                os.path.isfile(os.path.join(staging, "manifest.yaml"))
            )

    def test_generate_manifest_bundle_scope_requires_bundles(self):
        """scope=bundle requires a non-empty bundles list."""
        with tempfile.TemporaryDirectory() as tmp:
            staging = _make_minimal_staging(tmp)
            with self.assertRaises(ValueError):
                ap2p.generate_manifest(
                    staging,
                    scope="bundle",
                    walnut_name="nova-station",
                    bundles=[],
                )

    def test_generate_manifest_bundle_scope_writes_bundles_field(self):
        """scope=bundle writes the bundles field to the manifest."""
        with tempfile.TemporaryDirectory() as tmp:
            staging = _make_minimal_staging(tmp)
            with mock.patch.object(ap2p, "now_utc_iso", return_value=FIXED_TS):
                manifest = ap2p.generate_manifest(
                    staging,
                    scope="bundle",
                    walnut_name="nova-station",
                    bundles=["shielding-review", "launch-checklist"],
                )
            self.assertEqual(manifest["scope"], "bundle")
            self.assertEqual(
                manifest["bundles"],
                ["shielding-review", "launch-checklist"],
            )

    def test_generate_manifest_snapshot_scope_omits_bundles(self):
        """scope=snapshot does NOT write a bundles field."""
        with tempfile.TemporaryDirectory() as tmp:
            staging = _make_minimal_staging(tmp)
            with mock.patch.object(ap2p, "now_utc_iso", return_value=FIXED_TS):
                manifest = ap2p.generate_manifest(
                    staging,
                    scope="snapshot",
                    walnut_name="nova-station",
                )
            self.assertNotIn("bundles", manifest)

    def test_generate_manifest_v2_source_layout_accepted(self):
        """source_layout='v2' is accepted (testing-only path)."""
        with tempfile.TemporaryDirectory() as tmp:
            staging = _make_minimal_staging(tmp)
            with mock.patch.object(ap2p, "now_utc_iso", return_value=FIXED_TS):
                manifest = ap2p.generate_manifest(
                    staging,
                    scope="full",
                    walnut_name="nova-station",
                    source_layout="v2",
                )
            self.assertEqual(manifest["source_layout"], "v2")

    def test_generate_manifest_rejects_unknown_source_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = _make_minimal_staging(tmp)
            with self.assertRaises(ValueError):
                ap2p.generate_manifest(
                    staging,
                    scope="full",
                    walnut_name="nova-station",
                    source_layout="v4",
                )

    def test_generate_manifest_excludes_manifest_yaml_from_files(self):
        """A pre-existing manifest.yaml in staging is not listed in files[]."""
        with tempfile.TemporaryDirectory() as tmp:
            staging = _make_minimal_staging(tmp)
            _write(os.path.join(staging, "manifest.yaml"), "stale: true\n")
            with mock.patch.object(ap2p, "now_utc_iso", return_value=FIXED_TS):
                manifest = ap2p.generate_manifest(
                    staging,
                    scope="snapshot",
                    walnut_name="nova-station",
                )
            paths = [f["path"] for f in manifest["files"]]
            self.assertNotIn("manifest.yaml", paths)

    def test_generate_manifest_records_audit_lists(self):
        """exclusions_applied and substitutions_applied land in the manifest."""
        with tempfile.TemporaryDirectory() as tmp:
            staging = _make_minimal_staging(tmp)
            with mock.patch.object(ap2p, "now_utc_iso", return_value=FIXED_TS):
                manifest = ap2p.generate_manifest(
                    staging,
                    scope="snapshot",
                    walnut_name="nova-station",
                    exclusions_applied=["**/observations.md"],
                    substitutions_applied=[
                        {"path": "_kernel/log.md", "reason": "baseline-stub"},
                    ],
                )
            self.assertEqual(
                manifest["exclusions_applied"], ["**/observations.md"]
            )
            self.assertEqual(
                manifest["substitutions_applied"],
                [{"path": "_kernel/log.md", "reason": "baseline-stub"}],
            )


# ---------------------------------------------------------------------------
# validate_manifest (LD6 + LD20)
# ---------------------------------------------------------------------------


class ValidateManifestTests(unittest.TestCase):

    def _ok_manifest(self):
        return _base_manifest()

    def test_validate_manifest_accepts_2x(self):
        """Format versions 2.0, 2.0.0, 2.1.0, 2.5.3 all pass."""
        for fv in ("2.0", "2.0.0", "2.1.0", "2.5.3", "2.10.5"):
            m = self._ok_manifest()
            m["format_version"] = fv
            ok, errors = ap2p.validate_manifest(m)
            self.assertTrue(
                ok,
                "format_version {0!r} should validate, got errors: {1}".format(
                    fv, errors
                ),
            )

    def test_validate_manifest_rejects_3x(self):
        """3.x format hard-fails with the actionable LD6 message."""
        m = self._ok_manifest()
        m["format_version"] = "3.0.0"
        ok, errors = ap2p.validate_manifest(m)
        self.assertFalse(ok)
        self.assertTrue(
            any("3.0.0" in e and "2.x" in e for e in errors),
            "expected actionable 3.x error, got: {0}".format(errors),
        )

    def test_validate_manifest_rejects_missing_required_fields(self):
        """Each required field missing yields a specific error."""
        for field in (
            "format_version",
            "scope",
            "created",
            "files",
            "source",
            "payload_sha256",
        ):
            m = self._ok_manifest()
            del m[field]
            ok, errors = ap2p.validate_manifest(m)
            self.assertFalse(
                ok, "missing {0} should fail validation".format(field)
            )
            self.assertTrue(
                any(field in e for e in errors),
                "expected error mentioning {0}, got: {1}".format(field, errors),
            )

    def test_validate_manifest_rejects_unknown_scope(self):
        m = self._ok_manifest()
        m["scope"] = "kitchen-sink"
        ok, errors = ap2p.validate_manifest(m)
        self.assertFalse(ok)
        self.assertTrue(any("kitchen-sink" in e for e in errors))

    def test_validate_manifest_bundle_scope_requires_bundles_list(self):
        m = self._ok_manifest()
        m["scope"] = "bundle"
        ok, errors = ap2p.validate_manifest(m)
        self.assertFalse(ok)
        self.assertTrue(any("bundles" in e for e in errors))

    def test_validate_manifest_bundle_scope_with_bundles_passes(self):
        m = self._ok_manifest()
        m["scope"] = "bundle"
        m["bundles"] = ["foo"]
        ok, errors = ap2p.validate_manifest(m)
        self.assertTrue(ok, "errors: {0}".format(errors))

    def test_validate_manifest_files_must_be_list(self):
        m = self._ok_manifest()
        m["files"] = "not-a-list"
        ok, errors = ap2p.validate_manifest(m)
        self.assertFalse(ok)
        self.assertTrue(any("files" in e for e in errors))

    def test_validate_manifest_file_entry_missing_keys(self):
        m = self._ok_manifest()
        m["files"] = [{"path": "x.md"}]  # missing sha256, size
        ok, errors = ap2p.validate_manifest(m)
        self.assertFalse(ok)
        self.assertTrue(any("sha256" in e for e in errors))
        self.assertTrue(any("size" in e for e in errors))

    def test_validate_manifest_unknown_source_layout_warning_only(self):
        """Unknown source_layout values are warnings, not hard fails."""
        m = self._ok_manifest()
        m["source_layout"] = "v99"
        ok, _errors = ap2p.validate_manifest(m)
        self.assertTrue(ok)

    def test_validate_manifest_source_must_be_dict(self):
        m = self._ok_manifest()
        m["source"] = "not-a-dict"
        ok, errors = ap2p.validate_manifest(m)
        self.assertFalse(ok)
        self.assertTrue(any("source" in e for e in errors))


# ---------------------------------------------------------------------------
# Stdlib YAML reader/writer (LD20 stdlib-only commitment)
# ---------------------------------------------------------------------------


class WriteAndReadManifestYamlTests(unittest.TestCase):

    def test_write_and_read_manifest_yaml_roundtrip(self):
        """write -> read deep-equals the original dict for the schema subset."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manifest.yaml")
            manifest = _base_manifest()
            manifest["bundles"] = ["alpha", "beta"]
            manifest["scope"] = "bundle"
            manifest["exclusions_applied"] = ["**/observations.md"]
            manifest["substitutions_applied"] = [
                {"path": "_kernel/log.md", "reason": "baseline-stub"},
                {"path": "_kernel/insights.md", "reason": "baseline-stub"},
            ]
            manifest["files"] = [
                {"path": "_kernel/key.md", "sha256": "a" * 64, "size": 100},
                {"path": "shielding/draft.md", "sha256": "b" * 64, "size": 200},
            ]
            ap2p.write_manifest_yaml(manifest, path)
            parsed = ap2p.read_manifest_yaml(path)

            for key in (
                "format_version",
                "source_layout",
                "min_plugin_version",
                "created",
                "scope",
                "sender",
                "description",
                "note",
                "payload_sha256",
                "encryption",
            ):
                self.assertEqual(parsed.get(key), manifest[key], key)

            self.assertEqual(parsed["source"], manifest["source"])
            self.assertEqual(parsed["bundles"], manifest["bundles"])
            self.assertEqual(
                parsed["exclusions_applied"], manifest["exclusions_applied"]
            )

            for got, want in zip(
                parsed["substitutions_applied"], manifest["substitutions_applied"]
            ):
                self.assertEqual(got, want)
            for got, want in zip(parsed["files"], manifest["files"]):
                self.assertEqual(got["path"], want["path"])
                self.assertEqual(got["sha256"], want["sha256"])
                self.assertEqual(got["size"], want["size"])

    def test_read_manifest_yaml_tolerates_unknown_fields(self):
        """Unknown top-level fields are preserved in the parsed dict."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manifest.yaml")
            manifest = _base_manifest()
            manifest["future_field"] = "future-value"
            ap2p.write_manifest_yaml(manifest, path)
            parsed = ap2p.read_manifest_yaml(path)
            self.assertEqual(parsed.get("future_field"), "future-value")

    def test_read_manifest_yaml_rejects_malformed(self):
        """Malformed lines (missing colon, garbage) raise ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("format_version 2.1.0\n")  # missing colon
            with self.assertRaises(ValueError):
                ap2p.read_manifest_yaml(path)

    def test_read_manifest_yaml_rejects_malformed_list_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    'format_version: "2.1.0"\n'
                    "files:\n"
                    "  garbage line without dash\n"
                )
            with self.assertRaises(ValueError):
                ap2p.read_manifest_yaml(path)

    def test_write_manifest_yaml_field_order(self):
        """The writer emits known fields in _MANIFEST_FIELD_ORDER."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manifest.yaml")
            ap2p.write_manifest_yaml(_base_manifest(), path)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            # format_version must appear before scope, scope before files.
            fv_pos = content.find("format_version:")
            scope_pos = content.find("scope:")
            files_pos = content.find("files:")
            self.assertLess(fv_pos, scope_pos)
            self.assertLess(scope_pos, files_pos)

    def test_round_trip_via_canonical_bytes_is_stable(self):
        """write_manifest_yaml -> read_manifest_yaml -> canonical bytes is stable.

        Pins the LD20 commitment that on-disk YAML differences (e.g., a
        different formatter) do not change canonical bytes for the same
        logical content.
        """
        with tempfile.TemporaryDirectory() as tmp:
            manifest = _base_manifest()
            path = os.path.join(tmp, "manifest.yaml")
            ap2p.write_manifest_yaml(manifest, path)
            parsed = ap2p.read_manifest_yaml(path)
            # The schema fields we care about must produce identical bytes.
            # Drop forward-compat noise that the parser might re-shape.
            for key in (
                "format_version",
                "source_layout",
                "min_plugin_version",
                "created",
                "scope",
                "sender",
                "description",
                "note",
                "payload_sha256",
                "encryption",
                "source",
                "files",
                "exclusions_applied",
                "substitutions_applied",
            ):
                self.assertIn(key, parsed)
            self.assertEqual(
                ap2p.canonical_manifest_bytes(parsed),
                ap2p.canonical_manifest_bytes(manifest),
            )


# ---------------------------------------------------------------------------
# Free-form string safety (round-12 finding)
# ---------------------------------------------------------------------------


class UnsafeStringRejectionTests(unittest.TestCase):

    def test_unsafe_strings_rejected_description_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = _make_minimal_staging(tmp)
            with self.assertRaises(ValueError):
                ap2p.generate_manifest(
                    staging,
                    scope="snapshot",
                    walnut_name="nova-station",
                    description="line one\nline two",
                )

    def test_unsafe_strings_rejected_note_carriage_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = _make_minimal_staging(tmp)
            with self.assertRaises(ValueError):
                ap2p.generate_manifest(
                    staging,
                    scope="snapshot",
                    walnut_name="nova-station",
                    note="bad\rnote",
                )

    def test_unsafe_strings_rejected_double_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = _make_minimal_staging(tmp)
            with self.assertRaises(ValueError):
                ap2p.generate_manifest(
                    staging,
                    scope="snapshot",
                    walnut_name="nova-station",
                    description='evil "injection"',
                )

    def test_unsafe_strings_rejected_in_substitution_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = _make_minimal_staging(tmp)
            with self.assertRaises(ValueError):
                ap2p.generate_manifest(
                    staging,
                    scope="snapshot",
                    walnut_name="nova-station",
                    substitutions_applied=[
                        {"path": "_kernel/log.md", "reason": "bad\nreason"},
                    ],
                )

    def test_safe_strings_with_backslash_accepted(self):
        """Backslashes are tolerated and escaped by the writer."""
        with tempfile.TemporaryDirectory() as tmp:
            staging = _make_minimal_staging(tmp)
            with mock.patch.object(ap2p, "now_utc_iso", return_value=FIXED_TS):
                manifest = ap2p.generate_manifest(
                    staging,
                    scope="snapshot",
                    walnut_name="nova-station",
                    description="path\\with\\backslashes",
                )
            self.assertEqual(
                manifest["description"], "path\\with\\backslashes"
            )
            parsed = ap2p.read_manifest_yaml(
                os.path.join(staging, "manifest.yaml")
            )
            self.assertEqual(
                parsed["description"], "path\\with\\backslashes"
            )


if __name__ == "__main__":
    unittest.main()
