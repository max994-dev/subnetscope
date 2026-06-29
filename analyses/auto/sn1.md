# Subnet 1 — Apex

> Auto-analyzed: 2026-06-29 21:55 EEST · easy-entry score: 26.1/100 · refreshes hourly

*This analysis is auto-generated from live chain data. It updates every hour.
For hand-curated notes, check the Bittensor Discord or the subnet's own docs.*

## Quick stats

| Metric | Value |
|---|---|
| Category | llm |
| GPU need | heavy |
| Reward shape | peak |
| Active miners | 4 |
| UID slots | 256/256 (FULL) |
| Burn fee | 0.000500 τ `[░░░░░░░░░░░░░░░░░░░░]` 0.0% of max |
| Burn min / max | 0.000500 τ → 100.0000 τ |
| Emission / day | 65.6349 τ |
| Top-1 share | 41.8% |
| Liquidity (TAO in) | 26,278.3 τ |
| Price (τ/α) | 0.008731 |
| Age | 974 days |


## Easy-entry score: 26 / 100  🔴 low

- heavy GPU need
- subnet is FULL (eviction war)
- cheap burn fee 0.0005 τ

| Component | Points |
|---|---|
| GPU friction | 1.1 / 22 |
| Decentralization (top-1) | 11.6 / 20 |
| Active miners | 0.3 / 16 |
| Free slots | 0.0 / 13 |
| Burn fee | 11.0 / 11 |
| Liquidity | 1.2 / 10 |
| Emission | 0.8 / 8 |

## What is known about this category

This subnet produces or evaluates natural-language content. A local LLM (Llama 3.1 8B) is usually sufficient; GPU is helpful for fast inference but not always required.

## GPU / hardware

Heavy GPU — A100 or H100 class hardware gives the most competitive edge.

## Reward shape

**Peaked:** emission concentrates in a handful of miners (top ~5 take >80%). Breaking into that small leading group is essential.

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
5. `btcli subnet register --netuid 1 --wallet.name <cold> --wallet.hotkey <hot>`
6. Monitor your score during the immunity window.

## Links

- GitHub: <https://github.com/macrocosm-os/apex>
- Website: <https://apex.macrocosmos.ai>
- Discord: <macrocrux>
- TAO.app: <https://tao.app/subnet/1>

