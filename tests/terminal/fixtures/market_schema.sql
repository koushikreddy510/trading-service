-- Tables owned by the data-store project, as the terminal expects to find them.
-- Copied from data-store/{schema,ohlc,financials_schema}.sql so tests exercise the
-- real column names and constraints. `symbols` also carries the columns added by
-- enrich_market_cap.py; `result_announcements` is reconstructed from the INSERT in
-- scrape_latest_results.py (its DDL is not in the repository).

CREATE TABLE symbols (
  tradingsymbol TEXT PRIMARY KEY,
  fyers_token TEXT,
  name TEXT,
  isin TEXT,
  updated_at TIMESTAMP DEFAULT now(),
  market_cap DOUBLE PRECISION,
  listing_date DATE,
  sector TEXT
);

CREATE TABLE IF NOT EXISTS ohlcv_1d (
  tradingsymbol TEXT NOT NULL,
  date DATE NOT NULL,
  open DOUBLE PRECISION NOT NULL,
  high DOUBLE PRECISION NOT NULL,
  low DOUBLE PRECISION NOT NULL,
  close DOUBLE PRECISION NOT NULL,
  volume BIGINT NOT NULL,
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now(),
  PRIMARY KEY (tradingsymbol, date)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_1d_date ON ohlcv_1d(date);
CREATE INDEX IF NOT EXISTS idx_ohlcv_1d_symbol ON ohlcv_1d(tradingsymbol);

CREATE TABLE IF NOT EXISTS stock_financials (
    id SERIAL PRIMARY KEY,
    tradingsymbol TEXT NOT NULL,
    nse_symbol TEXT NOT NULL,
    company_name TEXT DEFAULT '',
    result_type TEXT NOT NULL CHECK (result_type IN ('quarterly', 'annual')),
    period TEXT NOT NULL,
    period_end_date DATE,
    revenue DOUBLE PRECISION,
    expenses DOUBLE PRECISION,
    operating_profit DOUBLE PRECISION,
    opm_pct DOUBLE PRECISION,
    other_income DOUBLE PRECISION,
    interest DOUBLE PRECISION,
    depreciation DOUBLE PRECISION,
    profit_before_tax DOUBLE PRECISION,
    tax DOUBLE PRECISION,
    net_profit DOUBLE PRECISION,
    npm_pct DOUBLE PRECISION,
    eps DOUBLE PRECISION,
    pe_ratio DOUBLE PRECISION,
    pb_ratio DOUBLE PRECISION,
    market_cap_cr DOUBLE PRECISION,
    dividend_yield DOUBLE PRECISION,
    roce_pct DOUBLE PRECISION,
    roe_pct DOUBLE PRECISION,
    revenue_growth_pct DOUBLE PRECISION,
    profit_growth_pct DOUBLE PRECISION,
    source TEXT DEFAULT 'screener.in',
    scraped_at TIMESTAMP DEFAULT now(),
    UNIQUE(tradingsymbol, result_type, period)
);
CREATE INDEX IF NOT EXISTS idx_financials_symbol ON stock_financials(tradingsymbol);
CREATE INDEX IF NOT EXISTS idx_financials_nse ON stock_financials(nse_symbol);
CREATE INDEX IF NOT EXISTS idx_financials_period ON stock_financials(period_end_date);
CREATE INDEX IF NOT EXISTS idx_financials_type ON stock_financials(result_type);

CREATE TABLE IF NOT EXISTS result_announcements (
    id SERIAL PRIMARY KEY,
    nse_symbol TEXT NOT NULL,
    company_name TEXT,
    result_date DATE,
    quarter TEXT,
    source TEXT,
    scraped_at TIMESTAMP DEFAULT now(),
    UNIQUE (nse_symbol, result_date, quarter)
);
