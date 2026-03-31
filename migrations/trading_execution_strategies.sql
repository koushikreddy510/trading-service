-- Execution strategies (risk/entry/exit) and per-account enablement.
-- Run after trading_schema.sql.

-- Where entry signals come from:
--   screener_filtered — only symbols that pass the linked strategy-screener strategy (/run/{screener_strategy_id})
--   full_universe     — entire listed universe (OHLC/symbols in market DB); use for custom scanners
--   pattern_52w       — driven by 52w pattern API params in config_json (strategy-screener /patterns/52w)

CREATE TABLE IF NOT EXISTS trading_execution_strategies (
    id SERIAL PRIMARY KEY,
    slug VARCHAR(64) NOT NULL UNIQUE,
    display_name VARCHAR(256) NOT NULL,
    profile VARCHAR(32) NOT NULL DEFAULT 'balanced',
    entry_style VARCHAR(32) NOT NULL DEFAULT 'confluence',
    universe_source VARCHAR(32) NOT NULL DEFAULT 'screener_filtered',
    screener_strategy_id INTEGER,
    screener_strategy_name VARCHAR(128) DEFAULT '',
    config_json JSONB NOT NULL DEFAULT '{}',
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    CONSTRAINT chk_universe_source CHECK (universe_source IN ('screener_filtered', 'full_universe', 'pattern_52w'))
);

CREATE INDEX IF NOT EXISTS idx_trading_exec_strategies_active ON trading_execution_strategies(is_active) WHERE is_active = true;

CREATE TABLE IF NOT EXISTS trading_strategy_account_map (
    id SERIAL PRIMARY KEY,
    execution_strategy_id INTEGER NOT NULL REFERENCES trading_execution_strategies(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES trading_accounts(id) ON DELETE CASCADE,
    is_enabled BOOLEAN NOT NULL DEFAULT false,
    overrides_json JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    UNIQUE(execution_strategy_id, account_id)
);

CREATE INDEX IF NOT EXISTS idx_trading_strategy_map_account ON trading_strategy_account_map(account_id);

-- Link open positions to an execution strategy (filled after automation attributes trades)
ALTER TABLE trading_positions ADD COLUMN IF NOT EXISTS execution_strategy_id INTEGER REFERENCES trading_execution_strategies(id);

CREATE INDEX IF NOT EXISTS idx_trading_positions_exec_strategy ON trading_positions(execution_strategy_id) WHERE execution_strategy_id IS NOT NULL;

-- Seed sample strategies (idempotent)
INSERT INTO trading_execution_strategies (slug, display_name, profile, entry_style, universe_source, screener_strategy_id, screener_strategy_name, config_json)
VALUES
(
    'momentum_breakout_aggressive',
    'Momentum breakout (aggressive)',
    'aggressive',
    'breakout',
    'screener_filtered',
    NULL,
    '',
    '{"description": "Enter on strength after screener pass; trail 2-3% from peak", "trail_pct": 2.5, "max_position_pct_equity": 15, "notes": "Link screener_strategy_id to a multi-TF / volume breakout strategy"}'::jsonb
),
(
    'momentum_pullback_conservative',
    'Momentum pullback (conservative)',
    'conservative',
    'pullback',
    'screener_filtered',
    NULL,
    '',
    '{"description": "Trend + dip entry; fixed stop", "stop_loss_pct": 4.5, "partial_take_pct": 8, "max_position_pct_equity": 10}'::jsonb
),
(
    'swing_confluence_core',
    'Swing confluence (core)',
    'balanced',
    'confluence',
    'screener_filtered',
    NULL,
    '',
    '{"description": "Multiple indicators agree", "breakeven_after_pct": 3, "trail_after_breakeven_pct": 2, "max_hold_sessions": 20, "max_position_pct_equity": 12}'::jsonb
),
(
    'near_52w_quality',
    'Near 52w + quality',
    'conservative',
    'breakout',
    'pattern_52w',
    NULL,
    '',
    '{"description": "5-10% below 52w high band; optional financials gate in app layer", "pct_from_high_min": 5, "pct_from_high_max": 10, "max_position_pct_equity": 10}'::jsonb
),
(
    'sector_rotation_momo',
    'Sector rotation momentum',
    'aggressive',
    'sector_rotation',
    'screener_filtered',
    NULL,
    '',
    '{"description": "Strong names in leading parent sectors", "trail_pct": 2, "max_position_pct_equity": 12, "notes": "Combine screener with sector_filter from Run UI or API"}'::jsonb
),
(
    'mean_reversion_small',
    'Mean reversion (small / tactical)',
    'speculative',
    'mean_reversion',
    'screener_filtered',
    NULL,
    '',
    '{"description": "Oversold bounce; tight risk", "stop_loss_pct": 2.5, "max_hold_sessions": 5, "max_position_pct_equity": 5}'::jsonb
),
(
    'commodity_trend',
    'Commodity trend (MCX)',
    'balanced',
    'breakout',
    'screener_filtered',
    NULL,
    '',
    '{"description": "MCX trend/breakout from commodity screener strategies", "market_type": "commodities", "max_position_pct_equity": 10}'::jsonb
)
ON CONFLICT (slug) DO NOTHING;
