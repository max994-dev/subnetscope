"""Render a `rich` table for one-shot listing or live dashboard."""
from __future__ import annotations

from rich.console import Console
from rich.table import Table

from ..types import SubnetRow

CATEGORY_STYLES = {
    "agent":   "bold cyan",
    "llm":     "bold green",
    "vision":  "bold magenta",
    "audio":   "bold yellow",
    "data":    "bold blue",
    "trading": "bold red",
    "storage": "bold dim white",
    "compute": "bold bright_cyan",
    "science": "bold bright_magenta",
    "infra":   "bold bright_blue",
    "other":   "dim",
}

GPU_STYLES = {
    "heavy":  "bold red",
    "medium": "bold yellow",
    "low":    "bold cyan",
    "none":   "bold green",
    "varies": "dim white",
    "?":      "dim",
}

REWARD_STYLES = {
    "winner": "bold red",
    "peak":   "bold red",
    "topN":   "bold yellow",
    "flat":   "bold green",
    "?":      "dim",
}


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _fmt_tao(x: float, decimals: int = 2) -> str:
    if x == 0:
        return "—"
    if x >= 1000:
        return f"{x:,.0f} τ"
    if x >= 1:
        return f"{x:.{decimals}f} τ"
    return f"{x:.4f} τ"


def _fmt_burn(x: float) -> str:
    """Format the registration burn fee with enough precision to be useful."""
    if x == 0:
        return "—"
    if x >= 1000:
        return f"{x:,.0f} τ"
    if x >= 1:
        return f"{x:.4f} τ"
    if x >= 0.001:
        return f"{x:.6f} τ"
    return f"{x:.8f} τ"


def _burn_demand_pct(current: float, lo: float, hi: float) -> float | None:
    """Where the current burn sits between min_burn and max_burn (0-100%)."""
    if hi <= lo or current <= 0:
        return None
    return max(0.0, min(100.0, (current - lo) / (hi - lo) * 100.0))


def build_table(rows: list[SubnetRow], *, sort_by: str, max_desc: int = 50,
                title: str | None = None) -> Table:
    table = Table(
        title=title,
        title_style="bold white",
        header_style="bold white on grey23",
        row_styles=["", "on grey7"],
        expand=True,
        show_lines=False,
    )
    table.add_column("UID", justify="right", width=4)
    table.add_column("Name", overflow="ellipsis", min_width=10, max_width=18)
    table.add_column("Type", justify="center", width=8)
    table.add_column("GPU", justify="center", width=6)
    table.add_column("Burn Fee", justify="right", width=12)
    table.add_column("Demand", justify="right", width=7)
    table.add_column("Reward", justify="center", width=7)
    table.add_column("Top1%", justify="right", width=6)
    table.add_column("Miners", justify="right", width=6)
    table.add_column("Used/Max", justify="right", width=10)
    table.add_column("Liquidity", justify="right", width=10)
    table.add_column("Emission/d", justify="right", width=10)
    table.add_column("Description", overflow="ellipsis", no_wrap=True)

    for r in rows:
        cat_style = CATEGORY_STYLES.get(r.category, "dim")
        gpu_style = GPU_STYLES.get(r.gpu_need, "dim")
        rew_style = REWARD_STYLES.get(r.reward_shape, "dim")
        is_full = r.slots_free == 0 and r.max_n > 0

        # Burn fee styling:
        #   - red+bold when subnet is full (you'll evict + the fee is more
        #     likely to climb under registration pressure)
        #   - bold when this is the sort column
        burn_text = _fmt_burn(r.recycle_tao)
        if is_full:
            burn_text = f"[bold red]{burn_text}[/bold red]"
        elif sort_by == "fee":
            burn_text = f"[bold]{burn_text}[/bold]"

        demand = _burn_demand_pct(r.recycle_tao, r.min_burn_tao, r.max_burn_tao)
        if demand is None:
            demand_text = "—"
        else:
            color = ("red" if demand >= 50
                     else "yellow" if demand >= 10
                     else "green" if demand > 0
                     else "dim")
            demand_text = f"[{color}]{demand:.0f}%[/{color}]"

        slots_text = f"{r.subnetwork_n}/{r.max_n}"
        if is_full:
            slots_text = f"[bold red]{slots_text}[/bold red]"

        top1_text = f"{r.top1_share * 100:.0f}%" if r.top1_share is not None else "—"
        miners_text = (f"{r.active_miners}"
                       if r.active_miners is not None else "—")

        table.add_row(
            f"[bold]{r.netuid}[/bold]",
            r.name or f"sn{r.netuid}",
            f"[{cat_style}]{r.category}[/{cat_style}]",
            f"[{gpu_style}]{r.gpu_need}[/{gpu_style}]",
            burn_text,
            demand_text,
            f"[{rew_style}]{r.reward_shape}[/{rew_style}]",
            top1_text,
            miners_text,
            slots_text,
            _fmt_tao(r.tao_in, 0),
            _fmt_tao(r.emission_per_day, 1),
            _truncate(r.description, max_desc),
        )
    return table


def print_table(rows: list[SubnetRow], *, sort_by: str, max_desc: int = 50,
                title: str | None = None, console: Console | None = None) -> None:
    console = console or Console()
    table = build_table(rows, sort_by=sort_by, max_desc=max_desc, title=title)
    console.print(table)
