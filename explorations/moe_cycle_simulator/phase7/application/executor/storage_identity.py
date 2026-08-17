#!/usr/bin/env python3
"""Derive a stable, machine-readable identity for a Linux mount point."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from explorations.moe_cycle_simulator.phase7.application.executor.common import M0Error


def _unescape(value: str) -> str:
    for encoded, decoded in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(encoded, decoded)
    return value


def mount_identity(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    if path.is_symlink() or not path.exists() or os.path.realpath(path_text) != path_text:
        raise M0Error(f"mount target must be an existing real path: {path_text}")
    raw = Path("/proc/self/mountinfo").read_bytes()
    selected: dict[str, Any] | None = None
    for line in raw.decode("utf-8", errors="strict").splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            raise M0Error("malformed /proc/self/mountinfo row")
        fields = left.split()
        tail = right.split()
        if len(fields) < 6 or len(tail) < 3:
            raise M0Error("truncated /proc/self/mountinfo row")
        mount_point = _unescape(fields[4])
        if mount_point != path_text:
            continue
        selected = {
            "mount_id": int(fields[0]),
            "parent_id": int(fields[1]),
            "major_minor": fields[2],
            "root": _unescape(fields[3]),
            "mount_point": mount_point,
            "mount_options": sorted(fields[5].split(",")),
            "filesystem_type": tail[0],
            "mount_source": _unescape(tail[1]),
            "super_options": sorted(tail[2].split(",")),
            "device_id": path.stat().st_dev,
            "boot_id": Path("/proc/sys/kernel/random/boot_id")
            .read_text(encoding="utf-8")
            .strip(),
            "mountinfo_sha256": hashlib.sha256(raw).hexdigest(),
        }
        break
    if selected is None:
        raise M0Error(f"path is not an explicit mount point: {path_text}")
    identity_fields = {
        key: value for key, value in selected.items() if key != "mountinfo_sha256"
    }
    canonical = json.dumps(
        identity_fields,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    selected["mount_identity_sha256"] = hashlib.sha256(canonical).hexdigest()
    return selected


def validate_mount_identity(path_text: str, expected_sha256: str) -> dict[str, Any]:
    observed = mount_identity(path_text)
    if observed["mount_identity_sha256"] != expected_sha256:
        raise M0Error(f"mount identity changed: {path_text}")
    return observed
