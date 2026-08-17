#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from moe_sim_phase3 import Engine, EngineError  # noqa: E402


def new_engine(library: Path) -> Engine:
    return Engine(
        library,
        phase2_ledger_sha256="1" * 64,
        canonical_bundle_semantic_root="2" * 64,
        engine_build_sha256="3" * 64,
        engine_profile_sha256="4" * 64,
        checkpoint_schema_sha256="5" * 64,
    )


def build(library: Path, reverse: bool) -> str:
    with new_engine(library) as engine:
        engine.add_resource("gpu0", 1)
        events = [
            ("acquire", 10, 80, 0, (), "request-1"),
            ("service", 20, 100, 2, ("acquire",), "request-1"),
            ("release", 30, 10, 1, ("service",), "request-1"),
        ]
        if reverse:
            events.reverse()
        for event_id, time_fs, priority, action, dependencies, owner in events:
            engine.add_event(
                event_id,
                time_fs,
                priority,
                action,
                "gpu0",
                owner,
                dependencies=dependencies,
            )
        engine.finalize()
        assert engine.run() == 1  # TerminalStatus::kQuiescent
        return engine.state_digest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", type=Path, required=True)
    args = parser.parse_args()
    assert build(args.library, False) == build(args.library, True)
    with new_engine(args.library) as engine:
        engine.add_resource("unsupported", 1, resource_kind=5)
        engine.add_event(
            "bad", 1, 90, 3, "unsupported", "request-1"
        )
        try:
            engine.finalize()
        except EngineError as error:
            assert "UNSUPPORTED_PHASE4_RESOURCE" in str(error)
        else:
            raise AssertionError("unsupported Phase 4 resource was accepted")
    print("PHASE3_PYTHON_BINDING_TEST: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
