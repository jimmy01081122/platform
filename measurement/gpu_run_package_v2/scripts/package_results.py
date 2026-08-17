#!/usr/bin/env python3
"""Audit and package one verified v2 trace session."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from collectors.trace_contract import sha256_file  # noqa: E402
from trace_package_verify import (  # noqa: E402
    APPROVED_INCOMPLETE,
    COMPLETE,
    package_root,
    verify_root,
)


def refresh_checksums(root: Path) -> None:
    paths = [
        path for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.name not in ("checksums.sha256", "TRACE_COMPLETENESS_REPORT.json")
    ]
    (root / "checksums.sha256").write_text(
        "".join(
            f"{sha256_file(path)}  {path.relative_to(root).as_posix()}\n"
            for path in sorted(paths)
        ),
        encoding="utf-8",
    )


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_sidecar(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.session_root.resolve()
    if not root.is_dir() or root.is_symlink():
        parser.error("--session-root must be a real directory")

    audit = subprocess.run([
        sys.executable,
        str(PACKAGE_ROOT / "scripts/trace_audit.py"),
        "--session-root",
        str(root),
    ])
    if audit.returncode not in (COMPLETE, APPROVED_INCOMPLETE):
        raise SystemExit(
            f"trace audit failed with exit {audit.returncode}; refusing to package"
        )
    verify_code, verify_report = verify_root(root)
    if verify_code not in (COMPLETE, APPROVED_INCOMPLETE):
        raise SystemExit(
            f"root verification failed with exit {verify_code}; refusing to package"
        )

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive = (
        args.output.resolve()
        if args.output
        else root.parent / f"{root.name}-{stamp}.tar.gz"
    )
    if root == archive or root in archive.parents:
        parser.error("--output must be outside --session-root")
    sidecar = archive.with_suffix(archive.suffix + ".sha256")
    if archive.exists() or sidecar.exists():
        parser.error("refusing to overwrite existing archive or SHA-256 sidecar")
    session_manifest = json.loads(
        (root / "SESSION_MANIFEST.json").read_text(encoding="utf-8")
    )
    release_class = session_manifest["release_class"]
    manifest = {
        "schema_version": "gpu-result-archive-v2",
        "release_class": release_class,
        "release_eligible": (
            release_class == "formal_release"
            and verify_code == COMPLETE
            and verify_report.get("release_eligible") is True
        ),
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_root_name": root.name,
        "source_verification_exit_code": verify_code,
        "source_verification_status": (
            "complete" if verify_code == COMPLETE else "approved_incomplete"
        ),
        "measurement_claim_inferred_by_packager": False,
        "file_coverage": {
            "excluded": [
                "RESULT_PACKAGE_MANIFEST.json",
                "checksums.sha256",
                "TRACE_COMPLETENESS_REPORT.json",
            ],
            "file_count": 0,
            "files": [],
        },
    }
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.is_symlink():
            raise SystemExit(f"unsafe symlink in session: {path}")
        if path.name in (
            "RESULT_PACKAGE_MANIFEST.json",
            "checksums.sha256",
            "TRACE_COMPLETENESS_REPORT.json",
        ):
            continue
        manifest["file_coverage"]["files"].append({
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        })
    manifest["file_coverage"]["file_count"] = len(
        manifest["file_coverage"]["files"]
    )
    result_manifest = root / "RESULT_PACKAGE_MANIFEST.json"
    result_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    refresh_checksums(root)
    final_root_code, _ = verify_root(root)
    if final_root_code not in (COMPLETE, APPROVED_INCOMPLETE):
        raise SystemExit(
            f"root failed after manifest/checksum finalization: {final_root_code}"
        )
    archive.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive.name}.", suffix=".tmp", dir=archive.parent
    )
    os.close(descriptor)
    temporary_archive = Path(temporary_name)
    published = False
    try:
        with tarfile.open(temporary_archive, "w:gz") as tar:
            tar.add(root, arcname=root.name, recursive=True)
        with temporary_archive.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary_archive, archive)
        published = True
        fsync_directory(archive.parent)

        checksum = sha256_file(archive)
        atomic_write_sidecar(sidecar, f"{checksum}  {archive.name}\n")
        with package_root(archive) as extracted:
            archive_code, _ = verify_root(extracted)
        if archive_code not in (COMPLETE, APPROVED_INCOMPLETE):
            raise SystemExit(
                f"post-package verification failed with exit {archive_code}"
            )
    except BaseException:
        temporary_archive.unlink(missing_ok=True)
        if published:
            archive.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            fsync_directory(archive.parent)
        raise
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
