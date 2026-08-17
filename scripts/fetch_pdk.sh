#!/usr/bin/env bash
# Fetch the academic Nangate45 / FreePDK45 standard-cell liberty used for the
# S6+ real gate-level STA-lite. The .lib is NOT committed (size + academic
# license); this script pulls it into syn/lib/ on demand.
#
# License note: Nangate/FreePDK45 is distributed for academic/research use.
# We use it only to obtain realistic (not production) cell area/delay numbers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DST="$ROOT/syn/lib/nangate45.lib"
URL="https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib"
mkdir -p "$ROOT/syn/lib"
if [[ -s "$DST" ]]; then
  echo "PDK liberty already present: $DST"; exit 0
fi
echo "Fetching Nangate45 typical liberty..."
if command -v wget >/dev/null 2>&1; then wget -q "$URL" -O "$DST";
else curl -fsSL "$URL" -o "$DST"; fi
cells=$(grep -c "cell (" "$DST" || true)
echo "OK: $DST ($(wc -c < "$DST") bytes, $cells cells)"
