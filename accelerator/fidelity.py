"""Fidelity labelling and the accelerator-package hard isolation rule.

Root spec (PLATFORM_FLOW_SPECIFICATION.md) §3.1 defines the fidelity taxonomy.
§3.1 also states the hard isolation: the candidate-processor path is ``ANALYTICAL``,
un-measured regions are ``PROJECTED``, and the GPU / transfer path (which has a
measured surrogate) is ``MEASURED_SURROGATE``.

This module encodes ONE rule that the whole ``accelerator/`` package obeys:

    Every component in accelerator/ MUST carry ANALYTICAL or PROJECTED.
    None may carry MEASURED_SURROGATE.

The candidate support processor has no silicon and no on-device measurement, so a
measured-surrogate label would be a fidelity-layer lie (root spec §3.1 "結論不得
跨 fidelity 層誇大"; §14.8 "將模型估計寫成實機量測" is forbidden). The rule is
enforced here at construction time, not left to reviewer vigilance.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Fidelity(str, Enum):
    """The subset of the root-spec §3.1 taxonomy this package is allowed to use.

    The full taxonomy has seven labels; only these two are legal for anything the
    candidate processor produces. The others are intentionally NOT members here so
    that a stray reference fails fast rather than silently mislabelling.
    """

    ANALYTICAL = "ANALYTICAL"    # closed-form model, no on-device support
    PROJECTED = "PROJECTED"      # extrapolated into an un-measured region


# Labels that exist in the root taxonomy but are forbidden inside accelerator/.
# Kept as plain strings so we can detect and reject them by value.
FORBIDDEN_IN_ACCELERATOR = frozenset(
    {
        "MEASURED_SURROGATE",  # implies fitted to on-device measurement (none exists)
        "CYCLE_ACCURATE",      # implies cycle-exact silicon knowledge (none exists)
        "CYCLE_RESOLVED",      # reserved for the measured GPU-path engine layers
        "EVENT_DRIVEN",
        "STATISTICAL",
    }
)


class FidelityViolation(ValueError):
    """A component tried to claim a fidelity label forbidden in accelerator/."""


def require_accelerator_fidelity(label: str | Fidelity) -> Fidelity:
    """Return the validated Fidelity, or raise FidelityViolation.

    This is the single gate every accelerator component passes its label through.
    It rejects MEASURED_SURROGATE (and every other on-device / cycle-exact label)
    explicitly, so a typo or an over-claim cannot enter the package.
    """
    if isinstance(label, Fidelity):
        return label
    text = str(label).strip()
    if text in FORBIDDEN_IN_ACCELERATOR:
        raise FidelityViolation(
            f"fidelity {text!r} is forbidden in accelerator/: the candidate "
            "processor has no silicon and no on-device measurement; use ANALYTICAL "
            "or PROJECTED (root spec §3.1)."
        )
    try:
        return Fidelity(text)
    except ValueError as exc:
        raise FidelityViolation(
            f"fidelity {text!r} is not one of {[f.value for f in Fidelity]}; "
            "accelerator/ components must be ANALYTICAL or PROJECTED."
        ) from exc


@dataclass(frozen=True)
class Provenance:
    """Where an analytical/projected number came from and what it may NOT claim.

    Mirrors the discipline used in src/edgeflow/residency.py: a modelled quantity
    always travels with its evidence anchor (or an explicit "no evidence" marker)
    and an explicit claim limit. The candidate-processor path never fabricates a
    point value that reads as a measurement.
    """

    fidelity: Fidelity
    evidence_refs: tuple[str, ...]   # evidence/ paths or hardware/ STA anchors; may be empty
    claim_limit: str                 # what this number is NOT allowed to be used for
    measured: bool = False           # always False in accelerator/ (kept for schema symmetry)

    def __post_init__(self) -> None:
        require_accelerator_fidelity(self.fidelity)
        if self.measured:
            raise FidelityViolation(
                "accelerator/ provenance cannot be measured=True; the candidate "
                "processor path has no on-device measurement."
            )

    def to_dict(self) -> dict:
        return {
            "fidelity": self.fidelity.value,
            "evidence_refs": list(self.evidence_refs),
            "claim_limit": self.claim_limit,
            "measured": self.measured,
        }
