"""Tracked content-addressed archive for immutable G3-R3/R4 failure evidence."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_PATH = PACKAGE_ROOT / "historical_evidence/g3_failures_v1/archive.json"
EXPECTED_RECORDS = {
    "r3_session_sha256": (
        "granite-c1a-formal-g3-r3-20260718",
        "session.json",
        2785,
        "ce1220cf78655a24123f116dfd6414fee7d176c917798f9b5bb83eb9a90a9c80",
    ),
    "r4_session_sha256": (
        "granite-c1a-formal-g3-r4-20260719",
        "session.json",
        3469,
        "767887e9d90a4980deeb56dfd005014a1981e72a5e377d41deb2a75edf890dff",
    ),
    "r4_suite_snapshot_sha256": (
        "granite-c1a-formal-g3-r4-20260719",
        "suite_snapshot.json",
        23246,
        "a2493710880581946f290b7aab97ff57aaffd7c5bf3782dd430247fea128a705",
    ),
    "r4_journal_sha256": (
        "granite-c1a-formal-g3-r4-20260719",
        "journal.jsonl",
        10877,
        "3947a13a741c91aa884fa3cfeedab3ddb01c9df48a65b7eaca9df8cb4b2d917c",
    ),
    "r4_failed_state_sha256": (
        "granite-c1a-formal-g3-r4-20260719",
        "state/74a319779c4db268ee72a05c493efa4df5c74de4912f788970a2e1c287cbc2b5.json",
        416,
        "026e0d13ee55873595b2891f966c101fd935fabd0b20435b9fcfbbf935e6bdbd",
    ),
    "r4_failure_quality_sha256": (
        "granite-c1a-formal-g3-r4-20260719",
        ".tmp/74a319779c4db268ee72a05c493efa4df5c74de4912f788970a2e1c287cbc2b5/"
        "failure_quality_results.jsonl",
        1597,
        "4f97e5cbf4719929056b31567149bb46e6445eee5245167c17332aea878765dd",
    ),
}


class HistoricalEvidenceError(RuntimeError):
    pass


def _load_archive(path: Path = ARCHIVE_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HistoricalEvidenceError("historical evidence archive is unreadable") from error
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema_version", "evidence_role", "immutability",
            "records", "claim_boundary",
        }
        or value.get("schema_version") != "g25-historical-evidence-archive-v1"
        or value.get("evidence_role") != "immutable_failure_evidence"
        or value.get("immutability") != "never_resume_retry_modify_or_supplement"
        or value.get("claim_boundary")
        != "presence_and_byte_identity_only_not_formal_pass"
    ):
        raise HistoricalEvidenceError("historical evidence archive identity differs")
    records = value.get("records")
    if not isinstance(records, list) or len(records) != len(EXPECTED_RECORDS):
        raise HistoricalEvidenceError("historical evidence record count differs")
    return value


def verify_historical_evidence_archive(
    path: Path = ARCHIVE_PATH,
) -> dict[str, str]:
    archive = _load_archive(path)
    observed: dict[str, str] = {}
    seen_ids: set[str] = set()
    for row in archive["records"]:
        if not isinstance(row, dict) or set(row) != {
            "binding_id", "session_id", "original_relative_path",
            "content_encoding", "original_bytes", "original_sha256",
            "gzip_base64",
        }:
            raise HistoricalEvidenceError("historical evidence record shape differs")
        binding_id = row.get("binding_id")
        if binding_id not in EXPECTED_RECORDS or binding_id in seen_ids:
            raise HistoricalEvidenceError("historical evidence binding set differs")
        seen_ids.add(binding_id)
        session_id, relative, expected_bytes, expected_sha256 = EXPECTED_RECORDS[
            binding_id
        ]
        if (
            row.get("session_id") != session_id
            or row.get("original_relative_path") != relative
            or row.get("content_encoding") != "gzip_base64"
            or row.get("original_bytes") != expected_bytes
            or row.get("original_sha256") != expected_sha256
            or not isinstance(row.get("gzip_base64"), str)
        ):
            raise HistoricalEvidenceError(
                f"historical evidence metadata differs: {binding_id}"
            )
        try:
            compressed = base64.b64decode(row["gzip_base64"], validate=True)
            payload = gzip.decompress(compressed)
        except (ValueError, OSError) as error:
            raise HistoricalEvidenceError(
                f"historical evidence blob cannot be decoded: {binding_id}"
            ) from error
        if (
            len(payload) != expected_bytes
            or hashlib.sha256(payload).hexdigest() != expected_sha256
        ):
            raise HistoricalEvidenceError(
                f"historical evidence payload identity differs: {binding_id}"
            )
        observed[binding_id] = expected_sha256
    if seen_ids != set(EXPECTED_RECORDS):
        raise HistoricalEvidenceError("historical evidence binding set is incomplete")
    return observed
