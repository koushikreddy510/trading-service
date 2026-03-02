# Trading Service Design

- **Architecture**: Python FastAPI app, broker adapters (Flattrade, Paytm Money, Kotak), single Postgres DB.
- **Multi-broker, multi-account**: Config-driven; one adapter per broker; multiple accounts via `trading_accounts` table.
- **Flows**: Strategy eval → signals → place order (adapter) → persist order → Slack on fill/cancel; position sync from broker.
- **Tables**: `trading_accounts`, `trading_orders`, `trading_positions`, `trading_pnl_snapshots`, `trading_trailing_stops` (see `migrations/trading_schema.sql`).
- **Dashboard**: Any frontend (e.g. strategy-screener Trading page) calls this service’s API; set `REACT_APP_TRADING_API_URL` to this service base URL.
