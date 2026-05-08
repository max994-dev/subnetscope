"""CSV + JSON export helpers for subnet rows."""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .types import ScanResult, SubnetRow

CSV_FIELDS = [
    "netuid", "name", "category", "gpu_need", "reward_shape",
    "recycle_tao", "min_burn_tao", "max_burn_tao", "difficulty",
    "burn_registration_allowed", "pow_registration_allowed",
    "subnetwork_n", "max_n", "slots_free", "max_validators",
    "tao_in", "alpha_in", "price_tao_per_alpha",
    "emission_per_block", "emission_per_day",
    "age_blocks", "age_days",
    "active_miners", "top1_share", "top5_share", "top10_share",
    "top50_share", "incentive_gini",
    "rho", "kappa", "alpha_high", "alpha_low",
    "alpha_sigmoid_steepness", "liquid_alpha_enabled",
    "immunity_period", "tempo", "yuma_version",
    "commit_reveal_enabled", "weights_rate_limit",
    "github_repo", "subnet_url", "discord", "name_source",
    "description",
]


def _row_to_dict(r: SubnetRow) -> dict:
    d = asdict(r)
    if isinstance(d.get("fetched_at"), datetime):
        d["fetched_at"] = d["fetched_at"].isoformat()
    return d


def export_csv(rows: list[SubnetRow], path: str | Path) -> Path:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            d = _row_to_dict(r)
            for k in ("name", "description", "github_repo", "subnet_url", "discord"):
                if d.get(k) is None:
                    d[k] = ""
            w.writerow(d)
    return p


def export_json(scan: ScanResult, path: str | Path) -> Path:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": scan.fetched_at.isoformat(),
        "head_block": scan.head_block,
        "subnet_count": len(scan.rows),
        "failures": scan.failures,
        "subnets": [_row_to_dict(r) for r in scan.rows],
    }
    with p.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return p
