"""Slice-2 executable reference model: expert-weight (de)compression codec.

This is the GOLDEN semantics for the streaming DECOMPRESSOR HW block that candidate
C6 (DECISION_LOG D-057) identified as the mechanism worth building for a transfer-bound
MoE system. It is deterministic and integer-exact on the decode path so a future RTL
decompressor can be verified bit-for-bit against it (same executable-reference -> RTL
equivalence methodology as slice-1's residency engine).

Scheme: group-wise SYMMETRIC integer quantization (the practical scheme AWQ / INT4
compressed-tensors use):
  * split each row into contiguous groups of G elements;
  * per group, scale = max(|w|) / qmax, qmax = 2^(N-1) - 1  (N = code bits);
  * code = clip(round(w / scale), -qmax, qmax)   (signed int, stored in N bits);
  * decode (HW path) = code * scale, with scale kept in fp16.

Effective bits/param = N + scale_bits / G  (scale_bits = 16, fp16 per group). This is
exactly the "group scales add ~6% overhead" note in the model configs, made precise.

No RNG here (encode/decode are deterministic functions of the input tensor). The RNG
for representative-weight generation lives in the RD study script, seeded.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SCALE_BITS = 16  # fp16 per-group scale


@dataclass(frozen=True)
class CodecResult:
    codes: np.ndarray        # int32 quantization codes in [-qmax, qmax]
    scales: np.ndarray       # fp16 per-group scales (as float32 view for math)
    n_bits: int
    group_size: int
    eff_bits_per_param: float
    ratio_vs_native: float   # native_bits / eff_bits
    clip_percentile: float = 100.0  # scale reference percentile of |w| (100 = max)


def effective_bits(n_bits: int, group_size: int, scale_bits: int = SCALE_BITS) -> float:
    """Stored bits per parameter including amortized per-group scale overhead."""
    return n_bits + scale_bits / group_size


def encode(w: np.ndarray, n_bits: int, group_size: int, native_bits: float,
           clip_percentile: float = 100.0) -> CodecResult:
    """Group-wise symmetric int-N encode. w is 2-D (rows x cols); cols % G may be != 0
    (last group is short, handled).

    `clip_percentile` sets the per-group scale reference: 100 = max(|w|) (outlier-
    sensitive); <100 clips the scale to that percentile of |w|, sending the few heavy
    outliers to the code rail but giving the bulk finer resolution (the standard
    outlier-aware trick behind AWQ/GPTQ-style low-bit quant). Decode is unchanged."""
    assert w.ndim == 2, "expects a 2-D weight matrix (rows x cols)"
    assert 2 <= n_bits <= 8
    assert 0.0 < clip_percentile <= 100.0
    qmax = (1 << (n_bits - 1)) - 1
    rows, cols = w.shape
    codes = np.zeros((rows, cols), dtype=np.int32)
    n_groups = (cols + group_size - 1) // group_size
    scales = np.zeros((rows, n_groups), dtype=np.float16)
    for g in range(n_groups):
        s0, s1 = g * group_size, min((g + 1) * group_size, cols)
        blk = w[:, s0:s1].astype(np.float32)
        ab = np.abs(blk)
        ref = (np.max(ab, axis=1) if clip_percentile >= 100.0
               else np.percentile(ab, clip_percentile, axis=1))
        scale = np.where(ref > 0, ref / qmax, 1.0).astype(np.float16)
        scales[:, g] = scale
        sc = scale.astype(np.float32)[:, None]
        q = np.round(blk / sc)
        codes[:, s0:s1] = np.clip(q, -qmax, qmax).astype(np.int32)
    eb = effective_bits(n_bits, group_size)
    return CodecResult(codes=codes, scales=scales, n_bits=n_bits, group_size=group_size,
                       eff_bits_per_param=eb, ratio_vs_native=native_bits / eb,
                       clip_percentile=clip_percentile)


def decode(res: CodecResult, cols: int) -> np.ndarray:
    """Integer-exact HW decode path: reconstruct fp32 = code * fp16-scale. Deterministic."""
    rows = res.codes.shape[0]
    out = np.zeros((rows, cols), dtype=np.float32)
    for g in range(res.scales.shape[1]):
        s0, s1 = g * res.group_size, min((g + 1) * res.group_size, cols)
        sc = res.scales[:, g].astype(np.float32)[:, None]
        out[:, s0:s1] = res.codes[:, s0:s1].astype(np.float32) * sc
    return out


def quantize_scale_to_fixed(scale_fp: float, frac_bits: int) -> int:
    """Fixed-point (integer) representation of a per-group scale: round(scale * 2^F).

    A real streaming decompressor dequantizes to a fixed-point element, not IEEE float,
    so its arithmetic is integer and can be matched bit-for-bit by RTL. This is the
    integer analogue of the fp16-scale decode above."""
    return int(round(float(scale_fp) * (1 << frac_bits)))


def decode_fixed(codes: np.ndarray, scales_q: np.ndarray, group_size: int,
                 frac_bits: int, out_bits: int, cols: int) -> np.ndarray:
    """INTEGER-EXACT HW decode: out = sat( (code * scale_q + 2^(F-1)) >>> F ), signed.

    Round-half-up via a bias add then arithmetic right shift; saturating to a signed
    `out_bits` element. Deterministic pure-integer arithmetic -> an RTL decompressor
    that implements the same op is bit-for-bit identical (the equivalence target for
    slice-2, mirroring slice-1's RTL == golden). `scales_q` is (rows x n_groups) int."""
    rows = codes.shape[0]
    out = np.zeros((rows, cols), dtype=np.int64)
    bias = 1 << (frac_bits - 1)
    lo, hi = -(1 << (out_bits - 1)), (1 << (out_bits - 1)) - 1
    for g in range(scales_q.shape[1]):
        s0, s1 = g * group_size, min((g + 1) * group_size, cols)
        sc = scales_q[:, g].astype(np.int64)[:, None]
        prod = codes[:, s0:s1].astype(np.int64) * sc + bias
        shifted = prod >> frac_bits            # arithmetic shift (floor) on int64
        out[:, s0:s1] = np.clip(shifted, lo, hi)
    return out


def sqnr_db(w: np.ndarray, w_hat: np.ndarray) -> float:
    """Signal-to-quantization-noise ratio in dB (weight-reconstruction distortion).

    This is a representation-distortion proxy, NOT task accuracy (see A-020)."""
    w = w.astype(np.float64)
    err = w - w_hat.astype(np.float64)
    sig = float(np.sum(w * w))
    noise = float(np.sum(err * err))
    if noise == 0:
        return float("inf")
    if sig == 0:
        return float("-inf")
    return 10.0 * np.log10(sig / noise)


def roundtrip(w: np.ndarray, n_bits: int, group_size: int, native_bits: float,
              clip_percentile: float = 100.0):
    """Convenience: encode+decode, return (w_hat, CodecResult, sqnr_db, max_abs_err)."""
    res = encode(w, n_bits, group_size, native_bits, clip_percentile)
    w_hat = decode(res, w.shape[1])
    return w_hat, res, sqnr_db(w, w_hat), float(np.max(np.abs(w - w_hat)))
