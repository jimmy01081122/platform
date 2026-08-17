from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collectors.c1_telemetry import TelemetrySampler


class C1TelemetryTests(unittest.TestCase):
    def test_nvidia_smi_and_proc_partial_availability_are_real_samples(self):
        values = {
            "utilization.gpu": "42",
            "clocks.current.graphics": "1200",
            "clocks.current.memory": "5000",
            "temperature.gpu": "55",
            "memory.used": "100",
            "clocks_throttle_reasons.active": "0x0000000000000000",
        }

        def command(args, **_kwargs):
            queries = args[1].split("=", 1)[1].split(",")
            if "power.draw" in queries:
                return subprocess.CompletedProcess(args, 1, "", "Not Supported")
            return subprocess.CompletedProcess(
                args, 0, ",".join(values[item] for item in queries) + "\n", ""
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proc = root / "proc"
            proc.mkdir()
            (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0\n")
            (proc / "meminfo").write_text(
                "MemTotal: 1000 kB\nMemAvailable: 400 kB\n"
            )
            output = root / "telemetry.jsonl"
            sampler = TelemetrySampler(
                output, interval_ms=100, command_runner=command, proc_root=proc
            )
            with patch("collectors.c1_telemetry.shutil.which", return_value="/bin/nvidia-smi"):
                sampler.probe()
            output.write_text("")
            sampler.started_ns = 1
            sampler._sample_once()
            (proc / "stat").write_text("cpu  120 0 120 860 0 0 0 0\n")
            sampler._sample_once()
            summary = sampler.stop()

            self.assertEqual(42.0, summary["gpu_utilization_percent"])
            self.assertEqual(100 * 1024 * 1024, summary["vram_used_bytes"])
            self.assertIn("power_watts", summary["unavailable_reasons"])
            self.assertGreaterEqual(summary["cpu_utilization_percent"], 0)
            self.assertEqual(600 * 1024, summary["system_memory_used_bytes"])
            self.assertEqual(2, summary["sample_count"])
            self.assertGreater(output.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
