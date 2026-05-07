"""Phase-5 no-op upgrade record writer.

Per originally landed a self-contained writer so
the no-op record path was implementable BEFORE T2 shipped
``_record_codec.py``. T2 now lands the codec, so this module
delegates to ``_record_codec.write_atomic`` -- one-line import; same
on-disk behavior (JSON-text-as-YAML, valid YAML 1.2 since YAML is a
JSON superset). Net behavior unchanged; codec consolidation completed.
"""

from __future__ import annotations

from typing import Any, Mapping

from system_upgrade._record_codec import write_atomic as write_noop_record


__all__ = ("write_noop_record",)
