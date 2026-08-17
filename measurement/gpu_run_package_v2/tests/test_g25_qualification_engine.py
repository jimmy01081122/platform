from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import g25_qualification as g25
from scripts import projectctl
from adapters.models.granite_moe.adapter import GraniteMoeAdapter
from scheduler.execution_lock import execution_lock
from scheduler.g25_cgroup_v2 import DrainEvidence
from tests.fake_c1_adapter import FakeC1Adapter


def parameter_evidence_fixture() -> dict:
    parameters = [
        {
            "name": f"model.parameter_{index:02d}",
            "shape": [64],
            "stride": [1],
            "numel": 64,
            "element_size": 2,
            "dtype": "bfloat16",
            "device_kind": "cuda",
            "device_location": "cuda:0",
            "requires_grad": False,
            "object_id": 1000 + index,
            "storage_data_ptr": 2000 + index * 128,
            "storage_nbytes": 128,
            "mutation_version": 0,
            "content_sha256": f"{index + 1:064x}",
        }
        for index in range(16)
    ]
    return {
        "manifest_schema": "granite-parameter-identity-v1",
        "parameter_tensors": len(parameters),
        "total_numel": sum(item["numel"] for item in parameters),
        "dtypes": ["bfloat16"],
        "device_kinds": ["cuda"],
        "device_locations": ["cuda:0"],
        "model_class": "GraniteMoeForCausalLM",
        "model_module": "transformers.models.granitemoe.modeling_granitemoe",
        "config_model_type": "granitemoe",
        "config_architectures": ["GraniteMoeForCausalLM"],
        "parameters": parameters,
        "parameter_manifest_sha256": g25.canonical_hash(parameters),
    }


class RoutingSpyAdapter(FakeC1Adapter):
    def __init__(self):
        super().__init__()
        self.enable_calls = 0

    def enable_routing_capture(self):
        self.enable_calls += 1
        raise AssertionError("qualification must never enable routing capture")


class FakeTimeoutProcess:
    pid = 4242
    returncode = -15

    def __init__(self):
        self.calls = 0

    def communicate(self, timeout=None):
        self.calls += 1
        if self.calls == 1:
            raise subprocess.TimeoutExpired(["worker"], timeout)
        return "", ""


class FakeIgnoreTermProcess:
    pid = 4343
    returncode = -9

    def __init__(self):
        self.calls = 0

    def communicate(self, timeout=None):
        self.calls += 1
        if self.calls == 1:
            raise subprocess.TimeoutExpired(["worker"], timeout)
        return "", ""


class FakeOuterSignalProcess:
    pid = 4444
    returncode = -9

    def __init__(self):
        self.calls = 0

    def communicate(self, timeout=None):
        self.calls += 1
        if self.calls == 1:
            signal_handler = g25.signal.getsignal(g25.signal.SIGTERM)
            signal_handler(g25.signal.SIGTERM, None)
        return "", ""


class FakeContainmentController:
    def __init__(self, *, kill_on_timeout=False):
        self.kill_on_timeout = kill_on_timeout
        self.mountpoint = Path("/sys/fs/cgroup")
        self._cells = {}

    def cell(self, cell_id="f" * 64):
        value = SimpleNamespace(
            controller=self,
            cell_id=cell_id,
            relative_path=f"/fixture.service/g25-cell-{cell_id}",
            device=10,
            inode=11,
            populated_zero_observed=False,
            closed=False,
        )
        self._cells[cell_id] = value
        return value

    @staticmethod
    def _drain(*, initial, term, killed):
        return DrainEvidence(
            initial_populated=initial,
            term_sent=term,
            term_sent_monotonic_ns=1 if term else None,
            term_grace_seconds=30,
            cgroup_kill_written=killed,
            cgroup_kill_monotonic_ns=2 if killed else None,
            populated_zero_monotonic_ns=3,
            final_populated=0,
        )

    def finalize_normal_exit(self, cell):
        cell.populated_zero_observed = True
        return self._drain(initial=0, term=False, killed=True)

    def terminate_and_drain(self, cell, *, graceful):
        cell.populated_zero_observed = True
        return self._drain(
            initial=1, term=graceful, killed=self.kill_on_timeout
        )

    def emergency_kill(self, cell):
        cell.populated_zero_observed = True
        return self._drain(initial=1, term=False, killed=True)

    def close_cell(self, cell):
        cell.closed = True
        self._cells.pop(cell.cell_id, None)


class G25QualificationEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = g25.load_contract()
        cls.profile_map = g25.load_profile_map()
        cls.selections = g25._manifest_selections(cls.contract)
        cls.profile_sha256 = g25.sha256_file(g25.PROFILE_MAP_PATH)

    def evidence(self, classification="QUALIFIED", ceiling=256, instance="c1a-t1-00"):
        return g25.synthetic_worker_evidence(
            self.selections[instance], ceiling, classification
        )

    @staticmethod
    def lifetime_guard_fixture(*, complete: bool = True) -> dict:
        return {
            "schema_version": "g25-worker-lifetime-guard-v2",
            "mechanism": "systemd-delegated-cgroup-v2+pdeathsig-v2",
            "expected_parent_pid": 123,
            "expected_parent_start_ticks": 456,
            "lease_fd": 7,
            "lease_device": 8,
            "lease_inode": 9,
            "pdeathsig": "SIGKILL",
            "pdeathsig_number": 9,
            "ready_observed": True,
            "move_observed": True,
            "go_sent": True,
            "membership_ack_observed": True,
            "cell_cgroup_path": f"/fixture.service/g25-cell-{'f' * 64}",
            "cell_cgroup_device": 10,
            "cell_cgroup_inode": 11,
            "cgroup_kill_supported": True,
            "populated_zero_observed": complete,
            "drain": {
                "initial_populated": 0,
                "term_sent": False,
                "term_sent_monotonic_ns": None,
                "term_grace_seconds": 30,
                "cgroup_kill_written": True,
                "cgroup_kill_monotonic_ns": 2,
                "populated_zero_monotonic_ns": 3,
                "final_populated": 0,
            } if complete else None,
        }

    def actual_evidence(self, selection, ceiling, pilot_matrix,
                        classification="QUALIFIED"):
        evidence = g25.synthetic_worker_evidence(
            selection, ceiling, classification
        )
        evidence["execution_identity"] = {
            "mode": "qualification",
            "model": {
                "model_id": self.contract["frozen_inputs"]["model_id"],
                "model_revision": self.contract["frozen_inputs"]["model_revision"],
                "tokenizer_revision": self.contract["frozen_inputs"]
                ["tokenizer_revision"],
            },
            "precision": {
                "required": "bf16",
                "pre_generation": "bf16",
                "post_generation": "bf16",
            },
            "parameters": {
                "required": {
                    "dtype": "bfloat16",
                    "device_kind": "cuda",
                    "model_class": "GraniteMoeForCausalLM",
                    "config_model_type": "granitemoe",
                },
                "pre_generation": parameter_evidence_fixture(),
                "post_generation": parameter_evidence_fixture(),
            },
            "chat_template_sha256": self.contract["frozen_inputs"]
            ["chat_template_sha256"],
            "prompt_construction_revision": self.contract["frozen_inputs"]
            ["prompt_construction_revision"],
            "system_message_sha256": self.contract["frozen_inputs"]
            ["system_message_sha256"],
            "tokenizer_config_sha256": self.contract["frozen_inputs"]
            ["tokenizer_config_sha256"],
            "generation_config_file_sha256": self.contract["frozen_inputs"]
            ["generation_config_file_sha256"],
            "special_tokens_map_sha256": self.contract["frozen_inputs"]
            ["special_tokens_map_sha256"],
            "eos_token_id": self.contract["frozen_inputs"]["eos_token_id"],
            "pad_token_id": self.contract["frozen_inputs"]["pad_token_id"],
            "rendered_chat_sha256": pilot_matrix["frozen_rendered_inputs"]
            [selection["instance_id"]]["rendered_chat_sha256"],
            "seed": self.contract["frozen_inputs"]["seed"],
            "runtime": {
                "torch": self.contract["frozen_inputs"]["torch_version"],
                "transformers": self.contract["frozen_inputs"]
                ["transformers_version"],
            },
            "device": {
                "kind": "cuda", "locations": ["cuda:0"],
                "name": "mock RTX 3050", "uuid": "GPU-mock"
            },
        }
        evidence["parent_process"] = {
            "worker_pid": 4242,
            "started_unix_ns": 100,
            "finished_unix_ns": 200,
            "termination_signal": None,
            "timeout_stage": "completed",
            "term_grace_seconds": 30,
            "worker_argv_sha256": "a" * 64,
            "io_manifest_sha256": "b" * 64,
            "evidence_file_sha256": "c" * 64,
            "stdout_sha256": "d" * 64,
            "stderr_sha256": "e" * 64,
            "lifetime_guard": self.lifetime_guard_fixture(),
        }
        return evidence

    def row(self, instance="c1a-t1-00", ceiling=256, classification="QUALIFIED"):
        evidence = self.evidence(classification, ceiling, instance)
        raw = g25.canonical_bytes(evidence)
        return g25.build_cell_row(
            session_id="synthetic",
            selection=self.selections[instance],
            ceiling=ceiling,
            profile_sha256=self.profile_sha256,
            generation_config_sha256=g25.canonical_hash(
                g25.resolve_task_profile(self.profile_map, "T1", ceiling=ceiling)
            ),
            evidence=evidence,
            evidence_descriptor={
                "path": f"raw/{instance}__{ceiling}.json",
                "bytes": len(raw),
                "sha256": g25.canonical_hash(evidence),
            },
        )

    def complete_rows(self, scenario="common-384"):
        rows = []
        for ceiling in g25.qualification_ceilings(self.contract):
            for instance in g25.qualification_instances(self.contract):
                rows.append(self.row(
                    instance,
                    ceiling,
                    g25._synthetic_class(scenario, instance, ceiling),
                ))
        return rows

    def test_classifier_has_six_fail_closed_classes(self):
        cases = {
            "QUALIFIED": self.evidence(),
            "TRUNCATED": self.evidence("TRUNCATED"),
            "TIMEOUT": self.evidence("TIMEOUT"),
            "RUNTIME_FAILURE": self.evidence("RUNTIME_FAILURE"),
            "INVALID_EVIDENCE": self.evidence("INVALID_EVIDENCE"),
        }
        invalid_output = self.evidence()
        invalid_output["parser_outcome"] = "unparseable"
        cases["INVALID_OUTPUT"] = invalid_output
        for expected, evidence in cases.items():
            with self.subTest(expected=expected):
                actual, _reason = g25.classify_worker_evidence(
                    evidence,
                    expected_prompt_hash=self.selections["c1a-t1-00"]["prompt_hash"],
                    ceiling=256,
                    synthetic_session=True,
                )
                self.assertEqual(expected, actual)

    def test_actual_execution_identity_must_match_frozen_contract(self):
        evidence = self.evidence()
        evidence["execution_identity"] = {
            "mode": "qualification",
            "model": {
                "model_id": "substituted-model",
                "model_revision": self.contract["frozen_inputs"]["model_revision"],
                "tokenizer_revision": self.contract["frozen_inputs"]["tokenizer_revision"],
            },
            "chat_template_sha256": self.contract["frozen_inputs"]["chat_template_sha256"],
            "seed": self.contract["frozen_inputs"]["seed"],
            "runtime": {
                "torch": self.contract["frozen_inputs"]["torch_version"],
                "transformers": self.contract["frozen_inputs"]["transformers_version"],
            },
            "device": {"kind": "cuda", "name": "RTX 3050", "uuid": "GPU-test"},
        }
        classification, _reason = g25.classify_worker_evidence(
            evidence,
            expected_prompt_hash=self.selections["c1a-t1-00"]["prompt_hash"],
            ceiling=256,
            synthetic_session=False,
        )
        self.assertEqual("INVALID_EVIDENCE", classification)
        for spoofed_mode in ("synthetic_governance", "supervisor_timeout"):
            spoofed = self.evidence()
            spoofed["execution_identity"]["mode"] = spoofed_mode
            classification, _reason = g25.classify_worker_evidence(
                spoofed,
                expected_prompt_hash=self.selections["c1a-t1-00"]["prompt_hash"],
                ceiling=256,
                synthetic_session=False,
            )
            self.assertEqual("INVALID_EVIDENCE", classification)

    def test_actual_precision_missing_non_bf16_or_drift_is_invalid_evidence(self):
        pilot_session, pilot_matrix, pilot_artifacts = g25.load_pilot_contracts()
        mocked_matrix = copy.deepcopy(pilot_matrix)
        for identity in mocked_matrix["frozen_rendered_inputs"].values():
            identity["input_token_count"] = 3
            identity["input_token_ids_sha256"] = g25.canonical_hash([11, 12, 13])
        selection = self.selections["c1a-t1-00"]
        baseline = self.actual_evidence(selection, 256, mocked_matrix)
        cases = []
        missing = copy.deepcopy(baseline)
        missing["execution_identity"].pop("precision")
        cases.append(missing)
        non_bf16 = copy.deepcopy(baseline)
        non_bf16["execution_identity"]["precision"].update({
            "pre_generation": "fp16", "post_generation": "fp16"
        })
        cases.append(non_bf16)
        drift = copy.deepcopy(baseline)
        drift["execution_identity"]["precision"]["post_generation"] = "fp16"
        cases.append(drift)
        with patch.object(
            g25, "load_pilot_contracts",
            return_value=(pilot_session, mocked_matrix, pilot_artifacts),
        ):
            valid, _reason = g25.classify_worker_evidence(
                baseline,
                expected_prompt_hash=selection["prompt_hash"],
                ceiling=256,
                synthetic_session=False,
                decode_output_ids=lambda _ids: baseline["text"],
            )
            self.assertEqual("QUALIFIED", valid)
            for evidence in cases:
                classification, reason = g25.classify_worker_evidence(
                    evidence,
                    expected_prompt_hash=selection["prompt_hash"],
                    ceiling=256,
                    synthetic_session=False,
                    decode_output_ids=lambda _ids: evidence["text"],
                )
                self.assertEqual("INVALID_EVIDENCE", classification)
                self.assertIn("BF16 precision", reason)

    def test_actual_parameter_observation_accepts_cuda0_and_rejects_runtime_drift(self):
        pilot_session, pilot_matrix, pilot_artifacts = g25.load_pilot_contracts()
        mocked_matrix = copy.deepcopy(pilot_matrix)
        for identity in mocked_matrix["frozen_rendered_inputs"].values():
            identity["input_token_count"] = 3
            identity["input_token_ids_sha256"] = g25.canonical_hash([11, 12, 13])
        selection = self.selections["c1a-t1-00"]
        baseline = self.actual_evidence(selection, 256, mocked_matrix)
        mutations = []
        dtype_drift = copy.deepcopy(baseline)
        dtype_drift["execution_identity"]["parameters"]["post_generation"][
            "dtypes"
        ] = ["float16"]
        mutations.append(dtype_drift)
        device_drift = copy.deepcopy(baseline)
        device_drift["execution_identity"]["parameters"]["post_generation"][
            "device_locations"
        ] = ["cpu"]
        mutations.append(device_drift)
        count_drift = copy.deepcopy(baseline)
        count_drift["execution_identity"]["parameters"]["post_generation"][
            "total_numel"
        ] += 1
        mutations.append(count_drift)
        for field, value in (
            ("object_id", 9001),
            ("storage_data_ptr", 9002),
            ("mutation_version", 1),
            ("content_sha256", "f" * 64),
        ):
            manifest_drift = copy.deepcopy(baseline)
            post = manifest_drift["execution_identity"]["parameters"][
                "post_generation"
            ]
            post["parameters"][0][field] = value
            post["parameter_manifest_sha256"] = g25.canonical_hash(
                post["parameters"]
            )
            mutations.append(manifest_drift)
        with patch.object(
            g25, "load_pilot_contracts",
            return_value=(pilot_session, mocked_matrix, pilot_artifacts),
        ):
            classification, _reason = g25.classify_worker_evidence(
                baseline,
                expected_prompt_hash=selection["prompt_hash"],
                ceiling=256,
                synthetic_session=False,
                decode_output_ids=lambda _ids: baseline["text"],
            )
            self.assertEqual("QUALIFIED", classification)
            self.assertEqual(
                ["cuda:0"], baseline["execution_identity"]["device"]["locations"]
            )
            for evidence in mutations:
                classification, reason = g25.classify_worker_evidence(
                    evidence,
                    expected_prompt_hash=selection["prompt_hash"],
                    ceiling=256,
                    synthetic_session=False,
                    decode_output_ids=lambda _ids: evidence["text"],
                )
                self.assertEqual("INVALID_EVIDENCE", classification)
                self.assertIn("parameter", reason)

    def test_parent_replay_owns_eos_decode_and_parser_truth(self):
        pilot_session, pilot_matrix, pilot_artifacts = g25.load_pilot_contracts()
        mocked_matrix = copy.deepcopy(pilot_matrix)
        for identity in mocked_matrix["frozen_rendered_inputs"].values():
            identity["input_token_count"] = 3
            identity["input_token_ids_sha256"] = g25.canonical_hash([11, 12, 13])
        selection = self.selections["c1a-t1-00"]
        baseline = self.actual_evidence(selection, 256, mocked_matrix)

        def with_output(evidence, ids, stop, text="#### 42", parser="parseable"):
            value = copy.deepcopy(evidence)
            value["output_token_ids"] = list(ids)
            value["output_token_count"] = len(ids)
            value["output_hash"] = g25.canonical_hash(list(ids))
            value["stop_reason"] = stop
            value["text"] = text
            value["parser_outcome"] = parser
            return value

        cases = []
        cases.append((
            with_output(baseline, [7, 8], "eos_token"),
            lambda evidence: evidence["text"],
            "INVALID_EVIDENCE",
        ))
        cases.append((
            with_output(baseline, [7, 0], "eos_token", parser="unparseable"),
            lambda evidence: evidence["text"],
            "INVALID_EVIDENCE",
        ))
        cases.append((
            with_output(baseline, [7, 0], "eos_token", text="no answer"),
            lambda evidence: evidence["text"],
            "INVALID_EVIDENCE",
        ))
        cases.append((
            with_output(baseline, [7, 0], "eos_token"),
            lambda _evidence: "#### 99",
            "INVALID_EVIDENCE",
        ))
        cases.append((
            with_output(baseline, [0, 7], "eos_token"),
            lambda evidence: evidence["text"],
            "INVALID_EVIDENCE",
        ))
        cases.append((
            with_output(baseline, range(1, 257), "max_new_tokens"),
            lambda evidence: evidence["text"],
            "TRUNCATED",
        ))
        cases.append((
            with_output(baseline, [*range(1, 257), 0], "eos_token"),
            lambda _evidence: (_ for _ in ()).throw(
                AssertionError("over-ceiling output must not be decoded")
            ),
            "INVALID_OUTPUT",
        ))
        cases.append((
            with_output(baseline, [7, 8], "illegal_stop"),
            lambda evidence: evidence["text"],
            "INVALID_OUTPUT",
        ))
        # 42 is parseable but incorrect for c1a-t1-00 (reference 100). Task
        # correctness is intentionally not an execution-qualification gate.
        cases.append((
            with_output(baseline, [7, 0], "eos_token", text="#### 42"),
            lambda evidence: evidence["text"],
            "QUALIFIED",
        ))
        with patch.object(
            g25, "load_pilot_contracts",
            return_value=(pilot_session, mocked_matrix, pilot_artifacts),
        ):
            for evidence, decoder_value, expected in cases:
                with self.subTest(expected=expected, output=evidence["output_token_ids"][:3]):
                    classification, _reason = g25.classify_worker_evidence(
                        evidence,
                        expected_prompt_hash=selection["prompt_hash"],
                        ceiling=256,
                        synthetic_session=False,
                        decode_output_ids=lambda ids, value=evidence, function=decoder_value: function(value),
                    )
                    self.assertEqual(expected, classification)

    def test_real_over_ceiling_terminal_eos_cannot_enter_ledger_or_selector(self):
        pilot_session, pilot_matrix, pilot_artifacts = g25.load_pilot_contracts()
        mocked_matrix = copy.deepcopy(pilot_matrix)
        for identity in mocked_matrix["frozen_rendered_inputs"].values():
            identity["input_token_count"] = 3
            identity["input_token_ids_sha256"] = g25.canonical_hash([11, 12, 13])
        selection = self.selections["c1a-t1-00"]
        rows = {}
        for ceiling in (256, 384, 512):
            for output_count in (ceiling - 1, ceiling, ceiling + 1):
                evidence = self.actual_evidence(selection, ceiling, mocked_matrix)
                eos_token_id = self.contract["frozen_inputs"]["eos_token_id"]
                evidence["output_token_ids"] = [1] * (output_count - 1) + [eos_token_id]
                evidence["output_token_count"] = output_count
                evidence["output_hash"] = g25.canonical_hash(
                    evidence["output_token_ids"]
                )
                decoder_calls = []

                def decoder(_ids, *, _text=evidence["text"]):
                    decoder_calls.append(True)
                    return _text

                with patch.object(
                    g25, "load_pilot_contracts",
                    return_value=(pilot_session, mocked_matrix, pilot_artifacts),
                ):
                    row = g25.build_cell_row(
                        session_id="mocked-real",
                        selection=selection,
                        ceiling=ceiling,
                        profile_sha256=self.profile_sha256,
                        generation_config_sha256=g25.canonical_hash(
                            g25.resolve_task_profile(
                                self.profile_map, "T1", ceiling=ceiling
                            )
                        ),
                        evidence=evidence,
                        evidence_descriptor={
                            "path": f"raw/c1a-t1-00__{ceiling}.json",
                            "bytes": 1,
                            "sha256": "0" * 64,
                        },
                        synthetic_session=False,
                        decode_output_ids=decoder,
                    )
                rows[(ceiling, output_count)] = row
                if output_count <= ceiling:
                    self.assertEqual("QUALIFIED", row["qualification_class"])
                    self.assertEqual([True, True], decoder_calls)
                else:
                    self.assertEqual("INVALID_OUTPUT", row["qualification_class"])
                    self.assertEqual([], decoder_calls)
                self.assertIs(
                    output_count <= ceiling,
                    row["parent_output_replay"]["within_frozen_ceiling"],
                )

        row = rows[(256, 257)]
        self.assertFalse(row["parent_output_replay"]["legal_stop"])
        forged = copy.deepcopy(row)
        forged["qualification_class"] = "QUALIFIED"
        forged["execution_status"] = "complete"
        with self.assertRaises(Exception):
            g25.validate_schema(g25.CELL_SCHEMA, forged)
        with self.assertRaisesRegex(ValueError, "QUALIFIED cell"):
            g25._assert_cell_ceiling_invariants(forged)

    def test_real_audit_rejects_coherently_rehashed_over_ceiling_qualified_raw(self):
        pilot_session, pilot_matrix, pilot_artifacts = g25.load_pilot_contracts()
        mocked_matrix = copy.deepcopy(pilot_matrix)
        for identity in mocked_matrix["frozen_rendered_inputs"].values():
            identity["input_token_count"] = 3
            identity["input_token_ids_sha256"] = g25.canonical_hash([11, 12, 13])

        def provider(selection, ceiling):
            return self.actual_evidence(selection, ceiling, mocked_matrix)

        with tempfile.TemporaryDirectory() as directory, patch.object(
            g25, "load_pilot_contracts",
            return_value=(pilot_session, mocked_matrix, pilot_artifacts),
        ):
            root, _verdict, _audit = g25.write_qualification_session(
                Path(directory), "mocked-real", provider,
                synthetic=False, gpu_used=True,
                decode_output_ids=lambda _ids: "#### 42",
            )
            ledger_path = root / "ledger.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            target = next(
                row for row in ledger["cells"]
                if row["instance_id"] == "c1a-t1-00" and row["ceiling"] == 512
            )
            raw_path = root / target["evidence_paths"][0]["path"]
            evidence = json.loads(raw_path.read_text(encoding="utf-8"))
            eos_token_id = self.contract["frozen_inputs"]["eos_token_id"]
            evidence["output_token_ids"] = [1] * 512 + [eos_token_id]
            evidence["output_token_count"] = 513
            evidence["output_hash"] = g25.canonical_hash(evidence["output_token_ids"])
            g25._write_json(raw_path, evidence)

            descriptor = target["evidence_paths"][0]
            descriptor["bytes"] = raw_path.stat().st_size
            descriptor["sha256"] = g25.sha256_file(raw_path)
            target["worker_evidence_sha256"] = g25.canonical_hash(evidence)
            # Forge a semantically plausible bounded replay while retaining the
            # rehashed over-ceiling raw evidence.  Ledger/selector-only checks
            # cannot recover the preimage length, so final audit must replay raw.
            replay = target["parent_output_replay"]
            replay["output_token_ids_sha256"] = g25.canonical_hash(
                evidence["output_token_ids"]
            )
            replay["output_token_count"] = 512
            replay["within_frozen_ceiling"] = True
            replay["ceiling_reached"] = True
            replay["replay_sha256"] = g25.canonical_hash({
                key: value for key, value in replay.items()
                if key != "replay_sha256"
            })
            target["output_token_count"] = 512
            target["output_hash"] = evidence["output_hash"]
            target["output_token_ids_sha256"] = g25.canonical_hash(
                evidence["output_token_ids"]
            )
            target["parent_output_replay_sha256"] = g25.canonical_hash(replay)
            g25.validate_schema(g25.CELL_SCHEMA, target)
            g25._assert_cell_ceiling_invariants(target)
            g25._write_json(root / "cells" / f"{target['cell_id']}.json", target)

            ledger["cell_set_sha256"] = g25.canonical_hash(ledger["cells"])
            g25._write_json(ledger_path, ledger)
            session_path = root / "session.json"
            session = json.loads(session_path.read_text(encoding="utf-8"))
            session["artifacts"]["ledger"] = g25.sha256_file(ledger_path)
            g25._write_json(session_path, session)

            audit = g25.audit_session(
                root, decode_output_ids=lambda _ids: "#### 42"
            )
            self.assertEqual("failed", audit["status"])
            self.assertTrue(any(
                "canonical cell replay drift" in finding
                or "classification replay drift" in finding
                for finding in audit["findings"]
            ))

    def test_task_correctness_metadata_cannot_change_classification(self):
        evidence = self.evidence()
        evidence["task_outcome"] = "incorrect"
        classification, _reason = g25.classify_worker_evidence(
            evidence,
            expected_prompt_hash=self.selections["c1a-t1-00"]["prompt_hash"],
            ceiling=256,
            synthetic_session=True,
        )
        self.assertEqual("QUALIFIED", classification)
        row = self.row()
        self.assertNotIn("task_outcome", row)
        self.assertNotIn("correctness", row)
        self.assertNotIn("reference", row)

    def test_worker_effective_generation_config_is_raw_evidence_bound(self):
        evidence = self.evidence(ceiling=384)
        evidence["effective_generation_config"]["do_sample"] = True
        evidence["effective_generation_config_sha256"] = g25.canonical_hash(
            evidence["effective_generation_config"]
        )
        classification, _reason = g25.classify_worker_evidence(
            evidence,
            expected_prompt_hash=self.selections["c1a-t1-00"]["prompt_hash"],
            ceiling=384,
            synthetic_session=True,
        )
        self.assertEqual("INVALID_EVIDENCE", classification)

    def test_task_profile_mapping_is_frozen_and_has_no_per_sample_override(self):
        profile = g25.resolve_task_profile(self.profile_map, "T1", ceiling=384)
        self.assertEqual(384, profile["max_new_tokens"])
        self.assertEqual(
            {"max_new_tokens", "do_sample", "num_beams", "use_cache", "seed"},
            set(profile),
        )
        self.assertFalse({
            "profile", "role", "candidate_ceilings", "timeout_seconds",
            "legal_stop_reasons",
        }.intersection(profile))
        with self.assertRaises(ValueError):
            g25.resolve_task_profile(self.profile_map, "T1", ceiling=768)
        with self.assertRaises(ValueError):
            g25.resolve_task_profile(self.profile_map, "T2", ceiling=256)

    def test_shared_generation_core_never_enables_routing(self):
        adapter = RoutingSpyAdapter()
        adapter.collect_quality_result = types.MethodType(
            GraniteMoeAdapter.collect_quality_result, adapter
        )
        prompt = "What is 6 times 7?"
        evidence = g25.execute_generation_core(
            adapter,
            execution={"sample_id": "fixture", "pass_id": "P0"},
            prompt=prompt,
            sample={"task_id": "T1", "reference": 42},
            generation_config={
                "max_new_tokens": 256,
                "do_sample": False,
                "num_beams": 1,
                "use_cache": True,
                "seed": 20260718,
            },
            request_id="fixture-cell",
            runtime_closure_verifier=lambda role: {
                "schema_version": "g25-test-runtime-closure-v1",
                "role": role,
            },
        )
        self.assertEqual(0, adapter.enable_calls)
        self.assertFalse(evidence["routing_capture_enabled"])
        self.assertFalse(evidence["profiler_enabled"])
        self.assertEqual("eos_token", evidence["stop_reason"])
        self.assertEqual("parseable", evidence["parser_outcome"])

    def test_parent_process_binds_480_second_timeout_and_kills_process_group(self):
        process = FakeTimeoutProcess()
        controller = FakeContainmentController()
        containment = controller.cell()
        with tempfile.TemporaryDirectory() as temporary, execution_lock(
            Path(temporary)
        ) as lease, patch.object(
            g25.subprocess, "Popen", return_value=process
        ) as popen, patch.object(
            g25, "_await_worker_guard_ready",
            return_value=self.lifetime_guard_fixture(complete=False),
        ):
            lease_fd = lease.fileno()
            result = g25.invoke_worker_process(
                ["worker"], lease=lease, containment=containment,
                timeout_seconds=480
            )
        self.assertTrue(result["timed_out"])
        self.assertEqual(30, result["term_grace_seconds"])
        self.assertEqual("SIGTERM", result["termination_signal"])
        popen.assert_called_once()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(lease_fd, popen.call_args.kwargs["pass_fds"][0])
        self.assertEqual(4, len(popen.call_args.kwargs["pass_fds"]))
        self.assertTrue(result["lifetime_guard"]["populated_zero_observed"])
        self.assertEqual(0, result["lifetime_guard"]["drain"]["final_populated"])
        with tempfile.TemporaryDirectory() as temporary, execution_lock(
            Path(temporary)
        ) as lease, self.assertRaises(ValueError):
            g25.invoke_worker_process(
                ["worker"], lease=lease,
                containment=FakeContainmentController().cell(),
                timeout_seconds=479,
            )

    def test_parent_process_fields_override_worker_claims(self):
        worker = self.evidence()
        worker.update({
            "return_code": 0,
            "timed_out": False,
            "wall_time_seconds": 0.001,
        })
        supervisor = {
            "supervisor_result": True,
            "worker_pid": 4242,
            "argv": ["worker"],
            "stdout": json.dumps(worker),
            "stderr": "worker failed",
            "return_code": 23,
            "timed_out": False,
            "wall_time_seconds": 12.5,
            "parent_started_unix_ns": 100,
            "parent_finished_unix_ns": 200,
            "termination_signal": None,
            "timeout_stage": "completed",
            "term_grace_seconds": 30,
            "io_manifest_sha256": "f" * 64,
        }
        normalized = g25.normalize_worker_process_result(
            supervisor, selection=self.selections["c1a-t1-00"], ceiling=256
        )
        self.assertEqual(23, normalized["return_code"])
        self.assertFalse(normalized["timed_out"])
        self.assertEqual(12.5, normalized["wall_time_seconds"])
        self.assertEqual(100, normalized["parent_process"]["started_unix_ns"])

    def test_worker_ignoring_term_is_killed_after_exact_30_second_grace(self):
        process = FakeIgnoreTermProcess()
        controller = FakeContainmentController(kill_on_timeout=True)
        containment = controller.cell()
        with tempfile.TemporaryDirectory() as temporary, execution_lock(
            Path(temporary)
        ) as lease, patch.object(
            g25.subprocess, "Popen", return_value=process
        ), patch.object(
            g25, "_await_worker_guard_ready",
            return_value=self.lifetime_guard_fixture(complete=False),
        ):
            result = g25.invoke_worker_process(
                ["worker"], lease=lease, containment=containment,
                timeout_seconds=480
            )
        self.assertEqual("SIGKILL", result["termination_signal"])
        self.assertEqual(-9, result["return_code"])
        self.assertTrue(result["lifetime_guard"]["drain"]["cgroup_kill_written"])
        self.assertEqual({}, controller._cells)

    def test_outer_termination_kills_detached_worker_group_before_parent_unwinds(self):
        process = FakeOuterSignalProcess()
        controller = FakeContainmentController(kill_on_timeout=True)
        containment = controller.cell()
        with tempfile.TemporaryDirectory() as temporary, execution_lock(
            Path(temporary)
        ) as lease, patch.object(
            g25.subprocess, "Popen", return_value=process
        ), patch.object(
            g25, "_await_worker_guard_ready",
            return_value=self.lifetime_guard_fixture(complete=False),
        ):
            with self.assertRaisesRegex(
                g25.WorkerSupervisorInterrupted, "active worker group killed"
            ):
                g25.invoke_worker_process(
                    ["worker"], lease=lease, containment=containment,
                    timeout_seconds=480
                )
        self.assertEqual({}, controller._cells)

    def test_real_worker_protocol_reads_evidence_file_and_ignores_stdout(self):
        worker = self.evidence()
        supervisor = {
            "supervisor_result": True,
            "worker_pid": 4242,
            "argv": ["worker"],
            "stdout": "untrusted log noise, not JSON",
            "stderr": "",
            "return_code": 0,
            "timed_out": False,
            "wall_time_seconds": 1.25,
            "parent_started_unix_ns": 100,
            "parent_finished_unix_ns": 200,
            "termination_signal": None,
            "timeout_stage": "completed",
            "term_grace_seconds": 30,
            "io_manifest_sha256": "f" * 64,
        }
        with tempfile.TemporaryDirectory() as temporary:
            evidence_path = Path(temporary) / "evidence.json"

            def fake_invoke(*_args, **_kwargs):
                evidence_path.write_text(json.dumps(worker), encoding="utf-8")
                return dict(supervisor)

            with patch.object(g25, "invoke_worker_process", side_effect=fake_invoke):
                with execution_lock(Path(temporary) / "runs") as lease:
                    result = g25.invoke_worker_evidence_process(
                        ["worker"], evidence_path, lease=lease,
                        containment=FakeContainmentController().cell(),
                        timeout_seconds=480,
                    )
        normalized = g25.normalize_worker_process_result(
            result, selection=self.selections["c1a-t1-00"], ceiling=256
        )
        self.assertEqual(worker["output_hash"], normalized["output_hash"])
        self.assertEqual(64, len(normalized["parent_process"]["stdout_sha256"]))

    def test_exact_matrix_is_connected_to_parent_timeout_provider(self):
        timeout = {
            "supervisor_result": True,
            "worker_pid": 4242,
            "argv": ["worker"],
            "return_code": -15,
            "timed_out": True,
            "exception": "timeout",
            "wall_time_seconds": 480.0,
            "parent_started_unix_ns": 100,
            "parent_finished_unix_ns": 200,
            "termination_signal": "SIGTERM",
            "timeout_stage": "cell_timeout_sigterm",
            "term_grace_seconds": 30,
            "io_manifest_sha256": "f" * 64,
            "lifetime_guard": self.lifetime_guard_fixture(),
        }
        with tempfile.TemporaryDirectory() as temporary, execution_lock(
            Path(temporary)
        ) as lease:
            provider = g25.subprocess_evidence_provider(
                lambda selection, ceiling: [selection["instance_id"], str(ceiling)],
                lease,
                lambda _selection, _ceiling: FakeContainmentController().cell(),
            )
            with patch.object(g25, "invoke_worker_process", return_value=timeout) as invoke:
                results = g25.run_qualification_matrix(self.contract, provider)
        self.assertEqual(12, len(results))
        self.assertEqual(12, invoke.call_count)
        for selection, ceiling, evidence in results:
            classification, _reason = g25.classify_worker_evidence(
                evidence,
                expected_prompt_hash=selection["prompt_hash"],
                ceiling=ceiling,
                synthetic_session=False,
            )
            self.assertEqual("TIMEOUT", classification)

    def test_selector_requires_exact_complete_matrix(self):
        rows = self.complete_rows()
        ledger = g25.build_ledger(
            "synthetic", rows, profile_sha256=self.profile_sha256
        )
        self.assertEqual(12, ledger["complete_cell_count"])
        self.assertEqual(384, g25.select_common_ceiling(ledger)["selected_common_ceiling"])
        with self.assertRaises(ValueError):
            g25.build_ledger(
                "synthetic", rows[:-1], profile_sha256=self.profile_sha256
            )
        with self.assertRaises(ValueError):
            g25.build_ledger(
                "synthetic", rows[:-1] + [copy.deepcopy(rows[0])],
                profile_sha256=self.profile_sha256,
            )

    def test_selector_rejects_coherently_rehashed_impossible_qualified_rows(self):
        rows = self.complete_rows("all-256")
        forged = copy.deepcopy(rows)
        target = next(row for row in forged if row["ceiling"] == 256)
        replay = target["parent_output_replay"]
        replay["eos_is_unique_terminal"] = False
        replay["eos_occurrence_count"] = 0
        replay["replay_sha256"] = g25.canonical_hash({
            key: value for key, value in replay.items() if key != "replay_sha256"
        })
        target["parent_output_replay_sha256"] = g25.canonical_hash(replay)
        with self.assertRaises(Exception):
            g25.build_ledger(
                "synthetic", forged, profile_sha256=self.profile_sha256
            )

        ledger = g25.build_ledger(
            "synthetic", rows, profile_sha256=self.profile_sha256
        )
        forged_ledger = copy.deepcopy(ledger)
        target = next(
            row for row in forged_ledger["cells"] if row["ceiling"] == 256
        )
        replay = target["parent_output_replay"]
        replay["eos_is_unique_terminal"] = False
        replay["eos_occurrence_count"] = 0
        replay["replay_sha256"] = g25.canonical_hash({
            key: value for key, value in replay.items() if key != "replay_sha256"
        })
        target["parent_output_replay_sha256"] = g25.canonical_hash(replay)
        forged_ledger["cell_set_sha256"] = g25.canonical_hash(
            forged_ledger["cells"]
        )
        with self.assertRaises(Exception):
            g25.select_common_ceiling(forged_ledger)

    def test_selector_scenarios_cover_minimal_ceiling_and_no_common_ceiling(self):
        expected = {
            "all-256": 256,
            "common-384": 384,
            "common-512": 512,
            "no-common": None,
            "timeout": None,
            "runtime-failure": None,
            "invalid-evidence": None,
        }
        for scenario, ceiling in expected.items():
            with self.subTest(scenario=scenario):
                ledger = g25.build_ledger(
                    "synthetic",
                    self.complete_rows(scenario),
                    profile_sha256=self.profile_sha256,
                )
                verdict = g25.select_common_ceiling(ledger)
                self.assertEqual(ceiling, verdict["selected_common_ceiling"])

    def test_mutated_profile_config_and_classification_are_rejected_or_audited(self):
        rows = self.complete_rows()
        mutated = copy.deepcopy(rows)
        mutated[0]["generation_config_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            g25.build_ledger(
                "synthetic", mutated, profile_sha256=self.profile_sha256
            )
        mutated = copy.deepcopy(rows)
        mutated[0]["qualification_class"] = "TIMEOUT"
        ledger = g25.build_ledger(
            "synthetic", mutated, profile_sha256=self.profile_sha256
        )
        self.assertEqual(
            384, g25.select_common_ceiling(ledger)["selected_common_ceiling"]
        )

    def test_frozen_sample_and_prompt_substitution_is_rejected(self):
        rows = self.complete_rows()
        rows[0]["sample_id"] = "adversarial-sample"
        rows[0]["prompt_hash"] = "0" * 64
        rows[0]["cell_id"] = g25.cell_identity(
            "synthetic", rows[0]["instance_id"], rows[0]["sample_id"],
            rows[0]["ceiling"], rows[0]["generation_profile_sha256"],
            rows[0]["generation_config_sha256"],
        )
        with self.assertRaisesRegex(ValueError, "frozen sample identity"):
            g25.build_ledger(
                "synthetic", rows, profile_sha256=self.profile_sha256
            )

    def test_synthetic_session_is_12_cells_replayable_and_non_formal(self):
        with tempfile.TemporaryDirectory() as directory:
            root, verdict, audit = g25.write_synthetic_session(
                Path(directory), "g25-synth", "common-384"
            )
            self.assertEqual(384, verdict["selected_common_ceiling"])
            self.assertEqual("complete", audit["status"])
            self.assertEqual(verdict, g25.replay_session(root))
            self.assertEqual(12, len(list((root / "cells").glob("*.json"))))
            session = json.loads((root / "session.json").read_text())
            self.assertFalse(session["formal_c1_evidence"])
            self.assertFalse(session["gpu_used"])
            self.assertFalse((root / "suite_snapshot.json").exists())
            self.assertNotEqual("projectctl-session-v2", session["schema_version"])

    def test_mocked_real_session_persistence_uses_actual_schema_variant(self):
        pilot_session, pilot_matrix, pilot_artifacts = g25.load_pilot_contracts()
        mocked_matrix = copy.deepcopy(pilot_matrix)
        for identity in mocked_matrix["frozen_rendered_inputs"].values():
            identity["input_token_count"] = 3
            identity["input_token_ids_sha256"] = g25.canonical_hash([11, 12, 13])

        def provider(selection, ceiling):
            classification = g25._synthetic_class(
                "common-384", selection["instance_id"], ceiling
            )
            return self.actual_evidence(
                selection, ceiling, mocked_matrix, classification
            )

        with tempfile.TemporaryDirectory() as directory, patch.object(
            g25, "load_pilot_contracts",
            return_value=(pilot_session, mocked_matrix, pilot_artifacts),
        ):
            root, verdict, audit = g25.write_qualification_session(
                Path(directory), "mocked-real", provider,
                synthetic=False, gpu_used=True,
                decode_output_ids=lambda _ids: "#### 42",
            )
            session = json.loads((root / "session.json").read_text())
            self.assertFalse(session["synthetic"])
            self.assertTrue(session["gpu_used"])
            self.assertEqual("qualification_execution_complete", session["status"])
            self.assertEqual(384, verdict["selected_common_ceiling"])
            self.assertEqual("complete", audit["status"])

    def test_audit_rejects_deleted_duplicate_and_mutated_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _verdict, _audit = g25.write_synthetic_session(
                Path(directory), "g25-synth", "common-384"
            )
            raw = next((root / "raw").glob("*.json"))
            raw.write_text("{}\n", encoding="utf-8")
            audit = g25.audit_session(root)
            self.assertEqual("failed", audit["status"])
            self.assertGreater(audit["finding_count"], 0)

    def test_audit_rejects_inventory_session_and_ledger_adversaries(self):
        mutations = ("delete_raw", "extra_raw", "delete_cell", "session", "ledger")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root, _verdict, _audit = g25.write_synthetic_session(
                    Path(directory), "g25-synth", "common-384"
                )
                if mutation == "delete_raw":
                    next((root / "raw").glob("*.json")).unlink()
                elif mutation == "extra_raw":
                    (root / "raw" / "extra.json").write_text("{}\n", encoding="utf-8")
                elif mutation == "delete_cell":
                    next((root / "cells").glob("*.json")).unlink()
                elif mutation == "session":
                    session = json.loads((root / "session.json").read_text())
                    session["artifacts"]["ledger"] = "0" * 64
                    g25._write_json(root / "session.json", session)
                else:
                    ledger = json.loads((root / "ledger.json").read_text())
                    ledger["cells"][0]["qualification_class"] = "TIMEOUT"
                    ledger["cell_set_sha256"] = g25.canonical_hash(ledger["cells"])
                    g25._write_json(root / "ledger.json", ledger)
                    session = json.loads((root / "session.json").read_text())
                    session["artifacts"]["ledger"] = g25.sha256_file(root / "ledger.json")
                    g25._write_json(root / "session.json", session)
                    with self.assertRaises(ValueError):
                        g25.replay_session(root)
                audit = g25.audit_session(root)
                self.assertEqual("failed", audit["status"])

    def test_projectctl_start_requires_all_hash_bound_application_inputs(self):
        parser = projectctl.build_parser()
        args = parser.parse_args([
            "qualification", "synthetic-run", "--session", "synth",
            "--scenario", "common-384",
        ])
        self.assertEqual("qualification", args.area)
        with self.assertRaises(SystemExit):
            parser.parse_args(["qualification", "start"])
        start = parser.parse_args([
            "qualification", "start",
            "--approval-record", "/approval.json",
            "--review-record", "/review.md",
            "--evaluation-record", "/evaluation.json",
            "--review-tag", "review-tag",
            "--model-snapshot", "/model",
        ])
        self.assertEqual("start", start.qualification_action)
        self.assertFalse(hasattr(start, "session"))


if __name__ == "__main__":
    unittest.main()
