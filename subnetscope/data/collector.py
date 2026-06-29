"""Top-level orchestrator: gathers SDK rows, enriches with Taostats, and
labels each row with a category."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from ..categorize import Categorizer
from ..config import Config
from ..types import ScanResult, SubnetRow
from .sdk import SDKClient
from .taostats import TaostatsClient

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int], None]


class Collector:
    def __init__(self, cfg: Config, sdk: SDKClient, taostats: TaostatsClient,
                 categorizer: Categorizer):
        self.cfg = cfg
        self.sdk = sdk
        self.taostats = taostats
        self.categorizer = categorizer

    def scan(self, progress_cb: ProgressFn | None = None) -> ScanResult:
        head = self.sdk.current_block()

        # Build the netuid universe from config.
        all_netuids = self.sdk.list_subnets(include_root=self.cfg.universe.include_root)
        whitelist = set(self.cfg.universe.whitelist_netuids or [])
        blacklist = set(self.cfg.universe.blacklist_netuids or [])
        if whitelist:
            netuids = [n for n in all_netuids if n in whitelist]
        else:
            netuids = [n for n in all_netuids if n not in blacklist]
        log.info("Scanning %d subnets (head block %d)", len(netuids), head)

        rows, failures = self.sdk.fetch_all_rows(
            netuids, head_block=head, progress_cb=progress_cb,
            fetch_metagraph=self.cfg.scan.fetch_metagraph,
        )

        # Enrich with Taostats descriptions when SDK identity is missing.
        ts_meta = self.taostats.fetch_subnet_metadata()
        if ts_meta:
            for r in rows:
                meta = ts_meta.get(r.netuid)
                if not meta:
                    continue
                if not r.name and meta.get("name"):
                    r.name = meta["name"]
                    r.name_source = "taostats"
                if not r.description and meta.get("description"):
                    r.description = meta["description"]
                if not r.github_repo and meta.get("github_repo"):
                    r.github_repo = meta["github_repo"]
                if not r.subnet_url and meta.get("subnet_url"):
                    r.subnet_url = meta["subnet_url"]
                if not r.discord and meta.get("discord"):
                    r.discord = meta["discord"]

        self.categorizer.apply(rows)

        return ScanResult(
            rows=rows,
            head_block=head,
            fetched_at=datetime.now(timezone.utc),
            failures=failures,
        )

    def close(self) -> None:
        self.sdk.close()
        self.taostats.close()


def build_collector(cfg: Config) -> Collector:
    sdk = SDKClient(network=cfg.network, scan=cfg.scan)
    taostats = TaostatsClient(api_key=cfg.network.taostats_api_key)
    categorizer = Categorizer.load(cfg.categorize)
    return Collector(cfg=cfg, sdk=sdk, taostats=taostats, categorizer=categorizer)


_GPU_RANK = {"none": 0, "low": 1, "medium": 2, "varies": 3, "heavy": 4, "?": 5}
_REWARD_RANK = {"flat": 0, "topN": 1, "peak": 2, "winner": 3, "?": 4}


def _build_key_map():
    """Return {sort_key_name: row -> sortable_value}. Recreated each call so
    the closure for `_demand` can capture per-row state cleanly."""
    def _demand(r: SubnetRow) -> float:
        if r.max_burn_tao <= r.min_burn_tao or r.recycle_tao <= 0:
            return -1.0
        return (r.recycle_tao - r.min_burn_tao) / (r.max_burn_tao - r.min_burn_tao)

    return {
        "netuid":     lambda r: r.netuid,
        "fee":        lambda r: r.recycle_tao,
        "burn":       lambda r: r.recycle_tao,           # alias for fee
        "demand":     _demand,
        "name":       lambda r: (r.name or "").lower(),
        "type":       lambda r: r.category,
        "category":   lambda r: r.category,
        "gpu":        lambda r: _GPU_RANK.get(r.gpu_need, 99),
        "reward":     lambda r: _REWARD_RANK.get(r.reward_shape, 99),
        "score":      lambda r: r.easy_entry_score if r.easy_entry_score is not None else -1.0,
        "top1":       lambda r: r.top1_share if r.top1_share is not None else -1,
        "inc_burn":   lambda r: r.incentive_burn if r.incentive_burn is not None else -1,
        "own_div":    lambda r: r.owner_dividend_share if r.owner_dividend_share is not None else -1,
        "owner":      lambda r: r.owner_emission_share if r.owner_emission_share is not None else -1,
        "miners":     lambda r: r.active_miners if r.active_miners is not None else -1,
        "gini":       lambda r: r.incentive_gini if r.incentive_gini is not None else -1,
        "emission":   lambda r: r.emission_per_day,
        "liquidity":  lambda r: r.tao_in,
        "alpha_liq":  lambda r: r.alpha_in,
        "age":        lambda r: r.age_days,
        "slots_used": lambda r: r.subnetwork_n,
        "slots_free": lambda r: r.slots_free,
        "used_max":   lambda r: r.subnetwork_n / r.max_n if r.max_n > 0 else -1,
        "fullness":   lambda r: r.subnetwork_n / r.max_n if r.max_n > 0 else -1,
        "price":      lambda r: r.price_tao_per_alpha,
    }


SORT_KEY_NAMES = tuple(_build_key_map().keys())


def parse_sort_spec(spec: str | list, default_order: str = "asc") -> list[tuple[str, str]]:
    """Parse a multi-key sort spec into [(key, order), ...].

    Accepted shapes:
      - "fee"
      - "fee:asc"
      - "gpu, reward, slots_free, fee"             (default_order applied)
      - "gpu:asc, reward:asc, slots_free:desc"     (per-key direction)
      - ["gpu:asc", "reward:asc", ...]             (already-split list)

    Unknown keys are skipped silently (logged at WARNING level).
    """
    if not spec:
        return []
    if isinstance(spec, str):
        items = [s.strip() for s in spec.split(",") if s.strip()]
    else:
        items = [str(s).strip() for s in spec if str(s).strip()]

    out: list[tuple[str, str]] = []
    valid_keys = SORT_KEY_NAMES
    for it in items:
        if ":" in it:
            key, _, order = it.partition(":")
            key = key.strip().lower()
            order = order.strip().lower() or default_order
        else:
            key = it.lower()
            order = default_order
        if key not in valid_keys:
            log.warning("Unknown sort key %r — ignored", key)
            continue
        order = "desc" if order in ("desc", "descending", "down", "high") else "asc"
        out.append((key, order))
    return out


def sort_rows(rows: list[SubnetRow], sort_by, order: str = "asc") -> list[SubnetRow]:
    """Sort by a single key or a multi-key spec.

    `sort_by` may be:
      - a single key string ("fee") + `order`
      - a multi-key string ("gpu:asc, reward:asc, fee:asc")
      - a list of (key, order) tuples
    """
    key_map = _build_key_map()

    if isinstance(sort_by, str) and "," not in sort_by and ":" not in sort_by:
        spec = [(sort_by.lower(), order.lower())]
    elif isinstance(sort_by, list) and sort_by and isinstance(sort_by[0], tuple):
        spec = [(k.lower(), o.lower()) for k, o in sort_by]
    else:
        spec = parse_sort_spec(sort_by, default_order=order)

    if not spec:
        spec = [("fee", "asc")]

    # Stable multi-key sort: apply secondary keys first so the primary wins.
    result = list(rows)
    for key, dirn in reversed(spec):
        keyfn = key_map.get(key, key_map["fee"])
        result.sort(key=keyfn, reverse=(dirn == "desc"))
    return result


def format_sort_spec(spec: list[tuple[str, str]]) -> str:
    """Render a sort spec for display (e.g. 'gpu↑ reward↑ slots_free↓')."""
    parts = []
    for key, dirn in spec:
        arrow = "↓" if dirn == "desc" else "↑"
        parts.append(f"{key}{arrow}")
    return " · ".join(parts)


def filter_rows(rows: list[SubnetRow], types: list[str] | None) -> list[SubnetRow]:
    if not types:
        return rows
    wanted = {t.strip().lower() for t in types if t.strip()}
    if not wanted:
        return rows
    return [r for r in rows if r.category in wanted]
