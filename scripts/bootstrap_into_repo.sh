#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <target-repo-root>" >&2
  exit 2
fi

src="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="$1"
mkdir -p "$target"

if [[ -n "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "target is not empty; this script will only add missing files and will not overwrite" >&2
fi

while IFS= read -r -d '' path; do
  rel="${path#"$src"/}"
  case "$rel" in
    runs/*|data/raw/*|data/canonical/*) continue ;;
  esac
  if [[ -d "$path" ]]; then
    mkdir -p "$target/$rel"
  elif [[ ! -e "$target/$rel" ]]; then
    mkdir -p "$(dirname "$target/$rel")"
    cp -a "$path" "$target/$rel"
  else
    echo "skip existing: $rel"
  fi
done < <(find "$src" -mindepth 1 -print0)

echo "workspace scaffold added to: $target"
echo "next: cd \"$target\" && make doctor"
