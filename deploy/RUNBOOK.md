# Runbook: what runs in production, and how to start it anywhere

Checked against the live server on 2026-10-22. `README.md` in this folder explains the design; this file is the
operational truth: what is running, what state it keeps, and the commands that rebuild it on a new machine.

## 1. What is running today

**Server:** excloud VM `210.79.129.174` (ssh alias `excloud`, user `ubuntu`), Ubuntu 24.04, x86_64, 2 vCPU, 4 GB RAM, 24 GB disk.
Everything is Docker Compose in `/opt/trading-pipeline`. There is no cron, nginx or systemd unit of ours on the host
(`/opt/excloud` is the provider's agent).

```
/opt/trading-pipeline/
  data-store/          git: koushikreddy510/data-store         scrapers + HTTP runner
  strategy-screener/   git: koushikreddy510/strategy-screener  strategies, /admin job endpoints
  trading-service/     git: koushikreddy510/trading-service    terminal API + websocket, orders, scheduler, THIS deploy kit
  trade/               git: koushikreddy510/trade_go           only web/ is used: the Workspace React app
```

| Container | Port | Memory (idle) | What it is |
|---|---|---|---|
| `web` | **80, 443 public** | ~10 MB | Caddy: the Workspace build, `/terminal` + websocket proxy, `/scheduler/*`, one shared password |
| `trading-service` | 127.0.0.1:8001 | ~90 MB | terminal API, live Flattrade feed, rules engine, paper + live orders |
| `scheduler` | 127.0.0.1:8002 | ~45 MB | cron replacement: HTTP calls on `scheduler/schedules.yaml` (IST) |
| `strategy-screener` | 127.0.0.1:8000 | ~310 MB | strategies, universes, `/admin/*` job endpoints, AI report |
| `data-store` | 127.0.0.1:8003 | ~310 MB | candle / financials / results scrapers behind a small runner |
| `postgres` | 127.0.0.1:5432 | ~270 MB | one database `market` for everything (~1 GB with 4 years of daily candles) |

Total ~1.7 GB RAM in use; the busiest moments are image builds and the large scrapes.

The site is plain HTTP on the IP (`SITE_ADDRESS=:80`), behind one shared basic-auth password. Set a domain in
`SITE_ADDRESS` to get HTTPS from Caddy automatically.

### Daily automation (IST, `scheduler/schedules.yaml`; history at `http://<ip>/scheduler/ui`)

| When | Job | What it does |
|---|---|---|
| 07:45 Mon-Fri | `broker_auto_login` | unattended Flattrade login (password + TOTP); the token is stored in `terminal_broker_sessions` and handed to the running feed, no restart |
| 08:00 | `fyers_token_refresh` | Fyers login (not configured today: the token file is empty) |
| 08:15 | `broker_tokens_check` | asks each broker whether today's token works; Slack `#trading-jobs` only if not |
| 08:30 | `mcx_instruments_refresh` | MCX futures list from Flattrade's scrip master |
| 09:20, 09:25 | `universe_resolve_all`, `entry_candidate_scan` | trading universes and entry candidates |
| every 5 min 09-15 | `sync_positions` | broker positions (Slack only on failure) |
| 10:00 | `sync_pnl` | P&L snapshot |
| 11:30 | `ohlcv_1d_flattrade` | daily candles, **runs the yfinance script** (`ohlcv_1d_job.py`, split- and dividend-adjusted) despite the name |
| 13:00, 13:30 | `ohlcv_stocks`, `commodity_ohlcv` | Fyers-based refreshes (no Fyers token today, so they do nothing useful) |
| 14:30 | `scrape_latest_results` | NSE results |
| 16:15, 16:30 | `terminal_screener_scan`, `terminal_screener_sync` | screener metrics, screener -> watchlist sync |
| 21:00 | `terminal_data_digest` | data freshness digest to Slack `#trading-data` |
| 02:00, 02:30, 11:30 daily | `scrape_screener_latest`, `daily_ai_report` (x2) | screener.in latest results, AI report |
| Fri 20:30 / Sun 04:30 | `scrape_financials`, `enrich_sectors` | financials batch, sectors |

### State that is NOT in git (what a backup must carry)

| What | Where | Notes |
|---|---|---|
| Secrets and settings | `trading-service/deploy/.env` (38 lines, mode 600) | Postgres password, dashboard login hash, Flattrade key/secret/password/TOTP, Slack webhooks, screener.in cookie |
| The database | Docker volume `deploy_postgres_data` | candles (4 years daily), financials, rules, positions, orders, broker sessions, live-order settings |
| Job run history | Docker volume `deploy_scheduler_data` (`job_runs.db`) | nice to keep, not required |
| Fyers token | `data-store/fyers_access_token.txt` | empty today |
| Caddy certificates | Docker volume `deploy_caddy_data` | none today (no domain), Caddy re-issues them anyway |

## 2. Commands

All of them run on the server: `/opt/trading-pipeline/trading-service/deploy/deploy.sh <command>`.

| Command | Use |
|---|---|
| `status`, `logs <service>` | health of every service; follow a service's log |
| `web`, `trading`, `screener`, `data-store`, `all` | pull `main` (fast-forward only), rebuild, restart |
| `backup [dir]` | one `trading-backup-<date>.tar.gz` with the database dump, `.env`, job history and Fyers token. **It contains every secret: move it with scp, never commit it** |
| `setup <backup>` | fresh Ubuntu VM -> running copy in **standby** (see below), from one backup file |
| `go-live` | a standby copy becomes the live server |
| `restore <backup>` | only the restore step (database, `.env`, history, token) on an existing install |

### Standby: why a new server starts switched off

The same Flattrade account must never be driven from two servers. `setup` therefore starts the copy with:

- `STANDBY=1`: trading-service refuses every broker call (place, modify, cancel, order sync). It cannot send an order even if
  the restored database says live trading is on.
- the **scheduler not started**: no second morning login (a new login could cut off the live server's session), no duplicate scrapes.
- `TERMINAL_MODE=demo`: synthetic prices, so it does not open a second Flattrade websocket.
- Slack webhooks blanked: no duplicate messages.

The real settings are kept beside it as `deploy/.env.live`; `go-live` puts them back and starts everything.
`LIVE=1 deploy.sh setup <backup>` skips standby (only for rebuilding after the old server is gone).

## 3. Start the app on a new machine

Needs: Ubuntu 22.04/24.04 (x86_64 or arm64), ~2 GB free RAM, ~20 GB disk, outbound internet, ports 80/443 open,
and a GitHub token that can read the four repos (fine-grained, read-only "Contents").

```bash
# on the OLD server: take a backup and copy it to the new one
ssh excloud '/opt/trading-pipeline/trading-service/deploy/deploy.sh backup'
scp excloud:~/trading-backup-*.tar.gz .
scp trading-backup-*.tar.gz ubuntu@<new-ip>:~

# on the NEW server
sudo mkdir -p /opt/trading-pipeline && sudo chown "$USER" /opt/trading-pipeline && cd /opt/trading-pipeline
git clone https://x-access-token:<GH_TOKEN>@github.com/koushikreddy510/trading-service.git
GH_TOKEN=<GH_TOKEN> trading-service/deploy/deploy.sh setup ~/trading-backup-<date>.tar.gz
```

`setup` installs Docker if missing, clones the other three repos, restores the backup, builds every image and starts
everything except the scheduler, in standby. It ends with `status`: every service should answer (data-store and
strategy-screener answer 404 on `/`, which is normal). Then open `http://<new-ip>/` with the same password as before.

## 4. Move production to another server (e.g. Oracle)

1. **New server up in standby** (section 3) from a fresh backup. Check the Workspace, charts, screener, backtests.
2. **Fixed IP:** give the new server its permanent public IP (Oracle: a reserved public IP) *before* step 5.
3. **Freeze the old server** (outside market hours): `ssh excloud 'cd /opt/trading-pipeline/trading-service/deploy && docker compose -f docker-compose.ec2.yml --env-file .env stop scheduler trading-service'`
4. **Final backup and restore:** `deploy.sh backup` on the old server, copy it, `deploy.sh restore <file>` on the new one, so the newest
   orders, positions and sessions move across. This puts the live `.env` in place (the standby one is kept as `.env.before-restore`).
5. **Flattrade static IP:** in Flattrade (wall.flattrade.in → Pi → API) set the new server's IP as the primary static IP.
   It can be changed only **once a week**, and orders from any other IP are rejected from then on.
6. **Go live:** `deploy.sh all` on the new server (starts everything, scheduler included). Check the next morning's 07:45 login
   and 08:15 check in `#trading-jobs`, and that orders work.
7. **Then** stop or terminate the old server. Keep its last backup.

`go-live` is the shortcut when no final restore is needed (nothing changed on the old server since the standby copy was made).

## 5. Things that bite

- **Oracle blocks ports twice:** the subnet security list *and* iptables on Ubuntu images
  (`sudo iptables -I INPUT 5 -p tcp -m multiport --dports 80,443 -j ACCEPT && sudo netfilter-persistent save`).
- **Oracle reclaims idle Always Free instances:** idle means, over 7 days, CPU 95th percentile < 20%, network < 20% and
  (A1 only) memory < 20%. A Pay-As-You-Go account is exempt.
- **Flattrade tokens** expire about 05:30 IST; the 07:45 auto-login renews them. `python -m broker_auth status` and anything
  reading `FLATTRADE_TOKEN` from `.env` sees an old token: the current one is in the database (`terminal_broker_sessions`).
- **Daily candles are adjusted** (yfinance, dividends and splits) and kept 1,500 days. Do not mix in raw broker prices.
- **The scheduler's HTTP timeout is 600 s**; long jobs (daily candles) show "timed out" in the history but finish anyway.
- `requirements.txt` files are unpinned: a rebuild months later can pull different library versions.
