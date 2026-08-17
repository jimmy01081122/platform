# Data Registry

Register every raw or external dataset without copying or modifying it in place.

Each entry should include:

- dataset/trace ID。
- local or remote location。
- checksum or immutable revision。
- schema and semantic mapping revision。
- collection/import command。
- completeness and known gaps。
- allowed transformations。
- canonical output path。

## External Switch Colab traces

- Registry: [`switch_colab_trace_readonly_v1.yaml`](switch_colab_trace_readonly_v1.yaml)
- Latest quick-audit evidence: [`switch_colab_trace_audit_summary.json`](switch_colab_trace_audit_summary.json)
- Registry schema: [`../../schemas/external_trace_registry.schema.json`](../../schemas/external_trace_registry.schema.json)
- Provenance and semantic limits: [`../../docs/methodology/SWITCH_COLAB_TRACE_PROVENANCE.md`](../../docs/methodology/SWITCH_COLAB_TRACE_PROVENANCE.md)
- Read-only auditor: [`../../scripts/audit_external_switch_traces.py`](../../scripts/audit_external_switch_traces.py)

The external root and its full 1,527-row inventory remain outside this repository.
