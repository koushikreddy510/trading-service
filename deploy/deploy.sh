#!/usr/bin/env bash
# One script for every deployment of the pipeline on one VM. Run it ON the VM (GitHub Actions does it over ssh).
#
#   deploy.sh init              once, on a fresh VM: docker, the four repos, .env, dashboard login
#   deploy.sh all               pull everything, rebuild, restart, apply database schemas
#   deploy.sh web               the Workspace (trade/web) + its Caddy front door
#   deploy.sh trading           trading-service and the scheduler (both live in the trading-service repo)
#   deploy.sh screener          strategy-screener
#   deploy.sh data-store        data-store scrapers + runner (also re-applies the schemas)
#   deploy.sh migrate           apply the market-table schemas only
#   deploy.sh flattrade-login '<url>'   daily: turn the URL Flattrade redirected your browser to into today's token and restart trading
#   deploy.sh status | logs [service]
#
#   deploy.sh backup [dir]      everything not in git (database, .env, scheduler history, Fyers token) -> one .tar.gz
#   deploy.sh setup <backup>    a fresh Ubuntu VM (x86 or ARM) -> running copy, in STANDBY (no orders, no scheduler, no Slack, demo prices)
#   deploy.sh go-live           a standby copy becomes the live server (real .env back, scheduler on)
#   deploy.sh restore <backup>  only the restore step of setup (database, .env, history, token)
#
# Environment: BRANCH (default main), GH_TOKEN (init only, to clone private repos), GH_OWNER (default koushikreddy510).
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DEPLOY_DIR/../.." && pwd)"               # parent of the four repos
BRANCH="${BRANCH:-main}"
GH_OWNER="${GH_OWNER:-koushikreddy510}"
dc() { (cd "$DEPLOY_DIR" && docker compose -f docker-compose.ec2.yml --env-file .env "$@"); }

repo_of()     { case "$1" in web) echo trade;; trading|scheduler) echo trading-service;; screener) echo strategy-screener;; data-store) echo data-store;; esac; }
services_of() { case "$1" in web) echo web;; trading) echo "trading-service scheduler";; screener) echo strategy-screener;; data-store) echo data-store;; esac; }

pull() {  # fast-forward only: a diverged or dirty checkout stops the deploy instead of guessing
  git -C "$ROOT/$1" fetch --quiet origin "$BRANCH"
  git -C "$ROOT/$1" checkout --quiet "$BRANCH"
  git -C "$ROOT/$1" merge --ff-only --quiet "origin/$BRANCH"
  echo "  $1 @ $(git -C "$ROOT/$1" rev-parse --short HEAD)"
}

migrate() {  # same files the old deploy workflow applied; most are IF NOT EXISTS, so errors on re-run are expected and ignored
  echo "== schemas"
  for f in data-store/schema.sql data-store/ohlc.sql data-store/enrich_symbols.sql data-store/financials_schema.sql \
           strategy-screener/migrations/add_financials_source.sql data-store/commodity_schema.sql \
           data-store/results_schema.sql data-store/flattrade_tokens_schema.sql; do
    [ -f "$ROOT/$f" ] || { echo "  missing $f"; continue; }
    dc exec -T postgres sh -c 'psql -q -U "$POSTGRES_USER" -d market' < "$ROOT/$f" >/dev/null 2>&1 || true
    echo "  applied $f"
  done
}

status() {
  dc ps
  for p in 8000:strategy-screener: 8001:trading-service:health 8002:scheduler:health 8003:data-store:; do
    IFS=: read -r port name path <<<"$p"
    printf '  %-18s' "$name"; curl -s -o /dev/null -m 5 -w '%{http_code}\n' "http://127.0.0.1:$port/$path" || echo down
  done
}

init() {
  command -v docker >/dev/null || { echo "== installing docker"; curl -fsSL https://get.docker.com | sudo sh; sudo usermod -aG docker "$USER" || true; }
  docker compose version >/dev/null || { echo "docker compose v2 is required"; exit 1; }
  echo "== repos in $ROOT"
  for r in data-store strategy-screener trading-service trade; do
    [ -d "$ROOT/$r/.git" ] && { echo "  have $r"; continue; }
    gh_name="$r"; [ "$r" = trade ] && gh_name=trade_go   # the GitHub repo is trade_go; the folder stays trade
    if [ -n "${GH_TOKEN:-}" ]; then git clone --quiet "https://x-access-token:${GH_TOKEN}@github.com/$GH_OWNER/$gh_name.git" "$ROOT/$r"
    else git clone --quiet "git@github.com:$GH_OWNER/$gh_name.git" "$ROOT/$r"; fi
    # the trade repo's Go code is not deployed; its web/ folder is the Workspace
  done
  touch "$ROOT/data-store/fyers_access_token.txt"          # a missing file would be mounted as a directory
  if [ ! -f "$DEPLOY_DIR/.env" ]; then
    cp "$DEPLOY_DIR/env.example" "$DEPLOY_DIR/.env"; chmod 600 "$DEPLOY_DIR/.env"
    pw="$(openssl rand -hex 16)"; login="$(openssl rand -base64 12 | tr -d '=+/')"
    hash="$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "$login")"
    sed -i "s|^PGPASSWORD=.*|PGPASSWORD=$pw|; s|^BASIC_AUTH_HASH=.*|BASIC_AUTH_HASH='$hash'|" "$DEPLOY_DIR/.env"
    echo "== dashboard login (shown once):  admin / $login"
  fi
  echo "== done. Edit $DEPLOY_DIR/.env if needed (SITE_ADDRESS, Flattrade, Fyers), then: deploy.sh all"
}

backup() {  # backup [dir]
  local out tmp file; out="${1:-$HOME}"; tmp="$(mktemp -d)"; file="$out/trading-backup-$(date +%Y%m%d-%H%M).tar.gz"
  echo "== backup"
  dc exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d market -Fc' > "$tmp/market.dump"
  [ -s "$tmp/market.dump" ] || { echo "  pg_dump produced nothing"; exit 1; }
  cp "$DEPLOY_DIR/.env" "$tmp/env"
  # sqlite's own backup, so a job finishing mid-copy cannot leave a torn file
  if dc exec -T scheduler python -c "import sqlite3; d=sqlite3.connect('/tmp/runs.db'); sqlite3.connect('/data/job_runs.db').backup(d); d.close()" 2>/dev/null; then
    dc cp scheduler:/tmp/runs.db "$tmp/job_runs.db" >/dev/null
  fi
  if [ -s "$ROOT/data-store/fyers_access_token.txt" ]; then cp "$ROOT/data-store/fyers_access_token.txt" "$tmp/"; fi
  (umask 077 && tar czf "$file" -C "$tmp" .); rm -rf "$tmp"
  echo "  $file ($(du -h "$file" | cut -f1)) - holds every secret in .env: copy it with scp, never commit it"
}

restore() {  # restore <backup>: .env first, so postgres initialises with the backup's password
  [ -f "${1:-}" ] || { echo "usage: deploy.sh restore <trading-backup-*.tar.gz>"; exit 1; }
  local tmp; tmp="$(mktemp -d)"; tar xzf "$1" -C "$tmp"
  echo "== restore $(basename "$1")"
  if [ -f "$DEPLOY_DIR/.env" ] && ! cmp -s "$tmp/env" "$DEPLOY_DIR/.env"; then cp "$DEPLOY_DIR/.env" "$DEPLOY_DIR/.env.before-restore"; fi
  install -m 600 "$tmp/env" "$DEPLOY_DIR/.env"
  rm -f "$DEPLOY_DIR/.env.live"   # a standby's saved settings are older than the backup's: `all` now runs live
  if [ -f "$tmp/fyers_access_token.txt" ]; then cp "$tmp/fyers_access_token.txt" "$ROOT/data-store/"; fi
  dc up -d --wait postgres
  # --clean replaces whatever is there; pg_restore exits non-zero on harmless notices, so count the tables instead
  dc exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d market --clean --if-exists --no-owner' < "$tmp/market.dump" 2>/dev/null || true
  echo "  tables: $(dc exec -T postgres sh -c "psql -U \"\$POSTGRES_USER\" -d market -Atc \"select count(*) from pg_tables where schemaname = 'public'\"")"
  if [ -f "$tmp/job_runs.db" ]; then
    dc build scheduler >/dev/null
    dc run --rm --no-deps -v "$tmp:/restore:ro" --entrypoint sh scheduler -c 'cp /restore/job_runs.db /data/job_runs.db' && echo "  scheduler history restored"
  fi
  rm -rf "$tmp"
}

standby_env() {  # keep the real .env as .env.live; the running one may not trade, schedule, post to Slack or share the broker feed
  cp "$DEPLOY_DIR/.env" "$DEPLOY_DIR/.env.live"; chmod 600 "$DEPLOY_DIR/.env.live"
  sed -i -E '/^STANDBY=/d; s/^TERMINAL_MODE=.*/TERMINAL_MODE=demo/; s/^((TRADING_)?SLACK_[A-Z_]*WEBHOOK_URL[A-Z_]*)=.*/\1=/' "$DEPLOY_DIR/.env"
  echo "STANDBY=1" >> "$DEPLOY_DIR/.env"
}

setup() {  # setup <backup>
  [ -f "${1:-}" ] || { echo "usage: deploy.sh setup <trading-backup-*.tar.gz>"; exit 1; }
  local backup; backup="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
  command -v docker >/dev/null || { echo "== installing docker"; curl -fsSL https://get.docker.com | sudo sh; sudo usermod -aG docker "$USER"; }
  # a fresh docker group membership only applies to new logins: re-run inside it
  docker info >/dev/null 2>&1 || exec sg docker -c "$(printf '%q ' "${BASH_SOURCE[0]}" setup "$backup")"
  # the backup's .env goes in before init, so init keeps it instead of generating new passwords
  tar xzf "$backup" -C "$DEPLOY_DIR" ./env && mv "$DEPLOY_DIR/env" "$DEPLOY_DIR/.env" && chmod 600 "$DEPLOY_DIR/.env"
  init
  restore "$backup"
  [ -n "${LIVE:-}" ] || standby_env
  for s in data-store screener trading web; do pull "$(repo_of "$s")"; done
  dc build
  if [ -n "${LIVE:-}" ]; then dc up -d; else dc up -d postgres data-store strategy-screener trading-service web; fi
  migrate; sleep 10; status
  [ -n "${LIVE:-}" ] || echo "== STANDBY: no orders, no scheduler, no Slack, demo prices. When this server should take over: deploy.sh go-live"
}

go_live() {
  [ -f "$DEPLOY_DIR/.env.live" ] || { echo "not a standby copy (no deploy/.env.live)"; exit 1; }
  mv "$DEPLOY_DIR/.env.live" "$DEPLOY_DIR/.env"
  dc up -d; sleep 10; status
  echo "== LIVE. Register this server's IP with Flattrade (orders come only from it), then stop the old server."
}

deploy() {  # deploy <web|trading|screener|data-store>
  echo "== $1"; pull "$(repo_of "$1")"
  # shellcheck disable=SC2046
  dc build $(services_of "$1"); dc up -d $(services_of "$1")
}

cmd="${1:-}"; shift || true
case "$cmd" in
  init) init ;;
  all) for s in data-store screener trading web; do echo "== $s"; pull "$(repo_of "$s")"; done
       dc up -d postgres; dc build; dc up -d; migrate; status ;;
  web|trading|screener|data-store) deploy "$cmd"; [ "$cmd" = data-store ] && migrate; status ;;
  migrate) migrate ;;
  backup) backup "$@" ;;
  restore) restore "$@" ;;
  setup) setup "$@" ;;
  go-live) go_live ;;
  flattrade-login)  # needs python3 + requests on the VM (both ship with Ubuntu server images); FLATTRADE_API_KEY/SECRET come from .env
    [ -n "${1:-}" ] || { echo "usage: deploy.sh flattrade-login '<the full URL your browser was redirected to>'"; exit 1; }
    (cd "$ROOT/trading-service" && set -a && . "$DEPLOY_DIR/.env" && set +a && python3 -m broker_auth flattrade --code "$1" --env-file "$DEPLOY_DIR/.env")
    dc up -d trading-service ;;
  status) status ;;
  logs) dc logs --tail 100 -f "$@" ;;
  *) sed -n '2,21p' "${BASH_SOURCE[0]}"; exit 1 ;;
esac
