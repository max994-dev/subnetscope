# Subnet 4 — Targon

> Auto-analyzed: 2026-06-29 21:55 EEST · easy-entry score: 29.0/100 · refreshes hourly

*This analysis is auto-generated from live chain data. It updates every hour.
For hand-curated notes, check the Bittensor Discord or the subnet's own docs.*

## Quick stats

| Metric | Value |
|---|---|
| Category | llm |
| GPU need | heavy |
| Reward shape | winner |
| Active miners | 4 |
| UID slots | 256/256 (FULL) |
| Burn fee | 0.000500 τ `[░░░░░░░░░░░░░░░░░░░░]` 0.0% of max |
| Burn min / max | 0.000500 τ → 100.0000 τ |
| Emission / day | 442.6923 τ |
| Top-1 share | 76.7% |
| Liquidity (TAO in) | 130,476.5 τ |
| Price (τ/α) | 0.053585 |
| Age | 986 days |


## Easy-entry score: 29 / 100  🔴 low

- heavy GPU need
- subnet is FULL (eviction war)
- cheap burn fee 0.0005 τ
- high emission (443 τ-eq/d)

| Component | Points |
|---|---|
| GPU friction | 1.1 / 22 |
| Decentralization (top-1) | 4.7 / 20 |
| Active miners | 0.3 / 16 |
| Free slots | 0.0 / 13 |
| Burn fee | 11.0 / 11 |
| Liquidity | 6.4 / 10 |
| Emission | 5.6 / 8 |

## What is known about this category

This subnet produces or evaluates natural-language content. A local LLM (Llama 3.1 8B) is usually sufficient; GPU is helpful for fast inference but not always required.

## GPU / hardware

Heavy GPU — A100 or H100 class hardware gives the most competitive edge.

## Reward shape

**Winner-take-all:** the single best scorer takes most (>50%) of the miner emission. Expect lumpy, high-variance income. You need to be consistently near the top to earn meaningfully.

## Cost to operate a miner

✅ no paid API keys required · 🖥️ GPU required · 📋 verified from repo

**Monthly recurring:** ~$1,510 – $3,530 · **One-time setup:** ~$0.10 – $0.13

### Monthly costs

| Item | Required | USD / month | Notes |
|---|:-:|---|---|
| GPU rental (H100 strongly preferred) | ✓ | $1,500 – $3,500 | multi-modal inference benchmark — A100 is competitive, H100 dominates throughput-based scoring |
| VPS for orchestration | ✓ | $10 – $30 |  |

### One-time costs

| Item | Required | USD | Notes |
|---|:-:|---|---|
| Registration burn fee (one-time per UID) | ✓ | $0.10 – $0.13 | 0.0005 τ · @ $209.12/τ · includes ~20% buffer for fee jitter |

> **Subscription required:** no — no third-party API keys needed.
> Hardware-heavy: A100/H100 class GPU is the table stakes.

## Getting started (generic)

1. Search for this subnet's GitHub/Discord using the links below.
2. Set up a Bittensor wallet: hot+cold key pair.
3. Fund with burn fee + buffer (~0.0006 τ recommended).
4. Clone the subnet repo, run the miner in test mode first.
5. `btcli subnet register --netuid 4 --wallet.name <cold> --wallet.hotkey <hot>`
6. Monitor your score during the immunity window.

## Links

- GitHub: <https://github.com/manifold-inc/targon>
- Website: <https://targon.com>
- TAO.app: <https://tao.app/subnet/4>

