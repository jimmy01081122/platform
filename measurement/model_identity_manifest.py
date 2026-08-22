#!/usr/bin/env python3
"""Build a fail-closed identity manifest for the pinned TRACK_GPU Mixtral model.

The production contract is intentionally embedded in this stdlib-only tool.  A
``PASS`` manifest is written only after the repository/revision arguments,
``config.json``, safetensors index, and every one of the 19 shards match the
known files at the pinned Hugging Face revision.

``checksum_manifest_sha256`` is SHA-256 over UTF-8 JSON produced with
``sort_keys=True``, ``ensure_ascii=False`` and separators ``(",", ":")``.  The
hashed ``checksum_manifest`` object is included verbatim in the output so a
consumer can recompute the digest without filesystem access.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence


SCHEMA_VERSION = "track-gpu-model-identity-v1"
CHECKSUM_SCHEMA_VERSION = "track-gpu-model-checksum-manifest-v1"
CANONICALIZATION = (
    "utf8-json-sort-keys-ensure-ascii-false-separators-comma-colon-no-newline"
)
MODEL_ID = "mistralai/Mixtral-8x7B-Instruct-v0.1"
MODEL_REVISION = "eba92302a2861cdc0098cc54bc9f17cb2c47eb61"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHARD_LIKE_RE = re.compile(r"^model-(\d+)-of-(\d+)\.safetensors$")


class ModelIdentityError(RuntimeError):
    """A mismatch that prevents a verified model identity from being emitted."""


@dataclasses.dataclass(frozen=True)
class FileContract:
    path: str
    bytes: int
    sha256: str


@dataclasses.dataclass(frozen=True)
class ModelContract:
    model_id: str
    revision: str
    config: FileContract
    safetensors_index: FileContract
    shards: tuple[FileContract, ...]
    config_values: tuple[tuple[str, Any], ...]

    def validate(self) -> None:
        if not self.model_id or not self.revision:
            raise ModelIdentityError("internal model contract has an empty identity")
        if not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise ModelIdentityError("internal model contract revision is not a commit SHA")
        if not self.shards:
            raise ModelIdentityError("internal model contract has no shards")
        expected_names = tuple(
            f"model-{index:05d}-of-{len(self.shards):05d}.safetensors"
            for index in range(1, len(self.shards) + 1)
        )
        observed_names = tuple(spec.path for spec in self.shards)
        if observed_names != expected_names:
            raise ModelIdentityError(
                "internal shard contract is not the exact ordered shard sequence"
            )
        all_specs = (self.config, self.safetensors_index, *self.shards)
        paths = [spec.path for spec in all_specs]
        if len(paths) != len(set(paths)):
            raise ModelIdentityError("internal file contract contains duplicate paths")
        shard_hashes = [spec.sha256 for spec in self.shards]
        if len(shard_hashes) != len(set(shard_hashes)):
            raise ModelIdentityError("internal file contract contains duplicate shards")
        for spec in all_specs:
            if Path(spec.path).name != spec.path or not spec.path:
                raise ModelIdentityError(
                    f"internal file contract path is not a root basename: {spec.path!r}"
                )
            if isinstance(spec.bytes, bool) or not isinstance(spec.bytes, int) or spec.bytes < 0:
                raise ModelIdentityError(
                    f"internal file contract has invalid byte count: {spec.path}"
                )
            if not SHA256_RE.fullmatch(spec.sha256):
                raise ModelIdentityError(
                    f"internal file contract has invalid SHA-256: {spec.path}"
                )


def _file(path: str, size: int, sha256: str) -> FileContract:
    return FileContract(path=path, bytes=size, sha256=sha256)


# SHA-256 values are the LFS object digests returned for the immutable revision;
# sizes were independently observed in the downloaded snapshot.  Matching the
# full table makes the revision check content-backed rather than an argv claim.
CANONICAL_CONTRACT = ModelContract(
    model_id=MODEL_ID,
    revision=MODEL_REVISION,
    config=_file(
        "config.json",
        720,
        "9d56d04b36d0fd12ff54ae4c5bac769cc176e254e64ff71144614b6318b40793",
    ),
    safetensors_index=_file(
        "model.safetensors.index.json",
        92_658,
        "a8f30ebfaf569d5cc6358a32009342a3e73d4553a340cc38a6319457d9dc13e6",
    ),
    shards=(
        _file(
            "model-00001-of-00019.safetensors",
            4_892_809_584,
            "54669c5aec29fe5e4edd8098f7b564a137ba36be22ad25a194cd93f2bb54c940",
        ),
        _file(
            "model-00002-of-00019.safetensors",
            4_983_004_016,
            "29e15364d8ab1d6ee229233381f295e9ff96237efed04750591f7da52ab6cc0e",
        ),
        _file(
            "model-00003-of-00019.safetensors",
            4_983_004_016,
            "d0b63fca793cc29421cc5a46851992975cbe083aaded1b2f31113a45a0c90954",
        ),
        _file(
            "model-00004-of-00019.safetensors",
            4_899_035_200,
            "67e0596920fe543415c0191e867a9a7de942a2924d6277cd98c8c5b34e11e436",
        ),
        _file(
            "model-00005-of-00019.safetensors",
            4_983_004_016,
            "e330eabd70b467ddcbd8d3d6b2b9c3eba66655b0ed9f84e19f270da3623dc455",
        ),
        _file(
            "model-00006-of-00019.safetensors",
            4_983_004_016,
            "048fa5347877b6d04eccf69765d23e5561cc9820dd4d0e5ba2df0100204dfb04",
        ),
        _file(
            "model-00007-of-00019.safetensors",
            4_899_035_248,
            "83bfed6169c1f5b0ae854fb3311b576d06209ee5af45d7d46bcbc25098a4d02b",
        ),
        _file(
            "model-00008-of-00019.safetensors",
            4_983_004_072,
            "af316ad784027edba47bf0959c821682c931c9c901d3d755038b358d9c7a28c0",
        ),
        _file(
            "model-00009-of-00019.safetensors",
            4_983_004_072,
            "5882e4366c63048a0ad36ef6d90194a2fabdb42a2140be79c8e0ec2e8ac2ccc5",
        ),
        _file(
            "model-00010-of-00019.safetensors",
            4_899_035_248,
            "77813d1dbee63419226ac15e4b8f28d075c3f7921cc664090236c491667eaf29",
        ),
        _file(
            "model-00011-of-00019.safetensors",
            4_983_004_072,
            "ff24540d9967fe43c0c17cadaea7f2a34d080a2f9e58b913038b9bfd0bf8ca49",
        ),
        _file(
            "model-00012-of-00019.safetensors",
            4_983_004_072,
            "48bc12845676eab1adb3cfce7037a7ecd664a0d5f5deaf93c7362a5bb5173298",
        ),
        _file(
            "model-00013-of-00019.safetensors",
            4_983_004_072,
            "e56a2e7eda699bf4ec1433bd07d7cb86488420813e66463d2e2296d7accebc5c",
        ),
        _file(
            "model-00014-of-00019.safetensors",
            4_899_035_248,
            "da627f6a3c8fdc6e35b9918d2aa53704d4044191fcc86c7c0b1ac57f00e707f7",
        ),
        _file(
            "model-00015-of-00019.safetensors",
            4_983_004_072,
            "61e0f22bff93a68e114dbc3d75c1dd1e6687d554dba0cfdf1743950aa04ff1cf",
        ),
        _file(
            "model-00016-of-00019.safetensors",
            4_983_004_072,
            "76466bfc2312f11559480981f212e4cca6e98096bf8df0fd90cce1f0f4709a9c",
        ),
        _file(
            "model-00017-of-00019.safetensors",
            4_899_035_248,
            "570af3b802bedc0d54d0481d124f63d449dda40a4294a82a39f8dc3704057a5c",
        ),
        _file(
            "model-00018-of-00019.safetensors",
            4_983_004_072,
            "4c603b65cbd5ddadcd5ece8add68b9d47f98f7264dbb0a5313172c78491e0329",
        ),
        _file(
            "model-00019-of-00019.safetensors",
            4_221_679_088,
            "272f33c76bcacf6cfced497dc0579e107de3874b9f93126f5e69b5b1ae7e72a0",
        ),
    ),
    config_values=(
        ("architectures", ["MixtralForCausalLM"]),
        ("model_type", "mixtral"),
        ("torch_dtype", "bfloat16"),
        ("hidden_size", 4096),
        ("intermediate_size", 14336),
        ("num_hidden_layers", 32),
        ("num_attention_heads", 32),
        ("num_key_value_heads", 8),
        ("num_local_experts", 8),
        ("num_experts_per_tok", 2),
        ("vocab_size", 32000),
        ("max_position_embeddings", 32768),
    ),
)


def canonical_json_bytes(value: Any) -> bytes:
    """Return the exact byte representation used by the checksum manifest."""

    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ModelIdentityError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModelIdentityError(f"JSON object contains duplicate key {key!r}")
        result[key] = value
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except ModelIdentityError:
        raise
    except Exception as exc:
        raise ModelIdentityError(
            f"cannot parse {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ModelIdentityError(f"expected a JSON object in {path}")
    return value


def _verify_file(model_path: Path, spec: FileContract) -> dict[str, Any]:
    path = model_path / spec.path
    if not path.is_file():
        raise ModelIdentityError(f"required model file is missing: {spec.path}")
    try:
        observed_size = path.stat().st_size
    except OSError as exc:
        raise ModelIdentityError(f"cannot stat {path}: {exc}") from exc
    if observed_size != spec.bytes:
        raise ModelIdentityError(
            f"{spec.path} byte-size mismatch: expected {spec.bytes}, got {observed_size}"
        )
    observed_sha256 = sha256_file(path)
    if observed_sha256 != spec.sha256:
        raise ModelIdentityError(
            f"{spec.path} SHA-256 mismatch: expected {spec.sha256}, got {observed_sha256}"
        )
    return {"path": spec.path, "bytes": observed_size, "sha256": observed_sha256}


def _discover_safetensors(model_path: Path) -> tuple[str, ...]:
    try:
        return tuple(
            sorted(
                path.relative_to(model_path).as_posix()
                for path in model_path.rglob("*.safetensors")
            )
        )
    except OSError as exc:
        raise ModelIdentityError(
            f"cannot enumerate safetensors below {model_path}: {exc}"
        ) from exc


def _validate_exact_shard_set(
    model_path: Path, expected: Sequence[FileContract]
) -> None:
    observed = _discover_safetensors(model_path)
    expected_paths = tuple(spec.path for spec in expected)

    shard_indices: dict[int, str] = {}
    for relative in observed:
        path = Path(relative)
        match = SHARD_LIKE_RE.fullmatch(path.name)
        if path.parent != Path(".") or match is None:
            continue
        shard_index = int(match.group(1))
        previous = shard_indices.get(shard_index)
        if previous is not None:
            raise ModelIdentityError(
                f"duplicate safetensor shard index {shard_index}: {previous}, {relative}"
            )
        shard_indices[shard_index] = relative

    missing = sorted(set(expected_paths) - set(observed))
    extra = sorted(set(observed) - set(expected_paths))
    if missing or extra or len(observed) != len(set(observed)):
        raise ModelIdentityError(
            "safetensor shard set mismatch: "
            f"missing={missing}, extra={extra}, observed_count={len(observed)}"
        )


def _validate_config(config: dict[str, Any], contract: ModelContract) -> None:
    for key, expected in contract.config_values:
        observed = config.get(key)
        if observed != expected:
            raise ModelIdentityError(
                f"config.json {key}: expected {expected!r}, got {observed!r}"
            )


def _validate_safetensors_index(
    index: dict[str, Any], expected_shards: Sequence[FileContract]
) -> None:
    weight_map = index.get("weight_map")
    if not isinstance(weight_map, dict) or not weight_map:
        raise ModelIdentityError("safetensors index weight_map must be a non-empty object")
    if any(not isinstance(name, str) or not name for name in weight_map):
        raise ModelIdentityError("safetensors index contains an invalid tensor name")
    shard_values = list(weight_map.values())
    if any(not isinstance(value, str) for value in shard_values):
        raise ModelIdentityError("safetensors index contains a non-string shard path")
    observed = set(shard_values)
    expected = {spec.path for spec in expected_shards}
    if observed != expected:
        raise ModelIdentityError(
            "safetensors index shard set mismatch: "
            f"missing={sorted(expected - observed)}, extra={sorted(observed - expected)}"
        )


def build_manifest(
    model_path: Path,
    *,
    model_id: str,
    revision: str,
    contract: ModelContract = CANONICAL_CONTRACT,
) -> dict[str, Any]:
    """Verify a snapshot and return a PASS manifest; never returns partial state."""

    contract.validate()
    if model_id != contract.model_id:
        raise ModelIdentityError(
            f"model_id mismatch: expected {contract.model_id!r}, got {model_id!r}"
        )
    if revision != contract.revision:
        raise ModelIdentityError(
            f"revision mismatch: expected {contract.revision!r}, got {revision!r}"
        )
    if not model_path.is_dir():
        raise ModelIdentityError(f"model path is not a directory: {model_path}")

    _validate_exact_shard_set(model_path, contract.shards)
    config_record = _verify_file(model_path, contract.config)
    config = _read_json_object(model_path / contract.config.path)
    _validate_config(config, contract)
    index_record = _verify_file(model_path, contract.safetensors_index)
    index = _read_json_object(model_path / contract.safetensors_index.path)
    _validate_safetensors_index(index, contract.shards)
    shard_records = [_verify_file(model_path, spec) for spec in contract.shards]

    checksum_manifest = {
        "schema_version": CHECKSUM_SCHEMA_VERSION,
        "model_id": model_id,
        "revision": revision,
        "files": [config_record, index_record, *shard_records],
    }
    checksum_manifest_sha256 = hashlib.sha256(
        canonical_json_bytes(checksum_manifest)
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "model_id": model_id,
        "revision": revision,
        "verification_status": "PASS",
        "model_path": str(model_path.resolve()),
        "config": config_record,
        "verified_config_values": dict(contract.config_values),
        "safetensors_index": index_record,
        "safetensor_shard_count": len(shard_records),
        "safetensor_bytes": sum(record["bytes"] for record in shard_records),
        "safetensor_shards": shard_records,
        "checksum_manifest_canonicalization": CANONICALIZATION,
        "checksum_manifest": checksum_manifest,
        "checksum_manifest_sha256": checksum_manifest_sha256,
    }


def validate_manifest(
    manifest: Any,
    *,
    expected_model_path: Path | None = None,
    verify_files: bool = False,
    contract: ModelContract = CANONICAL_CONTRACT,
) -> dict[str, Any]:
    """Validate an existing PASS manifest and optionally rehash its snapshot.

    The cheap path authenticates every embedded file row against the immutable
    contract and recomputes ``checksum_manifest_sha256``.  ``verify_files=True``
    additionally performs the expensive full on-disk hash pass and requires a
    byte-identical identity payload.  This split lets each measurement attempt
    pin a previously generated full manifest without silently trusting a PASS
    string, while a preflight can deliberately pay the ~93-GB verification cost.
    """

    contract.validate()
    if not isinstance(manifest, dict):
        raise ModelIdentityError("model identity manifest must be a JSON object")
    expected_path = expected_model_path.resolve() if expected_model_path else None
    expected_files = [
        dataclasses.asdict(spec)
        for spec in (contract.config, contract.safetensors_index, *contract.shards)
    ]
    expected_checksum_manifest = {
        "schema_version": CHECKSUM_SCHEMA_VERSION,
        "model_id": contract.model_id,
        "revision": contract.revision,
        "files": expected_files,
    }
    checks: tuple[tuple[str, Any], ...] = (
        ("schema_version", SCHEMA_VERSION),
        ("model_id", contract.model_id),
        ("revision", contract.revision),
        ("verification_status", "PASS"),
        ("checksum_manifest_canonicalization", CANONICALIZATION),
        ("checksum_manifest", expected_checksum_manifest),
        ("config", expected_files[0]),
        ("safetensors_index", expected_files[1]),
        ("safetensor_shards", expected_files[2:]),
        ("safetensor_shard_count", len(contract.shards)),
        ("safetensor_bytes", sum(spec.bytes for spec in contract.shards)),
        ("verified_config_values", dict(contract.config_values)),
    )
    for key, expected in checks:
        if manifest.get(key) != expected:
            raise ModelIdentityError(
                f"model identity manifest {key} differs from the frozen contract"
            )
    checksum = hashlib.sha256(
        canonical_json_bytes(manifest["checksum_manifest"])
    ).hexdigest()
    if manifest.get("checksum_manifest_sha256") != checksum:
        raise ModelIdentityError(
            "model identity checksum_manifest_sha256 does not match its payload"
        )
    model_path_value = manifest.get("model_path")
    if not isinstance(model_path_value, str) or not Path(model_path_value).is_absolute():
        raise ModelIdentityError("model identity manifest model_path is not absolute")
    manifest_path = Path(model_path_value).resolve()
    if expected_path is not None and manifest_path != expected_path:
        raise ModelIdentityError(
            f"model identity path mismatch: expected {expected_path}, got {manifest_path}"
        )
    if verify_files:
        rebuilt = build_manifest(
            manifest_path,
            model_id=contract.model_id,
            revision=contract.revision,
            contract=contract,
        )
        for key, _expected in checks:
            if rebuilt.get(key) != manifest.get(key):
                raise ModelIdentityError(
                    f"rehash result differs from manifest field {key}"
                )
        if rebuilt["checksum_manifest_sha256"] != checksum:
            raise ModelIdentityError("rehash result differs from checksum manifest")
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Write a new manifest without overwriting a prior attempt."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
    except OSError as exc:
        raise ModelIdentityError(f"cannot write fresh manifest {path}: {exc}") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = build_manifest(
            args.model_path,
            model_id=args.model_id,
            revision=args.revision,
            contract=CANONICAL_CONTRACT,
        )
        write_manifest(args.output, manifest)
    except KeyboardInterrupt:
        print(
            json.dumps({"status": "FAIL", "classification": "INTERRUPTED"}),
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "classification": "MODEL_IDENTITY_VERIFICATION_FAILED",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": "PASS",
                "output": str(args.output),
                "model_id": manifest["model_id"],
                "revision": manifest["revision"],
                "safetensor_shard_count": manifest["safetensor_shard_count"],
                "checksum_manifest_sha256": manifest["checksum_manifest_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
