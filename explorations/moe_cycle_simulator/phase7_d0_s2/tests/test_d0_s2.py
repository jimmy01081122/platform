"""CPU-only D0-S2 classifier and boundary tests."""

from __future__ import annotations

import unittest

from explorations.moe_cycle_simulator.phase7_d0_s2.classifier import classify_probe


def ready_fixture() -> dict:
    packages = {
        name: {
            "name": name,
            "present": True,
            "status": "COMPLETE",
            "version": "1.0.0",
            "metadata_path": f"/opt/{name}.dist-info",
            "record_sha256": "a" * 64,
            "distribution_sha256": "b" * 64,
            "hash_method": "RECORD_BYTES_PLUS_PATH_SIZE_INVENTORY",
            "file_count": 1,
            "regular_file_count": 1,
            "missing_file_count": 0,
            "symlink_file_count": 0,
        }
        for name in ("vllm", "torch", "transformers", "tokenizers", "huggingface_hub")
    }
    return {
        "gpu": {
            "query_status": "COMPLETE",
            "count": 1,
            "devices": [{
                "name": "NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
                "memory_total_bytes": 102_641_958_912,
                "driver_version": "595.71.05",
            }],
        },
        "runtime": {
            "container_image": "vLLM Inference Server",
            "container_digest": "UNAVAILABLE",
            "container_digest_status": "UNAVAILABLE",
            "cuda": {"runtime_status": "OBSERVED", "runtime_version": "12.8"},
            "torch": {"import_status": "COMPLETE", "cuda_build": "12.8", "cuda_available": True},
            "packages": packages,
        },
        "host": {"python": {"sha256": "c" * 64}},
        "storage": {"vault": {"mounted": True, "is_symlink": False, "free_bytes": 300 * 1024**3}},
        "instance": {"principal": "pod-fresh", "environment_label": "vLLM Inference Server"},
    }


class D0S2ClassifierTests(unittest.TestCase):
    def test_complete_runtime_is_ready_but_not_promotable(self) -> None:
        result = classify_probe(ready_fixture())
        self.assertEqual(result["d0_status"], "READY_FOR_GATE_M_APPLICATION")
        self.assertEqual(result["blocking_findings"], [])
        self.assertFalse(result["promotable"])
        self.assertIn("CONTAINER_DIGEST_UNAVAILABLE_NONBLOCKING", result["observational_findings"])
        self.assertEqual(result["authority"]["gpu"], "NONE")

    def test_cuda_floor_is_blocking(self) -> None:
        fixture = ready_fixture()
        fixture["runtime"]["cuda"]["runtime_version"] = "12.7"
        result = classify_probe(fixture)
        self.assertEqual(result["d0_status"], "INCOMPLETE_NOT_READY")
        self.assertIn("CUDA_RUNTIME_BELOW_12_8", result["blocking_findings"])

    def test_missing_digest_does_not_become_a_fabricated_value(self) -> None:
        result = classify_probe(ready_fixture())
        self.assertNotIn("CONTAINER_DIGEST_UNAVAILABLE_NONBLOCKING", result["blocking_findings"])
        self.assertNotEqual(result.get("container_digest"), "sha256:" + "0" * 64)


if __name__ == "__main__":
    unittest.main()
