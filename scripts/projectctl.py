#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "AGENTS.md", "README.md", "project/charter.yaml",
    "configs/platforms/discrete_edge_workstation.yaml",
    "configs/platforms/embedded_integrated_cpu_gpu.yaml",
    "schemas/manifest.schema.json", "schemas/metrics.schema.json",
    "schemas/simulation_config.schema.json", "schemas/calibration_pack.schema.json",
    "schemas/evidence.schema.json", "schemas/validation_gates.schema.json",
    "configs/fidelity/simulation_modes.yaml", "configs/fidelity/default_simulation.yaml",
    "configs/fidelity/evidence_taxonomy.yaml", "configs/fidelity/validation_gates.yaml",
    "configs/calibration/calibration_pack.template.yaml",
    "experiments/templates/experiment.yaml", "runs",
]
OPTIONAL_TOOLS = [
    "git", "docker", "podman", "gem5", "verilator", "iverilog",
    "yosys", "openroad", "sby", "spike", "qemu-system-riscv64",
]


def run_git(args: list[str]) -> str | None:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def doctor(_: argparse.Namespace) -> int:
    missing = [p for p in REQUIRED if not (ROOT / p).exists()]
    print(f"workspace: {ROOT}")
    print(f"python: {sys.version.split()[0]}")
    print(f"git_commit: {run_git(['rev-parse', 'HEAD']) or 'unavailable'}")
    print("optional_tools:")
    for tool in OPTIONAL_TOOLS:
        print(f"  {tool}: {shutil.which(tool) or 'not found'}")
    if missing:
        print("missing_required_paths:")
        for path in missing:
            print(f"  {path}")
        return 2
    print("workspace_contract: pass")
    return 0


def validate_id(value: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if not value or any(c not in allowed for c in value):
        raise ValueError("ID may contain only letters, numbers, underscore, and hyphen")
    return value


def new_exp(args: argparse.Namespace) -> int:
    exp_id = validate_id(args.experiment_id)
    src = ROOT / "experiments/templates/experiment.yaml"
    dst = ROOT / "experiments/specs" / f"{exp_id}.yaml"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and not args.force:
        print(f"exists: {dst}", file=sys.stderr)
        return 2
    text = src.read_text(encoding="utf-8").replace("experiment_id: replace_me", f"experiment_id: {exp_id}", 1)
    dst.write_text(text, encoding="utf-8")
    print(dst)
    return 0


def git_state() -> dict:
    commit = run_git(["rev-parse", "HEAD"])
    status = run_git(["status", "--porcelain"])
    return {"commit": commit, "dirty": bool(status), "status_porcelain": status or ""}


def init_run(args: argparse.Namespace) -> int:
    exp_id = validate_id(args.experiment_id)
    stage = args.stage.upper()
    if stage not in {f"S{i}" for i in range(8)}:
        print("stage must be S0 through S7", file=sys.stderr)
        return 2
    platform = validate_id(args.platform)
    spec = ROOT / "experiments/specs" / f"{exp_id}.yaml"
    if not spec.exists():
        print(f"missing experiment spec: {spec}", file=sys.stderr)
        return 2
    platform_file = ROOT / "configs/platforms" / f"{platform}.yaml"
    if not platform_file.exists():
        print(f"missing platform profile: {platform_file}", file=sys.stderr)
        return 2
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}__{exp_id}__{stage}"
    run_dir = ROOT / "runs" / run_id
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "artifacts").mkdir()
    (run_dir / "environment").mkdir()
    resolved = (
        "# Experiment spec\n" + spec.read_text(encoding="utf-8") +
        "\n# Platform profile\n" + platform_file.read_text(encoding="utf-8")
    )
    (run_dir / "resolved_config.yaml").write_text(resolved, encoding="utf-8")
    manifest = {
        "run_id": run_id,
        "experiment_id": exp_id,
        "stage": stage,
        "platform_profile": platform,
        "created_at": now.isoformat(),
        "git": git_state(),
        "command": sys.argv,
        "status": "initialized",
        "parameter_sources": [],
        "parent_runs": [],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (run_dir / "metrics.json").write_text(json.dumps({"schema_version": 1, "metrics": {}, "units": {}, "confidence": {}, "failure_classification": None}, indent=2) + "\n", encoding="utf-8")
    (run_dir / "logs/command.log").write_text(" ".join(sys.argv) + "\n", encoding="utf-8")
    (run_dir / "logs/stdout.log").write_text("", encoding="utf-8")
    (run_dir / "logs/stderr.log").write_text("", encoding="utf-8")
    tools = {"python": sys.version, "detected": {t: shutil.which(t) for t in OPTIONAL_TOOLS}}
    (run_dir / "environment/tool_versions.json").write_text(json.dumps(tools, indent=2) + "\n", encoding="utf-8")
    print(run_dir)
    return 0


def check_run(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir
    required = [
        "manifest.json", "resolved_config.yaml", "metrics.json",
        "logs/command.log", "logs/stdout.log", "logs/stderr.log",
        "environment/tool_versions.json", "artifacts",
    ]
    missing = [p for p in required if not (run_dir / p).exists()]
    for json_file in ["manifest.json", "metrics.json", "environment/tool_versions.json"]:
        path = run_dir / json_file
        if path.exists():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"invalid_json: {path}: {exc}", file=sys.stderr)
                return 2
    if missing:
        print("missing:")
        for p in missing:
            print(f"  {p}")
        return 2
    print(f"run_contract: pass: {run_dir}")
    return 0


def summary(_: argparse.Namespace) -> int:
    runs = ROOT / "runs"
    rows = []
    for manifest_path in sorted(runs.glob("*/manifest.json")):
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            rows.append((m.get("run_id"), m.get("stage"), m.get("platform_profile"), m.get("status")))
        except Exception:
            rows.append((manifest_path.parent.name, "?", "?", "invalid_manifest"))
    if not rows:
        print("no runs")
        return 0
    print("run_id\tstage\tplatform\tstatus")
    for row in rows:
        print("\t".join(str(x) for x in row))
    return 0


def validate_configs(_: argparse.Namespace) -> int:
    errors = []
    schema_paths = list((ROOT / "schemas").glob("*.schema.json"))
    schemas = {}
    for path in schema_paths:
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
            schemas[path.name] = schema
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    try:
        import jsonschema
        import yaml
    except ImportError as exc:
        errors.append(f"contract validation dependencies missing: {exc}")
    else:
        store = {schema.get("$id", path_name): schema for path_name, schema in schemas.items()}
        store.update(schemas)
        for path_name, schema in schemas.items():
            try:
                jsonschema.Draft7Validator.check_schema(schema)
            except Exception as exc:
                errors.append(f"{ROOT / 'schemas' / path_name}: invalid schema: {exc}")

        instances = [
            ("configs/fidelity/default_simulation.yaml", "simulation_config.schema.json"),
            ("configs/calibration/calibration_pack.template.yaml", "calibration_pack.schema.json"),
            ("configs/calibration/phase1_discrete_moe.yaml", "calibration_pack.schema.json"),
            ("configs/fidelity/validation_gates.yaml", "validation_gates.schema.json"),
        ]
        for config_rel, schema_name in instances:
            config_path = ROOT / config_rel
            try:
                instance = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                schema = schemas[schema_name]
                resolver = jsonschema.RefResolver(
                    base_uri=(ROOT / "schemas").as_uri() + "/",
                    referrer=schema,
                    store=store,
                )
                jsonschema.Draft7Validator(schema, resolver=resolver).validate(instance)
            except Exception as exc:
                errors.append(f"{config_path}: schema validation failed: {exc}")

        try:
            taxonomy = yaml.safe_load((ROOT / "configs/fidelity/evidence_taxonomy.yaml").read_text(encoding="utf-8"))
            if taxonomy["closed_enum"] != schemas["evidence.schema.json"]["enum"]:
                errors.append("evidence taxonomy does not exactly match evidence.schema.json")
        except Exception as exc:
            errors.append(f"evidence taxonomy validation failed: {exc}")
    for path in (ROOT / "configs/platforms").glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        if "profile_id:" not in text or "parameter_policy:" not in text:
            errors.append(f"{path}: missing required profile markers")
    if errors:
        print("configuration_validation: fail")
        for error in errors:
            print(f"  {error}")
        return 2
    print("configuration_validation: pass")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Workspace experiment control utility")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor")
    d.set_defaults(func=doctor)
    n = sub.add_parser("new-exp")
    n.add_argument("experiment_id")
    n.add_argument("--force", action="store_true")
    n.set_defaults(func=new_exp)
    i = sub.add_parser("init-run")
    i.add_argument("experiment_id")
    i.add_argument("--stage", required=True)
    i.add_argument("--platform", required=True)
    i.set_defaults(func=init_run)
    c = sub.add_parser("check-run")
    c.add_argument("run_dir")
    c.set_defaults(func=check_run)
    s = sub.add_parser("summary")
    s.set_defaults(func=summary)
    v = sub.add_parser("validate-configs")
    v.set_defaults(func=validate_configs)
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
