"""Attachment points A1..A6 (root spec §6.2): where the candidate processor can attach.

NOTE (root spec §6.2 / docs/PHASE_NAMING_MAP.md): attachment points A1..A6 are a
DIFFERENT thing from stages A1..A4. Here A1..A6 are candidate offload functions.

Each attachment point defines the THREE things root spec §6.2 mandates:

  1. work_unit        : the offloadable unit of work + its baseline cost
  2. accelerator_cost : the cost model on the candidate processor
  3. transfer_cost    : the cost to move data over and back (root spec: the third,
                        most-often-forgotten item, frequently decides break-even)

and its measurement status. Two hard rules (guide §4.4, root spec §6.2):

  * A2 (MoE dispatch data movement) and A6 (offloaded-KV attention) have NO
    measurement. They are modelled only; NO performance conclusion may be drawn
    until the GPU track supplies data. They are marked measured=False and their
    cost fields are PROJECTED with an explicit "no evidence" claim limit.
  * Everything here is ANALYTICAL or PROJECTED -- never MEASURED_SURROGATE. The
    numbers cited from evidence/ describe the BASELINE (what the GPU/CPU does
    today); the candidate-processor cost is always a model.

Baseline costs cite measured facts (root spec §2.2, §6.2, §11.2) by evidence path.
Citing a measured baseline is allowed; claiming a candidate-processor speedup is
NOT (that is C1's job -- guide §6).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from accelerator.fidelity import Fidelity, Provenance, require_accelerator_fidelity

# Granularity axis (guide §7): the work-unit granularity choice materially affects
# break-even and MUST be a C1 sensitivity axis. Recorded per attachment point.
GRANULARITIES = ("per_token", "per_layer", "per_batch", "per_block", "per_request")


@dataclass(frozen=True)
class WorkUnit:
    """The offloadable unit and its BASELINE cost (who does it today, at what cost)."""

    description: str
    granularity: str                 # one of GRANULARITIES; a C1 sensitivity axis
    baseline_owner: str              # who bears this today (GPU kernel / CPU / copy engine)
    baseline_cost_model: str         # closed-form / cited cost of the baseline
    provenance: Provenance

    def __post_init__(self) -> None:
        if self.granularity not in GRANULARITIES:
            raise ValueError(
                f"granularity {self.granularity!r} not in {GRANULARITIES}"
            )


@dataclass(frozen=True)
class CostModel:
    """A cost model on the candidate processor, or the transfer cost.

    ``form`` states the functional form (fixed / linear-in-bytes / piecewise ...);
    ``expression`` is the closed form; ``provenance`` carries fidelity + claim limit.
    """

    form: str
    expression: str
    provenance: Provenance


@dataclass(frozen=True)
class AttachmentPoint:
    point_id: str                    # "A1".."A6"
    function: str
    priority: str                    # "primary" / "secondary" (主 / 次)
    measured: bool                   # is there ANY on-device measurement for it?
    work_unit: WorkUnit
    accelerator_cost: CostModel
    transfer_cost: CostModel
    notes: str = ""
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # A2 and A6 must never be marked measured (guide §4.4).
        if self.point_id in ("A2", "A6") and self.measured:
            raise ValueError(
                f"{self.point_id} has NO measurement (guide §4.4); measured must be False"
            )
        for cm in (self.accelerator_cost, self.transfer_cost):
            require_accelerator_fidelity(cm.provenance.fidelity)

    def performance_conclusion_allowed(self) -> bool:
        """A2 and A6 (no measurement) forbid any performance conclusion here."""
        return self.measured

    def to_dict(self) -> dict[str, Any]:
        return {
            "point_id": self.point_id,
            "function": self.function,
            "priority": self.priority,
            "measured": self.measured,
            "performance_conclusion_allowed": self.performance_conclusion_allowed(),
            "work_unit": {
                "description": self.work_unit.description,
                "granularity": self.work_unit.granularity,
                "baseline_owner": self.work_unit.baseline_owner,
                "baseline_cost_model": self.work_unit.baseline_cost_model,
                "provenance": self.work_unit.provenance.to_dict(),
            },
            "accelerator_cost": {
                "form": self.accelerator_cost.form,
                "expression": self.accelerator_cost.expression,
                "provenance": self.accelerator_cost.provenance.to_dict(),
            },
            "transfer_cost": {
                "form": self.transfer_cost.form,
                "expression": self.transfer_cost.expression,
                "provenance": self.transfer_cost.provenance.to_dict(),
            },
            "notes": self.notes,
            "evidence_refs": list(self.evidence_refs),
        }


# --- claim limits reused below ------------------------------------------------
_NO_BENEFIT_CLAIM = (
    "ANALYTICAL candidate-processor cost model; sizing input for C1 DSE only. "
    "MUST NOT be read as a speedup/benefit or break-even claim (guide §6, root spec §14.8)."
)
_NO_EVIDENCE_CLAIM = (
    "NO on-device measurement exists for this attachment point (root spec §6.2, "
    "guide §4.4). PROJECTED model only; NO performance conclusion may be drawn "
    "until the GPU track supplies data."
)


def _ana(refs: tuple[str, ...], claim: str = _NO_BENEFIT_CLAIM) -> Provenance:
    return Provenance(fidelity=Fidelity.ANALYTICAL, evidence_refs=refs, claim_limit=claim)


def _proj_no_evidence() -> Provenance:
    return Provenance(fidelity=Fidelity.PROJECTED, evidence_refs=(), claim_limit=_NO_EVIDENCE_CLAIM)


def default_attachment_points() -> dict[str, AttachmentPoint]:
    """The six attachment points with A1..A4 fully defined and A2/A6 marked unmeasured.

    Priority order (guide §4.4): primary line A1..A4 first, then A5/A6.
    """
    points: list[AttachmentPoint] = []

    # A1 -- routing / gating decision compute, top-k selection. MEASURED baseline.
    points.append(
        AttachmentPoint(
            point_id="A1",
            function="routing / gating decision compute, top-k selection",
            priority="primary",
            measured=True,
            work_unit=WorkUnit(
                description=(
                    "one routing/gating decision producing top-k expert ids per token "
                    "per layer; measured routing tensor shape [159 tokens, 32 layers, "
                    "k=2] (root spec §2.2)."
                ),
                granularity="per_layer",
                baseline_owner="GPU kernel",
                baseline_cost_model=(
                    "baseline gating is a GPU kernel; routing decisions per (token,layer) "
                    "= 159*32; top-k=2. Baseline cost tracked via CTRL-PX0-*-routing and "
                    "OFF-E-PR* replay; the routing .npy is the frozen decision trace."
                ),
                provenance=_ana(
                    (
                        "evidence routing .npy (routing_sha256 "
                        "0a9225ec...e50d6, root spec §4.2/A2)",
                        "runs CTRL-PX0-*-routing (root spec §2.1)",
                    )
                ),
            ),
            accelerator_cost=CostModel(
                form="fixed_per_decision + linear_in_experts",
                expression=(
                    "cycles = pipeline_latency + ceil(num_experts / operations_per_cycle); "
                    "top-k via argmin microarch (root spec §11.2 anchor)"
                ),
                provenance=_ana(
                    (
                        "hardware/ STA argmin: comb 66.50 MHz/5641 um^2 vs registered "
                        "236.24 MHz/5627 um^2 (root spec §11.2)",
                    )
                ),
            ),
            transfer_cost=CostModel(
                form="linear_in_bytes",
                expression=(
                    "move-in: gate logits / routing metadata bytes / bandwidth; "
                    "move-out: top-k index bytes / bandwidth. Small metadata, likely "
                    "latency-bound (see small-transfer floor, root spec §8.1)."
                ),
                provenance=_ana(
                    (
                        "runs transfer microbench v1-v4 small-transfer floor ~0.037 ms "
                        "(root spec §2.3/§8.1)",
                    )
                ),
            ),
            evidence_refs=(
                "routing .npy [159,32,2]",
                "CTRL-PX0-*-routing",
                "OFF-E-PR*",
            ),
            notes="Granularity per_layer chosen; per_token is an alternative C1 sensitivity axis.",
        )
    )

    # A2 -- MoE dispatch data movement. NO MEASUREMENT.
    points.append(
        AttachmentPoint(
            point_id="A2",
            function="MoE dispatch data movement (token permutation, gather/scatter)",
            priority="primary",
            measured=False,
            work_unit=WorkUnit(
                description=(
                    "token permutation + gather/scatter to group tokens by selected "
                    "expert before grouped-GEMM, and scatter results back."
                ),
                granularity="per_layer",
                baseline_owner="GPU kernel",
                baseline_cost_model=(
                    "PROJECTED only. The existing gather_scatter probe (benchmark.py:463, "
                    "56 records) is a same-device SYNTHETIC proxy giving only the execute "
                    "term -- no T_prepare/T_queue/T_sync/T_move. No independent measurement."
                ),
                provenance=_proj_no_evidence(),
            ),
            accelerator_cost=CostModel(
                form="linear_in_tokens (PROJECTED)",
                expression=(
                    "cycles ~ pipeline_latency + ceil(permuted_tokens / operations_per_cycle); "
                    "UNVALIDATED -- no measurement to anchor the constant."
                ),
                provenance=_proj_no_evidence(),
            ),
            transfer_cost=CostModel(
                form="linear_in_bytes (PROJECTED)",
                expression=(
                    "move-in: permutation indices + token activations; move-out: scattered "
                    "results. Bytes known from shapes but timing UNMEASURED."
                ),
                provenance=_proj_no_evidence(),
            ),
            evidence_refs=("NONE -- GPU track measurement priority 1 (root spec §9.1)",),
            notes=(
                "NO measurement (guide §4.4). GPU track priority 1. No performance "
                "conclusion permitted here."
            ),
        )
    )

    # A3 -- transfer scheduling / DMA descriptor / prefetch issue. MEASURED baseline.
    points.append(
        AttachmentPoint(
            point_id="A3",
            function="transfer scheduling / DMA descriptor / prefetch issue",
            priority="primary",
            measured=True,
            work_unit=WorkUnit(
                description=(
                    "issue a transfer/prefetch: build a DMA descriptor and enqueue it; one "
                    "work unit = one descriptor / one expert-object move (352,321,536 B)."
                ),
                granularity="per_block",
                baseline_owner="CPU + copy engine",
                baseline_cost_model=(
                    "baseline: CPU builds descriptors, copy engine moves. Per-object H2D "
                    "measured 12.454-12.499 ms at 28.19-28.29 GB/s (root spec §2.2); "
                    "copy-engine contention is an aggregate-bandwidth/occupancy effect, "
                    "NOT a per-transfer latency multiplier (root spec §2.3/§8.1)."
                ),
                provenance=_ana(
                    (
                        "runs transfer microbench v1-v4 (root spec §2.1)",
                        "OFF-E-PR3 per-object H2D 12.454-12.499 ms @ ~28.29 GB/s (root spec §2.2)",
                    )
                ),
            ),
            accelerator_cost=CostModel(
                form="fixed_descriptor_issue + queue_occupancy",
                expression=(
                    "descriptor issue cost = pipeline_latency cycles; throughput bounded by "
                    "issue_width and queue_depth; move itself accounted in transfer_cost."
                ),
                provenance=_ana(
                    (
                        "hardware/ STA seqbuf residency engine ME sweep (root spec §11.2)",
                    )
                ),
            ),
            transfer_cost=CostModel(
                form="aggregate_bandwidth (shared copy engine)",
                expression=(
                    "N concurrent streams share copy-engine bandwidth: per-transfer latency "
                    "constant, total completion time grows -- the corrected model "
                    "(root spec §8.1). move bytes / memory_bandwidth."
                ),
                provenance=_ana(
                    (
                        "transfer microbench v1-v4; corrected contention model (root spec §2.3/§8.1)",
                    )
                ),
            ),
            evidence_refs=("transfer microbench v1-v4", "OFF-E-PR3 H2D constants"),
            notes="Granularity per_block (one DMA descriptor / object); per_batch is a C1 axis.",
        )
    )

    # A4 -- expert decompression / compressed movement. HW STA anchor (ANALYTICAL).
    points.append(
        AttachmentPoint(
            point_id="A4",
            function="expert decompression / compressed movement",
            priority="primary",
            measured=True,
            work_unit=WorkUnit(
                description=(
                    "decompress one expert object on the way in (or move it compressed): "
                    "unit = one 352,321,536 B expert object; baseline has no decompressor."
                ),
                granularity="per_block",
                baseline_owner="baseline has none (new capability)",
                baseline_cost_model=(
                    "baseline moves uncompressed (per-object H2D 12.454-12.499 ms). A "
                    "decompressor trades link bytes for on-chip decompress throughput; the "
                    "expert_decompressor.sv STA gives the achievable clock/area envelope."
                ),
                provenance=_ana(
                    (
                        "hardware/ expert_decompressor.sv: NB4/L8 811.08 MHz/4902 um^2; "
                        "NB8/L16 307.31 MHz/23815 um^2 (root spec §2.2/§11.2)",
                    )
                ),
            ),
            accelerator_cost=CostModel(
                form="linear_in_bytes @ decompress_throughput",
                expression=(
                    "decompress cycles = ceil(object_bytes / (operations_per_cycle * "
                    "bytes_per_op)); clock/area bounded by expert_decompressor.sv STA."
                ),
                provenance=_ana(
                    (
                        "expert_decompressor.sv STA 307-811 MHz (root spec §11.2)",
                    )
                ),
            ),
            transfer_cost=CostModel(
                form="linear_in_compressed_bytes",
                expression=(
                    "move-in compressed bytes = object_bytes / compression_ratio, / "
                    "memory_bandwidth; compression_ratio is a C1 sweep axis (root spec §10.1)."
                ),
                provenance=_ana(
                    (
                        "OFF-E-PR3 object bytes 352,321,536 (root spec §2.2)",
                    )
                ),
            ),
            evidence_refs=("expert_decompressor.sv 307-811 MHz", "OFF-E-PR3 object bytes"),
            notes="compression_ratio and decompress cost are C1 sweep axes (root spec §10.1).",
        )
    )

    # A5 -- KV block management / offload. SWAP-K measured structure (secondary).
    points.append(
        AttachmentPoint(
            point_id="A5",
            function="KV block management / offload",
            priority="secondary",
            measured=True,
            work_unit=WorkUnit(
                description=(
                    "manage/offload one KV block: block = 16 tokens, 2,097,152 B "
                    "(root spec §2.2); unit shares the residency-managed-object abstraction "
                    "with expert objects (root spec §4.1)."
                ),
                granularity="per_block",
                baseline_owner="vLLM CPU pool",
                baseline_cost_model=(
                    "baseline: vLLM CPU pool manages paged KV. Structure measured "
                    "(SWAP-K1/K2/K5): 128 KiB/token, 2 MiB/block. LIMITATION: event "
                    "block_size=0; byte accounting is derived from runtime shape/dtype, "
                    "not the event (root spec §2.2, guide claim boundary)."
                ),
                provenance=_ana(
                    (
                        "SWAP-K1/K2/K5 (root spec §2.1/§2.2); block_size=0 limitation "
                        "(root spec §2.2)",
                    )
                ),
            ),
            accelerator_cost=CostModel(
                form="fixed_per_block_mgmt",
                expression=(
                    "block table / victim-select cost = pipeline_latency cycles; victim "
                    "select bounded by banked-LRU STA (root spec §11.2)."
                ),
                provenance=_ana(
                    (
                        "hardware/ banked LRU victim N=128/B=16 200.33 MHz/8918 um^2 "
                        "(root spec §11.2)",
                    )
                ),
            ),
            transfer_cost=CostModel(
                form="linear_in_bytes (shared link with experts)",
                expression=(
                    "KV block move bytes / memory_bandwidth; KV blocks and expert objects "
                    "CONTEND on the same link/copy engine (root spec §4.1) -- coupling is a "
                    "C1 concern."
                ),
                provenance=_ana(
                    (
                        "SWAP-K2 block bytes 2,097,152; shared-link coupling (root spec §4.1)",
                    )
                ),
            ),
            evidence_refs=("SWAP-K1/K2/K5", "block_size=0 limitation"),
            notes=(
                "Secondary line. KV timing must NOT be claimed from SWAP-K2/K3 "
                "(block_size=0); long-context is PROJECTED (B1 constraint)."
            ),
        )
    )

    # A6 -- attention over offloaded KV. NO MEASUREMENT.
    points.append(
        AttachmentPoint(
            point_id="A6",
            function="attention over offloaded KV",
            priority="secondary",
            measured=False,
            work_unit=WorkUnit(
                description=(
                    "run attention against KV blocks that live off-device (offloaded). "
                    "Long-context regime forced when KV exceeds VRAM (1M ctx -> 128 GiB "
                    "> 96 GB, root spec §2.2)."
                ),
                granularity="per_block",
                baseline_owner="GPU",
                baseline_cost_model=(
                    "PROJECTED only. No measurement anywhere (root spec §6.2, §9.1). "
                    "Third-party corpus max query ~721 tokens cannot cover this regime "
                    "(GPU track priority 2 is the only source)."
                ),
                provenance=_proj_no_evidence(),
            ),
            accelerator_cost=CostModel(
                form="PROJECTED",
                expression="UNVALIDATED -- no measurement to anchor any constant.",
                provenance=_proj_no_evidence(),
            ),
            transfer_cost=CostModel(
                form="PROJECTED",
                expression=(
                    "move-in offloaded KV blocks per attention step; bytes derivable from "
                    "shape but timing UNMEASURED."
                ),
                provenance=_proj_no_evidence(),
            ),
            evidence_refs=("NONE -- GPU track measurement priority 2 (root spec §9.1)",),
            notes=(
                "NO measurement (guide §4.4). Highest information gain (root spec §9.1). "
                "No performance conclusion permitted here."
            ),
        )
    )

    return {p.point_id: p for p in points}


def unmeasured_points(points: dict[str, AttachmentPoint] | None = None) -> list[str]:
    pts = points or default_attachment_points()
    return sorted(pid for pid, p in pts.items() if not p.measured)
