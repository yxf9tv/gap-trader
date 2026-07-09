# Gap Trader

Detects price gaps between sharp sportsbook consensus and prediction market prices (Polymarket), then surfaces executable edge with Kelly sizing and velocity-gated stability checks.

## How It Works

1. **Fair value** — Polls ParlayAPI for sharp book odds (Pinnacle, Novig, ProphetX), devigs to a consensus fair price in cents
2. **Gap detection** — Compares fair against the best PM ask; if PM is below fair (net of fees + slippage), a gap is logged
3. **Two-sided** — Checks both the Yes and No outcome for undervalue relative to complement fair
4. **Velocity guard** — Rolling std filter skips enrichment when sharp consensus is jumping (fair value unstable)
5. **Kelly sizing** — Depth-capped quarter-Kelly stake, conviction liquidity from opposite-side bids
6. **Alerts** — Every gap logged to `alerts.jsonl` with edge, Kelly %, stake, and CLV fields

## Requirements

- Python 3.11+
- `pip install pmxt` (Polymarket self-custody via PMXT)
- ParlayAPI key
- Polymarket private key + proxy address

## Setup

```bash
cp .env.template .env
# Fill in your secrets:
#   PARLAY_API_KEY=...
#   POLY_PK=0x...
#   POLY_FUNDER=0x...
```

## Usage

```bash
# Dry-run (shadow mode — no orders placed)
python run_alerts.py --sport baseball_mlb --execution --gap-trade

# With manual confirmation
python run_alerts.py --sport baseball_mlb --execution --gap-trade --place

# Velocity guard tuning
python run_alerts.py --sport baseball_mlb --execution --gap-trade \
  --velocity-window 15 --velocity-std-mult 2.5

# Disable velocity guard
python run_alerts.py --sport baseball_mlb --execution --gap-trade --no-velocity-guard

# Check the pipeline without network
python run_alerts.py --selftest
```

## Key Configuration

| Flag | Default | Description |
|------|---------|-------------|
| `--pm-venues` | `kalshi,polymarket` | Venues for regular edge enrichment |
| `--gap-venues` | `polymarket` | Venues for complement-side gap check |
| `--gap-trade` | off | Enable two-sided gap detection |
| `--slippage-bps` | 5.0 | Slippage buffer (basis points) |
| `--min-net-edge` | 0.5 | Min edge in cents after fees + slippage |
| `--velocity-guard` | on | Rolling std filter on fair value |
| `--velocity-window` | 10 | History size for velocity guard |
| `--velocity-std-mult` | 2.0 | Std dev threshold for instability |

## Project Structure

```
├── run_alerts.py          # Main entry point — poll loop
├── fair_value.py          # Devigging + anchor-trust gate
├── signals.py             # Alert dataclass, break detection
├── execution.py           # ExecutionEngine, VelocityGuard, gap logic
├── pmxt_execution.py      # PMXT wrapper (Polymarket)
├── matching.py            # Market matching (side → outcome ID)
├── discovery.py           # Kalshi market discovery (optional)
├── parlay_api_client.py   # ParlayAPI signal feed adapter
├── utils.py               # Shared helpers (cents, book class)
├── smart_money_engine.py  # Core devig/Kelly math
├── registry.json          # Outcome ID mappings
└── test/                  # pytest suite
```

## Safety

- **Shadow mode:** Run without `--place` — gaps are detected and logged, no orders sent
- **GATE B:** Execution requires `EXECUTION_ENABLED=1` env var to actually send orders
- **Velocity guard:** Skips enrichment when fair value jumps >2σ from rolling mean
- All execution is manual-confirm until GATE B is explicitly passed
