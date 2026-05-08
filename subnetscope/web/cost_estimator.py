"""Per-subnet 'cost to operate a miner' estimator for the analysis pages.

Returns realistic monthly + one-time cost ranges so a prospective miner can
quickly answer "what subscriptions / hardware / API keys do I actually need?"

Two layers of knowledge:

1. **Per-category baselines** (`_CATEGORY_BASELINES`): generic estimates keyed
   off `category` × `gpu_need` — cover every subnet by default.

2. **Per-subnet overrides** (`_SUBNET_OVERRIDES`): hand-curated facts pulled
   from each subnet's repo / `env.example` (which API keys are *mandatory*,
   which are optional, what models the protocol pins, etc.). These replace
   the baseline entirely for a known subnet.

The output is normalized to a single shape so the analyzer template can
render it without knowing where the data came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ─── data shapes ────────────────────────────────────────────────────────────


@dataclass
class CostItem:
    item: str
    cost_usd_low: float
    cost_usd_high: float
    cadence: str = "monthly"   # "monthly" | "one-time"
    required: bool = True      # False = optional / "if you choose this path"
    note: str = ""


@dataclass
class CostEstimate:
    items: list[CostItem] = field(default_factory=list)
    summary_notes: list[str] = field(default_factory=list)
    needs_paid_api: bool = False
    needs_gpu: bool = False
    confidence: str = "baseline"   # "baseline" | "verified" (override applied)

    @property
    def monthly_low(self) -> float:
        return sum(i.cost_usd_low for i in self.items if i.cadence == "monthly")

    @property
    def monthly_high(self) -> float:
        return sum(i.cost_usd_high for i in self.items if i.cadence == "monthly")

    @property
    def one_time_low(self) -> float:
        return sum(i.cost_usd_low for i in self.items if i.cadence == "one-time")

    @property
    def one_time_high(self) -> float:
        return sum(i.cost_usd_high for i in self.items if i.cadence == "one-time")


# ─── per-(category, gpu_need) baselines ─────────────────────────────────────
# Costs are USD and reflect typical 2026 pricing. Ranges are deliberately
# wide because real spend depends on competitiveness (top-1 miners pay 5-10x
# baseline to win winner-take-all subnets).

_GPU_HOSTING: dict[str, tuple[float, float, str]] = {
    "none":   (5,    20,   "VPS (2 vCPU / 4 GB RAM, e.g. Hetzner CPX21)"),
    "low":    (50,   150,  "GPU VPS (RTX 3060 / A4000, 8-16 GB VRAM)"),
    "medium": (250,  600,  "GPU VPS (RTX 4090 / A10, 24 GB VRAM)"),
    "heavy":  (1500, 3500, "GPU rental (A100 / H100 80 GB)"),
    "varies": (50,   1500, "depends on chosen model size"),
}

_GPU_PURCHASE: dict[str, tuple[float, float, str]] = {
    "none":   (0,     0,     ""),
    "low":    (300,   600,   "RTX 3060 / used 3070 — break-even ~3-6 mo"),
    "medium": (700,   2200,  "RTX 4090 / used 3090 — break-even ~6-12 mo"),
    "heavy":  (8000,  35000, "A100 / H100 — usually rented, not bought"),
    "varies": (300,   8000,  ""),
}

# (category, gpu_need) → list of CostItem builders
def _baseline_items(category: str, gpu_need: str) -> list[CostItem]:
    cat = (category or "other").lower()
    gpu = (gpu_need or "varies").lower()
    items: list[CostItem] = []

    # Hosting: always required.
    lo, hi, note = _GPU_HOSTING.get(gpu, _GPU_HOSTING["varies"])
    items.append(CostItem(
        item=("GPU rental" if gpu in ("medium", "heavy") else "Hosting / VPS"),
        cost_usd_low=lo, cost_usd_high=hi, cadence="monthly",
        required=True, note=note,
    ))

    # LLM API costs by category — only if the subnet's pipeline calls cloud LLMs.
    if cat in ("text", "data", "agent"):
        items.append(CostItem(
            item="LLM API (OpenAI / Anthropic / OpenRouter)",
            cost_usd_low=30, cost_usd_high=300, cadence="monthly",
            required=False,  # most subnets allow local model fallback
            note="optional if you run a local model; mandatory on a few "
                 "(check env.example in the repo)",
        ))
    elif cat == "code":
        items.append(CostItem(
            item="LLM API (CodeLlama / Qwen Coder)",
            cost_usd_low=0, cost_usd_high=100, cadence="monthly",
            required=False,
            note="usually a local model is competitive; cloud API is optional",
        ))
    elif cat == "trading" or cat == "forecasting":
        items.append(CostItem(
            item="Market data feed (optional)",
            cost_usd_low=0, cost_usd_high=200, cadence="monthly",
            required=False,
            note="some miners pay for premium feeds (Polygon.io, Alpaca, etc.)",
        ))
    elif cat == "data":
        items.append(CostItem(
            item="Data sources (proxies, web APIs)",
            cost_usd_low=10, cost_usd_high=100, cadence="monthly",
            required=False,
            note="residential proxies / paid API access for scraping pipelines",
        ))

    # Storage subnets earn from disk; bandwidth dominates.
    if cat == "storage":
        items.append(CostItem(
            item="SSD storage + bandwidth",
            cost_usd_low=20, cost_usd_high=150, cadence="monthly",
            required=True,
            note="needed for retrieval throughput; outbound bandwidth fees vary",
        ))

    # Optional one-time hardware purchase.
    p_lo, p_hi, p_note = _GPU_PURCHASE.get(gpu, (0, 0, ""))
    if p_hi > 0:
        items.append(CostItem(
            item="GPU purchase (optional alternative to renting)",
            cost_usd_low=p_lo, cost_usd_high=p_hi, cadence="one-time",
            required=False, note=p_note,
        ))

    return items


# ─── per-subnet overrides (hand-verified from each repo's env.example/README) ─
# Keys: netuid → dict matching CostEstimate fields. The override REPLACES
# the baseline entirely for that subnet (so include hosting too).

_SUBNET_OVERRIDES: dict[int, dict[str, Any]] = {
    33: {  # ReadyAI / Conversation Genome — afterpartyai/bittensor-conversation-genome-project
        "items": [
            CostItem(
                item="OpenAI API (mandatory)",
                cost_usd_low=30, cost_usd_high=200, cadence="monthly",
                required=True,
                note="text-embedding-3-large embeddings are pinned by the protocol "
                     "for validator/miner compatibility — cannot be overridden. "
                     "GPT-5.2 is the default for completions but may be swapped "
                     "for Groq / Anthropic / OpenRouter / Chutes (still need OpenAI for embeddings).",
            ),
            CostItem(
                item="LLM completions (alternative provider, optional)",
                cost_usd_low=0, cost_usd_high=200, cadence="monthly",
                required=False,
                note="Groq / Anthropic / OpenRouter / Chutes if overriding the default GPT-5.2",
            ),
            CostItem(
                item="VPS (Linux, 2 vCPU)",
                cost_usd_low=5, cost_usd_high=20, cadence="monthly",
                required=True,
                note="no GPU — all inference is via cloud LLM API",
            ),
            CostItem(
                item="WandB account (validator only, optional)",
                cost_usd_low=0, cost_usd_high=50, cadence="monthly",
                required=False,
                note="free tier sufficient for most validators",
            ),
        ],
        "summary_notes": [
            "**Subscription required:** yes — OpenAI account with paid usage credits.",
            "Embedding model is hard-pinned to `text-embedding-3-large`; you "
            "cannot avoid OpenAI even if you swap the completion model.",
            "Heavy winner-take-all reward shape (top-1 captures ~91% of incentive). "
            "Cloud-API costs only pay off if you can land in the top tier.",
        ],
        "needs_paid_api": True,
        "needs_gpu": False,
        "confidence": "verified",
    },
    64: {  # Chutes — high GPU rental, no API keys
        "items": [
            CostItem(
                item="GPU rental (you provide compute to the network)",
                cost_usd_low=500, cost_usd_high=3500, cadence="monthly",
                required=True,
                note="this subnet IS the compute marketplace — your earnings = "
                     "your spare GPU capacity rented out. Buy or rent A10/A100/H100.",
            ),
            CostItem(
                item="VPS for the miner orchestrator",
                cost_usd_low=10, cost_usd_high=30, cadence="monthly",
                required=True,
                note="small CPU instance to run the Chutes node alongside the GPUs",
            ),
            CostItem(
                item="GPU purchase (long-term ROI play)",
                cost_usd_low=8000, cost_usd_high=35000, cadence="one-time",
                required=False,
                note="A100 80 GB ~$15-20k, H100 80 GB ~$25-35k. Owning beats "
                     "renting if utilization > 70% over 12+ months.",
            ),
        ],
        "summary_notes": [
            "**Subscription required:** no API keys — but you need real GPUs.",
            "Earnings track GPU type × utilization × spot price for the model "
            "the network is currently routing.",
        ],
        "needs_paid_api": False,
        "needs_gpu": True,
        "confidence": "verified",
    },
    4: {  # Targon — multi-modal inference
        "items": [
            CostItem(
                item="GPU rental (H100 strongly preferred)",
                cost_usd_low=1500, cost_usd_high=3500, cadence="monthly",
                required=True,
                note="multi-modal inference benchmark — A100 is competitive, "
                     "H100 dominates throughput-based scoring",
            ),
            CostItem(
                item="VPS for orchestration",
                cost_usd_low=10, cost_usd_high=30, cadence="monthly",
                required=True,
                note="",
            ),
        ],
        "summary_notes": [
            "**Subscription required:** no — no third-party API keys needed.",
            "Hardware-heavy: A100/H100 class GPU is the table stakes.",
        ],
        "needs_paid_api": False,
        "needs_gpu": True,
        "confidence": "verified",
    },
    19: {  # Inference (Nineteen)
        "items": [
            CostItem(
                item="GPU rental (A100 / H100)",
                cost_usd_low=1500, cost_usd_high=3500, cadence="monthly",
                required=True,
                note="LLM inference subnet; throughput is the scoring axis",
            ),
            CostItem(
                item="VPS for the orchestrator",
                cost_usd_low=10, cost_usd_high=30, cadence="monthly",
                required=True,
                note="",
            ),
        ],
        "summary_notes": [
            "**Subscription required:** no API keys.",
            "Pure GPU-throughput play; bigger GPU = higher rank.",
        ],
        "needs_paid_api": False,
        "needs_gpu": True,
        "confidence": "verified",
    },
    59: {  # Babelbit — Chutes-deployed, anti-Sybil flat rewards
        "items": [
            CostItem(
                item="Chutes container hosting",
                cost_usd_low=20, cost_usd_high=100, cadence="monthly",
                required=True,
                note="miners are deployed as Chutes containers; pay per GPU-second of usage",
            ),
            CostItem(
                item="VPS (operator coordinator)",
                cost_usd_low=5, cost_usd_high=20, cadence="monthly",
                required=True,
                note="small Linux VPS to manage the deployment",
            ),
        ],
        "summary_notes": [
            "**Subscription required:** Chutes account (free signup, pay-per-use).",
            "Flat reward shape (anti-Sybil): top-1 share <1% so consistent "
            "uptime + correct deployment matter more than raw compute.",
        ],
        "needs_paid_api": False,
        "needs_gpu": False,
        "confidence": "verified",
    },
}


# ─── public API ─────────────────────────────────────────────────────────────


def estimate(
    netuid: int,
    category: str | None,
    gpu_need: str | None,
    burn_tao: float,
    tao_usd: float | None,
) -> CostEstimate:
    """Return a CostEstimate for `netuid`, including the registration burn fee.

    Always adds a "Registration burn fee" item dynamically from `burn_tao`
    (converted to USD if `tao_usd` is provided).
    """
    override = _SUBNET_OVERRIDES.get(int(netuid))
    if override is not None:
        est = CostEstimate(
            items=list(override["items"]),
            summary_notes=list(override.get("summary_notes", [])),
            needs_paid_api=override.get("needs_paid_api", False),
            needs_gpu=override.get("needs_gpu", False),
            confidence="verified",
        )
    else:
        items = _baseline_items(category or "other", gpu_need or "varies")
        est = CostEstimate(
            items=items,
            summary_notes=[
                "Estimates are baseline ranges from the subnet's category and "
                "GPU need; check the subnet's `env.example` / README for "
                "specific API key requirements.",
            ],
            needs_paid_api=any(
                "API" in i.item and i.required for i in items
            ),
            needs_gpu=(gpu_need or "").lower() in ("low", "medium", "heavy"),
            confidence="baseline",
        )

    # Registration burn fee — always recomputed from live chain data.
    burn_usd_low = burn_usd_high = 0.0
    if tao_usd and burn_tao > 0:
        burn_usd_low = burn_tao * tao_usd
        burn_usd_high = burn_tao * tao_usd * 1.2  # +20% buffer for fee jitter
    elif burn_tao > 0:
        # Fall back to TAO denomination if USD rate isn't available.
        burn_usd_low = burn_usd_high = 0.0

    burn_note_parts = [f"{burn_tao:.4f} τ"]
    if tao_usd:
        burn_note_parts.append(f"@ ${tao_usd:,.2f}/τ")
    burn_note_parts.append("includes ~20% buffer for fee jitter")

    est.items.insert(0, CostItem(
        item="Registration burn fee (one-time per UID)",
        cost_usd_low=burn_usd_low,
        cost_usd_high=burn_usd_high,
        cadence="one-time",
        required=True,
        note=" · ".join(burn_note_parts),
    ))

    return est


def render_markdown(est: CostEstimate) -> str:
    """Return a markdown block for the cost section of an analysis page."""
    lines: list[str] = []

    # Quick-glance flags.
    flags: list[str] = []
    flags.append("🔑 paid API key required" if est.needs_paid_api
                 else "✅ no paid API keys required")
    flags.append("🖥️ GPU required" if est.needs_gpu
                 else "💻 no GPU required")
    if est.confidence == "verified":
        flags.append("📋 verified from repo")
    else:
        flags.append("ℹ️ baseline estimate (check repo to verify)")
    lines.append(" · ".join(flags))
    lines.append("")

    # Headline numbers.
    m_lo, m_hi = est.monthly_low, est.monthly_high
    o_lo, o_hi = est.one_time_low, est.one_time_high
    lines.append(
        f"**Monthly recurring:** ~${_fmt_usd(m_lo)} – ${_fmt_usd(m_hi)} "
        f"· **One-time setup:** ~${_fmt_usd(o_lo)} – ${_fmt_usd(o_hi)}"
    )
    lines.append("")

    # Itemized table.
    monthly = [i for i in est.items if i.cadence == "monthly"]
    onetime = [i for i in est.items if i.cadence == "one-time"]

    if monthly:
        lines.append("### Monthly costs")
        lines.append("")
        lines.append("| Item | Required | USD / month | Notes |")
        lines.append("|---|:-:|---|---|")
        for i in monthly:
            req = "✓" if i.required else "—"
            cost = _fmt_range(i.cost_usd_low, i.cost_usd_high)
            lines.append(f"| {i.item} | {req} | {cost} | {i.note} |")
        lines.append("")

    if onetime:
        lines.append("### One-time costs")
        lines.append("")
        lines.append("| Item | Required | USD | Notes |")
        lines.append("|---|:-:|---|---|")
        for i in onetime:
            req = "✓" if i.required else "—"
            cost = _fmt_range(i.cost_usd_low, i.cost_usd_high)
            lines.append(f"| {i.item} | {req} | {cost} | {i.note} |")
        lines.append("")

    if est.summary_notes:
        for n in est.summary_notes:
            lines.append(f"> {n}")
        lines.append("")

    return "\n".join(lines)


# ─── helpers ────────────────────────────────────────────────────────────────


def _fmt_usd(v: float) -> str:
    if v <= 0:
        return "0"
    if v < 10:
        return f"{v:.2f}"
    if v < 1000:
        return f"{v:.0f}"
    return f"{v:,.0f}"


def _fmt_range(lo: float, hi: float) -> str:
    if lo == hi:
        return f"${_fmt_usd(lo)}"
    return f"${_fmt_usd(lo)} – ${_fmt_usd(hi)}"
