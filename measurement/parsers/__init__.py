"""Parsers and validators for GPU-prep measurement outputs.

Design rule (TRACK_GPU_PREP §5 step 6): a parser handed a mis-shaped input MUST
fail loudly. It must never emit a warning to stderr and continue with exit 0 --
that is the exact failure mode that silently dropped Kimi's ``professional_law``
in ``hf_sample_download.py`` (EXTERNAL_CORPUS_AUDIT_20260818.md). Here every
structural violation raises ``ParseError`` / ``ValidationError``.
"""

from __future__ import annotations

from measurement.parsers.common import ParseError, ValidationError

__all__ = ["ParseError", "ValidationError"]
