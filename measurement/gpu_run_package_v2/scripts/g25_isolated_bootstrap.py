#!/usr/bin/env python3
"""Verify the frozen local runtime before importing any third-party module."""
from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _expose_verified_source_root() -> None:
    value = str(PACKAGE_ROOT)
    if value not in sys.path:
        sys.path.insert(0, value)


def _activate_verified_runtime(target: str, arguments: list[str]) -> None:
    if not (
        sys.flags.isolated
        and sys.flags.no_site
        and sys.flags.dont_write_bytecode
        and sys.flags.utf8_mode == 1
    ):
        raise RuntimeError("G2.5 bootstrap requires python -I -S -B -X utf8")
    _expose_verified_source_root()
    from scheduler.g25_runtime_closure import verify_current_attested_python_argv

    verify_current_attested_python_argv(target, arguments)
    from scheduler.g25_application import verify_runtime_inventory

    attestation = verify_runtime_inventory(
        verify_record_files=True,
        verify_exact_trees=True,
        require_isolated=True,
    )
    from scheduler.g25_runtime_closure import verify_live_loaded_closure

    verify_live_loaded_closure("bootstrap")
    runtime_root = attestation["runtime_root"]
    if runtime_root in sys.path:
        raise RuntimeError("G2.5 runtime was importable before verification")
    sys.path.insert(1, runtime_root)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        raise SystemExit("isolated bootstrap target is required")
    target = arguments.pop(0)
    if target == "worker":
        # This module is stdlib-only.  Install the kernel parent-death guard and
        # prove the inherited execution lease before runtime verification,
        # third-party imports, CUDA discovery, or model work.
        _expose_verified_source_root()
        from scheduler.g25_worker_lifetime import (
            install_parent_death_guard_from_environment,
        )

        install_parent_death_guard_from_environment()
    _activate_verified_runtime(target, arguments)
    if target == "projectctl":
        from scripts.projectctl import main as target_main
        from scheduler.g25_runtime_closure import verify_live_loaded_closure

        verify_live_loaded_closure("parent_imported")
    elif target == "worker":
        from scripts.g25_worker import main as target_main
        from scheduler.g25_runtime_closure import verify_live_loaded_closure

        verify_live_loaded_closure("worker_imported")
    elif target == "bf16-probe":
        if arguments:
            raise SystemExit("bf16-probe accepts no arguments")
        import torch
        from scheduler.g25_runtime_closure import verify_live_loaded_closure

        verify_live_loaded_closure("parent_preflight")
        print(int(torch.cuda.is_bf16_supported()))
        return 0
    else:
        raise SystemExit(f"unsupported isolated bootstrap target: {target}")
    return int(target_main(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
