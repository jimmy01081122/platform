# switch-base-32 demands fixture (S7 reproduction work-unit)

`demands.txt` is the compact per-(batch, layer_step) expert-demand work-unit
stream for the primary S4+ operating fixture (switch-base-32, mbpp, batch_size=4,
len=128), in the format read by the shared C kernel and the RTL testbench:

```
line 1:            <num_experts> <num_steps>      -> "32 192"
next num_steps:    <count> <expert_id> ...        (experts ascending, one step/line)
```

## Provenance (derived, not raw)

- Source (read-only, registered): `switch_model_sweep_16_32`
  (`/home/a/prototype/trace_data/moe_router_trace_switch_model_sweep_16_32_results`,
  model `google/switch-base-32`), see `data/registry/sources.yaml`.
- Derivation: raw `batch_expert_load_trace.csv` -> canonical `expert_demand`
  events (`scripts/build_canonical.py`) -> demands
  (`scripts/export_demands.py` == `edgeflow.residency.demands_from_events`).
- sha256(demands.txt): d8d84bfc20c0521e67ed155693ff3450322a40e02cfefdf6d1b6f22c07c09aea
- num_experts=32, num_steps=192.

This derived artifact is committed so the S5/S6/S7 chain reproduces from within
the repository without touching read-only raw traces or /tmp.
