"""Low-interference concurrent telemetry for C1 P5_BASIC."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

GPU_QUERIES = {
    "gpu_utilization_percent": "utilization.gpu",
    "gpu_clock_mhz": "clocks.current.graphics",
    "memory_clock_mhz": "clocks.current.memory",
    "power_watts": "power.draw",
    "temperature_celsius": "temperature.gpu",
    "vram_used_bytes": "memory.used",
    "throttle_reason": "clocks_throttle_reasons.active",
}
NUMERIC_FIELDS = set(GPU_QUERIES) - {"throttle_reason"}


class TelemetryUnavailable(RuntimeError):
    pass


class TelemetrySampler:
    def __init__(
        self,
        output_path: Path,
        *,
        interval_ms: int = 100,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        proc_root: Path = Path("/proc"),
    ) -> None:
        if interval_ms <= 0:
            raise ValueError("interval_ms must be positive")
        self.output_path = output_path
        self.interval_ms = interval_ms
        self.command_runner = command_runner
        self.proc_root = proc_root
        self.supported_gpu: dict[str, str] = {}
        self.unavailable: dict[str, str] = {}
        self.failures: list[str] = []
        self.samples: list[dict[str, Any]] = []
        self.started_ns: int | None = None
        self.ended_ns: int | None = None
        self._previous_cpu: tuple[int, int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run_smi(self, query: str) -> subprocess.CompletedProcess[str]:
        return self.command_runner(
            [
                "nvidia-smi", f"--query-gpu={query}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=5, check=False,
        )

    def probe(self) -> None:
        if shutil.which("nvidia-smi") is None:
            reason = "nvidia-smi is not installed or not on PATH"
            self.unavailable.update({field: reason for field in GPU_QUERIES})
        else:
            for field, query in GPU_QUERIES.items():
                try:
                    result = self._run_smi(query)
                except (OSError, subprocess.SubprocessError) as exc:
                    self.unavailable[field] = f"{type(exc).__name__}: {exc}"
                    continue
                if result.returncode == 0 and result.stdout.strip():
                    self.supported_gpu[field] = query
                else:
                    detail = result.stderr.strip() or "query returned no value"
                    self.unavailable[field] = f"nvidia-smi {query} unavailable: {detail}"
        for field, filename in (
            ("cpu_utilization_percent", "stat"),
            ("system_memory_used_bytes", "meminfo"),
        ):
            if not (self.proc_root / filename).is_file():
                self.unavailable[field] = f"{self.proc_root / filename} is unavailable"
        if not self.supported_gpu and all(
            field in self.unavailable
            for field in ("cpu_utilization_percent", "system_memory_used_bytes")
        ):
            raise TelemetryUnavailable("neither nvidia-smi nor /proc telemetry is available")

    @staticmethod
    def _parse_number(value: str) -> float | None:
        if value.strip().lower() in {"n/a", "[not supported]", "not supported", ""}:
            return None
        try:
            return float(value.strip())
        except ValueError:
            return None

    def _read_proc(self, row: dict[str, Any], reasons: dict[str, str]) -> None:
        stat_path = self.proc_root / "stat"
        if stat_path.is_file():
            raw = stat_path.read_text(encoding="utf-8")
            row["proc_stat_raw"] = raw
            first = raw.splitlines()[0].split()
            values = [int(item) for item in first[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values)
            if self._previous_cpu is None or total <= self._previous_cpu[0]:
                reasons["cpu_utilization_percent"] = "first sample has no CPU delta"
            else:
                total_delta = total - self._previous_cpu[0]
                idle_delta = idle - self._previous_cpu[1]
                row["cpu_utilization_percent"] = 100.0 * (
                    1.0 - idle_delta / total_delta
                )
            self._previous_cpu = (total, idle)
        meminfo_path = self.proc_root / "meminfo"
        if meminfo_path.is_file():
            raw = meminfo_path.read_text(encoding="utf-8")
            row["proc_meminfo_raw"] = raw
            values = {}
            for line in raw.splitlines():
                key, _, rest = line.partition(":")
                token = rest.strip().split()
                if token and token[0].isdigit():
                    values[key] = int(token[0]) * 1024
            if "MemTotal" in values and "MemAvailable" in values:
                row["system_memory_used_bytes"] = (
                    values["MemTotal"] - values["MemAvailable"]
                )
            else:
                reasons["system_memory_used_bytes"] = (
                    "/proc/meminfo lacks MemTotal or MemAvailable"
                )
        self_stat = self.proc_root / "self/stat"
        if self_stat.is_file():
            raw = self_stat.read_text(encoding="utf-8")
            row["proc_self_stat_raw"] = raw
            fields = raw.split()
            if len(fields) > 14:
                row["process_cpu_ticks"] = int(fields[13]) + int(fields[14])
        self_status = self.proc_root / "self/status"
        if self_status.is_file():
            raw = self_status.read_text(encoding="utf-8")
            row["proc_self_status_raw"] = raw
            for line in raw.splitlines():
                if line.startswith("VmRSS:"):
                    row["process_rss_bytes"] = int(line.split()[1]) * 1024
                    break

    def _sample_once(self) -> None:
        row: dict[str, Any] = {
            "sample_index": len(self.samples),
            "monotonic_ns": time.monotonic_ns(),
            "wall_time_ns": time.time_ns(),
        }
        reasons = dict(self.unavailable)
        if self.supported_gpu:
            queries = list(self.supported_gpu.values())
            try:
                result = self._run_smi(",".join(queries))
                row["nvidia_smi_raw"] = result.stdout
                if result.returncode != 0 or not result.stdout.strip():
                    raise RuntimeError(result.stderr.strip() or "empty nvidia-smi response")
                values = [item.strip() for item in result.stdout.splitlines()[0].split(",")]
                for (field, _query), raw in zip(self.supported_gpu.items(), values):
                    if field == "throttle_reason":
                        if raw and raw.lower() != "n/a":
                            row[field] = raw
                        else:
                            reasons[field] = "nvidia-smi reported N/A"
                    else:
                        number = self._parse_number(raw)
                        if number is None:
                            reasons[field] = "nvidia-smi reported a non-numeric value"
                        else:
                            row[field] = (
                                int(number * 1024 * 1024)
                                if field == "vram_used_bytes" else number
                            )
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                detail = f"nvidia-smi sampling failed: {type(exc).__name__}: {exc}"
                self.failures.append(detail)
                reasons.update({
                    field: detail for field in self.supported_gpu
                    if field not in row
                })
        try:
            self._read_proc(row, reasons)
        except (OSError, ValueError, IndexError) as exc:
            detail = f"/proc sampling failed: {type(exc).__name__}: {exc}"
            self.failures.append(detail)
            for field in ("cpu_utilization_percent", "system_memory_used_bytes"):
                if field not in row:
                    reasons[field] = detail
        row["unavailable_reasons"] = reasons
        self.samples.append(row)
        with self.output_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")

    def start(self) -> None:
        self.probe()
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text("", encoding="utf-8")
        self.started_ns = time.monotonic_ns()
        self._sample_once()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_ms / 1000.0):
            self._sample_once()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.ended_ns = time.monotonic_ns()
        if not self.samples:
            raise TelemetryUnavailable("telemetry backend produced no raw samples")
        observed: dict[str, Any] = {}
        unavailable: dict[str, str] = {}
        for field in (*GPU_QUERIES, "cpu_utilization_percent", "system_memory_used_bytes"):
            values = [row[field] for row in self.samples if row.get(field) is not None]
            if values:
                observed[field] = values[-1]
            else:
                unavailable[field] = next(
                    (
                        row.get("unavailable_reasons", {}).get(field)
                        for row in reversed(self.samples)
                        if row.get("unavailable_reasons", {}).get(field)
                    ),
                    "no sample provided this field",
                )
        return {
            **observed,
            "unavailable_reasons": unavailable,
            "sample_count": len(self.samples),
            "sampling_interval_ms": self.interval_ms,
            "telemetry_start_monotonic_ns": self.started_ns,
            "telemetry_end_monotonic_ns": self.ended_ns,
            "monotonic_ns": self.ended_ns,
            "failures": list(self.failures),
        }
