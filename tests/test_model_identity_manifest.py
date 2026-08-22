"""Pure-CPU tests for the pinned model identity manifest generator."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from measurement import model_identity_manifest as identity
from measurement.probes import serv_p0_25_arrival_driver as arrival_driver


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")


def _spec(path: Path) -> identity.FileContract:
    return identity.FileContract(
        path=path.name,
        bytes=path.stat().st_size,
        sha256=identity.sha256_file(path),
    )


def _tiny_snapshot(
    root: Path,
) -> tuple[Path, identity.ModelContract, dict[str, object]]:
    model_path = root / "tiny-model"
    model_path.mkdir(parents=True)
    config = {
        "architectures": ["TinyMixtralForCausalLM"],
        "model_type": "mixtral",
        "torch_dtype": "bfloat16",
    }
    (model_path / "config.json").write_bytes(_json_bytes(config))

    shard_payloads = (b"tiny-shard-alpha", b"tiny-shard-beta", b"tiny-shard-gamma")
    shard_paths: list[Path] = []
    for index, payload in enumerate(shard_payloads, start=1):
        shard = model_path / f"model-{index:05d}-of-00003.safetensors"
        shard.write_bytes(payload)
        shard_paths.append(shard)
    weight_map = {
        f"model.layers.{index}.weight": shard.name
        for index, shard in enumerate(shard_paths)
    }
    index_path = model_path / "model.safetensors.index.json"
    index_path.write_bytes(_json_bytes({"metadata": {}, "weight_map": weight_map}))
    contract = identity.ModelContract(
        model_id="example/tiny-mixtral",
        revision="1" * 40,
        config=_spec(model_path / "config.json"),
        safetensors_index=_spec(index_path),
        shards=tuple(_spec(path) for path in shard_paths),
        config_values=tuple(config.items()),
    )
    return model_path, contract, config


def _build(model_path: Path, contract: identity.ModelContract) -> dict[str, object]:
    return identity.build_manifest(
        model_path,
        model_id=contract.model_id,
        revision=contract.revision,
        contract=contract,
    )


def test_tiny_snapshot_passes_and_checksum_is_canonical(tmp_path: Path) -> None:
    model_path, contract, _ = _tiny_snapshot(tmp_path)
    manifest = _build(model_path, contract)

    assert manifest["verification_status"] == "PASS"
    assert manifest["safetensor_shard_count"] == 3
    assert manifest["safetensor_bytes"] == sum(spec.bytes for spec in contract.shards)
    assert [row["path"] for row in manifest["safetensor_shards"]] == [
        spec.path for spec in contract.shards
    ]
    expected = hashlib.sha256(
        identity.canonical_json_bytes(manifest["checksum_manifest"])
    ).hexdigest()
    assert manifest["checksum_manifest_sha256"] == expected
    assert manifest["checksum_manifest_canonicalization"] == identity.CANONICALIZATION


def test_checksum_manifest_does_not_depend_on_local_path(tmp_path: Path) -> None:
    left_path, left_contract, _ = _tiny_snapshot(tmp_path / "left")
    right_path, right_contract, _ = _tiny_snapshot(tmp_path / "right")

    left = _build(left_path, left_contract)
    right = _build(right_path, right_contract)
    assert left["model_path"] != right["model_path"]
    assert left["checksum_manifest"] == right["checksum_manifest"]
    assert left["checksum_manifest_sha256"] == right["checksum_manifest_sha256"]


@pytest.mark.parametrize("field", ["model_id", "revision"])
def test_wrong_pinned_identity_is_rejected(tmp_path: Path, field: str) -> None:
    model_path, contract, _ = _tiny_snapshot(tmp_path)
    kwargs = {"model_id": contract.model_id, "revision": contract.revision}
    kwargs[field] = "wrong-value"

    with pytest.raises(identity.ModelIdentityError, match=f"{field} mismatch"):
        identity.build_manifest(model_path, contract=contract, **kwargs)


def test_missing_shard_is_rejected(tmp_path: Path) -> None:
    model_path, contract, _ = _tiny_snapshot(tmp_path)
    (model_path / contract.shards[1].path).unlink()

    with pytest.raises(identity.ModelIdentityError, match="shard set mismatch"):
        _build(model_path, contract)


def test_extra_shard_is_rejected(tmp_path: Path) -> None:
    model_path, contract, _ = _tiny_snapshot(tmp_path)
    (model_path / "unregistered.safetensors").write_bytes(b"extra")

    with pytest.raises(identity.ModelIdentityError, match="shard set mismatch"):
        _build(model_path, contract)


def test_duplicate_shard_index_is_rejected(tmp_path: Path) -> None:
    model_path, contract, _ = _tiny_snapshot(tmp_path)
    (model_path / "model-1-of-3.safetensors").write_bytes(b"duplicate-index")

    with pytest.raises(identity.ModelIdentityError, match="duplicate safetensor shard"):
        _build(model_path, contract)


def test_same_size_corrupted_shard_is_rejected_by_sha256(tmp_path: Path) -> None:
    model_path, contract, _ = _tiny_snapshot(tmp_path)
    shard = model_path / contract.shards[0].path
    original = shard.read_bytes()
    shard.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    with pytest.raises(identity.ModelIdentityError, match="SHA-256 mismatch"):
        _build(model_path, contract)


def test_wrong_config_is_rejected_before_pass(tmp_path: Path) -> None:
    model_path, contract, config = _tiny_snapshot(tmp_path)
    config["torch_dtype"] = "float16"
    config_path = model_path / "config.json"
    config_path.write_bytes(_json_bytes(config))
    # Bind the changed bytes into a test-only file contract so this assertion
    # specifically exercises semantic config validation, not only its digest.
    contract = dataclasses.replace(contract, config=_spec(config_path))

    with pytest.raises(identity.ModelIdentityError, match="config.json torch_dtype"):
        _build(model_path, contract)


def test_wrong_index_shard_set_is_rejected(tmp_path: Path) -> None:
    model_path, contract, _ = _tiny_snapshot(tmp_path)
    index_path = model_path / "model.safetensors.index.json"
    index_path.write_bytes(
        _json_bytes(
            {
                "weight_map": {
                    "only.tensor": contract.shards[0].path,
                    "unknown.tensor": "unknown.safetensors",
                }
            }
        )
    )
    contract = dataclasses.replace(contract, safetensors_index=_spec(index_path))

    with pytest.raises(identity.ModelIdentityError, match="index shard set mismatch"):
        _build(model_path, contract)


def test_output_is_accepted_by_target5_identity_validator(tmp_path: Path) -> None:
    model_path, tiny_contract, _ = _tiny_snapshot(tmp_path)
    compatible_contract = dataclasses.replace(
        tiny_contract,
        model_id=arrival_driver.MODEL_ID,
        revision=arrival_driver.MODEL_REVISION,
    )
    output = tmp_path / "model_identity.json"
    identity.write_manifest(output, _build(model_path, compatible_contract))

    validated = arrival_driver.validate_model_identity(
        output,
        model_path,
        contract=compatible_contract,
    )
    assert validated["verification_status"] == "PASS"
    assert validated["checksum_manifest_sha256"] == json.loads(
        output.read_text(encoding="utf-8")
    )["checksum_manifest_sha256"]


def test_cli_writes_pass_only_after_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    model_path, contract, _ = _tiny_snapshot(tmp_path)
    monkeypatch.setattr(identity, "CANONICAL_CONTRACT", contract)
    output = tmp_path / "identity.json"
    argv = [
        "--model-path",
        str(model_path),
        "--model-id",
        contract.model_id,
        "--revision",
        contract.revision,
        "--output",
        str(output),
    ]

    assert identity.main(argv) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "PASS"
    assert json.loads(output.read_text(encoding="utf-8"))["verification_status"] == "PASS"


def test_cli_failure_does_not_create_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    model_path, contract, _ = _tiny_snapshot(tmp_path)
    monkeypatch.setattr(identity, "CANONICAL_CONTRACT", contract)
    output = tmp_path / "identity.json"
    argv = [
        "--model-path",
        str(model_path),
        "--model-id",
        contract.model_id,
        "--revision",
        "2" * 40,
        "--output",
        str(output),
    ]

    assert identity.main(argv) == 1
    printed = json.loads(capsys.readouterr().err)
    assert printed["classification"] == "MODEL_IDENTITY_VERIFICATION_FAILED"
    assert not output.exists()


def test_manifest_writer_refuses_to_overwrite_prior_attempt(tmp_path: Path) -> None:
    output = tmp_path / "identity.json"
    output.write_text("prior\n", encoding="utf-8")

    with pytest.raises(identity.ModelIdentityError, match="fresh manifest"):
        identity.write_manifest(output, {"verification_status": "PASS"})
    assert output.read_text(encoding="utf-8") == "prior\n"


def test_existing_manifest_validation_recomputes_checksum_and_path(tmp_path: Path) -> None:
    model_path, contract, _ = _tiny_snapshot(tmp_path)
    manifest = _build(model_path, contract)
    assert identity.validate_manifest(
        manifest,
        expected_model_path=model_path,
        verify_files=True,
        contract=contract,
    ) is manifest

    forged = json.loads(json.dumps(manifest))
    forged["checksum_manifest"]["files"][2]["sha256"] = "f" * 64
    forged["checksum_manifest_sha256"] = hashlib.sha256(
        identity.canonical_json_bytes(forged["checksum_manifest"])
    ).hexdigest()
    with pytest.raises(identity.ModelIdentityError, match="frozen contract"):
        identity.validate_manifest(forged, contract=contract)


def test_existing_manifest_validation_rejects_wrong_model_path(tmp_path: Path) -> None:
    model_path, contract, _ = _tiny_snapshot(tmp_path)
    manifest = _build(model_path, contract)
    with pytest.raises(identity.ModelIdentityError, match="path mismatch"):
        identity.validate_manifest(
            manifest,
            expected_model_path=tmp_path / "other-model",
            contract=contract,
        )
