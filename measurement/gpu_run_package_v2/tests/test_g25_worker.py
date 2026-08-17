from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.models.contract import RuntimeMetadata
from scripts import g25_qualification as g25
from scripts import g25_worker
from tests.fake_c1_adapter import FakeC1Adapter


def parameter_evidence_fixture() -> dict:
    parameters = [
        {
            "name": f"model.parameter_{index:02d}",
            "shape": [64], "stride": [1], "numel": 64,
            "element_size": 2, "dtype": "bfloat16",
            "device_kind": "cuda", "device_location": "cuda:0",
            "requires_grad": False, "object_id": 1000 + index,
            "storage_data_ptr": 2000 + index * 128,
            "storage_nbytes": 128, "mutation_version": 0,
            "content_sha256": f"{index + 1:064x}",
        }
        for index in range(16)
    ]
    return {
        "manifest_schema": "granite-parameter-identity-v1",
        "parameter_tensors": len(parameters),
        "total_numel": sum(item["numel"] for item in parameters),
        "dtypes": ["bfloat16"], "device_kinds": ["cuda"],
        "device_locations": ["cuda:0"],
        "model_class": "GraniteMoeForCausalLM",
        "model_module": "transformers.models.granitemoe.modeling_granitemoe",
        "config_model_type": "granitemoe",
        "config_architectures": ["GraniteMoeForCausalLM"],
        "parameters": parameters,
        "parameter_manifest_sha256": g25.canonical_hash(parameters),
    }


class CountingBf16Adapter(FakeC1Adapter):
    def __init__(self):
        super().__init__()
        self.load_calls = 0

    def load_model(self, **_kwargs):
        self.load_calls += 1

    def collect_runtime_metadata(self):
        parameters = parameter_evidence_fixture()
        return RuntimeMetadata(
            self.identity,
            "bf16",
            "cuda",
            "4.47.0",
            "2.7.1+cu128",
            0.0,
            None,
            1024,
            self.capture,
            parameter_evidence=parameters,
        )


class G25WorkerTests(unittest.TestCase):
    @staticmethod
    def runtime_closure_fixture(role: str) -> dict:
        return {
            "schema_version": "g25-test-runtime-closure-v1",
            "role": role,
        }

    def descriptor(self) -> dict:
        return g25.build_worker_descriptor(
            session_id="g25-worker-test",
            instance_id="c1a-t1-00",
            ceiling=256,
            model_snapshot_inventory_sha256="a" * 64,
            device_identity={
                "kind": "cuda",
                "name": "NVIDIA GeForce RTX 3050",
                "uuid": "GPU-test",
                "pci_bus_id": "00000000:01:00.0",
            },
        )

    def test_descriptor_and_argv_are_frozen_and_schema_checked(self):
        descriptor = self.descriptor()
        self.assertEqual("P0", descriptor["logical_pass"])
        self.assertEqual(256, descriptor["generation_config"]["max_new_tokens"])
        self.assertEqual(
            {"max_new_tokens", "do_sample", "num_beams", "use_cache", "seed"},
            set(descriptor["generation_config"]),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            argv = g25.build_worker_argv(
                root / "descriptor.json", root / "evidence.json", root / "model"
            )
        self.assertEqual(
            "/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2", argv[0]
        )
        self.assertEqual("--inhibit-cache", argv[1])
        self.assertEqual(["-I", "-S", "-B", "-X", "utf8"], argv[3:8])
        self.assertEqual(
            str(g25.PACKAGE_ROOT / "scripts/g25_isolated_bootstrap.py"), argv[8]
        )
        self.assertEqual("worker", argv[9])
        self.assertNotIn("--ceiling", argv)
        self.assertNotIn("--instance", argv)

    def test_descriptor_tamper_is_rejected(self):
        descriptor = self.descriptor()
        descriptor["generation_config"]["max_new_tokens"] = 384
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "descriptor.json"
            path.write_text(json.dumps(descriptor), encoding="utf-8")
            with self.assertRaises(ValueError):
                g25_worker.load_descriptor(path)

    def test_worker_writes_evidence_file_not_stdout_protocol(self):
        descriptor = self.descriptor()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            descriptor_path = root / "descriptor.json"
            evidence_path = root / "evidence.json"
            snapshot = root / "model"
            snapshot.mkdir()
            descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
            environment = {
                **os.environ,
                "C1_ADAPTER_FACTORY": "tests.fake_c1_adapter:FakeC1Adapter",
            }
            with patch.dict(os.environ, environment, clear=True):
                code = g25_worker.main([
                    "--cell-descriptor", str(descriptor_path),
                    "--evidence-out", str(evidence_path),
                    "--model-snapshot", str(snapshot),
                ], runtime_closure_verifier=self.runtime_closure_fixture)
            self.assertEqual(0, code)
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertFalse(evidence["routing_capture_enabled"])
            self.assertFalse(evidence["profiler_enabled"])
            self.assertEqual(256, evidence["effective_generation_config"]["max_new_tokens"])

    def test_each_cell_loads_model_once_and_binds_post_generation_bf16(self):
        adapter = CountingBf16Adapter()
        with patch.object(g25_worker, "construct_adapter", return_value=adapter):
            evidence = g25_worker.execute_descriptor(
                self.descriptor(),
                Path("/cpu-only-model-fixture"),
                runtime_closure_verifier=self.runtime_closure_fixture,
            )
        self.assertEqual(1, adapter.load_calls)
        self.assertEqual(
            {
                "required": "bf16",
                "pre_generation": "bf16",
                "post_generation": "bf16",
            },
            evidence["execution_identity"]["precision"],
        )
        self.assertEqual(
            {"torch": "2.7.1+cu128", "transformers": "4.47.0"},
            evidence["execution_identity"]["runtime"],
        )
        self.assertEqual(
            evidence["execution_identity"]["parameters"]["pre_generation"],
            evidence["execution_identity"]["parameters"]["post_generation"],
        )
        self.assertEqual("cuda", evidence["execution_identity"]["device"]["kind"])
        self.assertEqual(
            ["cuda:0"], evidence["execution_identity"]["device"]["locations"]
        )

    def test_existing_evidence_path_is_never_overwritten(self):
        descriptor = self.descriptor()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            descriptor_path = root / "descriptor.json"
            evidence_path = root / "evidence.json"
            snapshot = root / "model"
            snapshot.mkdir()
            descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
            evidence_path.write_text("preserve", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                g25_worker.main([
                    "--cell-descriptor", str(descriptor_path),
                    "--evidence-out", str(evidence_path),
                    "--model-snapshot", str(snapshot),
                ])
            self.assertEqual("preserve", evidence_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
