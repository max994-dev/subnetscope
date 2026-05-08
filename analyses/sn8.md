# Subnet 8 — Vanta (Proprietary Trading Network)

> Analyzed: 2026-05-05 · easy-entry score at time of analysis: 43.1/100

## TL;DR

Vanta (formerly PTN — Proprietary Trading Network, by Taoshi) is a
**decentralized trading signal network** — miners submit live trading signals
(long/short/flat) on crypto, forex, and equities and are scored on
**risk-adjusted performance** over a rolling 90-day challenge period. No GPU,
no model hosting. But: the 90-day challenge period before you earn anything,
the strict drawdown limits (max 10% drawdown), and the collateral requirement
make this one of the hardest entry subnets despite the low burn fee.

## What it actually does

Miners open and close positions on asset pairs by submitting signed orders to
the validator network. Validators simulate execution at real market prices
(including spread + carry costs) and track a portfolio for each miner.

Asset classes:
- **Crypto:** BTC, ETH, SOL, and others (0.01x–2.5x leverage)
- **Forex:** Major pairs (0.1x–10x leverage)
- **Equities:** US stocks (0.1x–2x leverage)

All positions are virtual — you don't trade real money — but slippage and
carry costs are modelled realistically.

The best aggregated signal is served to real-world clients via Taoshi's
trading product.

## Reward structure

Rolling performance-based debt system. Rewards target Sunday midnight payout.
Miners accumulate or lose "emission debt" based on risk-adjusted return
(Sharpe, drawdown, correlation across the miner cohort).

**90-day challenge period:** New miners must:
- Trade for at least 61 of the first 90 days
- Keep drawdown ≤ 10%
- Rank at the 25th percentile or above in performance

Only after passing this do they earn the full emission share. This gate
prevents casino-style bet-big-and-pray strategies.

## What miners run

- **Hardware:** No GPU, no server needed. Just a Python process that submits
  signed order messages.
- **Collateral:** 300–1000 "Theta" tokens must be deposited per position
  (each Theta = $500 of trading capacity).
- **Stack:** Python, the Vanta repo, your trading logic (any quantitative
  approach: momentum, mean-reversion, ML signals, options-inspired positions).

## Realistic earnings

At ~36 τ/day across 42 active miners (post-challenge):

| Skill | TAO/day | USD/day (TAO ≈ $250) |
|---|---|---|
| Passing challenge, mid-pack | ~0.3–0.6 τ | $75–$150 |
| Consistent Sharpe > 1 | ~0.7–1.5 τ | $175–$375 |
| Top-10 trader | ~2–4 τ | $500–$1000 |

## Honest assessment

**Pros:**
- No GPU, no hosting — pure quantitative skill
- 42 active miners across a wide asset universe — less crowded than data subnets
- Real-world signal deployment creates reputational / commercial value
- Multi-asset (crypto + forex + equities) lets you hedge model risk

**Cons:**
- 90-day challenge period with zero earnings if you fail criteria
- 10% max drawdown is strict — one bad week ends your challenge
- Theta collateral required upfront
- Slippage and carry costs modeled realistically — paper trading illusion
  doesn't work; you need real market intuition

## Getting started

1. Miner docs: <https://github.com/taoshidev/proprietary-trading-network/blob/main/docs/miner.md>
2. TAO Daily guide: <https://simplytao.ai/blog/your-simple-guide-to-vanta-sn8>
3. Wallet + ~0.005 τ buffer (low burn fee)
4. Deposit required Theta collateral
5. Start with a small position on BTC (low leverage) to understand the axon
   interface during the first days of the challenge
6. `btcli subnet register --netuid 8 --wallet.name <cold> --wallet.hotkey <hot>`

## Links

- GitHub: <https://github.com/taoshidev/proprietary-trading-network>
- TAO Daily: <https://taodaily.io/how-vanta-subnet-8-is-building-the-infrastructure-for-an-agent-driven-trading-economy-on-bittensor/>
- Simple guide: <https://simplytao.ai/blog/your-simple-guide-to-vanta-sn8>
- TAO.app: <https://tao.app/subnet/8>
