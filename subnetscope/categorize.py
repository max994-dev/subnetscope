"""Categorize subnets into types from name + description.

Order of precedence:
  1. Manual override file (`subnetscope/categories.json`) — netuid -> category
  2. Keyword match on (name + description + github_repo)
  3. `default_category` from config (usually "other")

Categories (intentionally short list):
  agent     - autonomous agents, swarms, task execution
  llm       - language model training/inference/fine-tune
  vision    - image/video gen, vision models, multimodal
  audio     - TTS, STT, music, speech
  data      - scraping, indexing, search, oracles, social
  trading   - price prediction, alpha signals, finance, sports
  storage   - distributed storage
  compute   - GPU/compute marketplaces, containers
  science   - protein folding, biotech, research
  infra     - meta layers, validator-of-validators, hash, governance
  other     - uncategorized fallback
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import CategorizeCfg
from .types import SubnetRow

log = logging.getLogger(__name__)


CATEGORIES = (
    "agent", "llm", "vision", "audio", "data",
    "trading", "storage", "compute", "science", "infra", "other",
)

# Default GPU requirement *for mining* on each category.
#   heavy   - high-end GPU (A100/H100, 40-80 GB VRAM) — model training, large vision
#   medium  - mid-range GPU (RTX 3090/4090) — inference, audio synth
#   low     - small GPU OR CPU works for many setups (calls external APIs)
#   none    - CPU + RAM + network, no GPU needed
#   varies  - mixed (compute marketplaces / infra subnets are heterogeneous)
#   ?       - unknown
GPU_NEED_BY_CATEGORY: dict[str, str] = {
    "llm":     "heavy",
    "vision":  "heavy",
    "audio":   "medium",
    "science": "medium",
    "agent":   "low",      # most call external LLM APIs; some run small models
    "data":    "none",
    "trading": "none",
    "storage": "none",
    "compute": "varies",   # YOU sell GPU as a miner; depends on subnet
    "infra":   "varies",   # TaoHash needs Bitcoin ASICs; others CPU
    "other":   "?",
}

GPU_NEEDS = ("heavy", "medium", "low", "none", "varies", "?")

# Order matters — earlier entries win on ties. Keywords are matched as
# substrings (case-insensitive) against name + description + repo + url.
# Be specific: avoid bare words like "predict" or "forecast" that overlap
# with weather science, agent goal-seeking, etc.
KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("science",  ("protein", "folding", "biotech", "medical", "molecul",
                  "drug discovery", "genomic", "weather forecast", "climate",
                  "research", "scientific", "biology", "chemistry",
                  "omron", "gaia", "niscience", "zeus")),
    ("trading",  ("price prediction", "price forecast", "alpha signal",
                  "trading bot", "trading strategy", "algorithmic trading",
                  "sports prediction", "sportstensor", "real estate price",
                  "nextplace", "bettensor", "betting market", "synth predict",
                  "infinite games", "precog", "proprietary trading",
                  "market prediction", "stock price")),
    ("agent",    (" agent", "agentic", "autogpt", "autonomous", "swarm",
                  "tool use", "cortex.t", "bitagent", "ridges", "agent arena")),
    ("llm",      ("llm", "language model", " chat ", "completion", "gpt-",
                  "mistral", "llama", "phi-", "claude", "pretrain", "fine-tune",
                  "finetune", "dippy", "roleplay", "targon", "apex",
                  "macrocosmos", "nineteen", "inference network",
                  "prompt", "reasoning", "text generation")),
    ("vision",   ("vision", "image", "diffusion", "video", "sdxl",
                  "stable diffusion", "image generation", "picture",
                  "alchemy", "score vision", "404 gen", "bitmind",
                  "wombo", "omega multi")),
    ("audio",    (" audio ", "voice", "tts", "stt", "speech", "music",
                  "sound", "speech-to-text", "text-to-speech")),
    ("storage",  ("storage", "ipfs", "file system", "object store", "storb")),
    ("compute",  ("compute", "gpu", "container", "kubernetes", "cluster",
                  "computehorde", "chutes", "render", "serverless compute")),
    ("data",     ("data", "scraping", "indexing", "search engine",
                  "oracle", "social network", "twitter", "x.com", "reddit",
                  "bitads", "smart scrape", "kaito", "masa", "graphite",
                  "dataverse", "chunking", "readyai", "condense", "amorphic",
                  "dojo", "efrontier", "docs insight", "fakenews", "supervoid",
                  "open kaito")),
    ("infra",    ("validator-of-validators", "meta layer", "registry",
                  "governance", "subvortex", "edgemaxxing", "taohash",
                  "de-ai", "bitsec", "red team", "root subnet")),
]


# Per-netuid GPU overrides — entries override the per-category default above.
# Useful for subnets whose actual hardware differs from their category default
# (e.g. TaoHash is "infra" but actually needs Bitcoin ASICs).
GPU_OVERRIDES: dict[int, str] = {
    # 63: "asic",   # TaoHash uses Bitcoin SHA-256 ASICs — uncomment if/when relevant
}


def gpu_need_for(category: str, netuid: int | None = None) -> str:
    if netuid is not None and netuid in GPU_OVERRIDES:
        return GPU_OVERRIDES[netuid]
    return GPU_NEED_BY_CATEGORY.get(category, "?")


def _concentration_from_positive_incentives(nz_sorted_desc: list[float]) -> dict[str, float | int]:
    """Internal: ``nz_sorted_desc`` = positive incentives sorted high → low."""
    n = len(nz_sorted_desc)
    total = sum(nz_sorted_desc)
    if n == 0 or total <= 0:
        return {}

    nz = nz_sorted_desc
    top1 = nz[0] / total
    top5 = sum(nz[:5]) / total
    top10 = sum(nz[:10]) / total
    top50 = sum(nz[:50]) / total

    asc = list(reversed(nz))
    cum = 0.0
    for i, x in enumerate(asc, 1):
        cum += i * x
    gini = (2 * cum) / (n * total) - (n + 1) / n if total > 0 else 0.0
    gini = max(0.0, min(1.0, gini))

    return {
        "active_miners": n,
        "top1_share": top1,
        "top5_share": top5,
        "top10_share": top10,
        "top50_share": top50,
        "gini": gini,
    }


def incentive_concentration(incentives: list[float]) -> dict[str, float | int]:
    """Compute concentration from the full metagraph ``incentive`` vector.

    Prefer :func:`miner_incentive_concentration` for directory tables — that
    excludes validator rows so ``top1_share`` matches “among miners”, not
    “largest single UID (possibly a validator)”.
    """
    nz = sorted((float(x) for x in incentives if float(x) > 0), reverse=True)
    return _concentration_from_positive_incentives(nz)


def miner_incentive_concentration(
    incentives: list[float],
    validator_permit: list[bool] | None,
) -> dict[str, float | int]:
    """Concentration among **non-validator** UIDs only (typical miner view).

    If ``validator_permit`` is missing or shorter than ``incentives``,
    missing entries are treated as non-validators. If ``validator_permit``
    is completely absent (None and no list), falls back to the full-vector
    behaviour (same as :func:`incentive_concentration`).
    """
    if not incentives:
        return {}
    if validator_permit is None:
        return incentive_concentration(incentives)

    n_inc = len(incentives)
    vp = list(validator_permit[:n_inc])
    if len(vp) < n_inc:
        vp.extend([False] * (n_inc - len(vp)))

    miner_vals: list[float] = []
    for i in range(n_inc):
        if i < len(vp) and vp[i]:
            continue
        try:
            f = float(incentives[i])
        except (TypeError, ValueError):
            f = 0.0
        if f > 0:
            miner_vals.append(f)
    miner_vals.sort(reverse=True)
    return _concentration_from_positive_incentives(miner_vals)


def reward_shape_from_distribution(metrics: dict) -> str:
    """Classify the miner reward curve from real concentration metrics.

      winner - top miner takes >50% of incentive (effectively winner-take-all)
      peak   - top 5 take >80%
      topN   - top 10 take >80% (the typical Bittensor shape)
      flat   - rewards spread broadly
      ?      - no data (no active miners or fetch disabled)
    """
    if not metrics:
        return "?"
    top1 = metrics.get("top1_share", 0)
    top5 = metrics.get("top5_share", 0)
    top10 = metrics.get("top10_share", 0)
    if top1 >= 0.50:
        return "winner"
    if top5 >= 0.80:
        return "peak"
    if top10 >= 0.80:
        return "topN"
    return "flat"


@dataclass
class Categorizer:
    cfg: CategorizeCfg
    overrides: dict[int, str]
    overrides_path: Path

    @classmethod
    def load(cls, cfg: CategorizeCfg, overrides_path: Path | str | None = None) -> "Categorizer":
        path = Path(overrides_path or Path(__file__).parent / "categories.json")
        overrides: dict[int, str] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for k, v in raw.items():
                    if k.startswith("_"):
                        continue
                    try:
                        netuid = int(k)
                    except (TypeError, ValueError):
                        continue
                    cat = str(v).strip().lower()
                    if cat in CATEGORIES:
                        overrides[netuid] = cat
            except Exception as e:  # noqa: BLE001
                log.warning("Failed to read categories override file %s: %s", path, e)
        return cls(cfg=cfg, overrides=overrides, overrides_path=path)

    def classify(self, row: SubnetRow) -> str:
        if self.cfg.use_overrides and row.netuid in self.overrides:
            return self.overrides[row.netuid]

        haystack = " ".join(filter(None, [
            row.name or "",
            row.description or "",
            row.github_repo or "",
            row.subnet_url or "",
        ])).lower()
        if not haystack.strip():
            return self.cfg.default_category

        for category, terms in KEYWORDS:
            for term in terms:
                if term in haystack:
                    return category
        return self.cfg.default_category

    def apply(self, rows: list[SubnetRow]) -> None:
        """Mutate rows in place: category, gpu_need, reward_shape.

        `reward_shape` requires `top*_share` to already be populated by the
        SDK metagraph fetch. Falls back to "?" if those are None.
        """
        for r in rows:
            r.category = self.classify(r)
            r.gpu_need = gpu_need_for(r.category, r.netuid)
            r.reward_shape = reward_shape_from_distribution({
                "top1_share": r.top1_share,
                "top5_share": r.top5_share,
                "top10_share": r.top10_share,
            } if r.top1_share is not None else {})
