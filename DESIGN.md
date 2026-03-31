# Trading Service Design

- **Architecture**: Python FastAPI app, broker adapters (Flattrade, Paytm Money, Kotak), single Postgres DB.
- **Multi-broker, multi-account**: Config-driven; one adapter per broker; multiple accounts via `trading_accounts` table.
- **Flows**: Strategy eval → signals → place order (adapter) → persist order → Slack on fill/cancel; position sync from broker.
- **Tables**: `trading_accounts`, `trading_orders`, `trading_positions`, `trading_pnl_snapshots`, `trading_trailing_stops` (`migrations/trading_schema.sql`); `trading_execution_strategies`, `trading_strategy_account_map` (`migrations/trading_execution_strategies.sql`); extended `universe_source` values (`migrations/trading_universe_sources_extend.sql`).
- **Dashboard**: Any frontend (e.g. strategy-screener Trading page) calls this service’s API; set `REACT_APP_TRADING_API_URL` to this service base URL.
- **Execution strategies & pipeline**: See [docs/TRADING_STRATEGIES_AND_PIPELINE.md](docs/TRADING_STRATEGIES_AND_PIPELINE.md) for sample strategies, architecture diagram, proposed DB tables, and phased implementation.
