# Standard-cell library provenance (S6+ real STA-lite)

- **File**: `nangate45.lib` (not committed; see `.gitignore`).
- **Source**: `NangateOpenCellLibrary_typical.lib` from The-OpenROAD-Project/
  OpenROAD-flow-scripts (`flow/platforms/nangate45/lib/`).
- **Fetch**: `scripts/fetch_pdk.sh` (downloads into this directory).
- **Node/corner**: Nangate45 / FreePDK45, typical corner (academic/predictive,
  NOT a production PDK).
- **Size**: ~6.7 MB, 241 cells.

## Usage and honesty scope

Used only to convert the S6 AIG-depth timing proxy into realistic gate-level
numbers:
- Area: sum of standard-cell areas (um^2), no routing/whitespace.
- Delay: `abc -liberty` static timing on the mapped netlist, **cell delays only**
  (WireLoad = "none"): no wire parasitics and no sign-off STA.

These are a higher evidence class than generic Yosys gate/AIG proxies, but they
are still **predictive academic-PDK estimates**, not production silicon area/
timing/power. Power remains unavailable (no activity + power methodology).
