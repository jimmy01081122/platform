#!/usr/bin/env python3
"""Render a candidate-only GPU/model compatibility and capacity-boundary plan."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml


def load_yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a mapping")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--gpu-profile", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    profiles = load_yaml(root / "configs/gpu_profiles.yaml")
    registry = load_yaml(root / "configs/model_registry.yaml")
    compatibility = load_yaml(root / "configs/model_compatibility.yaml")

    profile = profiles.get("profiles", {}).get(args.gpu_profile)
    if not isinstance(profile, dict):
        parser.error(f"unknown GPU profile: {args.gpu_profile}")
    aliases = compatibility.get("profile_aliases", {})
    compatibility_id = (
        args.gpu_profile
        if args.gpu_profile in compatibility.get("gpu_profiles", {})
        else aliases.get(args.gpu_profile)
    )
    template = compatibility.get("gpu_profiles", {}).get(compatibility_id)
    if not isinstance(template, dict):
        raise SystemExit(
            f"no explicit compatibility mapping for GPU profile {args.gpu_profile}"
        )

    models_by_tier: dict[str, list[dict]] = {
        tier: [] for tier in ("M0", "M1", "M2", "M3")
    }
    for model_key, model in registry.get("models", {}).items():
        tier = model.get("tier")
        if tier in models_by_tier:
            models_by_tier[tier].append({
                "model_key": model_key,
                "model_id": model.get("model_id"),
                "revision": model.get("revision"),
                "identity_resolved": bool(
                    model.get("model_id") and model.get("revision")
                ),
                "precision_candidates": [
                    item.get("precision_id")
                    for item in model.get("precision_candidates", [])
                ],
            })

    gates = compatibility.get("confirmation_contract", {}).get("gates", {})
    policy = profile.get("model_policy", {})
    tiers = {}
    for tier in ("M0", "M1", "M2", "M3"):
        tier_policy = policy.get(tier, {})
        fallback = template.get("fallback", {}).get(tier, [])
        tiers[tier] = {
            "models": models_by_tier[tier],
            "candidate_mode": tier_policy.get("default"),
            "fallback_modes": tier_policy.get("fallback", []),
            "candidate_fidelities": fallback,
            "gates": gates,
            "capacity_boundary_plan": {
                "retain_failed_candidate": True,
                "required_evidence": [
                    "allocation_or_runtime_failure",
                    "exact_configuration",
                    "failure_log",
                    "fallback_selected_or_terminal_status",
                ],
            },
            "status": "candidate_plan_only",
        }

    document = {
        "schema_version": "compatibility-plan-v2",
        "status": "degraded_plan_only",
        "execution_claimed": False,
        "measurement_claimed": False,
        "gpu_profile_id": args.gpu_profile,
        "compatibility_template": compatibility_id,
        "exact_sku": profile.get("exact_sku"),
        "tiers": tiers,
        "exit_semantics": {
            "code": 10,
            "meaning": "compatibility plan emitted; no model runner executed",
        },
    }
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 10


if __name__ == "__main__":
    raise SystemExit(main())
