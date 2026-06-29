# Subnet 9 — iota

> Auto-analyzed: 2026-06-29 21:55 EEST · easy-entry score: 18.6/100 · refreshes hourly

*This analysis is auto-generated from live chain data. It updates every hour.
For hand-curated notes, check the Bittensor Discord or the subnet's own docs.*

## Quick stats

| Metric | Value |
|---|---|
| Category | llm |
| GPU need | heavy |
| Reward shape | winner |
| Active miners | 1 |
| UID slots | 256/256 (FULL) |
| Burn fee | 0.000500 τ `[░░░░░░░░░░░░░░░░░░░░]` 0.0% of max |
| Burn min / max | 0.000500 τ → 100.0000 τ |
| Emission / day | 268.4915 τ |
| Top-1 share | 100.0% |
| Liquidity (TAO in) | 61,352.9 τ |
| Price (τ/α) | 0.034842 |
| Age | 976 days |


> **Trend (from 98 snapshots):** Burn fee has fallen ~19% over the last 98 snapshots.

## Easy-entry score: 19 / 100  🔴 low

- heavy GPU need
- top miner incentive 100% (winner-take-all)
- subnet is FULL (eviction war)
- cheap burn fee 0.0005 τ

| Component | Points |
|---|---|
| GPU friction | 1.1 / 22 |
| Decentralization (top-1) | 0.0 / 20 |
| Active miners | 0.1 / 16 |
| Free slots | 0.0 / 13 |
| Burn fee | 11.0 / 11 |
| Liquidity | 3.0 / 10 |
| Emission | 3.4 / 8 |

## What is known about this category

This subnet produces or evaluates natural-language content. A local LLM (Llama 3.1 8B) is usually sufficient; GPU is helpful for fast inference but not always required.

## GPU / hardware

Heavy GPU — A100 or H100 class hardware gives the most competitive edge.

## Reward shape

**Winner-take-all:** the single best scorer takes most (>50%) of the miner emission. Expect lumpy, high-variance income. You need to be consistently near the top to earn meaningfully.

## Cost to operate a miner

✅ no paid API keys required · 🖥️ GPU required · ℹ️ baseline estimate (check repo to verify)

**Monthly recurring:** ~$1,530 – $3,800 · **One-time setup:** ~$8,000 – $35,000

### Monthly costs

| Item | Required | USD / month | Notes |
|---|:-:|---|---|
| GPU rental | ✓ | $1,500 – $3,500 | GPU rental (A100 / H100 80 GB) |
| LLM API (OpenAI / Anthropic / OpenRouter) | — | $30 – $300 | optional if you run a local model; mandatory on a few (check env.example in the repo) |

### One-time costs

| Item | Required | USD | Notes |
|---|:-:|---|---|
| Registration burn fee (one-time per UID) | ✓ | $0.10 – $0.13 | 0.0005 τ · @ $209.12/τ · includes ~20% buffer for fee jitter |
| GPU purchase (optional alternative to renting) | — | $8,000 – $35,000 | A100 / H100 — usually rented, not bought |

> Estimates are baseline ranges from the subnet's category and GPU need; check the subnet's `env.example` / README for specific API key requirements.

## Getting started (generic)

1. Search for this subnet's GitHub/Discord using the links below.
2. Set up a Bittensor wallet: hot+cold key pair.
3. Fund with burn fee + buffer (~0.0006 τ recommended).
4. Clone the subnet repo, run the miner in test mode first.
5. `btcli subnet register --netuid 9 --wallet.name <cold> --wallet.hotkey <hot>`
6. Monitor your score during the immunity window.

## Links

- GitHub: <https://github.com/macrocosm-os/iota>
- Website: <https://iota.macrocosmos.ai/>
- Discord: <macrocrux>
- TAO.app: <https://tao.app/subnet/9>

