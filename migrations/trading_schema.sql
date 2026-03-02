-- Trading service: multi-broker, multi-account orders, positions, PnL, trailing stops

-- Broker accounts (credentials stored as env key reference or encrypted blob)
CREATE TABLE IF NOT EXISTS trading_accounts (
    id SERIAL PRIMARY KEY,
    broker VARCHAR(32) NOT NULL,
    account_id VARCHAR(128) NOT NULL,
    display_name VARCHAR(256) DEFAULT '',
    credentials_ref VARCHAR(256) DEFAULT '',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    UNIQUE(broker, account_id)
);

-- Orders placed by the service
CREATE TABLE IF NOT EXISTS trading_orders (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES trading_accounts(id),
    strategy_id INTEGER,
    strategy_name VARCHAR(128),
    symbol VARCHAR(64) NOT NULL,
    exchange VARCHAR(32) DEFAULT 'NSE',
    side VARCHAR(8) NOT NULL,
    quantity INTEGER NOT NULL,
    order_type VARCHAR(16) DEFAULT 'LIMIT',
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    broker_order_id VARCHAR(128),
    filled_qty INTEGER DEFAULT 0,
    filled_price NUMERIC(18, 4),
    trigger_price NUMERIC(18, 4),
    placed_at TIMESTAMP,
    filled_at TIMESTAMP,
    cancelled_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    timeframe VARCHAR(8),
    remarks TEXT
);

CREATE INDEX IF NOT EXISTS idx_trading_orders_account ON trading_orders(account_id);
CREATE INDEX IF NOT EXISTS idx_trading_orders_status ON trading_orders(status);
CREATE INDEX IF NOT EXISTS idx_trading_orders_placed_at ON trading_orders(placed_at DESC);

-- Positions (synced from broker or derived from fills)
CREATE TABLE IF NOT EXISTS trading_positions (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES trading_accounts(id),
    symbol VARCHAR(64) NOT NULL,
    exchange VARCHAR(32) DEFAULT 'NSE',
    side VARCHAR(8) NOT NULL,
    quantity INTEGER NOT NULL,
    avg_price NUMERIC(18, 4) NOT NULL,
    current_price NUMERIC(18, 4),
    unrealized_pnl NUMERIC(18, 2),
    last_synced_at TIMESTAMP DEFAULT now(),
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    UNIQUE(account_id, symbol, exchange, side)
);

CREATE INDEX IF NOT EXISTS idx_trading_positions_account ON trading_positions(account_id);

-- Daily or per-trade PnL snapshots
CREATE TABLE IF NOT EXISTS trading_pnl_snapshots (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES trading_accounts(id),
    snapshot_date DATE NOT NULL,
    realized_pnl NUMERIC(18, 2) DEFAULT 0,
    unrealized_pnl NUMERIC(18, 2) DEFAULT 0,
    total_pnl NUMERIC(18, 2) DEFAULT 0,
    snapshot_at TIMESTAMP DEFAULT now(),
    UNIQUE(account_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_trading_pnl_account_date ON trading_pnl_snapshots(account_id, snapshot_date DESC);

-- Trailing stop rules linked to order or position
CREATE TABLE IF NOT EXISTS trading_trailing_stops (
    id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES trading_orders(id),
    position_id INTEGER REFERENCES trading_positions(id),
    trail_type VARCHAR(16) NOT NULL,
    trail_value NUMERIC(18, 4),
    last_trigger_price NUMERIC(18, 4),
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    CHECK (order_id IS NOT NULL OR position_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_trading_trailing_active ON trading_trailing_stops(is_active) WHERE is_active = true;
