"""Pure-CPU, fail-closed tests for the frozen target_5 arrival driver."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
from pathlib import Path

import pytest

from measurement import model_identity_manifest
from measurement.parsers import serving_tail_parser
from measurement.probes import serv_p0_25_arrival_driver as driver


TEST_CONTRACT = dataclasses.replace(driver.TARGET5, requests=4)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _file_contract(path: Path) -> model_identity_manifest.FileContract:
    return model_identity_manifest.FileContract(
        path=path.name,
        bytes=path.stat().st_size,
        sha256=model_identity_manifest.sha256_file(path),
    )


@dataclasses.dataclass(frozen=True)
class SmallModelFixture:
    model_path: Path
    identity_path: Path
    contract: model_identity_manifest.ModelContract


@pytest.fixture
def small_model(tmp_path: Path) -> SmallModelFixture:
    """A tiny exact-file contract exercised by the production verifier."""

    model_path = tmp_path / "model"
    model_path.mkdir()
    config_path = model_path / "config.json"
    index_path = model_path / "model.safetensors.index.json"
    shard_path = model_path / "model-00001-of-00001.safetensors"
    _write_json(config_path, {})
    _write_json(
        index_path,
        {"weight_map": {"model.layers.0.fake.weight": shard_path.name}},
    )
    shard_path.write_bytes(b"small-test-safetensor-payload")
    contract = model_identity_manifest.ModelContract(
        model_id=driver.MODEL_ID,
        revision=driver.MODEL_REVISION,
        config=_file_contract(config_path),
        safetensors_index=_file_contract(index_path),
        shards=(_file_contract(shard_path),),
        config_values=(),
    )
    manifest = model_identity_manifest.build_manifest(
        model_path,
        model_id=driver.MODEL_ID,
        revision=driver.MODEL_REVISION,
        contract=contract,
    )
    identity_path = tmp_path / "model_identity.json"
    _write_json(identity_path, manifest)
    return SmallModelFixture(model_path, identity_path, contract)


@pytest.fixture
def config(tmp_path: Path, small_model: SmallModelFixture) -> driver.DriverConfig:
    paths = driver.default_paths()
    return driver.DriverConfig(
        attempt_id="SERV-P0-25-TAIL-20260822T000000Z-TEST",
        run_root=tmp_path / "runs",
        model_path=small_model.model_path,
        model_identity_json=small_model.identity_path,
        serving_runner=paths["serving_runner"],
        gpu_campaign_runner=paths["gpu_campaign_runner"],
        archived_gpu_campaign_runner=paths["archived_gpu_campaign_runner"],
        python_executable=Path("/fake/python3"),
    )


class FakeClock:
    def __init__(self) -> None:
        self.utc_calls = 0
        self.monotonic_calls = 0

    def utc_now(self) -> str:
        self.utc_calls += 1
        return f"2026-08-22T00:00:0{self.utc_calls}Z"

    def monotonic_ns(self) -> int:
        self.monotonic_calls += 1
        return self.monotonic_calls * 1_000_000


class FakeServer:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls: list[Path] = []

    def verify(self, execution_runner: Path) -> dict[str, object]:
        self.calls.append(execution_runner)
        if self.reject:
            raise driver.ArrivalDriverError("fake server capability rejected")
        return {
            "server_ownership": "fake_embedded_server",
            "external_server_reused": False,
        }


class FakeRunner:
    """Emit the actual serving_burst_runner schemas without importing GPU code."""

    def __init__(
        self,
        contract: driver.Target5Contract,
        *,
        tamper: str | None = None,
        returncode: int = 0,
    ) -> None:
        self.contract = contract
        self.tamper = tamper
        self.returncode = returncode
        self.calls: list[dict[str, object]] = []

    @staticmethod
    def _arg(argv: list[str], name: str) -> str:
        return argv[argv.index(name) + 1]

    def run(
        self,
        argv,
        *,
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> int:
        argv = list(argv)
        self.calls.append(
            {
                "argv": argv,
                "cwd": cwd,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
            }
        )
        stdout_path.write_text("fake runner started\n", encoding="utf-8")
        if self.returncode:
            stderr_path.write_text("fake failure", encoding="utf-8")
            return self.returncode

        run_root = Path(self._arg(argv, "--run-root"))
        experiment_id = self._arg(argv, "--experiment-id")
        model_path = self._arg(argv, "--model-path")
        raw = run_root / f"20260822T000000Z__{experiment_id}"
        raw.mkdir()
        contract = self.contract
        variables = {
            "model_path": model_path,
            "run_root": str(run_root),
            "experiment_id": experiment_id,
            "input_tokens": contract.input_tokens,
            "output_tokens": contract.output_tokens,
            "sampling_mode": contract.sampling_mode,
            "request_plan": None,
            "concurrency": contract.concurrency,
            "bursts": 1,
            "intra_burst_gap_ms": 0.0,
            "inter_burst_gap_ms": 0.0,
            "arrival_rate_rps": contract.arrival_rate_rps,
            "total_requests": contract.requests,
            "arrival_seed": contract.arrival_seed,
            "warmup_burst": True,
            "warmup_bursts": contract.warmup_bursts,
            "max_num_seqs": contract.max_num_seqs,
            "max_num_batched_tokens": contract.max_num_batched_tokens,
            "gpu_memory_utilization": contract.gpu_memory_utilization,
        }
        manifest_model_path = (
            "/tampered/model" if self.tamper == "manifest_model_path" else model_path
        )
        _write_json(
            raw / "manifest.json",
            {
                "schema_version": "phase7-serving-manifest-v2",
                "status": "RUNNING",
                "experiment_id": experiment_id,
                "runtime_class": "SERVING_VARIANT",
                "model_path": manifest_model_path,
                "model_revision": driver.MODEL_REVISION,
                "variables": variables,
                "sampling_mode": contract.sampling_mode,
                "arrival_mode": "POISSON_OPEN_LOOP",
                "arrival_contract": {
                    "rate_rps": contract.arrival_rate_rps,
                    "total_requests": contract.requests,
                    "seed": contract.arrival_seed,
                },
            },
        )

        prompt_ids = [100 + (index % 23) for index in range(contract.input_tokens)]
        prompt_hash = driver.sha256_json(prompt_ids)
        fixture_slots = [
            {
                "slot": index,
                "class": "homogeneous",
                "input_tokens": contract.input_tokens,
                "output_tokens": contract.output_tokens,
                "token_count": len(prompt_ids),
                "token_ids": prompt_ids,
                "token_ids_sha256": prompt_hash,
            }
            for index in range(contract.concurrency)
        ]
        if self.tamper == "fixture_hash":
            fixture_slots[0]["token_ids_sha256"] = "f" * 64
        _write_json(
            raw / "input_fixture.json",
            {
                "fixture_id": "phase7_mixtral_serving_repeated_anchor_v1",
                "request_plan": fixture_slots,
            },
        )

        request_plan = [
            {
                "slot": index,
                "class": "homogeneous",
                "input_tokens": contract.input_tokens,
                "output_tokens": contract.output_tokens,
            }
            for index in range(contract.concurrency)
        ]
        sampling = {
            "n": 1,
            "temperature": 0.0,
            "top_p": 1.0,
            "top_k": 0,
            "seed": 0,
            "stop": [],
            "ignore_eos": False,
            "detokenize": True,
            "skip_special_tokens": False,
            "max_tokens": contract.output_tokens,
            "min_tokens": 0,
        }
        engine_repr = (
            "AsyncEngineArgs("
            f"model='{model_path}', dtype='bfloat16', tensor_parallel_size=1, "
            "pipeline_parallel_size=1, max_model_len=32768, "
            f"max_num_seqs={contract.max_num_seqs}, "
            f"max_num_batched_tokens={contract.max_num_batched_tokens}, "
            f"gpu_memory_utilization={contract.gpu_memory_utilization}, "
            "enforce_eager=True, enable_prefix_caching=False)"
        )
        _write_json(
            raw / "requested_engine_args.json",
            {
                "engine_args": engine_repr,
                "sampling_params_by_slot": [
                    dict(sampling) for _ in range(contract.concurrency)
                ],
                "sampling_mode": contract.sampling_mode,
                "request_plan": request_plan,
                "model_revision": driver.MODEL_REVISION,
            },
        )

        rng = random.Random(contract.arrival_seed)
        offset_ns = 0
        trace: list[dict[str, object]] = []
        records: list[dict[str, object]] = []
        schedule_start_ns = 10_000_000_000
        output_ids = [200 + index for index in range(contract.output_tokens)]
        output_hash = driver.sha256_json(output_ids)
        for ordinal in range(contract.requests):
            if ordinal:
                offset_ns += int(
                    rng.expovariate(contract.arrival_rate_rps) * 1_000_000_000
                )
            arrival_index = ordinal + (1 if self.tamper == "index_shift" else 0)
            request_id = f"open-{arrival_index:06d}"
            scheduled_ns = schedule_start_ns + offset_ns
            trace_offset = offset_ns + (
                1 if self.tamper == "arrival_offset" and ordinal == 2 else 0
            )
            trace_scheduled = scheduled_ns + (
                1 if self.tamper == "schedule_sum" and ordinal == 2 else 0
            )
            trace.append(
                {
                    "schema_version": "phase7-serving-arrival-v1",
                    "arrival_mode": "POISSON_OPEN_LOOP",
                    "arrival_seed": contract.arrival_seed,
                    "arrival_rate_rps": contract.arrival_rate_rps,
                    "arrival_index": arrival_index,
                    "request_id": request_id,
                    "slot": ordinal % contract.concurrency,
                    "class": "homogeneous",
                    "input_tokens": contract.input_tokens,
                    "output_tokens": contract.output_tokens,
                    "scheduled_offset_ns": trace_offset,
                    "scheduled_monotonic_ns": trace_scheduled,
                }
            )
            observed_ns = scheduled_ns + 10
            first_ns = observed_ns + 20 + ordinal
            completed_ns = observed_ns + 1_000_000 + ordinal * 100_000
            row_output_ids = list(output_ids)
            if self.tamper == "output_count" and ordinal == 2:
                row_output_ids.pop()
            records.append(
                {
                    "schema_version": "phase7-serving-request-v1",
                    "request_id": request_id,
                    "input_tokens": contract.input_tokens,
                    "requested_output_tokens": contract.output_tokens,
                    "output_tokens": contract.output_tokens,
                    "input_ids_sha256": (
                        "e" * 64
                        if self.tamper == "input_hash" and ordinal == 2
                        else prompt_hash
                    ),
                    "output_ids_sha256": (
                        "d" * 64
                        if self.tamper == "output_hash" and ordinal == 2
                        else output_hash
                    ),
                    "output_token_ids": row_output_ids,
                    "output_text": "fake",
                    "submitted_monotonic_ns": observed_ns,
                    "client_scheduled_arrival_monotonic_ns": scheduled_ns + (
                        1 if self.tamper == "client_schedule" and ordinal == 2 else 0
                    ),
                    "server_observed_arrival_monotonic_ns": observed_ns,
                    "arrival_index": arrival_index,
                    "first_yield_monotonic_ns": first_ns,
                    "completed_monotonic_ns": completed_ns,
                    "ttft_ns": first_ns - observed_ns,
                    "completion_latency_ns": completed_ns - observed_ns,
                    "decode_updates": contract.output_tokens,
                    "finish_reason": "length",
                    "error": None,
                    "sampling_mode": contract.sampling_mode,
                }
            )

        request_rows = json.loads(json.dumps(records))
        if self.tamper == "row_divergence":
            request_rows[2]["output_text"] = "different"
        _write_jsonl(raw / "arrival_trace.jsonl", trace)
        _write_jsonl(raw / "requests.jsonl", request_rows)

        warmup_rows = []
        for index in range(contract.concurrency):
            row = json.loads(json.dumps(records[index % len(records)]))
            row["request_id"] = f"warmup-0000-{index:04d}"
            row["arrival_index"] = None
            row["client_scheduled_arrival_monotonic_ns"] = row[
                "submitted_monotonic_ns"
            ]
            warmup_rows.append(row)
        _write_jsonl(raw / "warmup_requests.jsonl", warmup_rows)
        _write_jsonl(raw / "telemetry.jsonl", [{"event": "pre"}, {"event": "post"}])
        _write_json(
            raw / "result.json",
            {
                "schema_version": "phase7-serving-result-v2",
                "arrival_mode": "POISSON_OPEN_LOOP",
                "arrival_rate_rps": contract.arrival_rate_rps,
                "arrival_seed": contract.arrival_seed,
                "status": "PASS",
                "records": records,
                "completed_request_count": contract.requests,
                "requested_request_count": contract.requests,
            },
        )
        _write_json(raw / "status.json", {"status": "PASS"})
        stdout_path.write_text("fake runner started\nfake runner PASS\n", encoding="utf-8")
        return 0


class InterruptRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(
        self,
        argv,
        *,
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> int:
        self.calls += 1
        with stdout_path.open("a", encoding="utf-8") as handle:
            handle.write("durable partial stdout\n")
            handle.flush()
        with stderr_path.open("a", encoding="utf-8") as handle:
            handle.write("durable partial stderr\n")
            handle.flush()
        raise KeyboardInterrupt


def _execute_test(
    config: driver.DriverConfig,
    small_model: SmallModelFixture,
    **kwargs,
) -> Path:
    return driver._execute_with_contract(
        config,
        contract=TEST_CONTRACT,
        model_contract=small_model.contract,
        **kwargs,
    )


def test_frozen_runner_argv_has_exact_target5_domain(config):
    argv = driver.build_runner_argv(config, Path("/fresh/raw"))
    joined = " ".join(argv)
    assert argv[1] == "-u"
    assert "--arrival-rate-rps 1.0472460793856333" in joined
    assert "--total-requests 10000" in joined
    assert "--arrival-seed 20260812" in joined
    assert "--concurrency 8" in joined
    assert "--input-tokens 128" in joined
    assert "--output-tokens 32" in joined
    assert "--max-num-seqs 8" in joined
    assert "--max-num-batched-tokens 1024" in joined
    assert "--sampling-mode NATURAL_EOS_CAPPED" in joined
    assert "--warmup-burst" in argv
    assert "resume" not in joined.lower()
    assert "start-request" not in joined.lower()


def test_driver_cpu_exact_runner_clock_and_server_pass(config, small_model):
    fake_runner = FakeRunner(TEST_CONTRACT)
    fake_clock = FakeClock()
    fake_server = FakeServer()
    attempt_dir = _execute_test(
        config,
        small_model,
        clock=fake_clock,
        process_runner=fake_runner,
        server_contract=fake_server,
    )
    assert fake_runner.calls
    assert fake_server.calls == [config.serving_runner]
    assert fake_clock.utc_calls >= 3
    assert fake_clock.monotonic_calls == 2
    summary = json.loads((attempt_dir / "serving_tail.json").read_text())
    serving_tail_parser.validate(summary)
    assert summary["attempt_id"] == config.attempt_id
    assert summary["start_request_index"] == 0
    assert summary["resumed_partial_progress"] is False
    assert summary["planned_requests"] == 4
    assert summary["completed_requests"] == 4
    assert summary["completion_latency"] == {
        "p50_ms": 1.15,
        "p95_ms": 1.285,
        "p99_ms": 1.297,
        "max_ms": 1.3,
    }
    assert len(summary["canonical_request_records_sha256"]) == 64
    manifest = json.loads((attempt_dir / "manifest.json").read_text())
    assert manifest["status"] == "PASS"
    runtime = json.loads(
        (attempt_dir / "environment" / "runtime_identity.json").read_text()
    )
    assert runtime["schema_version"] == "serv-p0-25-runtime-identity-v1"
    assert runtime["code_lineage"]["execution_runner"]["sha256"] == (
        driver.EXPECTED_SERVING_RUNNER_SHA256
    )
    assert (attempt_dir / "logs" / "stdout.log").read_text().endswith(
        "fake runner PASS\n"
    )
    assert (attempt_dir / "ATTEMPT_SHA256SUMS").is_file()


def test_driver_refuses_existing_attempt_instead_of_resuming(config, small_model):
    existing = config.run_root / config.attempt_id
    existing.mkdir(parents=True)
    fake_runner = FakeRunner(TEST_CONTRACT)
    with pytest.raises(driver.ArrivalDriverError, match="resume is forbidden"):
        _execute_test(
            config,
            small_model,
            process_runner=fake_runner,
            server_contract=FakeServer(),
        )
    assert not fake_runner.calls


def test_driver_streams_nonzero_runner_logs_and_preserves_attempt(config, small_model):
    with pytest.raises(driver.ArrivalDriverError, match="returned 7"):
        _execute_test(
            config,
            small_model,
            process_runner=FakeRunner(TEST_CONTRACT, returncode=7),
            server_contract=FakeServer(),
        )
    attempt = config.run_root / config.attempt_id
    manifest = json.loads((attempt / "manifest.json").read_text())
    assert manifest["status"] == "FAIL"
    assert manifest["failure_classification"] == "ARRIVAL_DRIVER_FAIL_CLOSED"
    assert manifest["runner_returncode"] == 7
    assert (attempt / "logs" / "stdout.log").read_text() == "fake runner started\n"
    assert (attempt / "logs" / "stderr.log").read_text() == "fake failure"
    assert (attempt / "failure.json").is_file()
    assert (attempt / "ATTEMPT_SHA256SUMS").is_file()


def test_driver_preserves_partial_streamed_logs_on_interrupt(config, small_model):
    runner = InterruptRunner()
    with pytest.raises(KeyboardInterrupt):
        _execute_test(
            config,
            small_model,
            process_runner=runner,
            server_contract=FakeServer(),
        )
    attempt = config.run_root / config.attempt_id
    assert runner.calls == 1
    assert "durable partial stdout" in (attempt / "logs" / "stdout.log").read_text()
    assert "durable partial stderr" in (attempt / "logs" / "stderr.log").read_text()
    manifest = json.loads((attempt / "manifest.json").read_text())
    assert manifest["status"] == "FAIL"
    assert manifest["failure_classification"] == "INTERRUPTED"
    assert manifest["runner_returncode"] is None
    sums = (attempt / "ATTEMPT_SHA256SUMS").read_text()
    assert "logs/stdout.log" in sums
    assert "logs/stderr.log" in sums


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("index_shift", r"arrival\[0\].index"),
        ("arrival_offset", r"arrival\[2\].offset"),
        ("schedule_sum", r"arrival\[2\].scheduled_monotonic_ns"),
        ("client_schedule", r"client_scheduled_arrival_monotonic_ns"),
        ("output_hash", r"output_ids_sha256"),
        ("output_count", r"output_token_ids length"),
        ("input_hash", r"input_ids_sha256"),
        ("fixture_hash", r"token_ids_sha256"),
        ("row_divergence", r"canonical row hashes"),
        ("manifest_model_path", r"raw manifest model_path"),
    ],
)
def test_driver_rejects_broken_raw_chain(config, small_model, tamper, message):
    with pytest.raises(driver.ArrivalDriverError, match=message):
        _execute_test(
            config,
            small_model,
            process_runner=FakeRunner(TEST_CONTRACT, tamper=tamper),
            server_contract=FakeServer(),
        )
    manifest = json.loads(
        (config.run_root / config.attempt_id / "manifest.json").read_text()
    )
    assert manifest["status"] == "FAIL"


def test_driver_rejects_server_contract_before_runner_dispatch(config, small_model):
    fake_runner = FakeRunner(TEST_CONTRACT)
    with pytest.raises(driver.ArrivalDriverError, match="fake server capability"):
        _execute_test(
            config,
            small_model,
            process_runner=fake_runner,
            server_contract=FakeServer(reject=True),
        )
    assert not fake_runner.calls


def test_model_identity_recomputes_embedded_manifest_digest(config, small_model):
    identity = json.loads(config.model_identity_json.read_text())
    identity["checksum_manifest"]["files"][0]["sha256"] = "0" * 64
    _write_json(config.model_identity_json, identity)
    with pytest.raises(driver.ArrivalDriverError, match="embedded checksum_manifest"):
        driver.validate_model_identity(
            config.model_identity_json,
            config.model_path,
            contract=small_model.contract,
        )


def test_model_identity_rejects_self_consistent_but_noncanonical_manifest(
    config, small_model
):
    identity = json.loads(config.model_identity_json.read_text())
    identity["checksum_manifest"]["files"][0]["sha256"] = "0" * 64
    identity["checksum_manifest_sha256"] = hashlib.sha256(
        model_identity_manifest.canonical_json_bytes(identity["checksum_manifest"])
    ).hexdigest()
    _write_json(config.model_identity_json, identity)
    with pytest.raises(driver.ArrivalDriverError, match="fresh canonical on-disk"):
        driver.validate_model_identity(
            config.model_identity_json,
            config.model_path,
            contract=small_model.contract,
        )


def test_model_identity_rehashes_files_on_disk(config, small_model):
    shard = config.model_path / "model-00001-of-00001.safetensors"
    shard.write_bytes(shard.read_bytes() + b"tamper")
    with pytest.raises(driver.ArrivalDriverError, match="on-disk model identity"):
        driver.validate_model_identity(
            config.model_identity_json,
            config.model_path,
            contract=small_model.contract,
        )


def test_model_identity_requires_exact_model_path(config, small_model):
    identity = json.loads(config.model_identity_json.read_text())
    identity["model_path"] = "/different/model"
    _write_json(config.model_identity_json, identity)
    with pytest.raises(driver.ArrivalDriverError, match="model_path"):
        driver.validate_model_identity(
            config.model_identity_json,
            config.model_path,
            contract=small_model.contract,
        )


def test_code_lineage_rejects_modified_campaign_runner(config, small_model, tmp_path):
    modified = tmp_path / "gpu_campaign_runner.py"
    modified.write_text("# modified\n", encoding="utf-8")
    altered = dataclasses.replace(config, gpu_campaign_runner=modified)
    fake_runner = FakeRunner(TEST_CONTRACT)
    with pytest.raises(driver.ArrivalDriverError, match="SHA-256 mismatch"):
        _execute_test(
            altered,
            small_model,
            process_runner=fake_runner,
            server_contract=FakeServer(),
        )
    assert not fake_runner.calls
