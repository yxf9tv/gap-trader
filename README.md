# Gap Trader

Detects price gaps between sharp sportsbook consensus and prediction market prices (Polymarket), surfaces executable edge with Kelly sizing and velocity-gated stability checks.

[![Production Deployment Pipeline](https://github.com/yxf9tv/gap-trader/actions/workflows/deploy.yml/badge.svg)](https://github.com/yxf9tv/gap-trader/actions/workflows/deploy.yml)

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

## Deployment

### Docker (production)

```bash
docker build -t gap-trader .
docker run -d --name gap-trader \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  --restart unless-stopped \
  gap-trader
```

### Docker Compose (recommended for VPS)

```yaml
# docker-compose.yml
version: "3.9"
services:
  gap-trader:
    build: .
    container_name: gap-trader
    env_file: .env
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

```bash
docker compose -f ~/apps/polymarket-suite/docker-compose.yml up -d --build gap-trader
```

### GitHub Actions (auto-deploy on push to main)

1. Add these secrets in your repo (`Settings > Secrets and variables > Actions`):

   | Secret | Value |
   |---|---|
   | `VPS_HOST` | Your VPS IP address |
   | `VPS_USER` | SSH username |
   | `SSH_PRIVATE_KEY` | SSH private key (PEM) |
   | `ENV_FILE_CONTENT` | Full `.env` file as a single string |

2. Push to `main` — the workflow SSHes in, pulls the repo, writes `.env`, and `docker compose up -d --build`s.

### Environment Variables

| Variable | Default | Required | Description |
|---|---|---|---|
| `PARLAY_API_KEY` | — | yes | ParlayAPI key |
| `POLY_PK` | — | yes | Polymarket private key |
| `POLY_FUNDER` | — | yes | Polymarket proxy/funder address |
| `SPORT` | `baseball_mlb` | no | Sport to poll |
| `POLL_INTERVAL` | `300` | no | Seconds between polls |
| `MIN_EDGE` | `1.0` | no | Min gap edge in cents |
| `GATE_FLOOR` | `0.51` | no | Sharp consensus gate |
| `SLIPPAGE_BPS` | `5.0` | no | Slippage buffer |
| `MIN_NET_EDGE` | `0.5` | no | Min edge after fees |
| `VELOCITY_WINDOW` | `10` | no | Rolling fair-value window |
| `VELOCITY_STD_MULT` | `2.0` | no | Std dev threshold |
| `EXECUTION_ENABLED` | `1` | no | Allow order placement |

## Safety

- **Shadow mode:** Run without `--place` — gaps are detected and logged, no orders sent
- **GATE B:** Execution requires `EXECUTION_ENABLED=1` env var to actually send orders
- **Velocity guard:** Skips enrichment when fair value jumps >2σ from rolling mean
- All execution is manual-confirm until GATE B is explicitly passed
