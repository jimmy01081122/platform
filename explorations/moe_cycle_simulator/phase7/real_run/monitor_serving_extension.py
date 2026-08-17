#!/usr/bin/env python3
"""Wait for one remote serving extension, back it up, and run fixed reviews."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any


def run_command(argv: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        capture_output=capture,
    )


def remote_file_exists(target: str, port: int, path: str) -> bool:
    command = f"test -f {shlex.quote(path)}"
    result = run_command(["ssh", "-p", str(port), target, command])
    return result.returncode == 0


def write_state(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssh-target", required=True)
    parser.add_argument("--ssh-port", type=int, default=2222)
    parser.add_argument("--remote-run-dir", type=str, required=True)
    parser.add_argument("--remote-log", type=str, required=True)
    parser.add_argument("--local-run-dir", type=Path, required=True)
    parser.add_argument("--local-log", type=Path, required=True)
    parser.add_argument("--parent-run-dir", type=Path, required=True)
    parser.add_argument("--stream-review-script", type=Path, required=True)
    parser.add_argument("--stream-review-output", type=Path, required=True)
    parser.add_argument("--open-review-script", type=Path, required=True)
    parser.add_argument("--open-review-output", type=Path, required=True)
    parser.add_argument("--expected-request-count", type=int, required=True)
    parser.add_argument("--poll-seconds", type=int, default=45)
    parser.add_argument("--bootstrap-resamples", type=int, default=10000)
    parser.add_argument("--state-output", type=Path, required=True)
    args = parser.parse_args()

    if args.poll_seconds < 1 or args.expected_request_count < 1:
        raise SystemExit("poll interval and expected request count must be positive")
    args.local_run_dir.parent.mkdir(parents=True, exist_ok=True)
    args.local_log.parent.mkdir(parents=True, exist_ok=True)
    args.state_output.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {
        "schema_version": "phase7-serving-extension-monitor-v1",
        "status": "WAITING_REMOTE_COMPLETION",
        "remote_run_dir": args.remote_run_dir,
        "local_run_dir": str(args.local_run_dir.resolve()),
        "expected_request_count": args.expected_request_count,
        "poll_seconds": args.poll_seconds,
    }
    write_state(args.state_output, state)

    while not remote_file_exists(args.ssh_target, args.ssh_port, f"{args.remote_run_dir}/result.json"):
        time.sleep(args.poll_seconds)

    state["status"] = "BACKING_UP"
    write_state(args.state_output, state)
    copy_run = run_command(
        [
            "scp",
            "-P",
            str(args.ssh_port),
            "-r",
            f"{args.ssh_target}:{args.remote_run_dir}",
            str(args.local_run_dir.parent),
        ]
    )
    copy_log = run_command(
        [
            "scp",
            "-P",
            str(args.ssh_port),
            f"{args.ssh_target}:{args.remote_log}",
            str(args.local_log),
        ]
    )
    state["backup"] = {
        "run_returncode": copy_run.returncode,
        "log_returncode": copy_log.returncode,
        "run_stderr": copy_run.stderr[-2000:],
        "log_stderr": copy_log.stderr[-2000:],
    }
    if copy_run.returncode != 0 or copy_log.returncode != 0:
        state["status"] = "BACKUP_FAILED"
        write_state(args.state_output, state)
        return 1

    state["status"] = "REVIEWING"
    write_state(args.state_output, state)
    stream = run_command(
        [
            "python3",
            str(args.stream_review_script),
            "--base-run-dir",
            str(args.parent_run_dir),
            "--extension-run-dir",
            str(args.local_run_dir),
            "--prefix-count",
            "1000",
            "--output",
            str(args.stream_review_output),
        ]
    )
    review = run_command(
        [
            "python3",
            str(args.open_review_script),
            "--run-dir",
            str(args.local_run_dir),
            "--expected-request-count",
            str(args.expected_request_count),
            "--output",
            str(args.open_review_output),
            "--bootstrap-resamples",
            str(args.bootstrap_resamples),
            "--require-stable-p99-ci",
        ]
    )
    state["review"] = {
        "stream_returncode": stream.returncode,
        "stream_stdout": stream.stdout[-4000:],
        "stream_stderr": stream.stderr[-2000:],
        "open_loop_returncode": review.returncode,
        "open_loop_stdout": review.stdout[-6000:],
        "open_loop_stderr": review.stderr[-2000:],
    }
    state["status"] = (
        "REVIEW_COMPLETE"
        if stream.returncode == 0 and review.returncode == 0
        else "REVIEW_COMPLETE_WITH_FAILURE"
    )
    write_state(args.state_output, state)
    return 0 if state["status"] == "REVIEW_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
