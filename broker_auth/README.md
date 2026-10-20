# broker_auth: the daily login-token step

Broker API tokens expire every day (Flattrade's at about 05:30 IST), and each login needs you in a browser (password + 2FA).
This folder does everything else: give it the app's API key/secret once, run one command each morning.

```bash
export FLATTRADE_API_KEY=...  FLATTRADE_API_SECRET=...      # or it asks
python -m broker_auth flattrade --env-file deploy/.env      # opens the login page, catches the redirect, saves the token
python -m broker_auth paytm     --env-file deploy/.env      # PAYTM_API_KEY / PAYTM_API_SECRET
deploy/deploy.sh trading                                    # the service reads the new token on start
```

- **All brokers at once:** `python -m broker_auth all --env-file deploy/.env` logs in to every broker whose `<BROKER>_API_KEY` / `_API_SECRET` are set, one after the other. `python -m broker_auth status` says which tokens are accepted right now.
- **Redirect URL:** register `http://127.0.0.1:5000/` (or pass `--port`) as the app's redirect URL when you create the key.
- **On a server without a browser:** log in on your laptop, then `python -m broker_auth flattrade --code '<the whole URL the browser ended on>'`.
- **Output:** `~/.broker_tokens/<broker>.json` (mode 600) and, with `--env-file`, `FLATTRADE_TOKEN`, `FLATTRADE_USER_ID`, or `PAYTM_ACCESS_TOKEN`, `PAYTM_PUBLIC_ACCESS_TOKEN`, `PAYTM_READ_ACCESS_TOKEN`.
- **Add a broker (Fyers next):** one `login_url` + one `exchange` function + one line in `PROVIDERS` in `providers.py`. Fyers' existing flow is in `data-store/generate_token.py`.

## The daily schedule (what a cron can and cannot do)

Flattrade and Paytm Money make a person log in (password + 2FA) every day, so no job can fetch their tokens by itself.
Fyers can log in unattended (PIN + TOTP key, already built into the strategy-screener). The scheduler therefore runs, Mon-Fri:

| IST | Job | What it does |
|---|---|---|
| 08:00 | `fyers_token_refresh` | strategy-screener logs in to Fyers and rewrites the token file (needs `FYERS_USERNAME`, `FYERS_PIN`, `FYERS_TOTP_KEY` in `deploy/.env`) |
| 08:15 | `broker_tokens_check` | asks each broker whether today's token is accepted; **posts to Slack only if one is not**, with the exact command to run |

So the morning routine for Flattrade / Paytm is: get the Slack message, run the one command, restart the service. Both jobs are in `scheduler/schedules.yaml`.

## What the brokers require (checked October 2026)

| | Flattrade (API v2) | Paytm Money |
|---|---|---|
| Static IP | **Mandatory** primary IP (secondary optional), entered when the key is created; orders only from it; changeable once per calendar week; unique per client (no family sharing yet) | **Not found** in any public page I could reach; check the key-creation form and Paytm Money support |
| Key creation | wall.flattrade.in → Pi → api(v2) → Create; needs redirect URL and IP; shows Pending until approved | developer.paytmmoney.com → Create New App (up to 5 apps); name, product type, redirect URL |
| Login | `auth.flattrade.in/?app_key=KEY` → redirect `?code=` | `login.paytmmoney.com/merchant-login?apiKey=KEY&state=` → redirect `?requestToken=` |
| Token | POST `authapi.flattrade.in/trade/apitoken` with `sha256(key+code+secret)` | POST `developer.paytmmoney.com/accounts/v2/gettoken`; returns access, public (websocket) and read tokens |
| Order limits | Limit and SL-Limit only (no market, CO or BO); 10 orders/second | not checked |
| Other | A v1 and a v2 token cannot be active together: logging in with one logs the other out | tokens sent as `x-jwt-token` |

Static IP rules come from SEBI's retail-algo framework in force since 1 April 2026; Fyers also validates a whitelisted IP on order requests.
For this project that means: **run order placement from the one VM whose Elastic IP you register**, and register the same IP on every broker app.
Reading data (login, candles, quotes) is not stated to be IP-restricted, but I could not confirm that for either broker.

## Symbols differ per broker

Our canonical id is the Fyers style, `NSE:SBIN-EQ`. Per broker:

| | Order/quote identifier | Where it comes from |
|---|---|---|
| Flattrade | exchange `NSE` + trading symbol `SBIN-EQ`, and a numeric **token** for market data | `SearchScrip`, or `https://flattrade.s3.ap-south-1.amazonaws.com/scripmaster/NSE_Equity.csv` |
| Paytm Money | numeric **security_id** with exchange and segment | `https://developer.paytmmoney.com/data/v1/scrips/security_master.csv` |
| Fyers | `NSE:SBIN-EQ` plus a numeric fytoken | `https://public.fyers.in/sym_details/NSE_CM.csv` |

- Flattrade: `terminal/feed/flattrade.py` (`TokenResolver`, cached in `terminal_broker_tokens`).
- Paytm Money: `trading_service/adapters/paytm_instruments.py` (`PaytmInstruments().resolve("NSE:SBIN-EQ")` → exchange, segment `E`, security_id), cash equities and ETFs only.
- Fyers: none needed, our ids are Fyers tickers.

Sources: [Flattrade API v2 notice](https://flattrade.in/kosh/flattrade-api-implementation-of-retail-algo-standards-api-version-2-launch/), [Flattrade pythonAPI token_generator](https://github.com/flattrade/pythonAPI), [Paytm Money pyPMClient](https://github.com/paytmmoney/pyPMClient), [Fyers retail-algo rules](https://support.fyers.in/portal/en/kb/articles/what-are-the-new-sebi-rules-for-retail-algo-trading-from-april-01-2026), [OpenAlgo broker adapters](https://github.com/marketcalls/openalgo).
