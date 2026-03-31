# Trading Pipeline – Single EC2 Deployment

Deploy data-store, strategy-screener, trading-service, and a central scheduler on a single EC2 (or any cloud VM) using Docker Compose.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Single EC2 / VM                                                        │
│  ┌─────────────┐ ┌─────────────────┐ ┌─────────────────┐ ┌──────────┐  │
│  │  Scheduler  │ │ strategy-       │ │ trading-service  │ │ data-     │  │
│  │  :8002      │ │ screener :8000  │ │ :8001            │ │ store    │  │
│  └──────┬──────┘ └─────────────────┘ └─────────────────┘ └────┬─────┘  │
│         │  HTTP triggers on cron schedule                     │         │
│         └──────────────────────┬──────────────────────────────┘         │
│                                │                                        │
│  ┌─────────────────────────────▼─────────────────────────────────────┐  │
│  │  Postgres (market + trading DBs)                                   │  │
│  └─────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

The **scheduler** replaces all cron jobs. It reads `schedules.yaml` and triggers:
- HTTP calls to strategy-screener (OHLCV, enrich, scrape, daily AI report)
- HTTP calls to trading-service (resolve-all universes, entry-candidate scan, sync positions, sync PnL)
- HTTP calls to data-store runner (commodity OHLCV, financials scrape)

For `universe_resolve_all`, set `STRATEGY_SCREENER_URL` (and `MARKET_DATABASE_URL` if you use `full_universe`) on the trading-service container so resolver calls succeed.

## Prerequisites

1. **Layout** – clone all three repos as siblings:
   ```
   ~/trading-pipeline/   (or /opt/trading-pipeline)
   ├── data-store/
   ├── strategy-screener/
   └── trading-service/
   ```

2. **Docker** – Docker Engine and Docker Compose v2+
3. **Fyers token** – `data-store/fyers_access_token.txt` with valid token (for OHLCV jobs)

## Quick Start

1. Copy env template and configure:
   ```bash
   cp deploy/env.example deploy/.env
   # Edit .env: PGPASSWORD, FYERS_*, SLACK_WEBHOOK_URL, etc.
   ```

2. Build and start:
   ```bash
   cd trading-service
   docker compose -f deploy/docker-compose.ec2.yml build
   docker compose -f deploy/docker-compose.ec2.yml up -d
   ```

3. Verify:
   - Scheduler: http://localhost:8002/health
   - Scheduler jobs: http://localhost:8002/jobs
   - Strategy-screener: http://localhost:8000
   - Trading-service: http://localhost:8001/health

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| PGPASSWORD | yes | Postgres password |
| PGUSER | no | Postgres user (default: market_user) |
| FYERS_CLIENT_ID | for OHLCV | Fyers app client ID |
| FYERS_ACCESS_TOKEN | for OHLCV | Fyers access token (or use token file) |
| FYERS_SECRET_KEY | for screener | For token generation |
| OPENAI_API_KEY | for AI report | OpenAI API key |
| SLACK_WEBHOOK_URL | no | Slack notifications |
| TRADING_SLACK_WEBHOOK_URL | no | Trading-specific Slack |
| TRADING_FLATTRADE_USER | for trading | Flattrade credentials |
| TRADING_FLATTRADE_TOKEN | for trading | Flattrade token |

## Scheduler

- Config: `scheduler/schedules.yaml`
- All times in **Asia/Kolkata (IST)**
- Manual run: `POST /jobs/{job_id}/run`
- Job log: `GET /jobs/{job_id}/log`

## ExCloud / Cloud Notes

- Use a t3.medium or larger for production
- Attach an EBS volume for Postgres data (or use RDS for Postgres)
- Open ports 80/443 (for Nginx reverse proxy) and optionally 8000–8002 for direct access
- Use `systemd` or cloud init to start compose on boot:

  ```ini
  [Unit]
  Description=Trading Pipeline
  Requires=docker.service
  After=docker.service

  [Service]
  Type=oneshot
  RemainAfterExit=yes
  WorkingDirectory=/opt/trading-pipeline/trading-service
  ExecStart=/usr/bin/docker compose -f deploy/docker-compose.ec2.yml up -d
  ExecStop=/usr/bin/docker compose -f deploy/docker-compose.ec2.yml down
  ```
