# Ramulator2 provenance (P-I memory-timing calibration)

- Tool: Ramulator 2.1, CMU-SAFARI, cycle-level DRAM simulator.
- Source: https://github.com/CMU-SAFARI/ramulator2
- Built commit: `99a0e1e87a9321587492fef5b0bd6197928f8d68` (recorded in image at
  `/opt/ramulator2/BUILT_COMMIT`; pin via `--build-arg RAMU_REF=<sha>`).
- License: MIT.
- Image: `edgehetero-mem:1` (Ubuntu 24.04, g++, CMake, Python venv + `pip install -e .`;
  build via `docker build -t edgehetero-mem:1 mem/`).
- DRAM standard: LPDDR5-6400 x16 (`LPDDR5_8Gb_x16` org, `LPDDR5_6400` timing),
  Open row policy, FRFCFS scheduler, AllBank refresh, RoBaRaCoCh mapping,
  CacheLineInterleave channel mapper. 12.8 GB/s JEDEC peak per channel.

## Scope of adoption

Adopted as a STANDALONE calibrator of the P-I effective-bandwidth/latency knobs
(`scripts/mem_calibrate.py` -> `data/canonical/moe_routing_v1/mem_timing.json`),
replacing the guessed analytic contention factors. The re-check
(`scripts/w3_mem_recheck.py`) confirms no P-I region flips the SW-vs-HW decision
(transfer dominates control >=68x; ~48x below the crossover), so the FULL
simulator-in-the-loop (gem5/external-frontend) integration remains deferred with
its adoption trigger tested and unmet.

## Reproduce

```
docker build -t edgehetero-mem:1 mem/
python3 scripts/mem_calibrate.py     # runs Ramulator2, writes mem_timing.json
python3 scripts/w3_mem_recheck.py    # SW-vs-HW recheck, writes w3_mem_recheck.json
```

## Honesty notes

- Per-channel calibration: `LoadStoreTrace` injects one request/cycle (a single
  stream), which saturates exactly one channel; aggregate P-I bandwidth is the
  per-channel achieved value x n_channels (each channel fed by its own copy-engine
  stream). Multi-channel single-stream runs would be injection-limited (a frontend
  artifact, not DRAM timing).
- Contention is modeled by interleaving the streaming trace with a fraction `f` of
  uniform-random accesses on the same channel (row-conflict thrashing); the
  transfer-available bandwidth is `achieved x (1-f)`.
- Read latency reported in controller cycles (`get_tCK` is not exposed to the
  Python binding); the P-I `link_latency` (0.5 us) is negligible vs the MB-scale
  expert-transfer time regardless.
