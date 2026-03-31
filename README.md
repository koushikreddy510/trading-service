# Trading Service

Multi-broker, multi-account trading: orders, positions, PnL, Slack notifications. Decoupled from the strategy-screener app.

## Setup

1. **Clone and install**
   ```bash
   cd trading-service
   pip install -r requirements.txt
   ```

2. **Database**  
   Create trading tables (Postgres, same or separate DB as screener):
   ```bash
   PGPASSWORD=xxx psql -h localhost -U user -d market -f migrations/trading_schema.sql
   ```

3. **Env**
   ```bash
   cp .env.example .env
   # Set DATABASE_URL, optional SLACK_WEBHOOK_URL and broker credentials
   ```

4. **Run API**
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8001
   ```
   Endpoints: `GET /trading/accounts`, `/trading/orders`, `/trading/positions`, `/trading/pnl`, `/trading/execution-strategies`, `/trading/accounts/{id}/execution-strategies` (see [docs/TRADING_STRATEGIES_AND_PIPELINE.md](docs/TRADING_STRATEGIES_AND_PIPELINE.md)).

   Apply migrations: `trading_schema.sql`, `trading_execution_strategies.sql`, and `trading_universe_sources_extend.sql`, or start the app once — `ensure_tables()` runs all three.

## Admin / Config

Similar to strategy-screener's Admin page. Use from the screener Admin (when `REACT_APP_TRADING_API_URL` is set) or call directly:

- `GET /admin/data-status` — Overview: accounts, positions count, last sync, PnL snapshots, env config
- `POST /admin/sync-positions` — Fetch positions from broker APIs (Flattrade etc.) and update DB
- `POST /admin/sync-pnl` — Create today's PnL snapshot from positions
- `GET /admin/jobs`, `GET /admin/job/{id}` — Job status (sync_positions, sync_pnl)

Broker credentials: `TRADING_FLATTRADE_USER`, `TRADING_FLATTRADE_TOKEN` (or per-account `TRADING_FLATTRADE_<credentials_ref>_USER`).

## Strategy Screener integration

In the screener frontend, set `REACT_APP_TRADING_API_URL=http://localhost:8001` (or your trading-service URL). The Trading page will call this API. In production, point it to the deployed trading-service (e.g. `https://koushik510.store/trading-api` with Nginx proxy).

## Brokers

- **Flattrade**: NSE/MCX; requires [flattrade/pythonAPI](https://github.com/flattrade/pythonAPI). Env: `TRADING_FLATTRADE_USER`, `TRADING_FLATTRADE_TOKEN`.
- **Paytm Money** / **Kotak**: Stubs; implement when APIs available.

See `DESIGN.md` for architecture and tables.
