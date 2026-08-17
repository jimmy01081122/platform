#!/usr/bin/env python3
"""Run the remaining frozen-stream serving extensions sequentially.

The queue starts the next GPU run only after the preceding run has produced a
remote result, has been copied locally, and has completed both review scripts.
Review failure is recorded and does not silently become a pass; it also does
not leave the GPU idle before the next independent condition is measured.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TASKS = (
    ("P0-25", "20260811T195121Z__SERV-P0-25-SHORT-C8-NATURAL-V1", 1.0472460793856333),
    ("P0-50", "20260811T200818Z__SERV-P0-50-SHORT-C8-NATURAL-V1", 2.0944921587712665),
    ("P0-75", "20260811T201710Z__SERV-P0-75-SHORT-C8-NATURAL-V1", 3.1417382381568997),
    ("P1-40", "20260811T202346Z__SERV-P1-40-SHORT-C8-NATURAL-V1", 1.6755937270170132),
)


def run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=False, text=True, capture_output=True)


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def wait_for_review(state_path: Path, poll_seconds: int) -> dict[str, Any]:
    terminal = {"REVIEW_COMPLETE", "REVIEW_COMPLETE_WITH_FAILURE", "BACKUP_FAILED"}
    while True:
        state = read_json(state_path)
        if state and state.get("status") in terminal:
            return state
        time.sleep(poll_seconds)


def make_run_id(condition: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}__SERV-{condition}-SHORT-C8-NATURAL-EXT10K-V1"


def launch_remote(
    *,
    target: str,
    port: int,
    runner: str,
    model_path: str,
    run_root: str,
    log_root: str,
    run_id: str,
    arrival_rate_rps: float,
) -> subprocess.CompletedProcess[str]:
    remote_run_dir = f"{run_root}/{run_id}"
    remote_log = f"{log_root}/{run_id}.log"
    runner_args = [
        "python3",
        runner,
        "--model-path",
        model_path,
        "--run-root",
        run_root,
        "--experiment-id",
        run_id,
        "--input-tokens",
        "128",
        "--output-tokens",
        "32",
        "--sampling-mode",
        "NATURAL_EOS_CAPPED",
        "--concurrency",
        "8",
        "--bursts",
        "1",
        "--arrival-rate-rps",
        repr(arrival_rate_rps),
        "--total-requests",
        "10000",
        "--arrival-seed",
        "20260812",
        "--warmup-burst",
        "--max-num-seqs",
        "8",
        "--max-num-batched-tokens",
        "1024",
        "--gpu-memory-utilization",
        "0.97",
    ]
    rendered = " ".join(shlex.quote(item) for item in runner_args)
    remote_command = (
        f"mkdir -p {shlex.quote(run_root)} {shlex.quote(log_root)} && "
        f"nohup {rendered} > {shlex.quote(remote_log)} 2>&1 < /dev/null & echo $!"
    )
    return run_command(["ssh", "-p", str(port), target, remote_command])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--current-state", type=Path, required=True)
    parser.add_argument("--queue-state", type=Path, required=True)
    parser.add_argument("--ssh-target", required=True)
    parser.add_argument("--ssh-port", type=int, default=2222)
    parser.add_argument("--remote-runner", required=True)
    parser.add_argument("--remote-model-path", required=True)
    parser.add_argument("--remote-run-root", required=True)
    parser.add_argument("--remote-log-root", required=True)
    parser.add_argument("--poll-seconds", type=int, default=45)
    parser.add_argument("--bootstrap-resamples", type=int, default=10000)
    args = parser.parse_args()

    if args.poll_seconds < 1 or args.bootstrap_resamples < 1:
        raise SystemExit("poll interval and bootstrap resamples must be positive")

    review_script = args.base_dir / "explorations/moe_cycle_simulator/phase7/real_run/review_serving_open_loop.py"
    stream_script = args.base_dir / "explorations/moe_cycle_simulator/phase7/real_run/review_serving_stream_extension.py"
    monitor_script = args.base_dir / "explorations/moe_cycle_simulator/phase7/real_run/monitor_serving_extension.py"

    queue: dict[str, Any] = {
        "schema_version": "phase7-serving-extension-queue-v1",
        "status": "WAITING_CURRENT_EXTENSION",
        "current_state": str(args.current_state),
        "poll_seconds": args.poll_seconds,
        "bootstrap_resamples": args.bootstrap_resamples,
        "tasks": [
            {
                "condition": condition,
                "parent_run": parent_run,
                "arrival_rate_rps": arrival_rate_rps,
                "status": "PENDING",
            }
            for condition, parent_run, arrival_rate_rps in TASKS
        ],
    }
    args.queue_state.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.queue_state, queue)

    current_state = wait_for_review(args.current_state, args.poll_seconds)
    queue["preceding_extension"] = {
        "state": current_state.get("status"),
        "state_path": str(args.current_state),
    }
    if current_state.get("status") == "BACKUP_FAILED":
        queue["status"] = "STOPPED_BACKUP_FAILURE"
        write_json(args.queue_state, queue)
        return 1

    for task in queue["tasks"]:
        condition = str(task["condition"])
        parent_run = str(task["parent_run"])
        rate = float(task["arrival_rate_rps"])
        suffix = condition.lower().replace("-", "_")
        run_id = make_run_id(condition)
        remote_run_dir = f"{args.remote_run_root}/{run_id}"
        remote_log = f"{args.remote_log_root}/{run_id}.log"
        local_run_dir = args.backup_root / "raw/runs" / run_id
        local_log = args.backup_root / "logs" / f"{run_id}.log"
        parent_run_dir = args.backup_root / "raw/runs" / parent_run
        stream_output = args.backup_root / f"preliminary_serving_{suffix}_stream_extension_v1.json"
        review_output = args.backup_root / f"preliminary_serving_formal_{suffix}_short_c8_natural_ext10k_v1.json"
        state_output = args.backup_root / f"preliminary_serving_{suffix}_extension_monitor_state.json"

        task.update(
            {
                "extension_run": run_id,
                "remote_run_dir": remote_run_dir,
                "status": "STARTING",
            }
        )
        queue["status"] = "STARTING_REMOTE_RUN"
        write_json(args.queue_state, queue)
        launch = launch_remote(
            target=args.ssh_target,
            port=args.ssh_port,
            runner=args.remote_runner,
            model_path=args.remote_model_path,
            run_root=args.remote_run_root,
            log_root=args.remote_log_root,
            run_id=run_id,
            arrival_rate_rps=rate,
        )
        task["launch_returncode"] = launch.returncode
        task["launch_stdout"] = launch.stdout[-2000:]
        task["launch_stderr"] = launch.stderr[-2000:]
        if launch.returncode != 0:
            task["status"] = "LAUNCH_FAILED"
            queue["status"] = "STOPPED_LAUNCH_FAILURE"
            write_json(args.queue_state, queue)
            return 1

        task["status"] = "RUNNING"
        queue["status"] = "MONITORING_REMOTE_RUN"
        write_json(args.queue_state, queue)
        monitor = run_command(
            [
                sys.executable,
                str(monitor_script),
                "--ssh-target",
                args.ssh_target,
                "--ssh-port",
                str(args.ssh_port),
                "--remote-run-dir",
                remote_run_dir,
                "--remote-log",
                remote_log,
                "--local-run-dir",
                str(local_run_dir),
                "--local-log",
                str(local_log),
                "--parent-run-dir",
                str(parent_run_dir),
                "--stream-review-script",
                str(stream_script),
                "--stream-review-output",
                str(stream_output),
                "--open-review-script",
                str(review_script),
                "--open-review-output",
                str(review_output),
                "--expected-request-count",
                "10000",
                "--poll-seconds",
                str(args.poll_seconds),
                "--bootstrap-resamples",
                str(args.bootstrap_resamples),
                "--state-output",
                str(state_output),
            ]
        )
        task["monitor_returncode"] = monitor.returncode
        task["monitor_stdout"] = monitor.stdout[-3000:]
        task["monitor_stderr"] = monitor.stderr[-2000:]
        task_state = read_json(state_output) or {}
        task["review_status"] = task_state.get("status", "MISSING")
        task["status"] = (
            "REVIEW_COMPLETE"
            if task["review_status"] == "REVIEW_COMPLETE"
            else "REVIEW_COMPLETE_WITH_FAILURE"
            if task["review_status"] == "REVIEW_COMPLETE_WITH_FAILURE"
            else "MONITOR_FAILED"
        )
        queue["status"] = "READY_FOR_NEXT" if task["status"].startswith("REVIEW_COMPLETE") else "STOPPED_MONITOR_FAILURE"
        write_json(args.queue_state, queue)
        if not task["status"].startswith("REVIEW_COMPLETE"):
            return 1

    queue["status"] = "QUEUE_COMPLETE"
    write_json(args.queue_state, queue)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
