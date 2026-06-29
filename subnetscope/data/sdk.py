"""Bittensor SDK wrapper for read-only subnet metadata.

Pulls per-subnet info needed for the directory:
  * recycle / burn cost (registration fee)
  * difficulty (PoW, optional)
  * SubnetIdentity (name, description, github, url, discord)
  * pool reserves (tao_in, alpha_in)
  * UID slots used vs total
  * emission per block
  * subnet age
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from ..config import NetworkCfg, ScanCfg
from ..types import SubnetRow

log = logging.getLogger(__name__)

BLOCK_TIME_SECONDS = 12.0
BLOCKS_PER_DAY = int(86400 / BLOCK_TIME_SECONDS)  # 7200


@dataclass
class _LazyImports:
    bt: Any = None


_imports = _LazyImports()


def _ensure_imports() -> None:
    if _imports.bt is not None:
        return
    try:
        import bittensor as bt
    except ImportError as e:
        raise ImportError(
            "subnetscope needs the Python package 'bittensor' (provides Subtensor). "
            "Install with: pip install 'bittensor>=9' or uv sync in the subnetscope repo. "
            "Note: bittensor-cli does not install bittensor — use a venv that includes both "
            "if you run subnetscope alongside miner tooling."
        ) from e
    _imports.bt = bt


class SDKClient:
    """Sync wrapper around `bittensor.Subtensor` for read-only listing.

    Bittensor's websocket transport is NOT thread-safe — concurrent `recv()`
    on a single Subtensor raises `ConcurrencyError`. We therefore keep a
    *per-thread* Subtensor in `threading.local()` so the parallel scan can
    safely use one connection per worker thread.
    """

    def __init__(self, network: NetworkCfg, scan: ScanCfg):
        self.network = network
        self.scan = scan
        self._main_subtensor: Any = None
        self._tls = threading.local()
        self._all_subtensors_lock = threading.Lock()
        self._all_subtensors: list[Any] = []
        # Persistent worker pool reused across scans. Creating a fresh pool per
        # scan spawned new threads each time, and each new thread opened a new
        # per-thread Subtensor that was never closed -> steady RAM growth.
        # Reusing one pool keeps the per-thread connections bounded.
        self._pool: Any = None
        self._pool_workers: int = 0

    @property
    def subtensor(self) -> Any:
        """The 'main thread' Subtensor — used for one-off calls."""
        if self._main_subtensor is None:
            _ensure_imports()
            log.info("Connecting to subtensor at %s ...", self.network.subtensor_endpoint)
            self._main_subtensor = _imports.bt.Subtensor(network=self.network.subtensor_endpoint)
            with self._all_subtensors_lock:
                self._all_subtensors.append(self._main_subtensor)
        return self._main_subtensor

    def _thread_subtensor(self) -> Any:
        """A Subtensor exclusive to the current thread (created on first use)."""
        sub = getattr(self._tls, "sub", None)
        if sub is None:
            _ensure_imports()
            sub = _imports.bt.Subtensor(network=self.network.subtensor_endpoint)
            self._tls.sub = sub
            with self._all_subtensors_lock:
                self._all_subtensors.append(sub)
        return sub

    def close(self) -> None:
        if self._pool is not None:
            try:
                self._pool.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
            self._pool = None
            self._pool_workers = 0
        with self._all_subtensors_lock:
            for sub in self._all_subtensors:
                try:
                    sub.close()
                except Exception:  # noqa: BLE001
                    pass
            self._all_subtensors.clear()
        self._main_subtensor = None
        self._tls = threading.local()

    def _retry(self, fn, *args, retries: int | None = None, base_delay: float = 1.0, **kwargs):
        retries = retries if retries is not None else max(1, self.scan.retries_per_subnet)
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                last_err = e
                wait = base_delay * (2 ** attempt)
                log.debug("RPC failed (attempt %d/%d): %s — retrying in %.1fs",
                          attempt + 1, retries, e, wait)
                time.sleep(wait)
        assert last_err is not None
        raise last_err

    def current_block(self) -> int:
        return int(self._retry(self.subtensor.get_current_block))

    def list_subnets(self, include_root: bool = False) -> list[int]:
        sub = self.subtensor
        try:
            netuids = self._retry(sub.get_subnets)
        except AttributeError:
            netuids = self._retry(sub.get_all_subnets_netuid)
        out = sorted(int(n) for n in netuids)
        if not include_root:
            out = [n for n in out if n != 0]
        return out

    def fetch_subnet_row(self, netuid: int, head_block: int,
                         subtensor: Any | None = None,
                         fetch_metagraph: bool = True) -> SubnetRow:
        """Fetch one full row of subnet metadata.

        Pass an explicit `subtensor` when calling from a worker thread —
        each thread must use its own connection.

        Issues 3-4 RPC calls per subnet:
          1. `subnet(netuid)` -> DynamicInfo (pool reserves, identity, emission)
          2. `get_subnet_hyperparameters(netuid)` -> all governance knobs
          3. `recycle(netuid)` -> current burn cost
          4. `metagraph(netuid, lite=True)` -> incentive distribution
             (skipped if fetch_metagraph=False; reward_shape becomes "?")
        """
        sub = subtensor if subtensor is not None else self.subtensor

        info = self._retry(sub.subnet, netuid)
        if info is None:
            raise RuntimeError(f"subnet({netuid}) returned None")

        tao_in = _to_tao(getattr(info, "tao_in", 0))
        alpha_in = _to_tao(getattr(info, "alpha_in", 0))
        price_obj = getattr(info, "price", None)
        price = _to_tao(price_obj) if price_obj is not None else (
            (tao_in / alpha_in) if alpha_in > 0 else 0.0
        )

        # In dTAO, the per-block emission has two parts:
        #   tao_in_emission  -> raw TAO injected into the pool
        #   alpha_out_emission -> alpha minted to miners (worth alpha * price in TAO)
        # We sum them into a single TAO-equivalent emission per block.
        # `info.emission` exists for backward compat but is 0 in modern SDKs.
        tao_in_em = _to_tao(getattr(info, "tao_in_emission", 0))
        alpha_out_em = _to_tao(getattr(info, "alpha_out_emission", 0))
        legacy_em = _to_tao(getattr(info, "emission", 0))
        emission = legacy_em or (tao_in_em + alpha_out_em * price)

        reg_block = int(getattr(info, "network_registered_at", 0) or 0)
        age_blocks = max(0, head_block - reg_block) if reg_block else 0

        identity = getattr(info, "subnet_identity", None)
        name = _decode_str(getattr(info, "subnet_name", None))
        description: str | None = None
        github_repo: str | None = None
        subnet_url: str | None = None
        discord: str | None = None
        name_source = "fallback"

        if identity is not None:
            name = name or _decode_str(getattr(identity, "subnet_name", None))
            description = _decode_str(getattr(identity, "description", None))
            github_repo = _decode_str(getattr(identity, "github_repo", None))
            subnet_url = _decode_str(getattr(identity, "subnet_url", None))
            discord = _decode_str(getattr(identity, "discord", None))
            if name:
                name_source = "identity"

        # One round-trip for EVERY governance/incentive knob.
        hp = _safe_hyperparameters(sub, netuid, self._retry)

        recycle_tao = _safe_recycle(sub, netuid)
        difficulty = int(hp["difficulty"]) if hp.get("difficulty") is not None else None
        pow_allowed = _coerce_bool(hp.get("registration_allowed"), default=False)
        burn_allowed = _coerce_bool(hp.get("network_registration_allowed"), default=True)
        # Some SDK versions only expose a single registration_allowed flag.
        if hp.get("network_registration_allowed") is None and recycle_tao > 0:
            burn_allowed = True
        min_burn_tao = _rao_to_tao(hp.get("min_burn"))
        max_burn_tao = _rao_to_tao(hp.get("max_burn"))

        max_n = _coerce_int(getattr(info, "max_n", None)
                            or getattr(info, "max_allowed_uids", None)
                            or hp.get("max_allowed_uids"), default=256)
        used_n = _safe_subnetwork_n(sub, netuid, info)
        slots_free = max(0, max_n - used_n)

        # Live miner incentive concentration (excludes validator_permit UIDs).
        conc: dict[str, float | int] = {}
        burn: dict[str, float] = {}
        owner_hk = _decode_str(getattr(info, "owner_hotkey", None))
        if fetch_metagraph:
            from ..categorize import miner_incentive_concentration
            try:
                meta = self._retry(sub.metagraph, netuid=netuid, lite=True)
                # Avoid `arr or []` — numpy arrays raise on bool().
                incentives_attr = getattr(meta, "incentive", None)
                if incentives_attr is not None:
                    incentives = [float(x) for x in incentives_attr]
                    vp_attr = getattr(meta, "validator_permit", None)
                    permits: list[bool] | None = None
                    if vp_attr is not None:
                        permits = [bool(x) for x in vp_attr]
                    conc = miner_incentive_concentration(incentives, permits)
                    # Owner/burn split: how much emission + incentive the
                    # subnet owner's own hotkey captures vs real miners.
                    emissions = _to_float_list_attr(getattr(meta, "emission", None))
                    dividends = _to_float_list_attr(getattr(meta, "dividends", None))
                    hotkeys = [str(x) for x in (getattr(meta, "hotkeys", None) or [])]
                    burn = _owner_burn_shares(
                        emissions, incentives, dividends, permits, hotkeys, owner_hk)
            except Exception as e:  # noqa: BLE001
                log.warning("metagraph fetch failed for netuid=%s: %s", netuid, e)
                conc = {}

        return SubnetRow(
            netuid=int(netuid),
            name=name,
            category="other",  # filled in by categorizer downstream
            description=description,
            github_repo=github_repo,
            subnet_url=subnet_url,
            discord=discord,
            recycle_tao=recycle_tao,
            difficulty=difficulty,
            pow_registration_allowed=pow_allowed,
            burn_registration_allowed=burn_allowed,
            min_burn_tao=min_burn_tao,
            max_burn_tao=max_burn_tao,
            tao_in=tao_in,
            alpha_in=alpha_in,
            price_tao_per_alpha=price,
            max_n=max_n,
            subnetwork_n=used_n,
            slots_free=slots_free,
            max_validators=_coerce_int(hp.get("max_validators"), default=None),
            emission_per_block=emission,
            emission_per_day=emission * BLOCKS_PER_DAY,
            age_blocks=age_blocks,
            age_days=age_blocks / BLOCKS_PER_DAY if age_blocks else 0.0,
            rho=_coerce_int(hp.get("rho"), default=None),
            kappa=_coerce_int(hp.get("kappa"), default=None),
            alpha_high=_coerce_int(hp.get("alpha_high"), default=None),
            alpha_low=_coerce_int(hp.get("alpha_low"), default=None),
            alpha_sigmoid_steepness=_coerce_float(hp.get("alpha_sigmoid_steepness")),
            liquid_alpha_enabled=_coerce_bool_opt(hp.get("liquid_alpha_enabled")),
            immunity_period=_coerce_int(hp.get("immunity_period"), default=None),
            tempo=_coerce_int(hp.get("tempo"), default=None),
            yuma_version=_coerce_int(hp.get("yuma_version"), default=None),
            commit_reveal_enabled=_coerce_bool_opt(
                hp.get("commit_reveal_weights_enabled") or hp.get("commit_reveal_enabled")
            ),
            weights_rate_limit=_coerce_int(hp.get("weights_rate_limit"), default=None),
            active_miners=_coerce_int(conc.get("active_miners"), default=None),
            top1_share=conc.get("top1_share"),
            top5_share=conc.get("top5_share"),
            top10_share=conc.get("top10_share"),
            top50_share=conc.get("top50_share"),
            incentive_gini=conc.get("gini"),
            incentive_burn=burn.get("incentive_burn"),
            owner_dividend_share=burn.get("owner_dividend_share"),
            owner_emission_share=burn.get("owner_emission_share"),
            validator_emission_share=burn.get("validator_emission_share"),
            miner_emission_share=burn.get("miner_emission_share"),
            name_source=name_source,
        )

    def fetch_all_rows(
        self,
        netuids: list[int],
        head_block: int,
        progress_cb=None,
        fetch_metagraph: bool = True,
    ) -> tuple[list[SubnetRow], dict[int, str]]:
        """Fetch rows for many subnets in parallel.

        Each worker thread uses its own Subtensor (websocket connection),
        which sidesteps bittensor's non-threadsafe receive loop.

        Returns (rows, failures) where failures is {netuid: error_message}.
        """
        rows: list[SubnetRow] = []
        failures: dict[int, str] = {}

        max_workers = max(1, min(self.scan.concurrency, 8))
        total = len(netuids)
        done = 0

        def _worker(netuid: int) -> SubnetRow:
            return self.fetch_subnet_row(
                netuid, head_block=head_block,
                subtensor=self._thread_subtensor(),
                fetch_metagraph=fetch_metagraph,
            )

        # Reuse one persistent pool (and its per-thread Subtensors) across
        # scans instead of creating/destroying one each time, which leaked a
        # fresh set of websocket connections per scan.
        if self._pool is None or self._pool_workers != max_workers:
            if self._pool is not None:
                self._pool.shutdown(wait=False)
            self._pool = ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="ssco")
            self._pool_workers = max_workers
        pool = self._pool
        futures = {pool.submit(_worker, n): n for n in netuids}
        for fut in as_completed(futures):
            n = futures[fut]
            try:
                rows.append(fut.result())
            except Exception as e:  # noqa: BLE001
                failures[n] = f"{type(e).__name__}: {e}"
            done += 1
            if progress_cb is not None:
                try:
                    progress_cb(done, total)
                except Exception:  # noqa: BLE001
                    pass
        rows.sort(key=lambda r: r.netuid)
        return rows, failures


# ---------------------------------------------------------------------- helpers


def _to_float_list_attr(x: Any) -> list[float]:
    """Normalise a metagraph numeric vector (Balance/float) to plain floats."""
    if x is None:
        return []
    out: list[float] = []
    for v in x:
        tao_attr = getattr(v, "tao", None)
        try:
            out.append(float(tao_attr) if tao_attr is not None else float(v))
        except Exception:  # noqa: BLE001
            out.append(0.0)
    return out


def _owner_burn_shares(
    emissions: list[float],
    incentives: list[float],
    dividends: list[float],
    permits: list[bool] | None,
    hotkeys: list[str],
    owner_hk: str | None,
) -> dict[str, float]:
    """Share of emission/incentive/dividends captured by the subnet owner's hotkey.

    ``incentive_burn`` is the owner-hotkey share of the *incentive* vector —
    how much of the miner-reward bucket validators route back to the owner
    (the "burn") instead of to real, competing miners. ``owner_dividend_share``
    is the owner-hotkey share of the *dividend* vector — i.e. whether the owner
    runs a productive validator. The emission shares partition the post-owner-cut
    UID pool into owner / non-owner validators / non-owner miners so the detail
    page can show a true split.
    """
    n = len(emissions)
    if n == 0 or not owner_hk:
        return {}
    perm = list(permits or [])
    if len(perm) < n:
        perm.extend([False] * (n - len(perm)))
    hk = list(hotkeys or [])
    if len(hk) < n:
        hk.extend([""] * (n - len(hk)))

    is_owner = [hk[i] == owner_hk for i in range(n)]
    tot_emi = sum(emissions) or 0.0
    out: dict[str, float] = {}
    if tot_emi > 0:
        owner_emi = sum(emissions[i] for i in range(n) if is_owner[i])
        val_emi = sum(emissions[i] for i in range(n)
                      if perm[i] and not is_owner[i])
        miner_emi = sum(emissions[i] for i in range(n)
                        if not perm[i] and not is_owner[i])
        out["owner_emission_share"] = owner_emi / tot_emi
        out["validator_emission_share"] = val_emi / tot_emi
        out["miner_emission_share"] = miner_emi / tot_emi

    tot_inc = sum(incentives) or 0.0
    if tot_inc > 0:
        owner_inc = sum(incentives[i] for i in range(min(n, len(incentives)))
                        if is_owner[i])
        out["incentive_burn"] = owner_inc / tot_inc

    tot_div = sum(dividends) or 0.0
    if tot_div > 0:
        owner_div = sum(dividends[i] for i in range(min(n, len(dividends)))
                        if is_owner[i])
        out["owner_dividend_share"] = owner_div / tot_div
    return out


def _decode_str(x: Any) -> str | None:
    if x is None:
        return None
    if isinstance(x, bytes):
        try:
            s = x.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return None
    else:
        s = str(x)
    s = s.strip().strip("\x00").strip()
    return s or None


def _to_tao(x: Any) -> float:
    """Normalise `bittensor.Balance`, ints (rao), or floats to TAO float."""
    if x is None:
        return 0.0
    tao_attr = getattr(x, "tao", None)
    if tao_attr is not None:
        try:
            return float(tao_attr)
        except Exception:  # noqa: BLE001
            pass
    try:
        v = float(x)
    except Exception:  # noqa: BLE001
        return 0.0
    return v / 1e9 if v > 1e6 else v


def _rao_to_tao(x: Any) -> float:
    """Always interpret `x` as integer rao and return TAO. For known fields."""
    if x is None:
        return 0.0
    try:
        return float(x) / 1e9
    except (TypeError, ValueError):
        return 0.0


def _safe_recycle(sub: Any, netuid: int) -> float:
    """Try several SDK paths for the *current* burn/recycle cost in TAO."""
    for fn_name in ("recycle", "burn", "get_subnet_burn_cost"):
        fn = getattr(sub, fn_name, None)
        if fn is None:
            continue
        try:
            r = fn(netuid) if fn_name != "get_subnet_burn_cost" else fn()
            v = _to_tao(r)
            if v > 0:
                return v
        except TypeError:
            try:
                r = fn(netuid=netuid)
                v = _to_tao(r)
                if v > 0:
                    return v
            except Exception:  # noqa: BLE001
                continue
        except Exception:  # noqa: BLE001
            continue
    fn = getattr(sub, "get_hyperparameter", None)
    if fn is not None:
        for hp in ("Burn", "burn"):
            try:
                v = fn(param_name=hp, netuid=netuid)
                return _to_tao(v)
            except Exception:  # noqa: BLE001
                continue
    return 0.0


def _safe_hyperparameters(sub: Any, netuid: int, retry_fn) -> dict[str, Any]:
    """Return all subnet hyperparameters as a dict (or {} on failure)."""
    fn = getattr(sub, "get_subnet_hyperparameters", None)
    if fn is None:
        return {}
    try:
        hp = retry_fn(fn, netuid)
    except Exception:  # noqa: BLE001
        return {}
    if hp is None:
        return {}
    out: dict[str, Any] = {}
    for attr in dir(hp):
        if attr.startswith("_"):
            continue
        try:
            v = getattr(hp, attr)
            if callable(v):
                continue
            out[attr] = v
        except Exception:  # noqa: BLE001
            continue
    return out


def _coerce_int(x: Any, default: int | None = 0) -> int | None:
    if x is None:
        return default
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _coerce_float(x: Any, default: float | None = None) -> float | None:
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _coerce_bool(x: Any, default: bool = False) -> bool:
    if x is None:
        return default
    try:
        return bool(x)
    except Exception:  # noqa: BLE001
        return default


def _coerce_bool_opt(x: Any) -> bool | None:
    if x is None:
        return None
    try:
        return bool(x)
    except Exception:  # noqa: BLE001
        return None


def _safe_subnetwork_n(sub: Any, netuid: int, info: Any) -> int:
    for attr in ("subnetwork_n", "n", "num_uids"):
        v = getattr(info, attr, None)
        if v is not None:
            try:
                return int(v)
            except Exception:  # noqa: BLE001
                pass
    fn = getattr(sub, "subnetwork_n", None)
    if fn is not None:
        try:
            return int(fn(netuid))
        except Exception:  # noqa: BLE001
            pass
    # Fall back to metagraph size.
    try:
        meta = sub.metagraph(netuid=netuid, lite=True)
        return int(getattr(meta, "n", 0) or len(getattr(meta, "uids", []) or []))
    except Exception:  # noqa: BLE001
        return 0
