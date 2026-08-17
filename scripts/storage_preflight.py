#!/usr/bin/env python3
"""Safe, temporary storage preflight for WSL Linux and a mounted Windows drive.

The benchmark never touches existing files below either target.  It creates one
uniquely named directory per target and removes it in a ``finally`` block.
Results should be treated as a preflight, not as a substitute for a sustained
benchmark with a representative dataset.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import re
import shlex
import shutil
import socket
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

MIB = 1024 * 1024
KIB = 1024
SEED = 20260718
DECISION_LABELS = {
    "read_directly": "read directly from E",
    "copy_active_subset": "copy active subset to WSL",
    "stream_and_cache": "stream and cache",
}


@dataclass(frozen=True)
class BenchmarkConfig:
    sequential_mib: int
    block_kib: int
    random_ops: int
    random_block_kib: int
    metadata_files: int
    parser_rows: int
    seed: int = SEED


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def rate(count: float, elapsed_s: float) -> float:
    return count / max(elapsed_s, 1e-12)


def timed(operation: Callable[[], object]) -> tuple[object, float]:
    started = time.perf_counter()
    result = operation()
    return result, time.perf_counter() - started


def sync_file(handle: object) -> None:
    handle.flush()  # type: ignore[attr-defined]
    os.fsync(handle.fileno())  # type: ignore[attr-defined]


def decode_mount_field(value: str) -> str:
    """Decode the octal escapes used by /proc/self/mountinfo."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def mount_info(path: Path) -> dict[str, object]:
    """Return the longest matching Linux mount entry plus statvfs capacity."""
    resolved = path.resolve()
    best: dict[str, str] | None = None
    try:
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            separator = fields.index("-")
            mount_point = decode_mount_field(fields[4])
            candidate = Path(mount_point)
            try:
                resolved.relative_to(candidate)
            except ValueError:
                continue
            if best is None or len(mount_point) > len(best["mount_point"]):
                best = {
                    "mount_point": mount_point,
                    "mount_options": fields[5],
                    "filesystem_type": fields[separator + 1],
                    "source": decode_mount_field(fields[separator + 2]),
                    "super_options": fields[separator + 3],
                }
    except (OSError, ValueError):
        best = None

    stat = os.statvfs(resolved)
    info: dict[str, object] = {
        "path": str(path),
        "resolved_path": str(resolved),
        "total_bytes": stat.f_blocks * stat.f_frsize,
        "available_bytes": stat.f_bavail * stat.f_frsize,
    }
    if best:
        info.update(best)
    return info


def benchmark_sequential(workdir: Path, config: BenchmarkConfig) -> dict[str, float]:
    path = workdir / "sequential.bin"
    total_bytes = config.sequential_mib * MIB
    block_size = min(config.block_kib * KIB, total_bytes)
    block = random.Random(config.seed).randbytes(block_size)

    def write_file() -> None:
        remaining = total_bytes
        with path.open("wb", buffering=0) as handle:
            while remaining:
                chunk = block[: min(block_size, remaining)]
                handle.write(chunk)
                remaining -= len(chunk)
            sync_file(handle)

    _, write_s = timed(write_file)

    def read_file() -> int:
        consumed = 0
        with path.open("rb", buffering=0) as handle:
            while data := handle.read(block_size):
                consumed += len(data)
        return consumed

    consumed, read_s = timed(read_file)
    if consumed != total_bytes:
        raise RuntimeError(f"short sequential read: expected {total_bytes}, got {consumed}")
    return {
        "bytes": total_bytes,
        "write_seconds": write_s,
        "write_mib_s": rate(total_bytes / MIB, write_s),
        "read_seconds": read_s,
        "read_mib_s": rate(total_bytes / MIB, read_s),
    }


def benchmark_random_io(workdir: Path, config: BenchmarkConfig) -> dict[str, float]:
    path = workdir / "random.bin"
    file_size = config.sequential_mib * MIB
    block_size = config.random_block_kib * KIB
    slots = file_size // block_size
    if slots < 2:
        raise ValueError("sequential size must hold at least two random-I/O blocks")
    rng = random.Random(config.seed)
    offsets = [rng.randrange(slots) * block_size for _ in range(config.random_ops)]
    block = rng.randbytes(block_size)

    with path.open("wb", buffering=0) as handle:
        handle.truncate(file_size)
        sync_file(handle)

    def random_write() -> None:
        with path.open("r+b", buffering=0) as handle:
            for offset in offsets:
                handle.seek(offset)
                handle.write(block)
            sync_file(handle)

    _, write_s = timed(random_write)

    def random_read() -> int:
        consumed = 0
        with path.open("rb", buffering=0) as handle:
            for offset in reversed(offsets):
                handle.seek(offset)
                consumed += len(handle.read(block_size))
        return consumed

    consumed, read_s = timed(random_read)
    expected = config.random_ops * block_size
    if consumed != expected:
        raise RuntimeError(f"short random read: expected {expected}, got {consumed}")
    return {
        "operations": config.random_ops,
        "block_bytes": block_size,
        "write_seconds": write_s,
        "write_iops": rate(config.random_ops, write_s),
        "read_seconds": read_s,
        "read_iops": rate(config.random_ops, read_s),
    }


def benchmark_metadata(workdir: Path, config: BenchmarkConfig) -> dict[str, float]:
    root = workdir / "metadata"
    root.mkdir()
    paths = [root / f"small_{index:06d}.txt" for index in range(config.metadata_files)]

    def create_files() -> None:
        for index, path in enumerate(paths):
            path.write_text(f"{index}\n", encoding="ascii")

    _, create_s = timed(create_files)

    def stat_files() -> int:
        return sum(path.stat().st_size for path in paths)

    total_size, stat_s = timed(stat_files)

    def list_files() -> int:
        return sum(1 for _ in root.iterdir())

    listed, list_s = timed(list_files)
    if listed != config.metadata_files:
        raise RuntimeError(f"metadata listing found {listed} files")

    def delete_files() -> None:
        for path in paths:
            path.unlink()

    _, delete_s = timed(delete_files)
    root.rmdir()
    return {
        "files": config.metadata_files,
        "payload_bytes": total_size,
        "create_seconds": create_s,
        "create_ops_s": rate(config.metadata_files, create_s),
        "stat_seconds": stat_s,
        "stat_ops_s": rate(config.metadata_files, stat_s),
        "list_seconds": list_s,
        "list_ops_s": rate(config.metadata_files, list_s),
        "delete_seconds": delete_s,
        "delete_ops_s": rate(config.metadata_files, delete_s),
    }


def benchmark_parser(workdir: Path, config: BenchmarkConfig) -> dict[str, float]:
    path = workdir / "dataset.jsonl"
    rng = random.Random(config.seed)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in range(config.parser_rows):
            record = {
                "query_id": row,
                "layer": row % 32,
                "selected_experts": [rng.randrange(64), rng.randrange(64)],
                "score": rng.random(),
                "text": f"synthetic-storage-preflight-{row % 101}",
            }
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        sync_file(handle)
    input_bytes = path.stat().st_size

    def parse() -> tuple[int, int]:
        rows = 0
        checksum = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                checksum += int(record["query_id"]) + sum(record["selected_experts"])
                rows += 1
        return rows, checksum

    (rows, checksum), parse_s = timed(parse)
    if rows != config.parser_rows:
        raise RuntimeError(f"parser read {rows} rows, expected {config.parser_rows}")
    return {
        "rows": rows,
        "bytes": input_bytes,
        "checksum": checksum,
        "parse_seconds": parse_s,
        "records_s": rate(rows, parse_s),
        "mib_s": rate(input_bytes / MIB, parse_s),
    }


def benchmark_target(label: str, root: Path, config: BenchmarkConfig) -> dict[str, object]:
    if not root.is_dir():
        raise FileNotFoundError(f"{label} root does not exist or is not a directory: {root}")
    if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        raise PermissionError(f"{label} root is not accessible for temporary benchmark: {root}")

    started_at = utc_now()
    started = time.perf_counter()
    temp_path = Path(tempfile.mkdtemp(prefix=".storage_preflight_", dir=root))
    cleaned = False
    try:
        metrics = {
            "sequential": benchmark_sequential(temp_path, config),
            "metadata": benchmark_metadata(temp_path, config),
            "random_io": benchmark_random_io(temp_path, config),
            "dataset_parser": benchmark_parser(temp_path, config),
        }
    finally:
        shutil.rmtree(temp_path, ignore_errors=False)
        cleaned = not temp_path.exists()
    return {
        "label": label,
        "root": str(root),
        "temporary_path": str(temp_path),
        "temporary_path_cleaned": cleaned,
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_seconds": time.perf_counter() - started,
        "filesystem": mount_info(root),
        "metrics": metrics,
    }


def metric_ratios(linux: dict[str, object], external: dict[str, object]) -> dict[str, float]:
    linux_metrics = linux["metrics"]  # type: ignore[index]
    external_metrics = external["metrics"]  # type: ignore[index]
    paths = {
        "sequential_write": ("sequential", "write_mib_s"),
        "sequential_read": ("sequential", "read_mib_s"),
        "metadata_create": ("metadata", "create_ops_s"),
        "metadata_stat": ("metadata", "stat_ops_s"),
        "random_write": ("random_io", "write_iops"),
        "random_read": ("random_io", "read_iops"),
        "dataset_parser": ("dataset_parser", "records_s"),
    }
    ratios = {}
    for name, (section, metric) in paths.items():
        baseline = float(linux_metrics[section][metric])  # type: ignore[index]
        measured = float(external_metrics[section][metric])  # type: ignore[index]
        ratios[name] = measured / baseline if baseline else 0.0
    return ratios


def decide(ratios: dict[str, float]) -> dict[str, object]:
    sequential_ok = min(ratios["sequential_read"], ratios["dataset_parser"])
    latency_ok = min(
        ratios["metadata_create"],
        ratios["metadata_stat"],
        ratios["random_read"],
        ratios["random_write"],
    )
    if sequential_ok >= 0.75 and latency_ok >= 0.50:
        key = "read_directly"
        rationale = "E throughput is close enough for both streaming and latency-sensitive access."
    elif sequential_ok >= 0.50:
        key = "stream_and_cache"
        rationale = "E streaming is usable, but caching avoids its weaker latency-sensitive path."
    else:
        key = "copy_active_subset"
        rationale = "E measured below half of WSL for a sequential/parser gate."
    return {
        "key": key,
        "label": DECISION_LABELS[key],
        "rationale": rationale,
        "thresholds": {
            "direct_min_sequential_parser_ratio": 0.75,
            "direct_min_latency_ratio": 0.50,
            "stream_cache_min_sequential_parser_ratio": 0.50,
        },
    }


def markdown_report(report: dict[str, object]) -> str:
    targets = report["targets"]  # type: ignore[assignment]
    linux = targets["wsl_linux"]  # type: ignore[index]
    external = targets["mnt_e"]  # type: ignore[index]
    ratios = report["comparison"]["mnt_e_over_wsl"]  # type: ignore[index]
    decision = report["decision"]  # type: ignore[assignment]

    def value(target: dict[str, object], section: str, metric: str) -> float:
        return float(target["metrics"][section][metric])  # type: ignore[index]

    rows = [
        ("Sequential write", "MiB/s", "sequential", "write_mib_s", "sequential_write"),
        ("Sequential read", "MiB/s", "sequential", "read_mib_s", "sequential_read"),
        ("Metadata create", "ops/s", "metadata", "create_ops_s", "metadata_create"),
        ("Metadata stat", "ops/s", "metadata", "stat_ops_s", "metadata_stat"),
        ("Random write", "IOPS", "random_io", "write_iops", "random_write"),
        ("Random read", "IOPS", "random_io", "read_iops", "random_read"),
        ("Dataset parser", "records/s", "dataset_parser", "records_s", "dataset_parser"),
    ]
    lines = [
        "# Storage preflight",
        "",
        f"- Decision: **{decision['label']}**",
        f"- Rationale: {decision['rationale']}",
        f"- Started: `{report['started_at']}`",
        f"- Finished: `{report['finished_at']}`",
        f"- Hostname: `{report['host']['hostname']}`",
        f"- Command: `{report['command']}`",
        "",
        "| Metric | Unit | WSL Linux | /mnt/e | E/WSL |",
        "|---|---:|---:|---:|---:|",
    ]
    for label, unit, section, metric, ratio_name in rows:
        lines.append(
            f"| {label} | {unit} | {value(linux, section, metric):.2f} | "
            f"{value(external, section, metric):.2f} | {float(ratios[ratio_name]):.3f} |"
        )
    lines.extend(
        [
            "",
            "## Filesystems",
            "",
            f"- WSL Linux: `{json.dumps(linux['filesystem'], sort_keys=True)}`",
            f"- /mnt/e: `{json.dumps(external['filesystem'], sort_keys=True)}`",
            "",
            "## Safety and interpretation",
            "",
            "- Existing files were neither opened nor modified; only unique temporary directories were used.",
            f"- Temporary cleanup succeeded: WSL={linux['temporary_path_cleaned']}, "
            f"/mnt/e={external['temporary_path_cleaned']}.",
            "- This is a small buffered-I/O preflight. OS page cache, antivirus, mount options, and "
            "background load can affect results; repeat with a representative size before a large placement decision.",
            "- The tool does not migrate, export, unregister, or re-import any WSL distribution.",
            "",
        ]
    )
    return "\n".join(lines)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux-root", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--e-root", type=Path, default=Path("/mnt/e"))
    parser.add_argument("--output-dir", type=Path, default=Path("results/storage_preflight"))
    parser.add_argument("--sequential-mib", type=positive_int, default=32)
    parser.add_argument("--block-kib", type=positive_int, default=1024)
    parser.add_argument("--random-ops", type=positive_int, default=1000)
    parser.add_argument("--random-block-kib", type=positive_int, default=4)
    parser.add_argument("--metadata-files", type=positive_int, default=500)
    parser.add_argument("--parser-rows", type=positive_int, default=20_000)
    parser.add_argument("--tag", default=None, help="Output filename tag (default: UTC timestamp)")
    return parser


def run(args: argparse.Namespace) -> tuple[dict[str, object], Path, Path]:
    config = BenchmarkConfig(
        sequential_mib=args.sequential_mib,
        block_kib=args.block_kib,
        random_ops=args.random_ops,
        random_block_kib=args.random_block_kib,
        metadata_files=args.metadata_files,
        parser_rows=args.parser_rows,
    )
    if config.random_block_kib * KIB * 2 > config.sequential_mib * MIB:
        raise ValueError("sequential-mib is too small for random-block-kib")

    started_at = utc_now()
    overall = time.perf_counter()
    linux = benchmark_target("wsl_linux", args.linux_root, config)
    external = benchmark_target("mnt_e", args.e_root, config)
    ratios = metric_ratios(linux, external)
    report: dict[str, object] = {
        "schema_version": 1,
        "benchmark": "storage_preflight",
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_seconds": time.perf_counter() - overall,
        "command": shlex.join([sys.executable, *sys.argv]),
        "working_directory": str(Path.cwd()),
        "host": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "processor": platform.processor(),
        },
        "config": asdict(config),
        "targets": {"wsl_linux": linux, "mnt_e": external},
        "comparison": {"mnt_e_over_wsl": ratios},
        "decision": decide(ratios),
        "warnings": [
            "Small buffered-I/O measurements may be influenced by the OS page cache.",
            "Repeat with representative sizes and workload state before moving a large active dataset.",
        ],
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    tag = args.tag or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = args.output_dir / f"storage_preflight_{tag}.json"
    markdown_path = args.output_dir / f"storage_preflight_{tag}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    return report, json_path, markdown_path


def main() -> int:
    args = build_parser().parse_args()
    try:
        report, json_path, markdown_path = run(args)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"storage preflight failed: {error}", file=sys.stderr)
        return 2
    print(f"decision: {report['decision']['label']}")
    print(f"json: {json_path}")
    print(f"markdown: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
