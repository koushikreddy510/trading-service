-- Terminal schema (trading-service). Idempotent: safe to run on every start.
--
-- Owns ONLY terminal state. It never creates or alters tables that belong to the
-- data-store project (symbols, ohlcv_1d, stock_financials, result_announcements);
-- the terminal reads those and degrades gracefully when they are missing.
--
-- Symbol ids everywhere are "EXCHANGE:TRADINGSYMBOL" exactly as data-store writes
-- them (e.g. NSE:SBIN-EQ), so joins need no mapping layer.

-- ---------------------------------------------------------------- watchlists
CREATE TABLE IF NOT EXISTS terminal_watchlists (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT 'local',
    name        TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS terminal_watchlist_items (
    watchlist_id TEXT NOT NULL REFERENCES terminal_watchlists(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    position     INTEGER NOT NULL DEFAULT 0,
    added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (watchlist_id, symbol)
);

-- ------------------------------------------------------------------ drawings
-- Drawings belong to a symbol, not a timeframe: points are (unix time, price),
-- so the same line shows on every timeframe like TradingView.
CREATE TABLE IF NOT EXISTS terminal_drawings (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT 'local',
    symbol      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    points      JSONB NOT NULL,
    style       JSONB NOT NULL DEFAULT '{}'::jsonb,
    label       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_terminal_drawings_symbol ON terminal_drawings (user_id, symbol);

-- --------------------------------------------------------------------- rules
-- An entry rule: "when THIS condition becomes true on a COMPLETED candle of
-- <timeframe>, open a <side> position with this stop and target".
CREATE TABLE IF NOT EXISTS terminal_rules (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT 'local',
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    side        TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    condition   JSONB NOT NULL,
    stop        JSONB NOT NULL,
    target      JSONB,
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    enabled     BOOLEAN NOT NULL DEFAULT true,
    once        BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_terminal_rules_symbol ON terminal_rules (symbol, timeframe, enabled);
-- Exit side of a rule (added later, so ALTER): a trailing stop that tightens on completed candles of its own
-- timeframe, and an exit condition checked on completed candles of `exit_timeframe`. Both are copied onto the
-- position when it opens, so editing a rule never changes a position that is already running.
ALTER TABLE terminal_rules ADD COLUMN IF NOT EXISTS trail            JSONB;
ALTER TABLE terminal_rules ADD COLUMN IF NOT EXISTS exit_condition   JSONB;
ALTER TABLE terminal_rules ADD COLUMN IF NOT EXISTS exit_timeframe   TEXT;
ALTER TABLE terminal_rules ADD COLUMN IF NOT EXISTS exit_condition_2 JSONB;
ALTER TABLE terminal_rules ADD COLUMN IF NOT EXISTS exit_timeframe_2 TEXT;
-- Entry scoring: optional indicator + up to 3 quantity levels based on distance from indicator at signal time.
-- entry_levels is [{qty, max_dist_pct?}, ...] sorted tightest-first; the last item is the catch-all.
ALTER TABLE terminal_rules ADD COLUMN IF NOT EXISTS entry_score_indicator JSONB;
ALTER TABLE terminal_rules ADD COLUMN IF NOT EXISTS entry_levels          JSONB;

-- A signal is the record of a rule firing on one closed bar. UNIQUE (rule_id,
-- bar_time) is what guarantees a rule can never fire twice for the same candle,
-- even after a restart or a replayed candle-close event.
CREATE TABLE IF NOT EXISTS terminal_signals (
    id          TEXT PRIMARY KEY,
    rule_id     TEXT REFERENCES terminal_rules(id) ON DELETE SET NULL,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    bar_time    TIMESTAMPTZ NOT NULL,
    entry       DOUBLE PRECISION NOT NULL,
    stop        DOUBLE PRECISION,
    target      DOUBLE PRECISION,
    status      TEXT NOT NULL CHECK (status IN ('open', 'rejected')),
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (rule_id, bar_time)
);
CREATE INDEX IF NOT EXISTS idx_terminal_signals_symbol ON terminal_signals (symbol, created_at DESC);

-- ----------------------------------------------------------- paper positions
CREATE TABLE IF NOT EXISTS terminal_paper_positions (
    id           TEXT PRIMARY KEY,
    signal_id    TEXT REFERENCES terminal_signals(id) ON DELETE SET NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    entry_price  DOUBLE PRECISION NOT NULL,
    stop         DOUBLE PRECISION,
    target       DOUBLE PRECISION,
    status       TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    opened_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at    TIMESTAMPTZ,
    exit_price   DOUBLE PRECISION,
    exit_reason  TEXT,
    realized_pnl DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_terminal_paper_status ON terminal_paper_positions (status, symbol);
ALTER TABLE terminal_paper_positions ADD COLUMN IF NOT EXISTS initial_stop     DOUBLE PRECISION;
ALTER TABLE terminal_paper_positions ADD COLUMN IF NOT EXISTS trail            JSONB;
ALTER TABLE terminal_paper_positions ADD COLUMN IF NOT EXISTS exit_condition   JSONB;
ALTER TABLE terminal_paper_positions ADD COLUMN IF NOT EXISTS exit_timeframe   TEXT;
-- Two-level exits: exit_condition fires first (50%), exit_condition_2 fires second (remaining 50%).
-- exit_level tracks which is pending (1=first, 2=second). exit_qty_1/price_1/pnl_1 record the partial close.
ALTER TABLE terminal_paper_positions ADD COLUMN IF NOT EXISTS exit_condition_2 JSONB;
ALTER TABLE terminal_paper_positions ADD COLUMN IF NOT EXISTS exit_timeframe_2 TEXT;
ALTER TABLE terminal_paper_positions ADD COLUMN IF NOT EXISTS exit_level       SMALLINT NOT NULL DEFAULT 1;
ALTER TABLE terminal_paper_positions ADD COLUMN IF NOT EXISTS exit_qty_1       INTEGER;
ALTER TABLE terminal_paper_positions ADD COLUMN IF NOT EXISTS exit_price_1     DOUBLE PRECISION;
ALTER TABLE terminal_paper_positions ADD COLUMN IF NOT EXISTS exit_pnl_1       DOUBLE PRECISION;

-- ---------------------------------------------------------------- market data
-- Only CLOSED 1-minute bars are stored. 1-second data is never persisted: ticks
-- are streamed to the browser and folded into the forming bar in memory.
-- Every other intraday timeframe is resampled from these rows on read.
CREATE TABLE IF NOT EXISTS terminal_ohlcv_1m (
    symbol  TEXT NOT NULL,
    ts      TIMESTAMPTZ NOT NULL,
    open    DOUBLE PRECISION NOT NULL,
    high    DOUBLE PRECISION NOT NULL,
    low     DOUBLE PRECISION NOT NULL,
    close   DOUBLE PRECISION NOT NULL,
    volume  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, ts)
);

-- Cache of broker instrument tokens (Flattrade SearchScrip results).
CREATE TABLE IF NOT EXISTS terminal_broker_tokens (
    symbol       TEXT PRIMARY KEY,
    exchange     TEXT NOT NULL,
    token        TEXT NOT NULL,
    tsym         TEXT NOT NULL,
    resolved_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ screener
-- One row per symbol per daily scan, computed from ohlcv_1d. The scan runs once
-- per day (after the data-store OHLCV job); screens are then plain SQL filters
-- over the latest run, so they are instant and need no recomputation.
CREATE TABLE IF NOT EXISTS terminal_screener_metrics (
    run_date           DATE NOT NULL,
    symbol             TEXT NOT NULL,
    asof               DATE NOT NULL,
    close              DOUBLE PRECISION,
    change_pct         DOUBLE PRECISION,
    volume             BIGINT,
    avg_volume_20      DOUBLE PRECISION,
    volume_ratio       DOUBLE PRECISION,
    ema_20             DOUBLE PRECISION,
    ema_50             DOUBLE PRECISION,
    sma_50             DOUBLE PRECISION,
    sma_200            DOUBLE PRECISION,
    rsi_14             DOUBLE PRECISION,
    atr_14             DOUBLE PRECISION,
    atr_pct            DOUBLE PRECISION,
    high_52w           DOUBLE PRECISION,
    low_52w            DOUBLE PRECISION,
    pct_from_52w_high  DOUBLE PRECISION,
    pct_from_52w_low   DOUBLE PRECISION,
    ret_20d            DOUBLE PRECISION,
    ret_60d            DOUBLE PRECISION,
    ret_120d           DOUBLE PRECISION,
    supertrend_dir     INTEGER,
    supertrend_value   DOUBLE PRECISION,
    bars               INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_terminal_screener_symbol ON terminal_screener_metrics (symbol);
-- columns added after the first release (financial-data screens, golden cross): safe to run on an existing table
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS volume_ratio_5d DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS golden_cross INTEGER;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS pe_ratio DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS pb_ratio DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS roce_pct DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS roe_pct DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS dividend_yield DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS earnings_yield_pct DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS peg_ratio DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS q_net_profit DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS q_opm_pct DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS q_opm_change DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS q_revenue_yoy_pct DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS q_profit_yoy_pct DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS q_revenue_qoq_pct DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS q_profit_qoq_pct DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS sales_cagr_3y DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS profit_cagr_3y DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS rev_growth_min_3y DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS interest_cover DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS interest_change_pct DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS pe_avg_eps DOUBLE PRECISION;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS q_growth_streak INTEGER;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS q_turnaround INTEGER;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS eps_years INTEGER;
ALTER TABLE terminal_screener_metrics ADD COLUMN IF NOT EXISTS magic_rank INTEGER;

-- ------------------------------------------------- screener -> watchlist sync
-- A strategy-screener strategy can feed ONE watchlist ("managed" list). The link says which
-- strategy and with what settings; members records which symbols the sync put there and why.
-- Only members with managed = true are ever removed by a later sync, so symbols the user
-- added by hand to the same list are never touched.
CREATE TABLE IF NOT EXISTS terminal_screener_links (
    watchlist_id   TEXT PRIMARY KEY REFERENCES terminal_watchlists(id) ON DELETE CASCADE,
    strategy_id    INTEGER NOT NULL,
    strategy_name  TEXT NOT NULL DEFAULT '',
    tolerance_pct  DOUBLE PRECISION NOT NULL,
    max_failing    INTEGER NOT NULL,
    max_symbols    INTEGER NOT NULL,
    filters        JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_run_at    TIMESTAMPTZ,
    last_asof      TEXT,
    matched_count  INTEGER NOT NULL DEFAULT 0,
    near_count     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS terminal_screener_members (
    watchlist_id TEXT NOT NULL REFERENCES terminal_watchlists(id) ON DELETE CASCADE,
    symbol       TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('matched', 'near')),
    gap_pct      DOUBLE PRECISION,
    reason       TEXT NOT NULL DEFAULT '',
    managed      BOOLEAN NOT NULL DEFAULT true,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (watchlist_id, symbol)
);

-- Drawing kinds grew (rectangle, text). Dropped and re-added so databases created with the old inline CHECK pick it up.
ALTER TABLE terminal_drawings DROP CONSTRAINT IF EXISTS terminal_drawings_kind_check;
ALTER TABLE terminal_drawings ADD CONSTRAINT terminal_drawings_kind_check
    CHECK (kind IN ('trend_line', 'ray', 'horizontal_line', 'rectangle', 'text'));

-- ------------------------------------------------------------------ backtests
-- One row per backtest run. The heavy work happens in a throw-away schema (see backtest.py); only the
-- request and the finished report are kept here. `account` is the seam for several broker accounts.
CREATE TABLE IF NOT EXISTS terminal_backtests (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT 'local',
    account     TEXT NOT NULL DEFAULT 'default',
    status      TEXT NOT NULL DEFAULT 'running',
    progress    TEXT,
    error       TEXT,
    params      JSONB NOT NULL,
    result      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_terminal_backtests_user ON terminal_backtests (user_id, created_at DESC);

-- The stock list as of the last data digest, so the next one can say which symbols were added or removed.
CREATE TABLE IF NOT EXISTS terminal_symbol_snapshot (
    symbol TEXT PRIMARY KEY
);

-- ------------------------------------------------------------------ instruments
-- Contracts the data-store's stock list does not have (MCX futures), from Flattrade's scrip master; see instruments.py.
-- price_factor is Flattrade's `prcftr` (filled in on first use); lot_size x price_factor = rupees per 1.00 move per lot.
CREATE TABLE IF NOT EXISTS terminal_instruments (
    symbol       TEXT PRIMARY KEY,
    exchange     TEXT NOT NULL,
    token        TEXT NOT NULL,
    tsym         TEXT NOT NULL,
    underlying   TEXT NOT NULL,
    instrument   TEXT NOT NULL,
    expiry       DATE,
    lot_size     INTEGER NOT NULL DEFAULT 1,
    price_factor DOUBLE PRECISION,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_terminal_instruments_underlying ON terminal_instruments (underlying, expiry);

-- Daily bars from the broker for symbols the data-store's ohlcv_1d does not cover (MCX contracts).
CREATE TABLE IF NOT EXISTS terminal_ohlcv_1d (
    symbol  TEXT NOT NULL,
    date    DATE NOT NULL,
    open    DOUBLE PRECISION NOT NULL,
    high    DOUBLE PRECISION NOT NULL,
    low     DOUBLE PRECISION NOT NULL,
    close   DOUBLE PRECISION NOT NULL,
    volume  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, date)
);

-- Rupees per 1.00 price move per unit of quantity: 1 for stocks, lot size x price factor for a futures lot.
ALTER TABLE terminal_paper_positions ADD COLUMN IF NOT EXISTS multiplier DOUBLE PRECISION NOT NULL DEFAULT 1;
