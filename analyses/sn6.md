# Subnet 6 — Numinous (Forecasting)

> Analyzed: 2026-05-04 · easy-entry score at time of analysis: 59.1/100

## TL;DR

Numinous is a **forecasting subnet** — miners submit probability
predictions on real-world binary events (politics, sports, markets,
geopolitics) and are scored on **Brier Score**, the gold-standard
metric for calibrated forecasting. It is one of the most intellectually
interesting subnets on Bittensor: pure prediction skill wins, no GPU
required, and the gateway gives miners access to GPT-5 and other
external tools. The downside: scores converge slowly (you need 100+
resolved events to climb), the reward shape is sharp (~Brier
winner-take-most), and 0.20 τ registration is mid-tier.

## What it actually does

Numinous wants to be a "world forecasting model" — an aggregate of
competing AI agents that produces calibrated probabilities better than
any single model.

For each open question:

- Validator hands the miner a question + resolution criteria
- Miner returns a probability `p ∈ [0, 1]`
- When the event resolves, miner is scored:
  `brier = (p − outcome)²` (lower is better)
- Final reward is a smoothed function of recent Brier scores

Categories (recent): prediction-market events (Polymarket, Kalshi),
sports outcomes, geopolitical events, macroeconomic prints.

## Reward structure

The mechanism is described as **winner-takes-most on Brier score**.
Top-1 share is moderate (~24% in recent scans), meaning the best
forecaster eats a big slice but not all of it. Reward is sticky to
*demonstrated calibration over hundreds of events*, not single-event
luck — newcomers take time to climb.

| Knob | Effect |
|---|---|
| Brier score (rolling window) | dominant reward signal |
| Number of events answered | minor, but you must answer enough |
| Stability across categories | helps tail / smoothing |

## What miners run

- **Hardware:** No GPU. Any modest VPS works. The compute happens
  *inside the validator's Docker sandbox* with a 240-second timeout
  and 2 MB code limit per request.
- **Stack:** Python script that the validator runs in a sandbox. You
  write the *forecasting logic*: data fetch → feature engineering →
  probability output.
- **External / gateway tools:** Numinous's gateway exposes GPT-5 and
  other data sources to your sandboxed code — you don't pay API costs
  yourself.
- **Time to first reward:** days. You need 50+ resolved events before
  the score converges.

## Realistic earnings

Per Macrocosmos's own write-up, top contributors share ~$100k/month in
TAO emissions. At today's ~36 τ/day to the subnet:

| Outcome | TAO/day | USD/day (TAO ≈ $250) |
|---|---|---|
| Naive 0.5 baseline | ~0 (gets pruned) | $0 |
| Mid-pack (better than chance) | ~0.05–0.15 τ | $12–$40 |
| Top-30 (real forecasting skill) | ~0.3–0.7 τ | $75–$175 |
| Top-10 (specialist/researcher) | ~0.7–1.5 τ | $175–$375 |
| Top-1 | ~1.5–3 τ | $375–$750 |

A documented top miner (UID 128) hit Brier 0.1772 with 71.8%
directional accuracy across 600+ events — that's the bar for top-tier.

## Honest assessment

**Pros:**

- No GPU, low ongoing cost
- Gateway gives you GPT-5 + data sources for free
- Pure intellectual skill subnet — if you're good at probabilistic
  reasoning you can compete with PhDs
- Real-world signal (questions resolve against actual outcomes), not
  toy benchmarks

**Cons:**

- Slow to climb — you can wait days/weeks before your score reflects
  your true skill
- All slots full → eviction war for entry
- Code-size limit (2 MB) and timeout (240 s) constrain how heavy your
  inference stack can be
- 0.20 τ registration is mid-tier (not cheap)
- Hardest mental load on this list — you're competing against actual
  forecasting researchers

## Getting started

1. Read the project page first to understand the mechanism:
   <https://pm.wiki/bn/projects/numinous>
2. Wallet: hot+cold pair, fund with ~0.25 τ buffer
3. Build a simple forecaster locally — start with a baseline that
   takes Polymarket's current price + GPT-5 reasoning and adjusts
4. Test it on resolved historical questions before going live
5. `btcli subnet register --netuid 6 --wallet.name <cold> --wallet.hotkey <hot>`
6. Be patient — give it 1–2 weeks of resolutions before judging your
   placement

## What I don't know

- Exact code-size and timeout limits today (changed historically)
- Whether validators rotate question batches across miners or send
  every question to every miner
- The exact smoothing / decay window on Brier
- Whether the Crunch platform (mentioned in Macrocosmos write-ups) is
  the official onboarding path now or just a partner

## Links

- Project page: <https://pm.wiki/bn/projects/numinous>
- Write-up of top miner performance: <https://taodaily.io/how-a-miner-on-numinous-bittensor-subnet-6-outperforms-gemini/>
- Macrocosmos: <https://www.macrocosmos.ai>
- TAO.app: <https://tao.app/subnet/6>
