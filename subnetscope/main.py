"""subnetscope CLI."""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from .config import Config
from .data.collector import (
    Collector, build_collector, filter_rows, sort_rows,
    parse_sort_spec, format_sort_spec,
)
from .exporters import export_csv, export_json
from .logging_setup import setup_logging
from .types import ScanResult, SubnetRow
from .ui.dashboard import Dashboard
from .ui.table import build_table, print_table

log = logging.getLogger("subnetscope")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
SORT_CHOICES = [
    "netuid", "fee", "burn", "demand",
    "name", "type", "category", "gpu", "reward",
    "top1", "miners", "gini",
    "emission", "liquidity", "age", "slots_used", "slots_free", "price",
]
CATEGORY_CHOICES = [
    "agent", "llm", "vision", "audio", "data",
    "trading", "storage", "compute", "science", "infra", "other",
]
GPU_CHOICES = ["heavy", "medium", "low", "none", "varies", "?"]


def _strip_config_arg_from_argv() -> None:
    """Remove our `--config <path>` from sys.argv before bittensor inits.

    Bittensor's `core/config.py` lazily parses sys.argv looking for its own
    `--config` option and prints `Loading config from: ...` for it. We
    already consumed our flag in click, so it's safe to drop it now.
    """
    argv = sys.argv
    out = [argv[0]] if argv else []
    skip_next = False
    for a in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if a == "--config":
            skip_next = True
            continue
        if a.startswith("--config="):
            continue
        out.append(a)
    sys.argv[:] = out


def _load_cfg(config_path: str) -> Config:
    cfg = Config.load(config_path)
    setup_logging(cfg.logging)
    _strip_config_arg_from_argv()
    return cfg


def _scan_with_progress(collector: Collector, console: Console) -> ScanResult:
    """Run a one-shot scan with a small in-place progress bar."""
    with Progress(
        TextColumn("[bold cyan]scanning subnets[/bold cyan]"),
        BarColumn(bar_width=30),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("scan", total=1)

        def cb(done: int, total: int) -> None:
            progress.update(task, completed=done, total=max(1, total))

        result = collector.scan(progress_cb=cb)
        progress.update(task, completed=progress.tasks[0].total or 1)
    return result


# ====================================================================== CLI


@click.group()
@click.version_option()
def cli() -> None:
    """subnetscope — list, sort, filter, and export Bittensor subnets."""


def _filter_gpu(rows: list[SubnetRow], gpu_needs: list[str]) -> list[SubnetRow]:
    if not gpu_needs:
        return rows
    wanted = {g.strip().lower() for g in gpu_needs if g.strip()}
    if not wanted:
        return rows
    return [r for r in rows if (r.gpu_need or "?").lower() in wanted]


@cli.command("list")
@click.option("--config", "config_path", default=str(DEFAULT_CONFIG_PATH),
              show_default=True, help="Path to config.yaml.")
@click.option("--sort", "sort_by", type=click.Choice(SORT_CHOICES), default=None,
              help="Sort column. Defaults to dashboard.sort_by from config.")
@click.option("--asc/--desc", "asc", default=None,
              help="Sort direction. Defaults to dashboard.sort_order from config.")
@click.option("--type", "types", multiple=True, type=click.Choice(CATEGORY_CHOICES),
              help="Filter to one or more subnet types (repeatable).")
@click.option("--gpu", "gpu_needs", multiple=True, type=click.Choice(GPU_CHOICES),
              help="Filter by GPU need (heavy|medium|low|none|varies). Repeatable.")
@click.option("--limit", type=int, default=0, show_default=True,
              help="Show only the first N rows after sort/filter (0 = all).")
@click.option("--max-desc", type=int, default=None,
              help="Truncate descriptions to N chars. Defaults to config value.")
def cmd_list(config_path: str, sort_by: str | None, asc: bool | None,
             types: tuple[str, ...], gpu_needs: tuple[str, ...],
             limit: int, max_desc: int | None) -> None:
    """One-shot table of all subnets to the terminal."""
    cfg = _load_cfg(config_path)

    # When --sort is given, it's a single-key override (with optional --asc/--desc).
    # Otherwise use the (possibly multi-key) config default.
    if sort_by:
        order = "asc" if asc is None or asc else "desc"
        sort_spec = [(sort_by.lower(), order)]
    else:
        sort_spec = parse_sort_spec(cfg.dashboard.sort_by,
                                    default_order=cfg.dashboard.sort_order)
        if not sort_spec:
            sort_spec = [("fee", "asc")]
    primary_key = sort_spec[0][0] if sort_spec else "fee"

    types_list = list(types) if types else list(cfg.dashboard.filter_types or [])
    max_desc = max_desc if max_desc is not None else cfg.dashboard.max_description_chars

    collector = build_collector(cfg)
    console = Console()
    try:
        scan = _scan_with_progress(collector, console)
    finally:
        collector.close()

    rows = filter_rows(scan.rows, types_list)
    rows = _filter_gpu(rows, list(gpu_needs))
    rows = sort_rows(rows, sort_spec)
    if limit > 0:
        rows = rows[:limit]

    title = (f"Bittensor subnets — sort: {format_sort_spec(sort_spec)} — "
             f"head block {scan.head_block} — "
             f"{scan.fetched_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print_table(rows, sort_by=primary_key, max_desc=max_desc, title=title, console=console)

    if scan.failures:
        console.print(Panel(
            "\n".join(f"netuid {n}: {err}" for n, err in scan.failures.items()),
            title=f"[red]{len(scan.failures)} subnet(s) failed[/red]",
            border_style="red",
        ))


@cli.command("watch")
@click.option("--config", "config_path", default=str(DEFAULT_CONFIG_PATH),
              show_default=True, help="Path to config.yaml.")
def cmd_watch(config_path: str) -> None:
    """Live auto-refreshing TUI dashboard."""
    cfg = _load_cfg(config_path)
    collector = build_collector(cfg)
    try:
        Dashboard(cfg, collector).run()
    finally:
        collector.close()


@cli.command("show")
@click.argument("netuid", type=int)
@click.option("--config", "config_path", default=str(DEFAULT_CONFIG_PATH),
              show_default=True, help="Path to config.yaml.")
def cmd_show(netuid: int, config_path: str) -> None:
    """Detailed view of one subnet."""
    cfg = _load_cfg(config_path)
    collector = build_collector(cfg)
    console = Console()
    try:
        head = collector.sdk.current_block()
        row = collector.sdk.fetch_subnet_row(netuid, head_block=head)
        meta = collector.taostats.fetch_subnet_metadata().get(netuid) or {}
        if not row.name and meta.get("name"):
            row.name = meta["name"]
            row.name_source = "taostats"
        if not row.description and meta.get("description"):
            row.description = meta["description"]
        if not row.github_repo and meta.get("github_repo"):
            row.github_repo = meta["github_repo"]
        if not row.subnet_url and meta.get("subnet_url"):
            row.subnet_url = meta["subnet_url"]
        collector.categorizer.apply([row])
    finally:
        collector.close()

    _print_detail(console, row)


def _print_detail(console: Console, r: SubnetRow) -> None:
    """Three-section detail: identity, economics, reward shape."""

    def _yn(x: bool | None) -> str:
        return "—" if x is None else ("yes" if x else "no")

    def _val_or_dash(x) -> str:
        return "—" if x is None else str(x)

    kappa_pct = (f"{r.kappa / 65535:.2%}"
                 if isinstance(r.kappa, int) else "—")

    identity = [
        ("Netuid",       f"{r.netuid}"),
        ("Name",         r.name or f"sn{r.netuid}"),
        ("Category",     r.category),
        ("GPU need",     r.gpu_need),
        ("Reward shape", r.reward_shape),
        ("GitHub",       r.github_repo or "—"),
        ("URL",          r.subnet_url or "—"),
        ("Discord",      r.discord or "—"),
        ("Name source",  r.name_source),
    ]
    # Demand gauge for the burn fee — shows where the live burn sits in the
    # min..max range. Useful for seeing "is this subnet hot or cold?"
    demand_text = "—"
    if r.max_burn_tao > r.min_burn_tao and r.recycle_tao > 0:
        frac = max(0.0, min(1.0,
                            (r.recycle_tao - r.min_burn_tao)
                            / (r.max_burn_tao - r.min_burn_tao)))
        bar_width = 20
        filled = round(frac * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        demand_text = f"[{bar}] {frac*100:.1f}% of max"

    full_marker = ""
    if r.slots_free == 0 and r.max_n > 0:
        full_marker = "  [bold red](FULL — registering evicts the lowest-incentive UID)[/bold red]"

    economics = [
        ("Burn fee (current, live)",  f"{r.recycle_tao:.6f} τ"),
        ("Burn fee min / max bounds", f"{r.min_burn_tao:.6f} τ  →  {r.max_burn_tao:.4f} τ"),
        ("Demand gauge",              demand_text),
        ("Burn reg allowed",          _yn(r.burn_registration_allowed)),
        ("PoW reg allowed",           _yn(r.pow_registration_allowed)),
        ("PoW difficulty",            _val_or_dash(r.difficulty)),
        ("UID slots",                 f"{r.subnetwork_n} used / {r.max_n} max  ({r.slots_free} free){full_marker}"),
        ("Validator slots (max)",     _val_or_dash(r.max_validators)),
        ("Pool TAO in",               f"{r.tao_in:,.4f} τ"),
        ("Pool alpha in",             f"{r.alpha_in:,.4f}"),
        ("Price",                     f"{r.price_tao_per_alpha:.6f} τ/α"),
        ("Emission/block",            f"{r.emission_per_block:.6f} τ"),
        ("Emission/day",              f"{r.emission_per_day:,.4f} τ"),
        ("Age",                       f"{r.age_days:,.1f} days  ({r.age_blocks:,} blocks)"),
    ]
    pct = lambda x: f"{x * 100:.1f}%" if isinstance(x, (int, float)) else "—"  # noqa: E731

    rewards_live = [
        ("Reward shape (from metagraph)", r.reward_shape),
        ("Active miners",                 _val_or_dash(r.active_miners)),
        ("Top-1 share of incentive",      pct(r.top1_share)),
        ("Top-5 share",                   pct(r.top5_share)),
        ("Top-10 share",                  pct(r.top10_share)),
        ("Top-50 share",                  pct(r.top50_share)),
        ("Gini coefficient",              f"{r.incentive_gini:.3f}" if r.incentive_gini is not None else "—"),
    ]
    rewards_chain = [
        ("rho (incentive sigmoid)",      _val_or_dash(r.rho)),
        ("kappa (consensus threshold)",  f"{_val_or_dash(r.kappa)}  ({kappa_pct})"),
        ("alpha_high / alpha_low",       f"{_val_or_dash(r.alpha_high)} / {_val_or_dash(r.alpha_low)}"),
        ("alpha_sigmoid_steepness",      _val_or_dash(r.alpha_sigmoid_steepness)),
        ("liquid_alpha_enabled",         _yn(r.liquid_alpha_enabled)),
        ("immunity_period (blocks)",     _val_or_dash(r.immunity_period)),
        ("tempo (blocks/cycle)",         _val_or_dash(r.tempo)),
        ("yuma_version",                 _val_or_dash(r.yuma_version)),
        ("commit_reveal_enabled",        _yn(r.commit_reveal_enabled)),
        ("weights_rate_limit",           _val_or_dash(r.weights_rate_limit)),
    ]

    sections: list[str] = []
    for header, fields in (("Identity", identity),
                           ("Economics", economics),
                           ("Reward distribution (live)", rewards_live),
                           ("Reward hyperparameters (chain)", rewards_chain)):
        sections.append(f"[bold underline]{header}[/bold underline]")
        sections.extend(f"  [bold]{k:<32}[/bold] {v}" for k, v in fields)
        sections.append("")

    if r.description:
        sections.append("[bold underline]Description[/bold underline]")
        sections.append(f"  {r.description}")

    console.print(Panel(
        "\n".join(sections).rstrip(),
        title=f"subnet {r.netuid} — {r.name or 'unnamed'}",
        border_style="cyan",
    ))


@cli.command("export")
@click.option("--config", "config_path", default=str(DEFAULT_CONFIG_PATH),
              show_default=True, help="Path to config.yaml.")
@click.option("--format", "fmt", type=click.Choice(["csv", "json", "both"]),
              default="csv", show_default=True, help="Export format.")
@click.option("--out", "out_dir", default="reports", show_default=True,
              help="Output directory.")
@click.option("--sort", "sort_by", type=click.Choice(SORT_CHOICES), default=None,
              help="Sort column for CSV (JSON keeps native scan order).")
@click.option("--asc/--desc", "asc", default=None, help="Sort direction.")
@click.option("--type", "types", multiple=True, type=click.Choice(CATEGORY_CHOICES),
              help="Filter to one or more subnet types (repeatable).")
@click.option("--gpu", "gpu_needs", multiple=True, type=click.Choice(GPU_CHOICES),
              help="Filter by GPU need (repeatable).")
def cmd_export(config_path: str, fmt: str, out_dir: str, sort_by: str | None,
               asc: bool | None, types: tuple[str, ...],
               gpu_needs: tuple[str, ...]) -> None:
    """Run a scan and write a CSV / JSON snapshot."""
    cfg = _load_cfg(config_path)

    if sort_by:
        order = "asc" if asc is None or asc else "desc"
        sort_spec = [(sort_by.lower(), order)]
    else:
        sort_spec = parse_sort_spec(cfg.dashboard.sort_by,
                                    default_order=cfg.dashboard.sort_order)
        if not sort_spec:
            sort_spec = [("fee", "asc")]

    types_list = list(types) if types else list(cfg.dashboard.filter_types or [])

    collector = build_collector(cfg)
    console = Console()
    try:
        scan = _scan_with_progress(collector, console)
    finally:
        collector.close()

    out_path = Path(out_dir).expanduser()
    out_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    written: list[Path] = []

    if fmt in ("csv", "both"):
        rows = filter_rows(scan.rows, types_list)
        rows = _filter_gpu(rows, list(gpu_needs))
        rows = sort_rows(rows, sort_spec)
        p = export_csv(rows, out_path / f"subnets-{stamp}.csv")
        written.append(p)

    if fmt in ("json", "both"):
        p = export_json(scan, out_path / f"subnets-{stamp}.json")
        written.append(p)

    for p in written:
        console.print(f"[green]wrote[/green] {p}  ({p.stat().st_size:,} bytes)")


@cli.command("web")
@click.option("--config", "config_path", default=str(DEFAULT_CONFIG_PATH),
              show_default=True, help="Path to config.yaml.")
@click.option("--host", default="0.0.0.0", show_default=True,
              help="Bind address. 0.0.0.0 = reachable on LAN; "
                   "use 127.0.0.1 to lock to localhost only.")
@click.option("--port", type=int, default=8765, show_default=True,
              help="Port to listen on.")
@click.option("--ttl", "cache_ttl", type=int, default=120, show_default=True,
              help="Seconds before a chain rescan kicks off in the "
                   "background. Pages always serve cached data instantly.")
@click.option("--state-db", "state_db_path", default=None,
              help="Path to SQLite state.db (defaults to <project>/state.db).")
@click.option("--no-prewarm", is_flag=True, default=False,
              help="Don't kick off the initial scan in the background at startup.")
@click.option("--reload", is_flag=True, default=False,
              help="Enable auto-reload (dev only - re-runs scan on every code change).")
def cmd_web(config_path: str, host: str, port: int, cache_ttl: int,
            state_db_path: str | None, no_prewarm: bool,
            reload: bool) -> None:
    """Serve the live web dashboard at http://HOST:PORT."""
    cfg = _load_cfg(config_path)
    console = Console()

    import uvicorn
    from .web.analysis import DEFAULT_ANALYSES_DIR, init_store as init_analysis_store
    from .web.app import create_app
    from .web.auto_analyzer import init_auto_analyzer
    from .web.burn_live import init_burn_live
    from .web.cache import init_scanner
    from .web.coldkey import init_coldkey_service
    from .web.emission_split import init_emission_split
    from .web.miner_rewards import init_miner_rewards
    from .web.tao_price import init_tao_price

    scanner = init_scanner(cfg, ttl_seconds=cache_ttl,
                           state_db_path=state_db_path)
    if not no_prewarm:
        scanner.prewarm_async()

    # Init the analysis store (so the auto-analyzer knows where to write).
    init_analysis_store()

    # Start hourly auto-analyzer (writes to analyses/auto/sn<N>.md).
    init_auto_analyzer(DEFAULT_ANALYSES_DIR, interval_seconds=3600)

    # Lightweight burn-fee live cache — reuses the SDK's subtensor connection.
    try:
        _sdk_client = scanner._collector.sdk
    except Exception:
        _sdk_client = None
    init_burn_live(sdk_client=_sdk_client, ttl=12.0)

    # Live TAO/USD price ticker for the dashboard (CoinGecko, free tier).
    init_tao_price(prewarm=not no_prewarm)

    # Read-only coldkey directory: free TAO + per-subnet stake positions.
    init_coldkey_service(sdk_client=_sdk_client,
                         ttl=float(cfg.coldkeys.cache_ttl_seconds),
                         prewarm=not no_prewarm)

    # Per-subnet owner / validators / miners emission split (uses chain-global
    # SubnetOwnerCut + each subnet's kappa). One cached RPC, refreshed every
    # 10 min.
    init_emission_split(sdk_client=_sdk_client, prewarm=not no_prewarm)

    # Per-subnet miner reward ranking (lazy: only fetched on detail page
    # view). 5-min TTL per subnet — way shorter than tempo so we always see
    # the latest paid distribution.
    init_miner_rewards(sdk_client=_sdk_client)

    app = create_app()

    db_display = state_db_path or "<project>/state.db"

    # Build URL list. If host is 0.0.0.0 we also surface the local LAN
    # IP so the user can paste it into a phone / other machine.
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    base = f"http://{display_host}:{port}"
    extra_urls = ""
    if host == "0.0.0.0":
        try:
            import socket as _socket
            # Collect all non-loopback IPs.
            all_ips: list[str] = []
            for info in _socket.getaddrinfo(_socket.gethostname(), None,
                                            _socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127."):
                    all_ips.append(ip)
            # Also try the routing-based trick (catches WSL2 NAT addr).
            try:
                s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                route_ip = s.getsockname()[0]
                s.close()
                if route_ip not in all_ips and not route_ip.startswith("127."):
                    all_ips.append(route_ip)
            except Exception:
                pass

            # Prefer non-private / non-WSL IPs (172.x is WSL NAT).
            def _score(ip: str) -> int:
                if ip.startswith("172."):
                    return 0   # WSL/Docker NAT — least preferred
                if ip.startswith("10.") or ip.startswith("192.168."):
                    return 1   # LAN
                return 2       # public IP — most preferred

            all_ips.sort(key=_score, reverse=True)
            url_lines = []
            for ip in all_ips:
                note = ""
                if ip.startswith("172."):
                    note = " [dim](WSL NAT — needs Windows portproxy)[/dim]"
                url_lines.append(
                    f"  [dim]→[/dim]       [link=http://{ip}:{port}]"
                    f"http://{ip}:{port}[/link]{note}"
                )
            if url_lines:
                extra_urls = "\n".join(url_lines) + "\n"
        except Exception:
            pass
        bind_note = "[yellow]bound[/yellow]    0.0.0.0 (reachable on LAN / all interfaces)"
    else:
        bind_note = f"bound     {host} (localhost only)"

    # Detect WSL2 — /proc/version contains "microsoft" on WSL.
    _is_wsl = False
    try:
        with open("/proc/version") as _f:
            _is_wsl = "microsoft" in _f.read().lower()
    except Exception:
        pass

    # Collect the WSL NAT IP (172.x) for the portproxy hint.
    _wsl_ip = ""
    if _is_wsl:
        try:
            import socket as _s2
            _sock = _s2.socket(_s2.AF_INET, _s2.SOCK_DGRAM)
            _sock.connect(("8.8.8.8", 80))
            _wsl_ip = _sock.getsockname()[0]
            _sock.close()
        except Exception:
            pass

    _wsl_hint = ""
    if _is_wsl and host == "0.0.0.0":
        _wsl_hint = (
            f"\n[yellow]  WSL2 detected.[/yellow] Uvicorn listens on {_wsl_ip or '172.x.x.x'}.\n"
            f"  To reach from Windows/internet, run in PowerShell (Admin):\n"
            f"    [dim]$wsl = (wsl hostname -I).Trim().Split(' ')[0][/dim]\n"
            f"    [dim]netsh interface portproxy add v4tov4 `[/dim]\n"
            f"    [dim]  listenport={port} listenaddress=0.0.0.0 `[/dim]\n"
            f"    [dim]  connectport={port} connectaddress=$wsl[/dim]\n"
            f"    [dim]netsh advfirewall firewall add rule name=subnetscope `[/dim]\n"
            f"    [dim]  dir=in action=allow protocol=TCP localport={port}[/dim]"
        )

    console.print(Panel(
        f"[bold cyan]subnetscope web[/bold cyan]\n"
        f"  local     [link={base}]{base}[/link]  (recommendations — default)\n"
        f"{extra_urls}"
        f"  table     [link={base}/dashboard]{base}/dashboard[/link]\n"
        f"  {bind_note}\n"
        f"  cache     {cache_ttl}s TTL (stale-while-revalidate)\n"
        f"  state db  {db_display}\n"
        f"  prewarm   {'OFF' if no_prewarm else 'ON (initial scan in background)'}\n"
        f"  health    {base}/api/health\n"
        f"  api docs  {base}/api/docs\n"
        f"{_wsl_hint}"
        f"\n[dim]Ctrl-C to stop[/dim]",
        border_style="cyan",
    ))

    uvicorn.run(app, host=host, port=port, reload=reload, log_level="info")


@cli.command("categories")
def cmd_categories() -> None:
    """Show the available subnet categories used by the classifier."""
    console = Console()
    table_lines = [f"  [bold]{c}[/bold]" for c in CATEGORY_CHOICES]
    console.print(Panel("\n".join(table_lines),
                        title="subnetscope categories", border_style="cyan"))
    console.print("Override per-netuid in [bold]subnetscope/categories.json[/bold].")


def _entry() -> None:
    try:
        cli(standalone_mode=False)
    except click.exceptions.Abort:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        log.exception("fatal")
        click.echo(f"error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    _entry()
