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
   Endpoints: `GET /trading/accounts`, `/trading/orders`, `/trading/positions`, `/trading/pnl`.

## Strategy Screener integration

In the screener frontend, set `REACT_APP_TRADING_API_URL=http://localhost:8001` (or your trading-service URL). The Trading page will call this API. In production, point it to the deployed trading-service (e.g. `https://koushik510.store/trading-api` with Nginx proxy).

## Brokers

- **Flattrade**: NSE/MCX; requires [flattrade/pythonAPI](https://github.com/flattrade/pythonAPI). Env: `TRADING_FLATTRADE_USER`, `TRADING_FLATTRADE_TOKEN`.
- **Paytm Money** / **Kotak**: Stubs; implement when APIs available.

See `DESIGN.md` for architecture and tables.
