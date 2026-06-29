"""easy_entry_score — combine the user's subnet-selection criteria into one
0..100 number that ranks subnets by "good place for a small miner to start".

Criteria (from the user's stated preferences):
  * low GPU need        (none > low > medium > heavy)
  * low Top-1 share     (less winner-take-all = more room for new miners)
  * many active miners  (proves the market exists)
  * low used/max        (room to register without eviction wars)
  * low burn fee        (cheap to enter)
  * high liquidity      (price stability)
  * high emission/d     (more rewards available)
"""
from __future__ import annotations

from dataclasses import dataclass

from ..types import SubnetRow

GPU_FRICTION = {
    "none":   1.00,
    "low":    0.85,
    "medium": 0.50,
    "varies": 0.40,
    "heavy":  0.05,
    "?":      0.30,
}


def _norm(values: list[float], v: float, *, lower_better: bool) -> float:
    """Min-max normalize `v` against `values` to [0,1]. Higher = better."""
    if not values:
        return 0.5
    lo, hi = min(values), max(values)
    if hi <= lo:
        return 0.5
    n = (v - lo) / (hi - lo)
    return 1.0 - n if lower_better else n


@dataclass
class ScoreBreakdown:
    score: float                           # 0..100
    gpu: float
    fee: float
    top1: float
    miners: float
    slots: float
    liquidity: float
    emission: float
    why: list[str]                         # short human-readable bullets


WEIGHTS = {
    "gpu":       0.22,
    "top1":      0.20,
    "miners":    0.16,
    "slots":     0.13,
    "fee":       0.11,
    "liquidity": 0.10,
    "emission":  0.08,
}


def score_subnet(r: SubnetRow, cohort: list[SubnetRow]) -> ScoreBreakdown:
    fees = [x.recycle_tao for x in cohort if x.recycle_tao > 0]
    miners = [x.active_miners or 0 for x in cohort]
    liqs = [x.tao_in for x in cohort]
    emis = [x.emission_per_day for x in cohort]

    s_gpu = GPU_FRICTION.get(r.gpu_need or "?", 0.3)

    if r.top1_share is None:
        s_top1 = 0.30                       # unknown ⇒ pessimistic
    else:
        s_top1 = max(0.0, 1.0 - r.top1_share)

    s_miners = _norm(miners, r.active_miners or 0, lower_better=False)
    s_slots = (r.slots_free / max(r.max_n, 1)) if r.max_n > 0 else 0.0
    s_fee = _norm(fees, r.recycle_tao, lower_better=True) if fees else 0.5
    s_liq = _norm(liqs, r.tao_in, lower_better=False)
    s_emi = _norm(emis, r.emission_per_day, lower_better=False)

    score = (
        WEIGHTS["gpu"]       * s_gpu +
        WEIGHTS["top1"]      * s_top1 +
        WEIGHTS["miners"]    * s_miners +
        WEIGHTS["slots"]     * s_slots +
        WEIGHTS["fee"]       * s_fee +
        WEIGHTS["liquidity"] * s_liq +
        WEIGHTS["emission"]  * s_emi
    ) * 100.0

    why: list[str] = []
    if s_gpu >= 0.8:           why.append(f"no/low GPU ({r.gpu_need})")
    elif s_gpu <= 0.1:         why.append(f"heavy GPU need")
    if r.top1_share is not None:
        if r.top1_share <= 0.20: why.append(f"top miner incentive only {r.top1_share*100:.0f}% (decentralized)")
        elif r.top1_share >= 0.80: why.append(f"top miner incentive {r.top1_share*100:.0f}% (winner-take-all)")
    if s_slots >= 0.10:        why.append(f"{r.slots_free} free UID slots")
    elif r.subnetwork_n >= r.max_n > 0: why.append("subnet is FULL (eviction war)")
    if s_fee >= 0.8 and r.recycle_tao > 0:
        why.append(f"cheap burn fee {r.recycle_tao:.4f} τ")
    if s_liq >= 0.7:           why.append(f"deep liquidity ({r.tao_in:,.0f} τ)")
    if s_emi >= 0.7:           why.append(f"high emission ({r.emission_per_day:.0f} τ-eq/d)")

    return ScoreBreakdown(
        score=round(score, 1),
        gpu=round(s_gpu, 3),
        top1=round(s_top1, 3),
        miners=round(s_miners, 3),
        slots=round(s_slots, 3),
        fee=round(s_fee, 3),
        liquidity=round(s_liq, 3),
        emission=round(s_emi, 3),
        why=why,
    )


def score_all(rows: list[SubnetRow]) -> dict[int, ScoreBreakdown]:
    """Score every row in the cohort against the cohort itself."""
    return {r.netuid: score_subnet(r, rows) for r in rows}
