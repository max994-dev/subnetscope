"""Auto-analysis generator and hourly scheduler.

For every subnet in the current scan, generates a markdown analysis file at
``analyses/auto/sn<N>.md``.  Hand-curated files at ``analyses/sn<N>.md``
always take precedence — the auto-generator never touches them.

The generated file contains:
  * A live metrics table (burn fee, slots, emission, top-1 share, …)
  * The easy-entry score breakdown with "why" bullets
  * Historical trend notes (if state.db has enough snapshots)
  * Category-specific advice drawn from a static knowledge base
  * A clear "Auto-generated" banner so users know it hasn't been reviewed

Refresh cadence: every ``interval_seconds`` (default 3600 = 1 hour).
The first run fires immediately after startup so pages aren't empty.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..types import SubnetRow
from .cost_estimator import estimate as estimate_costs, render_markdown as render_costs_md
from .score import ScoreBreakdown

log = logging.getLogger(__name__)

# ── category-level context strings ──────────────────────────────────────────

_CATEGORY_NOTES: dict[str, str] = {
    "compute": (
        "This subnet rents raw GPU or CPU compute to the network. "
        "Earnings are proportional to hardware quality and uptime. "
        "You need actual GPU hardware; no AI modelling skills are required."
    ),
    "data": (
        "This subnet collects, cleans, or enriches data. "
        "No GPU is usually needed. The competitive edge is data sourcing "
        "(proxies, API access, domain coverage) and freshness."
    ),
    "trading": (
        "This subnet aggregates trading strategies or price-path forecasts. "
        "No GPU; pure quantitative / statistical modelling. "
        "Reward shapes vary — check whether it's CRPS-scored, Brier-scored, "
        "or Sharpe/drawdown-gated."
    ),
    "storage": (
        "This subnet provides decentralised storage. "
        "Earnings depend on disk size, retrieval speed, and uptime. "
        "No GPU required; invest in SSD and upload bandwidth instead."
    ),
    "text": (
        "This subnet produces or evaluates natural-language content. "
        "A local LLM (Llama 3.1 8B) is usually sufficient; "
        "GPU is helpful for fast inference but not always required."
    ),
    "image": (
        "This subnet processes or generates images. "
        "A mid-range GPU (8+ GB VRAM) is typically needed."
    ),
    "audio": (
        "This subnet produces or evaluates audio / speech content. "
        "Whisper-class models need 4–8 GB VRAM; smaller tasks can run on CPU."
    ),
    "agent": (
        "This subnet runs autonomous agents that perceive and act on data. "
        "Requirements vary widely — read the repo docs carefully."
    ),
    "code": (
        "This subnet generates or evaluates code. "
        "A local code-focused LLM (CodeLlama, Qwen Coder) is competitive; "
        "solutions are tested against unit tests so correctness matters most."
    ),
    "science": (
        "This subnet applies ML to a scientific domain (genomics, chemistry, "
        "physics, etc.). Domain expertise gives a significant edge."
    ),
    "forecasting": (
        "This subnet produces probabilistic predictions on real-world events. "
        "Calibration (Brier score / CRPS) matters more than peak accuracy. "
        "No GPU needed; quality of your statistical model determines ranking."
    ),
    "other": (
        "Category information is limited. Check the subnet's GitHub and "
        "Discord for specifics before investing time or registration fees."
    ),
}

_GPU_NOTES: dict[str, str] = {
    "none": "No GPU required — a cheap VPS or spare CPU server works.",
    "low":  "Low GPU need — a 4–8 GB consumer card (GTX 1080, RTX 3060) is enough.",
    "medium": "Medium GPU — aim for an RTX 3090 / A10 (10–24 GB VRAM).",
    "heavy": "Heavy GPU — A100 or H100 class hardware gives the most competitive edge.",
    "varies": "GPU requirements vary; check the repo docs for the exact model size.",
}

_REWARD_NOTES: dict[str, str] = {
    "winner-take-all": (
        "**Winner-take-all:** the single best scorer takes all (or nearly all) "
        "emission each round. Expect lumpy, high-variance income. "
        "You need to be consistently near the top to earn meaningfully."
    ),
    "top-n": (
        "**Top-N rewards:** emission concentrates in the top-N miners. "
        "Breaking into the top tier is key; mid-pack earns modestly."
    ),
    "proportional": (
        "**Proportional rewards:** emission is distributed based on relative "
        "score. Consistent improvement directly translates to higher earnings."
    ),
    "flat": (
        "**Flat rewards:** all miners above a quality threshold earn equally. "
        "Low barrier to earning, but limited upside for the very best."
    ),
    "unknown": (
        "Reward shape is not yet categorised. Check the subnet's incentive "
        "mechanism docs to understand how emissions are distributed."
    ),
}

# ── formatting helpers ───────────────────────────────────────────────────────

def _fmt_burn(v: float) -> str:
    if v >= 1:
        return f"{v:.4f} τ"
    if v >= 0.01:
        return f"{v:.5f} τ"
    return f"{v:.6f} τ"


def _fmt_slots(r: SubnetRow) -> str:
    used, max_n, free = r.subnetwork_n, r.max_n, r.slots_free
    if max_n == 0:
        return "—"
    status = "FULL" if free == 0 else f"{free} free"
    return f"{used}/{max_n} ({status})"


def _score_badge(score: float) -> str:
    if score >= 60:
        return "🟢 high"
    if score >= 40:
        return "🟡 medium"
    return "🔴 low"


def _trend_note(history: list[dict]) -> str:
    """Return a one-sentence trend note from raw snapshot rows."""
    if len(history) < 2:
        return ""
    burns = [p["burn_tao"] for p in history if p.get("burn_tao") is not None]
    miners = [p["active_miners"] for p in history
              if p.get("active_miners") is not None]
    notes = []
    if len(burns) >= 2:
        pct = (burns[-1] - burns[0]) / max(burns[0], 1e-9) * 100
        if abs(pct) >= 10:
            direction = "risen" if pct > 0 else "fallen"
            notes.append(
                f"Burn fee has {direction} ~{abs(pct):.0f}% over the last "
                f"{len(history)} snapshots."
            )
    if len(miners) >= 2:
        delta = miners[-1] - miners[0]
        if abs(delta) >= 5:
            direction = "grown" if delta > 0 else "shrunk"
            notes.append(
                f"Active miner count has {direction} by {abs(delta)} "
                f"since the first recorded snapshot."
            )
    return "  ".join(notes)


# ── markdown template ────────────────────────────────────────────────────────

def _render(r: SubnetRow, sb: ScoreBreakdown,
            history: list[dict], now_iso: str,
            tao_usd: float | None = None) -> str:
    cat = (r.category or "other").lower()
    gpu = (r.gpu_need or "?").lower()
    reward = (r.reward_shape or "unknown").lower()
    name = r.name or f"sn{r.netuid}"

    cat_note = _CATEGORY_NOTES.get(cat, _CATEGORY_NOTES["other"])
    gpu_note = _GPU_NOTES.get(gpu, f"GPU need listed as **{gpu}**.")
    reward_note = _REWARD_NOTES.get(reward, _REWARD_NOTES["unknown"])

    trend = _trend_note(history)
    trend_section = (
        f"\n> **Trend (from {len(history)} snapshots):** {trend}\n"
        if trend else ""
    )

    why_bullets = "\n".join(f"- {w}" for w in (sb.why or []))
    if not why_bullets:
        why_bullets = "- No score breakdown available yet."

    burn_demand_bar = ""
    if (r.max_burn_tao > r.min_burn_tao and r.recycle_tao > 0):
        pct = (r.recycle_tao - r.min_burn_tao) / (
            r.max_burn_tao - r.min_burn_tao) * 100
        filled = math.floor(pct / 5)
        bar = "█" * filled + "░" * (20 - filled)
        burn_demand_bar = f" `[{bar}]` {pct:.1f}% of max"

    links_section = ""
    if r.github_repo:
        links_section += f"- GitHub: <{r.github_repo}>\n"
    if r.subnet_url:
        links_section += f"- Website: <{r.subnet_url}>\n"
    if r.discord:
        links_section += f"- Discord: <{r.discord}>\n"
    links_section += f"- TAO.app: <https://tao.app/subnet/{r.netuid}>\n"

    emission_day = r.emission_per_day or 0
    top1_pct = f"{r.top1_share * 100:.1f}%" if r.top1_share is not None else "—"
    age = f"{r.age_days:.0f} days" if r.age_days else "—"

    cost_est = estimate_costs(
        netuid=r.netuid,
        category=r.category,
        gpu_need=r.gpu_need,
        burn_tao=r.recycle_tao or 0.0,
        tao_usd=tao_usd,
    )
    cost_section_md = render_costs_md(cost_est)

    return f"""# Subnet {r.netuid} — {name}

> Auto-analyzed: {now_iso} · easy-entry score: {sb.score:.1f}/100 · refreshes hourly

*This analysis is auto-generated from live chain data. It updates every hour.
For hand-curated notes, check the Bittensor Discord or the subnet's own docs.*

## Quick stats

| Metric | Value |
|---|---|
| Category | {r.category or "?"} |
| GPU need | {r.gpu_need or "?"} |
| Reward shape | {r.reward_shape or "?"} |
| Active miners | {r.active_miners or "—"} |
| UID slots | {_fmt_slots(r)} |
| Burn fee | {_fmt_burn(r.recycle_tao)}{burn_demand_bar} |
| Burn min / max | {_fmt_burn(r.min_burn_tao)} → {_fmt_burn(r.max_burn_tao)} |
| Emission / day | {emission_day:,.4f} τ |
| Top-1 share | {top1_pct} |
| Liquidity (TAO in) | {r.tao_in:,.1f} τ |
| Price (τ/α) | {r.price_tao_per_alpha:.6f} |
| Age | {age} |

{trend_section}
## Easy-entry score: {sb.score:.0f} / 100  {_score_badge(sb.score)}

{why_bullets}

| Component | Score |
|---|---|
| GPU friction | {sb.gpu:.1f} / 20 |
| Decentralization (top-1) | {sb.top1:.1f} / 20 |
| Active miners | {sb.miners:.1f} / 15 |
| Free slots | {sb.slots:.1f} / 15 |
| Burn fee | {sb.fee:.1f} / 15 |
| Liquidity | {sb.liquidity:.1f} / 10 |
| Emission | {sb.emission:.1f} / 5 |

## What is known about this category

{cat_note}

## GPU / hardware

{gpu_note}

## Reward shape

{reward_note}

## Cost to operate a miner

{cost_section_md}
## Getting started (generic)

1. Search for this subnet's GitHub/Discord using the links below.
2. Set up a Bittensor wallet: hot+cold key pair.
3. Fund with burn fee + buffer (~{r.recycle_tao * 1.2:.4f} τ recommended).
4. Clone the subnet repo, run the miner in test mode first.
5. `btcli subnet register --netuid {r.netuid} --wallet.name <cold> --wallet.hotkey <hot>`
6. Monitor your score during the immunity window.

## Links

{links_section}
"""


# ── generator ────────────────────────────────────────────────────────────────

def generate_all(
    analyses_dir: Path,
    rows: list[SubnetRow],
    scores: dict[int, ScoreBreakdown],
    db,                      # StateDB instance
    tao_usd: float | None = None,
) -> tuple[int, int]:
    """Write auto-analyses for subnets that don't have a hand-curated file.

    Returns (generated, skipped). Pass `tao_usd` so the registration burn
    fee can be expressed in dollars in the cost section.
    """
    auto_dir = analyses_dir / "auto"
    auto_dir.mkdir(parents=True, exist_ok=True)

    now_iso = datetime.now(tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M %Z")
    generated = skipped = 0

    for r in rows:
        manual = analyses_dir / f"sn{r.netuid}.md"
        if manual.is_file():
            skipped += 1
            continue

        sb = scores.get(r.netuid)
        if sb is None:
            continue

        try:
            history = db.history(r.netuid, hours=24)
        except Exception:
            history = []

        try:
            md = _render(r, sb, history, now_iso, tao_usd=tao_usd)
        except Exception:
            log.exception("auto-analyze render failed for netuid=%d", r.netuid)
            continue

        out = auto_dir / f"sn{r.netuid}.md"
        out.write_text(md, encoding="utf-8")
        generated += 1

    return generated, skipped


# ── background scheduler ─────────────────────────────────────────────────────

class AutoAnalyzer:
    """Fires ``generate_all`` at startup and then every ``interval_seconds``."""

    def __init__(self, analyses_dir: Path, interval_seconds: int = 3600):
        self.analyses_dir = analyses_dir
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run: float = 0.0
        self._last_generated: int = 0
        self._last_skipped: int = 0

    def _get_scanner(self):
        from .cache import get_scanner
        return get_scanner()

    def _run_once(self) -> None:
        scanner = self._get_scanner()
        scan = scanner.get()
        scores = scanner.scores()
        if not scan.rows or not scores:
            log.warning("auto-analyzer: scan empty, skipping")
            return

        # Pull the live TAO/USD rate so the cost section can convert the
        # registration burn fee into dollars. Fall back to None if the
        # tao_price service hasn't been initialised or is still cold.
        tao_usd: float | None = None
        try:
            from .tao_price import get_tao_price_cache
            spot = get_tao_price_cache().get_spot()
            tao_usd = spot.get("usd") if isinstance(spot, dict) else None
        except Exception:
            tao_usd = None

        g, s = generate_all(self.analyses_dir, scan.rows, scores, scanner.db,
                            tao_usd=tao_usd)
        self._last_run = time.time()
        self._last_generated = g
        self._last_skipped = s
        log.info(
            "auto-analyzer: generated=%d  skipped(hand-curated)=%d  tao_usd=%s",
            g, s, f"${tao_usd:.2f}" if tao_usd else "n/a",
        )

    def _loop(self) -> None:
        # First run: wait until the scanner has data (prewarm may still be running).
        for _ in range(120):   # max 2 min wait
            if self._stop.is_set():
                return
            scanner = self._get_scanner()
            if scanner.cache_age_seconds() >= 0:
                break
            time.sleep(2)

        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception:
                log.exception("auto-analyzer: run_once failed")
            self._stop.wait(timeout=self.interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="auto-analyzer", daemon=True)
        self._thread.start()
        log.info("auto-analyzer: started (interval=%ds)", self.interval)

    def stop(self) -> None:
        self._stop.set()

    @property
    def status(self) -> dict:
        return {
            "last_run": datetime.fromtimestamp(self._last_run,
                                               tz=timezone.utc).isoformat()
                        if self._last_run else None,
            "last_generated": self._last_generated,
            "last_skipped": self._last_skipped,
            "interval_seconds": self.interval,
            "running": bool(self._thread and self._thread.is_alive()),
        }


# Module singleton.
_analyzer: AutoAnalyzer | None = None


def init_auto_analyzer(analyses_dir: Path,
                       interval_seconds: int = 3600) -> AutoAnalyzer:
    global _analyzer
    if _analyzer:
        _analyzer.stop()
    _analyzer = AutoAnalyzer(analyses_dir, interval_seconds)
    _analyzer.start()
    return _analyzer


def get_auto_analyzer() -> AutoAnalyzer | None:
    return _analyzer
