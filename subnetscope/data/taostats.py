"""Optional Taostats enricher for subnet name + description.

If no API key is configured, every method returns {} and the rest of the
pipeline relies on on-chain SubnetIdentity + categorizer fallback.

Docs: https://docs.taostats.io/
We try a couple of endpoint shapes since Taostats has revised the schema.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.taostats.io/api"
DEFAULT_TIMEOUT_S = 15.0


class TaostatsClient:
    """Returns per-netuid metadata: name, description, github, url, twitter."""

    def __init__(self, api_key: str = ""):
        self.api_key = (api_key or "").strip()
        self.enabled = bool(self.api_key)
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=DEFAULT_TIMEOUT_S,
                headers={
                    "Authorization": self.api_key,
                    "accept": "application/json",
                    "User-Agent": "subnetscope/0.1",
                },
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def fetch_subnet_metadata(self) -> dict[int, dict[str, str | None]]:
        """Return {netuid: {name, description, github_repo, subnet_url, ...}}.

        Returns {} on failure. Tries multiple endpoint shapes for resilience.
        """
        if not self.enabled:
            return {}

        endpoints = [
            ("/subnet/latest/v1", {"page": 1, "limit": 200}),
            ("/dtao/subnet/latest/v1", {"page": 1, "limit": 200}),
            ("/subnet/info/v1", {"limit": 200}),
        ]
        client = self._http()
        for path, params in endpoints:
            try:
                r = client.get(f"{API_BASE}{path}", params=params)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                data = r.json()
                rows = _extract_rows(data)
                if not rows:
                    continue
                out: dict[int, dict[str, str | None]] = {}
                for row in rows:
                    netuid = _coerce_int(row.get("netuid"))
                    if netuid is None:
                        continue
                    out[netuid] = {
                        "name": _first_str(row, ("name", "subnet_name", "subnet")),
                        "description": _first_str(row, ("description", "summary", "about")),
                        "github_repo": _first_str(row, ("github", "github_repo", "github_url", "repo")),
                        "subnet_url": _first_str(row, ("url", "subnet_url", "website", "homepage")),
                        "discord": _first_str(row, ("discord", "discord_url")),
                        "twitter": _first_str(row, ("twitter", "twitter_url", "x")),
                    }
                if out:
                    log.info("Taostats enrichment: %d subnets via %s", len(out), path)
                    return out
            except Exception as e:  # noqa: BLE001
                log.debug("Taostats endpoint %s failed: %s", path, e)
                continue
        log.warning("Taostats enrichment failed on all endpoints — falling back to SDK only")
        return {}


# ---------------------------------------------------------------------- helpers


def _extract_rows(data: Any) -> list[dict[str, Any]]:
    """Find the list of subnet dicts in a few common API envelope shapes."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("data", "results", "subnets", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _coerce_int(x: Any) -> int | None:
    if x is None:
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _first_str(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in ("none", "null"):
            return s
    return None
