"""Live TUI dashboard for subnetscope.

Shows the latest scan as a sortable + filterable table that auto-refreshes
on a configurable cadence. Press Ctrl+C to exit.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from ..config import Config
from ..data.collector import (
    Collector, filter_rows, sort_rows,
    parse_sort_spec, format_sort_spec,
)
from ..types import ScanResult, SubnetRow
from .table import CATEGORY_STYLES, build_table

log = logging.getLogger(__name__)


@dataclass
class DashboardState:
    last_scan: ScanResult | None = None
    sort_spec: list = field(default_factory=lambda: [("fee", "asc")])
    filter_types: list[str] = field(default_factory=list)
    scanning: bool = False
    scan_progress: tuple[int, int] = (0, 0)
    last_error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def primary_sort_key(self) -> str:
        return self.sort_spec[0][0] if self.sort_spec else "fee"

    def visible_rows(self) -> list[SubnetRow]:
        if not self.last_scan:
            return []
        rows = filter_rows(self.last_scan.rows, self.filter_types)
        return sort_rows(rows, self.sort_spec)


def _header_panel(state: DashboardState, cfg: Config) -> Panel:
    scan = state.last_scan
    if scan:
        ts = scan.fetched_at.astimezone().strftime("%H:%M:%S")
        head = scan.head_block
        n_rows = len(scan.rows)
        n_failed = len(scan.failures)
        line1 = (f"[bold cyan]subnetscope[/bold cyan]  "
                 f"head [bold]{head}[/bold]  "
                 f"scanned [bold]{n_rows}[/bold]"
                 + (f"  [red]failed {n_failed}[/red]" if n_failed else "")
                 + f"  last refresh [dim]{ts}[/dim]")
    else:
        line1 = "[bold cyan]subnetscope[/bold cyan]  initializing…"

    sort_label = format_sort_spec(state.sort_spec) or "—"
    filt = ",".join(state.filter_types) if state.filter_types else "all"
    line2 = (f"sort: [yellow]{sort_label}[/yellow]   "
             f"filter: [yellow]{filt}[/yellow]   "
             f"refresh: [yellow]{cfg.scan.refresh_seconds}s[/yellow]   "
             f"endpoint: [dim]{cfg.network.subtensor_endpoint}[/dim]")

    body: list = [Text.from_markup(line1), Text.from_markup(line2)]
    if state.scanning:
        done, total = state.scan_progress
        bar = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30),
            TextColumn("{task.completed}/{task.total}"),
            transient=False,
        )
        bar.add_task("scanning…", total=max(1, total), completed=done)
        body.append(bar)
    if state.last_error:
        body.append(Text.from_markup(f"[red]error:[/red] {state.last_error}"))

    return Panel(Group(*body), border_style="grey37", padding=(0, 1))


def _summary_panel(state: DashboardState) -> Panel:
    rows = state.visible_rows()
    if not rows:
        return Panel("[dim]no rows yet[/dim]", title="summary", border_style="grey37")

    counts = Counter(r.category for r in rows)
    fees = [r.recycle_tao for r in rows if r.recycle_tao > 0]
    cheapest = min(fees) if fees else 0.0
    median_fee = sorted(fees)[len(fees) // 2] if fees else 0.0
    most_expensive = max(fees) if fees else 0.0
    total_emission = sum(r.emission_per_day for r in rows)
    total_liq = sum(r.tao_in for r in rows)

    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold")
    t.add_column()
    t.add_row("subnets visible:", f"{len(rows)}")
    t.add_row("reg fee min/median/max:",
              f"{cheapest:.3f} / {median_fee:.3f} / {most_expensive:.3f} τ")
    t.add_row("total emission/day:", f"{total_emission:,.1f} τ")
    t.add_row("total pool liquidity:", f"{total_liq:,.0f} τ")

    cat_line_parts = []
    for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        style = CATEGORY_STYLES.get(cat, "dim")
        cat_line_parts.append(f"[{style}]{cat}[/{style}]:{n}")
    t.add_row("by category:", "  ".join(cat_line_parts))

    return Panel(t, title="summary", border_style="grey37")


def _table_panel(state: DashboardState, cfg: Config) -> Panel:
    rows = state.visible_rows()
    if not rows:
        return Panel("[dim]waiting for first scan…[/dim]", title="subnets",
                     border_style="grey37")
    table = build_table(rows, sort_by=state.primary_sort_key,
                        max_desc=cfg.dashboard.max_description_chars)
    return Panel(table, title=f"subnets ({len(rows)})", border_style="grey37")


def render(state: DashboardState, cfg: Config) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(_header_panel(state, cfg), size=5, name="header"),
        Layout(_summary_panel(state), size=8, name="summary"),
        Layout(_table_panel(state, cfg), name="table"),
    )
    return layout


class Dashboard:
    """Owns the Live render loop and a background scan thread."""

    def __init__(self, cfg: Config, collector: Collector):
        self.cfg = cfg
        self.collector = collector
        sort_spec = parse_sort_spec(cfg.dashboard.sort_by,
                                    default_order=cfg.dashboard.sort_order)
        if not sort_spec:
            sort_spec = [("fee", "asc")]
        self.state = DashboardState(
            sort_spec=sort_spec,
            filter_types=list(cfg.dashboard.filter_types or []),
        )
        self._stop = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._console = Console()

    def _progress_cb(self, done: int, total: int) -> None:
        self.state.scan_progress = (done, total)

    def _do_scan(self) -> None:
        try:
            self.state.scanning = True
            self.state.last_error = None
            result = self.collector.scan(progress_cb=self._progress_cb)
            self.state.last_scan = result
        except Exception as e:  # noqa: BLE001
            log.exception("scan failed")
            self.state.last_error = f"{type(e).__name__}: {e}"
        finally:
            self.state.scanning = False
            self.state.scan_progress = (0, 0)

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            self._do_scan()
            # Sleep in small chunks so we exit promptly on stop.
            slept = 0.0
            interval = max(5, self.cfg.scan.refresh_seconds)
            while slept < interval and not self._stop.is_set():
                time.sleep(0.5)
                slept += 0.5

    def run(self) -> None:
        self._scan_thread = threading.Thread(target=self._scan_loop,
                                             name="subnetscope-scan", daemon=True)
        self._scan_thread.start()
        try:
            with Live(render(self.state, self.cfg), refresh_per_second=2,
                      console=self._console, screen=False) as live:
                while not self._stop.is_set():
                    live.update(render(self.state, self.cfg))
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            if self._scan_thread is not None:
                self._scan_thread.join(timeout=3)
