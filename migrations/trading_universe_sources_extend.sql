-- Extend universe_source for strategy-screener *screen* outputs (Chart Patterns, candle patterns).
-- Idempotent: drop/recreate check constraint.

ALTER TABLE trading_execution_strategies DROP CONSTRAINT IF EXISTS chk_universe_source;

ALTER TABLE trading_execution_strategies ADD CONSTRAINT chk_universe_source CHECK (universe_source IN (
    'screener_filtered',
    'full_universe',
    'pattern_52w',
    'screen_structural',
    'screen_52w',
    'screen_candle',
    'union_screener_screen'
));

COMMENT ON COLUMN trading_execution_strategies.universe_source IS
'screener_filtered=/run/{id}; full_universe=MARKET_DATABASE_URL symbols; pattern_52w=legacy config band; '
'screen_*=live HTTP to strategy-screener patterns APIs; union_screener_screen=deduped union of screener + one screen (see config_json)';
