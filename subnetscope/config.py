"""Typed config loaded from `config.yaml`."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class NetworkCfg:
    subtensor_endpoint: str = "wss://entrypoint-finney.opentensor.ai:443"
    taostats_api_key: str = ""


@dataclass
class UniverseCfg:
    blacklist_netuids: list[int] = field(default_factory=list)
    whitelist_netuids: list[int] = field(default_factory=list)
    include_root: bool = False


@dataclass
class ScanCfg:
    concurrency: int = 8
    refresh_seconds: int = 90
    retries_per_subnet: int = 2
    # Fetch each subnet's metagraph to compute true reward concentration
    # (winner / peak / topN / flat). Adds ~1-2s per subnet. Disable for the
    # fastest possible scans; reward_shape will be "?" without it.
    fetch_metagraph: bool = True


@dataclass
class CategorizeCfg:
    use_overrides: bool = True
    default_category: str = "other"


@dataclass
class DashboardCfg:
    sort_by: str = "fee"
    sort_order: str = "asc"
    filter_types: list[str] = field(default_factory=list)
    max_description_chars: int = 50


@dataclass
class LoggingCfg:
    level: str = "INFO"
    file: str = "logs/subnetscope.log"
    rotate_max_bytes: int = 5_242_880
    rotate_backups: int = 3


@dataclass
class ColdkeyEntry:
    """A single read-only coldkey to expose in the wallet modal.

    Only the *public* SS58 address is stored. Subnetscope NEVER reads,
    requests or stores private keys, mnemonics, or passwords.
    """
    name: str = ""
    ss58: str = ""
    note: str = ""


@dataclass
class ColdkeysCfg:
    """List of public coldkey addresses surfaced in the dashboard modal."""
    entries: list[ColdkeyEntry] = field(default_factory=list)
    # Cache TTL for balance + stake-position queries (seconds).
    cache_ttl_seconds: int = 60
    # Allow the modal to query any pasted SS58 (not just configured ones).
    allow_adhoc_lookup: bool = True


@dataclass
class HotkeyEntry:
    """Public miner/validator hotkey SS58 for subnet detail highlights."""
    name: str = ""
    ss58: str = ""
    note: str = ""


@dataclass
class HotkeysCfg:
    """Watch list surfaced on each subnet's detail page (registration + UID)."""
    entries: list[HotkeyEntry] = field(default_factory=list)


@dataclass
class Config:
    network: NetworkCfg = field(default_factory=NetworkCfg)
    universe: UniverseCfg = field(default_factory=UniverseCfg)
    scan: ScanCfg = field(default_factory=ScanCfg)
    categorize: CategorizeCfg = field(default_factory=CategorizeCfg)
    dashboard: DashboardCfg = field(default_factory=DashboardCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)
    coldkeys: ColdkeysCfg = field(default_factory=ColdkeysCfg)
    hotkeys: HotkeysCfg = field(default_factory=HotkeysCfg)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path).expanduser().resolve()
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls(
            network=NetworkCfg(**(raw.get("network") or {})),
            universe=UniverseCfg(**(raw.get("universe") or {})),
            scan=ScanCfg(**(raw.get("scan") or {})),
            categorize=CategorizeCfg(**(raw.get("categorize") or {})),
            dashboard=DashboardCfg(**(raw.get("dashboard") or {})),
            logging=LoggingCfg(**(raw.get("logging") or {})),
            coldkeys=_parse_coldkeys(raw.get("coldkeys")),
            hotkeys=_parse_hotkeys(raw.get("hotkeys")),
        )


def _parse_coldkeys(raw) -> ColdkeysCfg:
    """Accept either a list of entries (legacy/short form) or a dict
    with `entries:`, `cache_ttl_seconds:`, `allow_adhoc_lookup:` keys."""
    if not raw:
        return ColdkeysCfg()
    if isinstance(raw, list):
        return ColdkeysCfg(entries=[_parse_entry(e) for e in raw])
    if isinstance(raw, dict):
        entries = [_parse_entry(e) for e in (raw.get("entries") or [])]
        return ColdkeysCfg(
            entries=entries,
            cache_ttl_seconds=int(raw.get("cache_ttl_seconds", 60)),
            allow_adhoc_lookup=bool(raw.get("allow_adhoc_lookup", True)),
        )
    return ColdkeysCfg()


def _parse_entry(e) -> ColdkeyEntry:
    if isinstance(e, str):
        return ColdkeyEntry(name="", ss58=e.strip(), note="")
    if isinstance(e, dict):
        return ColdkeyEntry(
            name=str(e.get("name", "")).strip(),
            ss58=str(e.get("ss58") or e.get("address") or "").strip(),
            note=str(e.get("note", "")).strip(),
        )
    return ColdkeyEntry()


def _parse_hotkeys(raw) -> HotkeysCfg:
    if not raw:
        return HotkeysCfg()
    if isinstance(raw, list):
        return HotkeysCfg(entries=[_parse_hotkey_entry(e) for e in raw])
    if isinstance(raw, dict):
        entries = [_parse_hotkey_entry(e) for e in (raw.get("entries") or [])]
        return HotkeysCfg(entries=entries)
    return HotkeysCfg()


def _parse_hotkey_entry(e) -> HotkeyEntry:
    if isinstance(e, str):
        return HotkeyEntry(name="", ss58=e.strip(), note="")
    if isinstance(e, dict):
        return HotkeyEntry(
            name=str(e.get("name", "")).strip(),
            ss58=str(e.get("ss58") or e.get("address") or "").strip(),
            note=str(e.get("note", "")).strip(),
        )
    return HotkeyEntry()
