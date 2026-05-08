"""Core data models used across subnetscope."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class SubnetRow:
    """One row of the subnet directory.

    All numeric fields are floats in TAO (or per-block TAO for emissions).
    `category` comes from `categorize.py`; `gpu_need` and `reward_shape`
    are derived from category + on-chain hyperparameters.
    """

    netuid: int
    name: str | None
    category: str
    description: str | None
    github_repo: str | None
    subnet_url: str | None
    discord: str | None

    # Registration economics
    recycle_tao: float                 # current burn cost to register one UID
    difficulty: int | None             # PoW difficulty (None for burn-only)
    pow_registration_allowed: bool
    burn_registration_allowed: bool
    min_burn_tao: float = 0.0
    max_burn_tao: float = 0.0

    # Pool / capacity
    tao_in: float = 0.0                # TAO reserves in pool
    alpha_in: float = 0.0              # alpha reserves in pool
    price_tao_per_alpha: float = 0.0
    max_n: int = 0                     # total UID slots
    subnetwork_n: int = 0              # used UID slots
    slots_free: int = 0                # max_n - subnetwork_n
    max_validators: int | None = None  # validator slots (subset of max_n)

    # Emissions
    emission_per_block: float = 0.0    # TAO/block
    emission_per_day: float = 0.0      # TAO/day (derived)

    # Age
    age_blocks: int = 0
    age_days: float = 0.0

    # Reward / incentive shape (from on-chain hyperparameters)
    rho: int | None = None                       # sigmoid steepness for incentive
    kappa: int | None = None                     # consensus threshold (u16: 0-65535)
    alpha_high: int | None = None                # bond high bound
    alpha_low: int | None = None                 # bond low bound
    alpha_sigmoid_steepness: float | None = None # modern dTAO incentive shaping
    liquid_alpha_enabled: bool | None = None
    immunity_period: int | None = None           # blocks new miners are protected
    tempo: int | None = None                     # blocks per emission cycle
    yuma_version: int | None = None
    commit_reveal_enabled: bool | None = None
    weights_rate_limit: int | None = None

    # Live miner reward concentration (computed from metagraph incentives)
    active_miners: int | None = None       # miners with non-zero incentive
    top1_share: float | None = None        # share of total incentive captured by top miner
    top5_share: float | None = None
    top10_share: float | None = None
    top50_share: float | None = None
    incentive_gini: float | None = None    # 0 = perfectly equal, 1 = winner-take-all

    # Derived labels
    gpu_need: str = "?"               # heavy | medium | low | none | varies | ?
    reward_shape: str = "?"           # winner | peak | topN | flat | ?

    # Provenance
    name_source: str = "unknown"       # "identity" | "taostats" | "fallback"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ScanResult:
    rows: list[SubnetRow]
    head_block: int
    fetched_at: datetime
    failures: dict[int, str] = field(default_factory=dict)  # netuid -> error msg
