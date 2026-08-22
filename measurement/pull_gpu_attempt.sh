#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: $0 <ssh-host> <ssh-port> <remote-attempt-dir> <local-attempt-dir>" >&2
    echo "example: $0 root@ssh1.vast.ai 21629 /workspace/runs/ATTEMPT /home/a/platform/runs/ATTEMPT" >&2
}

if [[ $# -ne 4 ]]; then
    usage
    exit 2
fi

ssh_host=$1
ssh_port=$2
remote_dir=${3%/}
local_dir=${4%/}

if [[ ! $ssh_port =~ ^[0-9]+$ ]]; then
    echo "invalid SSH port: $ssh_port" >&2
    exit 2
fi
if [[ ! $remote_dir =~ ^/workspace/runs/[A-Za-z0-9._-]+$ ]]; then
    echo "remote attempt must be one direct child of /workspace/runs: $remote_dir" >&2
    exit 2
fi
if [[ ! $local_dir =~ ^/home/a/platform/runs/[A-Za-z0-9._-]+$ ]]; then
    echo "local attempt must be one direct child of /home/a/platform/runs: $local_dir" >&2
    exit 2
fi

mkdir -p "$local_dir"

# Create the transport manifest only after the attempt has stopped writing.
# SHA256SUMS excludes itself and is stored alongside the raw attempt so both
# sides retain the exact transfer contract. No source file is changed.
ssh -p "$ssh_port" "$ssh_host" \
    "test -d '$remote_dir' && cd '$remote_dir' && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS"

# Deliberately omit --delete: pre-existing local material must never be removed
# by a pull operation, and interrupted transfers remain resumable via --partial.
rsync -a --partial --info=progress2 -e "ssh -p $ssh_port" \
    "$ssh_host:$remote_dir/" "$local_dir/"

(
    cd "$local_dir"
    sha256sum -c SHA256SUMS
)

remote_files=$(ssh -p "$ssh_port" "$ssh_host" \
    "find '$remote_dir' -type f | wc -l")
local_files=$(find "$local_dir" -type f | wc -l)
if [[ $remote_files -ne $local_files ]]; then
    echo "file-count mismatch: remote=$remote_files local=$local_files" >&2
    exit 1
fi

echo "GPU attempt pull verified: remote=$remote_dir local=$local_dir files=$local_files"
