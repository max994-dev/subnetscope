"""FastAPI app for the subnetscope web dashboard.

Routes:
  GET  /                         unified subnet table (score-ranked by default)
  GET  /dashboard                alias for /
  GET  /recommendations          redirect to / (legacy)
  GET  /subnet/{netuid}          detail page with sparklines

  GET  /api/rows                 HTMX partial: filtered/sorted table body
                                   (query ``tempo_only=1`` = subnets within tempo-near window)
  GET  /api/health               cache age, JSON
  GET  /api/scan-progress        first-scan progress (pct, phase)
  GET  /api/subnet/{netuid}      JSON detail
  GET  /api/score/{netuid}       JSON score breakdown
  GET  /api/recommendations      JSON top-N
  GET  /api/history/{netuid}     JSON time-series for sparklines
  GET  /api/alerts               JSON recent alerts
  GET  /api/analysis/{netuid}    JSON: rendered analysis HTML if file exists
  GET  /api/analyses             JSON: list of all netuids with any analysis
  POST /api/analyses/refresh     trigger immediate auto-analysis regeneration
  GET  /api/burn-live/{netuid}   JSON: lightweight 12 s-TTL burn fee (live)
  GET  /api/tao-price            JSON: live TAO/USD spot + 24h change (60 s TTL)
  GET  /api/tao-price/history    JSON: 24h price chart points (5 min TTL)
  GET  /api/coldkeys             JSON: coldkey directory configured in config.yaml
  GET  /api/coldkey/{ss58}       JSON: free TAO + per-subnet stake positions
  GET  /api/coldkey/{ss58}/history  JSON: stake value per hotkey over time (snapshots)
  GET  /api/emission-split/{netuid}  JSON: owner/validators/miners split
  GET  /api/miner-rewards/{netuid}   JSON: ranked per-miner reward distribution
  GET  /api/account              JSON: subnets a coldkey/IP is registered on (?q=)
  GET  /api/readme/{netuid}      JSON: rendered GitHub README HTML for the subnet
  GET  /static/*                 app.css, app.js, favicon.svg
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from urllib.parse import quote_plus

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.types import Scope

from ..categorize import CATEGORIES
from ..data.collector import (
    filter_rows, format_sort_spec, parse_sort_spec, sort_rows,
)
from ..types import ScanResult, SubnetRow
from .analysis import get_store as get_analysis_store
from .auto_analyzer import get_auto_analyzer, generate_all
from .burn_live import get_burn_cache
from .cache import get_scanner
from .coldkey import get_coldkey_service, is_valid_ss58
from .emission_split import get_emission_split_service
from .miner_rewards import get_miner_rewards_service
from .network_index import compute_rank_tenure, get_network_index
from .readme import get_readme_service
from .tao_price import get_tao_price_cache
from .watch_hotkeys import registration_status_for_subnet
from .alerts import (
    TEMPO_BLOCK_SECONDS,
    TEMPO_NEAR_BLOCKS,
    is_tempo_near,
    tempo_blocks_to_tick,
)

log = logging.getLogger(__name__)


def _live_scan(scanner, *, force: bool = False) -> tuple[ScanResult, bool]:
    """Return ``(scan, pending)``. When ``pending`` is True, ``scan`` is a
    placeholder and the real data is loading in the background (via prewarm or
    prior refresh). Never blocks on cold start."""
    if scanner.peek() is None:
        scanner.prewarm_async()
        return ScanResult.pending(), True
    if force:
        return scanner.get(force=True), False
    return scanner.get(force=False), False


WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Cache-bust static assets on every server restart so browsers pick up new CSS/JS.
templates.env.globals["asset_v"] = str(int(time.time()))
templates.env.filters["uquote"] = lambda s: quote_plus(str(s or ""), safe="")

GPU_OPTIONS = ["heavy", "medium", "low", "none", "varies", "?"]
SORT_KEYS = [
    "netuid", "score", "fee", "demand", "name", "type", "gpu", "reward",
    "top1", "inc_burn", "own_div", "owner", "miners", "gini", "emission",
    "liquidity", "alpha_liq", "age", "slots_used", "slots_free", "fullness",
    "price",
]


class CachedStatic(StaticFiles):
    """StaticFiles with a long Cache-Control on CSS/JS/SVG so browsers
    don't refetch them on every page nav."""

    async def get_response(self, path, scope: Scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        if resp.status_code == 200 and path.rsplit(".", 1)[-1].lower() in (
                "css", "js", "svg", "png", "ico", "woff", "woff2"):
            resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp


def _filter_gpu(rows: list[SubnetRow], gpu_needs: list[str]) -> list[SubnetRow]:
    if not gpu_needs:
        return rows
    wanted = {g.strip().lower() for g in gpu_needs if g.strip()}
    return [r for r in rows if (r.gpu_need or "?").lower() in wanted]


def _apply_filters_and_sort(
    rows: list[SubnetRow],
    *,
    types: list[str],
    gpu_needs: list[str],
    sort_spec_str: str,
    default_order: str,
    search: str,
    head_block: int = 0,
    only_tempo_near: bool = False,
) -> tuple[list[SubnetRow], list[tuple[str, str]]]:
    spec = parse_sort_spec(sort_spec_str, default_order=default_order) \
        or [("fee", "asc")]
    out = filter_rows(rows, types)
    out = _filter_gpu(out, gpu_needs)
    if search:
        q = search.strip().lower()
        out = [r for r in out if (
            q in (r.name or "").lower()
            or q in (r.description or "").lower()
            or q == str(r.netuid)
        )]
    if only_tempo_near and head_block > 0:
        out = [r for r in out if is_tempo_near(r, head_block)]
    out = sort_rows(out, spec)
    return out, spec


def _apply_real_split(split: dict, row: SubnetRow) -> dict:
    """Override the kappa-based estimate with the real metagraph-derived split.

    kappa is the consensus threshold, NOT the validator/miner emission split,
    and it hides incentive burn entirely. When we have the live owner-hotkey
    emission share, fold the burn into the owner bucket and split the rest by
    actual UID role. Mutates and returns ``split``.
    """
    if row.owner_emission_share is None:
        return split
    owner_frac = split["owner_cut_pct"] / 100.0
    rest = 1.0 - owner_frac
    owner_eff = owner_frac + rest * row.owner_emission_share
    val_eff = rest * (row.validator_emission_share or 0.0)
    min_eff = rest * (row.miner_emission_share or 0.0)
    emi = split["emission_per_day_tao"]
    split.update({
        "owner_pct": round(owner_eff * 100.0, 2),
        "validators_pct": round(val_eff * 100.0, 2),
        "miners_pct": round(min_eff * 100.0, 2),
        "owner_tao_day": emi * owner_eff,
        "validators_tao_day": emi * val_eff,
        "miners_tao_day": emi * min_eff,
        "real_split": True,
        "incentive_burn_pct": (round(row.incentive_burn * 100.0, 1)
                               if row.incentive_burn is not None else None),
    })
    return split


def _global_owner_pct() -> float | None:
    """Chain-global owner share of emission (same for every subnet)."""
    try:
        d = get_emission_split_service().split(
            kappa_u16=None, emission_per_day_tao=0.0)
        v = d.get("owner_pct")
        return float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        return None


def _row_dict(
    r: SubnetRow,
    score: float | None = None,
    *,
    head_block: int = 0,
    owner_pct: float | None = None,
) -> dict:
    """Serialize a SubnetRow for the template / JSON API."""
    burn_demand = None
    if r.max_burn_tao > r.min_burn_tao and r.recycle_tao > 0:
        burn_demand = (r.recycle_tao - r.min_burn_tao) \
            / (r.max_burn_tao - r.min_burn_tao)
    # Effective owner take = protocol cut + the owner hotkey's share of the
    # post-cut UID emission (validator dividends + any burned incentive).
    owner_cut_pct = owner_pct
    owner_take_pct = owner_pct
    if owner_pct is not None and r.owner_emission_share is not None:
        owner_frac = owner_pct / 100.0
        owner_take_pct = round(
            (owner_frac + (1.0 - owner_frac) * r.owner_emission_share) * 100.0, 2)
    incentive_burn_pct = (round(r.incentive_burn * 100.0, 1)
                          if r.incentive_burn is not None else None)
    owner_dividend_pct = (round(r.owner_dividend_share * 100.0, 1)
                          if r.owner_dividend_share is not None else None)
    tempo_btt: int | None = None
    tempo_near = False
    tempo_secs: int | None = None
    if head_block > 0:
        tempo_btt = tempo_blocks_to_tick(r, head_block)
        tempo_near = tempo_btt is not None and 0 < tempo_btt <= TEMPO_NEAR_BLOCKS
        if tempo_btt is not None:
            tempo_secs = int(tempo_btt * TEMPO_BLOCK_SECONDS)
    return {
        "netuid": r.netuid,
        "name": r.name or f"sn{r.netuid}",
        "category": r.category,
        "description": r.description or "",
        "gpu_need": r.gpu_need,
        "reward_shape": r.reward_shape,
        "burn_tao": r.recycle_tao,
        "min_burn_tao": r.min_burn_tao,
        "max_burn_tao": r.max_burn_tao,
        "burn_demand": burn_demand,
        "burn_pct": round(burn_demand * 100.0, 1) if burn_demand is not None else None,
        "subnetwork_n": r.subnetwork_n,
        "max_n": r.max_n,
        "slots_free": r.slots_free,
        "is_full": r.subnetwork_n >= r.max_n > 0,
        "tao_in": r.tao_in,
        "alpha_in": r.alpha_in,
        "price": r.price_tao_per_alpha,
        "emission_per_day": r.emission_per_day,
        "emission_per_block": r.emission_per_block,
        "age_days": r.age_days,
        "active_miners": r.active_miners,
        "top1_share": r.top1_share,
        "top5_share": r.top5_share,
        "top10_share": r.top10_share,
        "top50_share": r.top50_share,
        "incentive_gini": r.incentive_gini,
        "github_repo": r.github_repo,
        "subnet_url": r.subnet_url,
        "discord": r.discord,
        "rho": r.rho,
        "kappa": r.kappa,
        "alpha_high": r.alpha_high,
        "alpha_low": r.alpha_low,
        "alpha_sigmoid_steepness": r.alpha_sigmoid_steepness,
        "liquid_alpha_enabled": r.liquid_alpha_enabled,
        "immunity_period": r.immunity_period,
        "tempo": r.tempo,
        "yuma_version": r.yuma_version,
        "commit_reveal_enabled": r.commit_reveal_enabled,
        "weights_rate_limit": r.weights_rate_limit,
        "max_validators": r.max_validators,
        "burn_registration_allowed": r.burn_registration_allowed,
        "pow_registration_allowed": r.pow_registration_allowed,
        "difficulty": r.difficulty,
        "score": score,
        "easy_entry_score": r.easy_entry_score,
        "owner_pct": owner_take_pct,
        "owner_cut_pct": owner_cut_pct,
        "incentive_burn": r.incentive_burn,
        "incentive_burn_pct": incentive_burn_pct,
        "owner_dividend_share": r.owner_dividend_share,
        "owner_dividend_pct": owner_dividend_pct,
        "owner_emission_share": r.owner_emission_share,
        "validator_emission_share": r.validator_emission_share,
        "miner_emission_share": r.miner_emission_share,
        "tempo_near": tempo_near,
        "tempo_blocks_to_tick": tempo_btt,
        "tempo_secs_to_tick": tempo_secs,
    }


def _dashboard_template_context(
    request: Request,
    *,
    search: str = "",
) -> dict[str, Any]:
    scanner = get_scanner()
    cfg = scanner.cfg
    scan, scan_pending = _live_scan(scanner)
    scores = scanner.scores()
    rows, spec = _apply_filters_and_sort(
        scan.rows,
        types=list(cfg.dashboard.filter_types or []),
        gpu_needs=[],
        sort_spec_str=cfg.dashboard.sort_by,
        default_order=cfg.dashboard.sort_order,
        search=search,
        head_block=scan.head_block,
        only_tempo_near=False,
    )
    tao_spot = get_tao_price_cache().get_spot()
    coldkey_entries = [
        {"name": e.name, "ss58": e.ss58, "note": e.note}
        for e in (cfg.coldkeys.entries or []) if e.ss58
    ]
    op = _global_owner_pct()
    pk, po = spec[0] if spec else ("score", "desc")
    return {
        "request": request,
        "rows": [_row_dict(r, scores.get(r.netuid).score
                           if scores.get(r.netuid) else None,
                           head_block=scan.head_block,
                           owner_pct=op)
                 for r in rows],
        "all_rows_count": len(scan.rows),
        "categories": CATEGORIES,
        "gpu_options": GPU_OPTIONS,
        "sort_keys": SORT_KEYS,
        "default_sort": cfg.dashboard.sort_by,
        "default_order": cfg.dashboard.sort_order,
        "selected_types": list(cfg.dashboard.filter_types or []),
        "selected_gpus": [],
        "search": search,
        "head_block": scan.head_block,
        "fetched_at": scan.fetched_at.astimezone().strftime(
            "%Y-%m-%d %H:%M:%S %Z"),
        "sort_pretty": format_sort_spec(spec),
        "refresh_seconds": cfg.scan.refresh_seconds,
        "failures": scan.failures,
        "tao_spot": tao_spot,
        "coldkey_entries": coldkey_entries,
        "coldkey_allow_adhoc": bool(cfg.coldkeys.allow_adhoc_lookup),
        "scan_pending": scan_pending,
        "sort_primary_key": pk,
        "sort_primary_order": po,
        "tempo_near_blocks": TEMPO_NEAR_BLOCKS,
    }


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Nothing to set up here — scanner and auto-analyzer are started by
    # cmd_web before uvicorn.run(). Yield to run the app.
    yield
    # Shutdown: stop the auto-analyzer gracefully.
    aa = get_auto_analyzer()
    if aa:
        aa.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="subnetscope", docs_url="/api/docs",
                  redoc_url=None, openapi_url="/api/openapi.json",
                  lifespan=_lifespan)
    app.mount("/static", CachedStatic(directory=str(STATIC_DIR)), name="static")

    # ---- HTML routes -------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_home(request: Request):
        ctx = _dashboard_template_context(request, search="")
        return templates.TemplateResponse(request, "dashboard.html", ctx)

    @app.get("/recommendations")
    def recommendations_redirect():
        return RedirectResponse(url="/", status_code=307)

    @app.get("/api/rows", response_class=HTMLResponse)
    def api_rows(
        request: Request,
        sort: str = Query(""),
        types: list[str] = Query(default=[]),
        gpus: list[str] = Query(default=[]),
        search: str = Query(""),
        force: int = Query(0),
        tempo_only: int = Query(0),
    ):
        scanner = get_scanner()
        cfg = scanner.cfg
        scan, scan_pending = _live_scan(scanner, force=bool(force))
        scores = scanner.scores()
        sort_str = sort or cfg.dashboard.sort_by
        rows, spec = _apply_filters_and_sort(
            scan.rows,
            types=types,
            gpu_needs=gpus,
            sort_spec_str=sort_str,
            default_order=cfg.dashboard.sort_order,
            search=search,
            head_block=scan.head_block,
            only_tempo_near=bool(tempo_only),
        )
        op = _global_owner_pct()
        pk, po = spec[0] if spec else ("score", "desc")
        return templates.TemplateResponse(request, "_table.html", {
            "request": request,
            "rows": [_row_dict(r, scores.get(r.netuid).score
                               if scores.get(r.netuid) else None,
                               head_block=scan.head_block,
                               owner_pct=op)
                     for r in rows],
            "head_block": scan.head_block,
            "fetched_at": scan.fetched_at.astimezone().strftime(
                "%Y-%m-%d %H:%M:%S %Z"),
            "sort_pretty": format_sort_spec(spec),
            "all_rows_count": len(scan.rows),
            "scan_pending": scan_pending,
            "sort_primary_key": pk,
            "sort_primary_order": po,
            "tempo_near_blocks": TEMPO_NEAR_BLOCKS,
        })

    @app.get("/subnet/{netuid}", response_class=HTMLResponse)
    def subnet_detail(request: Request, netuid: int):
        scanner = get_scanner()
        scan, pending = _live_scan(scanner)
        if pending:
            return templates.TemplateResponse(request, "detail_pending.html", {
                "request": request,
                "netuid": netuid,
            })
        scores = scanner.scores()
        match = next((r for r in scan.rows if r.netuid == netuid), None)
        if match is None:
            return PlainTextResponse(
                f"netuid {netuid} not found in current scan", status_code=404)
        sb = scores.get(netuid)
        analysis = get_analysis_store().get(netuid)
        emission_split = get_emission_split_service().split(
            kappa_u16=match.kappa,
            emission_per_day_tao=match.emission_per_day,
        )
        # Replace the kappa-based estimate with the real metagraph-derived
        # split (folds incentive burn into the owner bucket) when available.
        emission_split = _apply_real_split(emission_split, match)
        miner_dist: dict[str, Any] | None = None
        mr_svc = get_miner_rewards_service()
        my_hotkeys = {(e.ss58 or "").strip()
                      for e in (scanner.cfg.hotkeys.entries or []) if e.ss58}
        if mr_svc is not None:
            try:
                # Show the full miner distribution (all UIDs), not just top-30.
                miner_dist = mr_svc.get(netuid, limit=1_000_000,
                                        highlight_hotkeys=my_hotkeys)
            except Exception as e:  # noqa: BLE001
                miner_dist = {"error": f"{type(e).__name__}: {e}",
                              "miners": [], "summary": {}}
        cfg = scanner.cfg
        watch_hotkeys = registration_status_for_subnet(
            scanner.collector.sdk,
            netuid,
            cfg.hotkeys.entries or [],
            miner_rewards_svc=mr_svc,
            coldkey_dir={(e.ss58 or "").strip(): (e.name or "").strip()
                         for e in (cfg.coldkeys.entries or []) if e.ss58},
        )
        # How long the current top miners have kept their rank (from recorded
        # rank-history snapshots) + their on-chain registration tenure.
        rank_tenure = None
        try:
            if miner_dist and miner_dist.get("miners"):
                ni = get_network_index()
                rank_tenure = compute_rank_tenure(
                    scanner.db, netuid, miner_dist["miners"],
                    interval_label=ni.interval_label if ni else "15 min",
                )
        except Exception:  # noqa: BLE001
            log.debug("rank tenure compute failed for sn%d", netuid,
                      exc_info=True)
        return templates.TemplateResponse(request, "detail.html", {
            "r": _row_dict(match, sb.score if sb else None,
                           head_block=scan.head_block,
                           owner_pct=_global_owner_pct()),
            "score_breakdown": {
                "score": sb.score, "gpu": sb.gpu, "top1": sb.top1,
                "miners": sb.miners, "slots": sb.slots, "fee": sb.fee,
                "liquidity": sb.liquidity, "emission": sb.emission,
            } if sb else None,
            "analysis": analysis,
            "emission_split": emission_split,
            "miner_dist": miner_dist,
            "head_block": scan.head_block,
            "fetched_at": scan.fetched_at.astimezone().strftime(
                "%Y-%m-%d %H:%M:%S %Z"),
            "watch_hotkeys": watch_hotkeys,
            "rank_tenure": rank_tenure,
            # Shared header tools (Wallet modal) need the coldkey directory.
            "coldkey_entries": [
                {"name": e.name, "ss58": e.ss58, "note": e.note}
                for e in (cfg.coldkeys.entries or []) if e.ss58
            ],
            "coldkey_allow_adhoc": bool(cfg.coldkeys.allow_adhoc_lookup),
        })

    # ---- JSON API ----------------------------------------------------------

    @app.get("/api/health")
    def api_health():
        scanner = get_scanner()
        fetched = scanner.cache_fetched_at()
        return {
            "ok": True,
            "scan_pending": scanner.peek() is None,
            "cache_age_seconds": scanner.cache_age_seconds(),
            "cache_fetched_at": fetched.isoformat() if fetched else None,
            "ttl_seconds": scanner.ttl_seconds,
            "scan": scanner.scan_progress(),
        }

    @app.get("/api/scan-progress")
    def api_scan_progress():
        scanner = get_scanner()
        return scanner.scan_progress()

    @app.get("/api/subnet/{netuid}")
    def api_subnet(netuid: int):
        scanner = get_scanner()
        scan, pending = _live_scan(scanner)
        if pending:
            return JSONResponse(
                {"error": "scan_in_progress",
                 "detail": "First chain scan still running; retry shortly."},
                status_code=503,
            )
        scores = scanner.scores()
        match = next((r for r in scan.rows if r.netuid == netuid), None)
        if match is None:
            return JSONResponse({"error": f"netuid {netuid} not found"},
                                status_code=404)
        sb = scores.get(netuid)
        return _row_dict(match, sb.score if sb else None,
                         head_block=scan.head_block,
                         owner_pct=_global_owner_pct())

    @app.get("/api/miner-rewards/{netuid}")
    def api_miner_rewards(netuid: int,
                          limit: int = Query(30, ge=1, le=512),
                          force: int = Query(0)):
        """Per-miner reward ranking for one subnet (top-`limit`).

        Uses a generous 45 s wait so external clients can poll for fresh
        data; the in-page render uses a shorter 8 s wait so cold-start
        doesn't slow page paint.
        """
        from .miner_rewards import LOOKUP_TIMEOUT_API_S
        svc = get_miner_rewards_service()
        if svc is None:
            return JSONResponse(
                {"error": "miner_rewards service not initialised"},
                status_code=503,
            )
        return svc.get(netuid, force=bool(force), limit=limit,
                       timeout_s=LOOKUP_TIMEOUT_API_S)

    @app.get("/api/emission-split/{netuid}")
    def api_emission_split(netuid: int):
        """Owner / validators / miners split for one subnet (per-day TAO)."""
        scanner = get_scanner()
        scan, pending = _live_scan(scanner)
        if pending:
            return JSONResponse(
                {"error": "scan_in_progress"}, status_code=503,
            )
        match = next((r for r in scan.rows if r.netuid == netuid), None)
        if match is None:
            return JSONResponse({"error": f"netuid {netuid} not found"},
                                status_code=404)
        split = get_emission_split_service().split(
            kappa_u16=match.kappa,
            emission_per_day_tao=match.emission_per_day,
        )
        split = _apply_real_split(split, match)
        return {"netuid": netuid, **split}

    @app.get("/api/score/{netuid}")
    def api_score(netuid: int):
        scanner = get_scanner()
        _, pending = _live_scan(scanner)
        if pending:
            return JSONResponse(
                {"error": "scan_in_progress"}, status_code=503,
            )
        scanner.get()  # ensure scored at least once
        sb = scanner.scores().get(netuid)
        if sb is None:
            return JSONResponse({"error": f"no score for netuid {netuid}"},
                                status_code=404)
        return {
            "netuid": netuid, "score": sb.score,
            "components": {
                "gpu": sb.gpu, "top1": sb.top1, "miners": sb.miners,
                "slots": sb.slots, "fee": sb.fee, "liquidity": sb.liquidity,
                "emission": sb.emission,
            },
        }

    @app.get("/api/recommendations")
    def api_recommendations(limit: int = Query(20, ge=1, le=200)):
        scanner = get_scanner()
        scan, pending = _live_scan(scanner)
        if pending:
            return JSONResponse(
                {"error": "scan_in_progress", "recommendations": []},
                status_code=503,
            )
        scores = scanner.scores()
        ranked = []
        by_id = {r.netuid: r for r in scan.rows}
        for nid, sb in scores.items():
            r = by_id.get(nid)
            if not r:
                continue
            ranked.append((r, sb))
        ranked.sort(key=lambda x: x[1].score, reverse=True)
        return {"recommendations": [{
            "netuid": r.netuid, "name": r.name or f"sn{r.netuid}",
            "category": r.category, "score": sb.score,
            "burn_tao": r.recycle_tao, "slots_free": r.slots_free,
            "max_n": r.max_n, "active_miners": r.active_miners,
            "top1_share": r.top1_share, "gpu_need": r.gpu_need,
            "emission_per_day": r.emission_per_day,
        } for r, sb in ranked[:limit]]}

    @app.get("/api/history/{netuid}")
    def api_history(netuid: int,
                    hours: float = Query(24.0, gt=0, le=720.0)):
        scanner = get_scanner()
        rows = scanner.db.history(netuid, hours=hours)
        return {"netuid": netuid, "hours": hours,
                "count": len(rows), "points": rows}

    @app.get("/api/alerts")
    def api_alerts(limit: int = Query(50, ge=1, le=200)):
        scanner = get_scanner()
        return {"alerts": scanner.db.recent_alerts(limit=limit)}

    @app.get("/api/analysis/{netuid}")
    def api_analysis(netuid: int):
        a = get_analysis_store().get(netuid)
        if a is None:
            return JSONResponse({"error": f"no analysis for netuid {netuid}",
                                 "netuid": netuid, "exists": False},
                                status_code=404)
        return {
            "netuid": netuid,
            "exists": True,
            "is_auto": a.is_auto,
            "title": a.title,
            "analyzed_label": a.analyzed_label,
            "file_mtime": a.file_mtime,
            "file_mtime_iso": a.file_mtime_iso,
            "html": a.body_html,
        }

    @app.get("/api/analyses")
    def api_analyses_list():
        store = get_analysis_store()
        curated = store.list_curated_netuids()
        all_n = store.list_netuids()
        aa = get_auto_analyzer()
        return {
            "netuids": all_n,
            "curated_netuids": curated,
            "auto_netuids": [n for n in all_n if n not in curated],
            "total": len(all_n),
            "curated": len(curated),
            "auto_generated": len(all_n) - len(curated),
            "dir": str(store.dir),
            "auto_analyzer": aa.status if aa else None,
        }

    @app.post("/api/analyses/refresh")
    def api_analyses_refresh():
        """Trigger an immediate auto-analysis regeneration (runs in background)."""
        scanner = get_scanner()
        store = get_analysis_store()
        aa = get_auto_analyzer()

        def _run():
            try:
                scan = scanner.get()
                scores = scanner.scores()
                tao_usd: float | None = None
                try:
                    spot = get_tao_price_cache().get_spot()
                    tao_usd = spot.get("usd") if isinstance(spot, dict) else None
                except Exception:
                    tao_usd = None
                g, s = generate_all(store.dir, scan.rows, scores, scanner.db,
                                    tao_usd=tao_usd)
                log.info("manual refresh: generated=%d skipped=%d tao_usd=%s",
                         g, s, f"${tao_usd:.2f}" if tao_usd else "n/a")
            except Exception:
                log.exception("manual refresh failed")

        threading.Thread(target=_run, name="manual-refresh", daemon=True).start()
        return {
            "ok": True,
            "message": "Regeneration started in background. "
                       "Reload detail pages in ~10 seconds to see updates.",
            "auto_analyzer": aa.status if aa else None,
        }

    # ------------------------------------------------------------------
    # Live burn-fee (12-second TTL lightweight query)
    # ------------------------------------------------------------------
    @app.get("/api/burn-live/{netuid}")
    def api_burn_live(netuid: int):
        """Return the current burn-fee for one subnet.

        The value is fetched from the chain (single substrate query) then
        cached for 12 seconds — matching Bittensor's block time so each
        page poll picks up new data within one block.
        """
        cache = get_burn_cache()
        result = cache.get(netuid)
        return {
            "netuid": netuid,
            "burn_tao": round(result["tao"], 6),
            "ts": result["ts"],
            "ts_iso": result["ts_iso"],
            "stale": result["stale"],
        }

    # ------------------------------------------------------------------
    # TAO/USD live price (CoinGecko, 60 s spot TTL, 5 min chart TTL)
    # ------------------------------------------------------------------
    @app.get("/api/tao-price")
    def api_tao_price():
        """Current TAO/USD spot + 24 h change. ~1 req/min to upstream."""
        return get_tao_price_cache().get_spot()

    @app.get("/api/tao-price/history")
    def api_tao_price_history(
        hours: float = Query(24.0, gt=0, le=168.0),
    ):
        """24 h (or up to 7 d) TAO/USD chart points. ~1 req/5min upstream."""
        return get_tao_price_cache().get_chart(hours=hours)

    # ------------------------------------------------------------------
    # Read-only coldkey directory + lookup (free TAO + per-subnet stake).
    # SS58 is public — no private keys are ever read.
    # ------------------------------------------------------------------
    @app.get("/api/coldkeys")
    def api_coldkeys():
        """Return the configured coldkey directory + adhoc-lookup policy."""
        cfg = get_scanner().cfg
        return {
            "entries": [
                {"name": e.name, "ss58": e.ss58, "note": e.note}
                for e in (cfg.coldkeys.entries or []) if e.ss58
            ],
            "allow_adhoc_lookup": bool(cfg.coldkeys.allow_adhoc_lookup),
            "cache_ttl_seconds": int(cfg.coldkeys.cache_ttl_seconds),
        }

    @app.get("/api/coldkey/{ss58}")
    def api_coldkey(ss58: str, force: int = Query(0)):
        """Free TAO + per-(hotkey,netuid) stake positions for one coldkey.

        If `ss58` isn't in the configured directory and `allow_adhoc_lookup`
        is false, the request is refused.
        """
        cfg = get_scanner().cfg
        configured = {(e.ss58 or "").strip() for e in cfg.coldkeys.entries or []}
        if not cfg.coldkeys.allow_adhoc_lookup and ss58 not in configured:
            return JSONResponse({
                "error": "ss58 not in configured coldkeys.entries and "
                         "allow_adhoc_lookup is false",
                "ss58": ss58,
            }, status_code=403)
        if not is_valid_ss58(ss58):
            return JSONResponse({
                "error": "invalid SS58 address",
                "ss58": ss58,
            }, status_code=400)
        svc = get_coldkey_service()
        if svc is None:
            return JSONResponse({"error": "coldkey service not initialised"},
                                status_code=503)
        return svc.lookup(ss58, force=bool(force))

    @app.get("/api/readme/{netuid}")
    def api_readme(netuid: int, force: int = Query(0)):
        """Rendered GitHub README HTML for a subnet (Readme tab)."""
        svc = get_readme_service()
        if svc is None:
            return JSONResponse({"error": "readme service not initialised"},
                                status_code=503)
        return svc.get(netuid, force=bool(force))

    @app.get("/api/account")
    def api_account(q: str = Query(""), force: int = Query(0)):
        """Find which subnets a coldkey's or IP's hotkeys are registered on.

        ``q`` is a coldkey SS58 (5…) or an IPv4 axon address. Coldkey lookups
        hit the chain directly (fresh); IP lookups read the cross-subnet sweep
        index (returns ``building: true`` until the first sweep completes).
        """
        q = (q or "").strip()
        if not q:
            return JSONResponse({"error": "missing query ?q=", "kind": "invalid"},
                                status_code=400)
        svc = get_network_index()
        if svc is None:
            return JSONResponse({"error": "network index not initialised"},
                                status_code=503)
        res = svc.lookup(q, force=bool(force))
        if res.get("kind") == "invalid":
            return JSONResponse(res, status_code=400)
        return res

    @app.get("/api/coldkey/{ss58}/history")
    def api_coldkey_stake_history(
        ss58: str,
        hours: float = Query(168.0, gt=0, le=720.0),
    ):
        """Snapshots from prior wallet modal loads (read-only, local DB)."""
        cfg = get_scanner().cfg
        configured = {(e.ss58 or "").strip() for e in cfg.coldkeys.entries or []}
        if not cfg.coldkeys.allow_adhoc_lookup and ss58 not in configured:
            return JSONResponse({
                "error": "ss58 not in configured coldkeys.entries and "
                         "allow_adhoc_lookup is false",
            }, status_code=403)
        if not is_valid_ss58(ss58):
            return JSONResponse({"error": "invalid SS58 address"}, status_code=400)
        scanner = get_scanner()
        pts = scanner.db.wallet_stake_history(ss58, hours=hours)
        return {"ss58": ss58, "hours": hours, "count": len(pts), "points": pts}

    return app
