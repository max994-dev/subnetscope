# subnetscope

Read-only Bittensor subnet directory. Lists, sorts, and filters every subnet
on the network by **registration fee**, **subnet type**, **liquidity**,
**emission**, and more — as a one-shot table, a live TUI dashboard, a
**web dashboard**, or a CSV / JSON export.

No wallet. No keys. No transactions. Just inspection.

> **Sister projects**: [`alphapilot`](../alphapilot/) (automated staking)
> and [`alphatrader`](../alphatrader/) (alpha trading bot). Subnetscope can
> share the same Taostats API key configured in either of those.

---

## Why

Bittensor has 100+ subnets. Picking which to register on, stake into, or
just understand is hard because **the chain doesn't carry a `subnet_type`
field** — every subnet is just a number with a self-reported name.
Subnetscope solves that with a small curated category override file plus a
keyword classifier so you can answer questions like:

- *"Show me all agent subnets sorted by registration fee, ascending."*
- *"Which LLM training subnets have the most free UID slots?"*
- *"Export every trading subnet to CSV for a spreadsheet."*

---

## Install

**`bittensor-cli` is not enough.** Subnetscope imports the **`bittensor`** SDK (`Subtensor`, metagraph, etc.). If you only installed `bittensor-cli` while fixing another project’s packages, install the SDK too, e.g. `pip install 'bittensor>=9'` or `uv pip install -e .` from this repo.

All `bt/*` projects share a **single global Python environment** at
`~/.local/lib/python3.12/site-packages` (no `venv` activation, no
per-project conflicts). Use the `gpip` wrapper for any `pip install`-style
work:

```bash
cd /home/administrator/bt/subnetscope
gpip install -e .
```

The console script ends up on `$PATH` automatically (`~/.local/bin/subnetscope`).

### `gpip` cheatsheet

```bash
gpip install <pkg>...                  # plain install
gpip install -r requirements.txt       # from a file
gpip install -e .                      # editable install (current dir)
gpip install --overrides FILE -r req   # honor pin overrides for conflicts
gpip uninstall <pkg>...
gpip list / show / freeze              # forwarded to `uv pip`
gpip help
```

Behind the scenes `gpip` is `uv pip install --target ~/.local/lib/python3.12/site-packages`
plus auto-symlinking of any new console scripts into `~/.local/bin/`.

### Resolving cross-project version pins

When a new project's `requirements.txt` pins a different version of
something already installed (e.g. `bittensor==9.0.0` vs the current
`10.3.0`), write a one-off override file and pass `--overrides`:

```bash
cat > /tmp/myproj-overrides.txt <<EOF
bittensor==10.3.0           # keep modern version (subnetscope needs it)
async-substrate-interface==2.0.3
pydantic==2.13.4
EOF
gpip install --overrides /tmp/myproj-overrides.txt -r requirements.txt
```

---

## Configure

Edit `config.yaml`:

- `network.subtensor_endpoint` — defaults to public Finney
- `network.taostats_api_key` — optional; raises name/description coverage
  from ~40 % (on-chain identity) to ~95 %. Same key as alphapilot.
- `universe.whitelist_netuids` — leave empty to scan **all** subnets, or
  list specific ones for fast iteration.
- `dashboard.sort_by` — default sort column for `list` and `watch`.
- `coldkeys.entries` — optional list of **public SS58 addresses** to
  surface in the dashboard's Wallet modal (see [Wallet modal](#wallet-modal)).
  No private keys, mnemonics, or passwords are ever read.

Per-subnet category overrides live in `subnetscope/categories.json` —
edit freely.

### Wallets (read-only) — `coldkeys:` section

Optional. Lets the dashboard's **Wallet** button open a modal showing
free TAO + per-subnet stake positions (alpha amount, TAO-equivalent
value, and % of portfolio) for any coldkey you list.

```yaml
coldkeys:
  cache_ttl_seconds: 60      # how long to reuse a snapshot before re-querying
  allow_adhoc_lookup: true   # if true, you can paste any SS58 in the modal
  entries:
    - name: "main"
      ss58: "5Fxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
      note: "primary cold storage"
    - name: "ghostar"
      ss58: "5Gxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

To get the SS58 of a wallet you created with `btcli`:

```bash
cat ~/.bittensor/wallets/<wallet_name>/coldkeypub.txt
```

The SS58 is **public** — safe to commit. Subnetscope only ever calls
`subtensor.get_balance(ss58)` and `subtensor.get_stake_info_for_coldkey(ss58)`.

---

## Usage

### One-shot table

```bash
# Use the multi-key default sort from config.yaml:
.venv/bin/python -m subnetscope.main list

# Override with a single sort key (uses --asc/--desc for direction):
.venv/bin/python -m subnetscope.main list --sort fee --asc
.venv/bin/python -m subnetscope.main list --sort emission --desc --limit 20

# Filter + sort:
.venv/bin/python -m subnetscope.main list --type agent --type llm
.venv/bin/python -m subnetscope.main list --gpu none --sort fee --asc
.venv/bin/python -m subnetscope.main list --sort reward --desc --type llm
```

### Default sort (multi-key)

The default sort comes from `dashboard.sort_by` in `config.yaml`. It's tuned
for "find the best subnet to mine on" and is composed of six keys, each
with its own direction:

```yaml
sort_by: "gpu:asc, reward:asc, slots_free:desc, fee:asc, liquidity:desc, emission:desc"
```

Reading order:
1. **gpu asc** — `none` first, `heavy` last (easier hardware first)
2. **reward asc** — `flat` first, `winner` last (more spread = more chance to earn)
3. **slots_free desc** — most free UID slots first (less crowded)
4. **fee asc** — cheapest burn first
5. **liquidity desc** — biggest pool first (more stable AMM)
6. **emission desc** — highest daily emission first (more rewards)

Each key acts as a tiebreaker for the previous one. CLI `--sort` always
overrides the multi-key default with a single-key sort.

#### Sort syntax reference

```yaml
sort_by: "fee"                                  # single key
sort_by: "fee:desc"                             # single key with direction
sort_by: "gpu, reward, fee"                     # multi-key, all using sort_order default
sort_by: "gpu:asc, reward:asc, fee:asc"         # multi-key with explicit direction
```

Valid sort keys: `netuid` `fee`/`burn` `demand` `name` `type` `gpu`
`reward` `top1` `miners` `gini` `emission` `liquidity` `age`
`slots_used` `slots_free` `fullness` `price`.

### Default table columns

| Column        | Meaning                                                          |
| ------------- | ---------------------------------------------------------------- |
| `UID`         | netuid (subnet id)                                               |
| `Name`        | from on-chain SubnetIdentity or Taostats                         |
| `Type`        | category (`agent`/`llm`/`vision`/...)                            |
| `GPU`         | mining GPU need: `heavy`/`medium`/`low`/`none`/`varies`          |
| `Burn Fee`    | live registration cost in TAO (this IS the only registration payment) |
| `Demand`      | where the live burn sits between `min_burn` and `max_burn` (0–100%) |
| `Reward`      | live reward distribution: `winner`/`peak`/`topN`/`flat`          |
| `Top1%`       | share of total miner incentive captured by the #1 miner          |
| `Miners`      | active miners (count with non-zero incentive on the metagraph)   |
| `Used/Max`    | UID slots used vs total — **red bold means subnet is FULL** (registering evicts the lowest-incentive UID) |
| `Liquidity`   | pool TAO reserves                                                |
| `Emission/d`  | TAO-equivalent daily emission                                    |
| `Description` | self-reported summary                                            |

`show <netuid>` adds a full breakdown including the **demand gauge**
(textual bar showing current burn position in the min..max range) plus
top-1/5/10/50 incentive shares, Gini coefficient, rho, kappa,
alpha_high/low, alpha_sigmoid_steepness, liquid_alpha_enabled,
immunity_period, tempo, yuma_version, commit_reveal flag, and more.

### About the registration / burn fee

There is **only one registration payment** in Bittensor — the burn fee
(`subtensor.recycle(netuid)`). When a subnet is full (Used == Max),
registering does not cost extra; the same burn fee evicts the
lowest-incentive UID instead of taking an empty slot.

The fee is **dynamic**: it auto-adjusts each `adjustment_interval` between
`min_burn` and `max_burn` based on registration rate vs target. Most
subnets currently sit at `min_burn` (~0.0005 τ); a hot, oversubscribed
subnet can climb toward `max_burn` (typically 100 τ).

The `Demand` column gives a quick read on this:

| Demand    | Meaning                                                |
| --------- | ------------------------------------------------------ |
| `0%`      | at floor — fee unlikely to climb soon                  |
| `green`   | mild demand — small upward pressure                    |
| `yellow`  | moderate demand — fee is rising                        |
| `red`     | hot — fee is near max, register fast or wait it out    |

### Live TUI dashboard

```bash
.venv/bin/python -m subnetscope.main watch
```

Auto-refreshes every `scan.refresh_seconds` (90 s default). Press
`Ctrl+C` to exit.

### Web dashboard

```bash
.venv/bin/subnetscope web                       # http://<host-ip>:8765 (LAN-reachable)
.venv/bin/subnetscope web --host 127.0.0.1      # localhost only
.venv/bin/subnetscope web --port 9000           # different port
.venv/bin/subnetscope web --ttl 60              # background rescan every 60 s
.venv/bin/subnetscope web --no-prewarm          # don't kick off background scan at startup
.venv/bin/subnetscope web --state-db /tmp/sn.db # custom history DB location
```

LAN-reachable by default — the banner prints both the local URL and the
detected LAN IP. Pass `--host 127.0.0.1` to lock it to localhost.

**Performance**

Page navigation is non-blocking: the chain scanner runs in a background
thread under a **stale-while-revalidate** policy, so every request
returns cached data instantly (typically < 30 ms), and a refresh is
triggered behind the scenes when the cache is older than `--ttl`. All
HTTP handlers are sync and run in FastAPI's threadpool, so a slow
chain RPC can never freeze the event loop.

**Features**

- **Live table** — same columns as `subnetscope list`; HTMX polls every
  `scan.refresh_seconds`.
- **Search box** — name / description / netuid; 150 ms debounce.
- **Multi-key sort textbox** — paste a spec like
  `gpu:asc, top1:asc, miners:desc, fee:asc`.
- **Category and GPU-need filters** — multi-select chips.
- **Watchlist** — click ☆ on any row to pin it (stored in `localStorage`,
  no server account needed). Toggle "show only watched" to filter.
- **Recommendations** at `/` (default landing page) and `/recommendations`
  (alias) — top-N subnets ranked by `easy_entry_score` (gpu need · top-1
  share · active miners · slots free · burn fee · liquidity · emission).
  Each row shows the *why* bullets. The full HTMX table lives at
  `/dashboard`.
- **Detail page** at `/subnet/<netuid>` — score breakdown, all on-chain
  hyperparameters, and **24-hour sparklines** for burn fee, price, emission,
  top-1 share, active miners, UID slots used.
- **Curated analyses** — drop a markdown file at `analyses/sn<N>.md` and
  it shows up as a card on the matching detail page. Edit the file and
  the next request picks it up (mtime cache). Subnets without a file
  show no card. See [Curated analyses](#curated-analyses) below.
- **Alerts bell 🔔** in the header — desktop notifications + dropdown panel.
  Polls `/api/alerts` every ~12 s. Triggers:
    * `slot-open` — a previously-full subnet now has free UID slots
    * `tempo-near` — ≤ 5 blocks until the next emission tick, only if a
      configured watch hotkey is registered on that subnet
    * `new-subnet` — a netuid that was not in the local DB before
- **Wallet modal 💰** in the header — opens a read-only view of any
  configured (or pasted) coldkey: free TAO, per-subnet alpha stake
  positions, TAO-equivalent value, % of portfolio, USD total. Uses only
  the public SS58. See [Wallet modal](#wallet-modal) below.
- **Force rescan** button bypasses the cache.
- **Shared scan cache** — one chain hit per `--ttl` window, regardless of
  how many tabs/HTMX polls hit the server. Initial scan runs in the
  background at startup so the first request doesn't have to wait 60 s+.
- **History DB** — every scan is snapshotted to `state.db` (SQLite, WAL
  mode). The detail-page sparklines and alert engine read from it.
- **JSON API** — `/api/rows`, `/api/subnet/<netuid>`, `/api/score/<netuid>`,
  `/api/recommendations`, `/api/history/<netuid>?hours=24`,
  `/api/alerts`, `/api/analysis/<netuid>`, `/api/analyses`,
  `/api/tao-price`, `/api/tao-price/history?hours=24`,
  `/api/coldkeys`, `/api/coldkey/<ss58>`,
  `/api/health`. Browse `/api/docs` for an interactive OpenAPI explorer.

#### Curated analyses

Free-form, human-written notes for subnets you actually plan to mine.
Stored as plain markdown files at `analyses/sn<N>.md` (one per subnet).

```text
analyses/
├── _template.md   ← copy this when adding a new one
├── sn6.md         ← Numinous (forecasting)
├── sn13.md        ← Data Universe (Macrocosmos)
├── sn59.md        ← Babelbit (translation)
└── sn72.md        ← StreetVision (NATIX)
```

Behaviour:

- File present → renders as a "curated" card at the top of the matching
  `/subnet/<N>` page (markdown → HTML, with tables, code, links).
- File missing → no card, no placeholder, page renders normally.
- Edit + save → next request picks up the change (mtime-keyed in-memory
  cache, no server restart).
- Convention: leading `# Subnet N — Name` becomes the card title, and a
  `> Analyzed: YYYY-MM-DD ...` blockquote becomes the freshness footer.

JSON access: `GET /api/analysis/<netuid>` returns the rendered HTML +
metadata (or 404). `GET /api/analyses` lists which netuids have files.

#### Wallet modal

Click the **💰 Wallet** button in the header (top-right) to open a
read-only view of any coldkey's TAO position. Source addresses are:

- **Configured** entries from `coldkeys.entries` in `config.yaml`
  (dropdown), or
- **Ad-hoc** — paste any SS58 in the text field (only when
  `coldkeys.allow_adhoc_lookup: true`, the default).

The modal shows:

| Block            | Meaning                                                                 |
|------------------|-------------------------------------------------------------------------|
| Free TAO         | unstaked balance returned by `subtensor.get_balance(ss58)`              |
| Staked value     | sum of (alpha × subnet pool price) across every active position        |
| Total portfolio  | free + staked, in TAO and USD (USD via the live CoinGecko ticker)      |
| Positions table  | per (hotkey, netuid): alpha staked, pool price, TAO value, % portfolio |

Snapshots are cached for `coldkeys.cache_ttl_seconds` (default 60 s).
Click ↻ to force-bypass the cache and re-query the chain.

Endpoints:

- `GET /api/coldkeys` → configured directory + adhoc policy.
- `GET /api/coldkey/<ss58>?force=0` → snapshot for one coldkey.

**Strict read-only:** subnetscope never reads private keys, mnemonics,
keystore files, or wallet passwords. Only the public SS58 hits the chain.

**No build step, no npm.** All assets (CSS, JS, sparkline drawer, watchlist,
alert poller, wallet modal) are hand-written and served from `/static`.
HTMX is loaded from `unpkg` (one `<script>` tag). Page loads are fast
(< 25 ms warm).

### Detailed view

```bash
.venv/bin/python -m subnetscope.main show 64
```

### Export CSV / JSON

```bash
.venv/bin/python -m subnetscope.main export --format csv
.venv/bin/python -m subnetscope.main export --format both --sort fee --asc
.venv/bin/python -m subnetscope.main export --format csv --type agent --out ~/Desktop
```

Output files are time-stamped: `reports/subnets-YYYYMMDD-HHMMSS.csv`.

### Categories

```bash
.venv/bin/python -m subnetscope.main categories
```

---

## Subnet categories + GPU need

Built-in classifier buckets every subnet into one of these categories.
Each category has a default GPU-mining requirement (overridable per-netuid):

| Category | Meaning                                            | GPU need |
| -------- | -------------------------------------------------- | -------- |
| `llm`    | language model training / inference / fine-tuning  | heavy    |
| `vision` | image / video generation, vision models            | heavy    |
| `audio`  | TTS, STT, music, speech                            | medium   |
| `science`| protein folding, biotech, research, weather        | medium   |
| `agent`  | autonomous agents (mostly call external APIs)      | low      |
| `data`   | scraping, indexing, search, oracles, social        | none     |
| `trading`| price prediction, alpha signals, sports            | none     |
| `storage`| distributed storage                                | none     |
| `compute`| GPU / compute marketplaces — you supply the GPU    | varies   |
| `infra`  | meta-layers, validator-of-validators, hash, gov.   | varies   |
| `other`  | uncategorized fallback                             | ?        |

GPU-need legend:

- `heavy` — high-end GPU (A100/H100, 40-80 GB VRAM) — model training
- `medium` — mid-range GPU (RTX 3090/4090) — inference, audio synth
- `low` — small GPU OR CPU works (calls external APIs)
- `none` — CPU + RAM + network, no GPU needed
- `varies` — depends on which subnet (e.g. compute marketplaces)
- `?` — unknown

## Reward shape (live, not theoretical)

`reward_shape` is computed from each subnet's **live metagraph** — the
actual on-chain `incentive` vector showing how miner rewards are
currently distributed. This catches subnets where the validator code
concentrates rewards even when the `rho` hyperparameter looks neutral.

| Shape    | Trigger                       | What it means for miners                            |
| -------- | ----------------------------- | --------------------------------------------------- |
| `winner` | top-1 miner ≥ 50 % of rewards | effectively winner-take-all — only #1 earns         |
| `peak`   | top-5 miners ≥ 80 %           | tight oligopoly — must be in top 5 to earn          |
| `topN`   | top-10 miners ≥ 80 %          | typical Bittensor — top ~10-50 share the bulk       |
| `flat`   | spread broader than that      | many miners earn meaningful amounts                 |
| `?`      | metagraph fetch disabled      | set `scan.fetch_metagraph: true` in config          |

Real-world example (probed live):

```
netuid 9  iota         winner   1 miner    top1=100%   pure winner-take-all
netuid 1  Apex         winner   3 miners   top1= 91%   miners 2-3 get scraps
netuid 4  Targon       peak     6 miners   top5= 98%   tight 5-way split
netuid 64 Chutes       topN    29 miners   top10=92%   broader competition
```

`show <netuid>` also displays the **Gini coefficient** of the incentive
distribution (0 = perfectly equal, 1 = single winner).

**Why not just trust `rho`?**  All current Bittensor subnets use the
default `rho=10`, which would *predict* a `topN` distribution — but in
practice each subnet's validator code shapes the W matrix very
differently. The metagraph is the only honest source of truth.

Disable metagraph fetching (faster scans, `reward_shape=?`) by setting
`scan.fetch_metagraph: false` in `config.yaml`.

Classification is **best-effort**. To correct or add a mapping, edit
`subnetscope/categories.json`:

```json
{
  "64": "compute",
  "100": "agent"
}
```

Manual overrides always win over the keyword classifier when
`categorize.use_overrides: true` (the default).

---

## What it reads from chain (per subnet)

Up to four RPC calls per subnet, per refresh:

**`subtensor.subnet(netuid)`** — DynamicInfo:
- name, description, github_repo, url, discord (SubnetIdentity)
- pool reserves: `tao_in`, `alpha_in`, `price`
- emissions: `tao_in_emission`, `alpha_out_emission` (combined to TAO/day)
- registration block (for `age_days`)

**`subtensor.get_subnet_hyperparameters(netuid)`** — all governance knobs:
- `rho`, `kappa`, `alpha_high`, `alpha_low`, `alpha_sigmoid_steepness`
- `liquid_alpha_enabled`
- `max_validators`, `immunity_period`, `tempo`
- `min_burn`, `max_burn`, `difficulty`
- `yuma_version`, `commit_reveal_weights_enabled`, `weights_rate_limit`

**`subtensor.recycle(netuid)`** — current burn cost (live, not bounded).

**`subtensor.metagraph(netuid, lite=True)`** — for live reward shape:
- `incentive` vector across all UIDs → top-N shares + Gini

The metagraph fetch can be disabled in config (`scan.fetch_metagraph: false`)
for ~2x faster scans.

All read-only. Subnetscope never signs or submits a transaction.

---

## Files

```
subnetscope/
├── pyproject.toml
├── config.yaml                  # network, scan, sort, filter, logging
├── README.md
├── subnetscope/
│   ├── main.py                  # CLI: list / watch / show / export / categories
│   ├── config.py                # typed config dataclasses
│   ├── types.py                 # SubnetRow, ScanResult
│   ├── categorize.py            # keyword classifier + overrides loader
│   ├── categories.json          # per-netuid manual overrides (editable)
│   ├── exporters.py             # CSV + JSON writers
│   ├── logging_setup.py
│   ├── data/
│   │   ├── sdk.py               # bittensor SDK wrapper (read-only)
│   │   ├── taostats.py          # optional Taostats enricher
│   │   └── collector.py         # merge + sort + filter
│   └── ui/
│       ├── table.py             # rich one-shot table
│       └── dashboard.py         # live TUI
├── logs/
└── reports/
```
