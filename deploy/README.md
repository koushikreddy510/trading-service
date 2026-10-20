# Deploying the whole pipeline (one VM, one script)

Everything runs with Docker Compose on **one** Linux VM. Nothing here deploys by itself: you run `deploy.sh`
(or GitHub Actions runs it for you after you add the workflow).

```
internet ──80/443──▶ web (Caddy)  ── static Workspace (trade/web build)
                       │            └─ /terminal, /terminal/ws ──▶ trading-service :8001
                       │  (password-protected)
   scheduler :8002 ──HTTP──▶ strategy-screener :8000 · trading-service :8001 · data-store :8003
                                         └──────── all read/write ────────▶ postgres (one DB: market)
```

| Container | Repo | What it is |
|---|---|---|
| `web` | `trade` (`web/`) | The Workspace React app, built to static files, served by Caddy (also HTTPS and the login) |
| `trading-service` | `trading-service` | Accounts/orders API **and** the `/terminal` API + websocket the Workspace talks to |
| `scheduler` | `trading-service` (`scheduler/`) | Cron replacement; calls the other services on the schedule in `schedules.yaml` (IST) |
| `strategy-screener` | `strategy-screener` | Strategies, universes, and the `/admin` job endpoints the Data screen drives |
| `data-store` | `data-store` | Scrapers (candles, financials, results, sectors) + a small HTTP runner |
| `postgres` | — | One database, `market`, for all of it |

**Not deployed:** the Go engine ("Atlas", the rest of the `trade` repo) and Redis. The Workspace does not call them;
only the old `?view=atlas` page does.

## Sizing (what to rent)

One VM is enough; no ECS, ECR, RDS, load balancer or autoscaling. Images are built on the VM, so there is no registry to pay for.

| | Choice | Why |
|---|---|---|
| Instance | **t3.medium** (2 vCPU, 4 GB) is enough for testing; **t3.large** (8 GB) is comfortable | Postgres + 5 small Python containers + Caddy idle at ~2 GB; the frontend build and big scrapes spike |
| Disk | 30 GB gp3 | 300 days × ~2,000 stocks of daily candles is small; Docker images take ~5 GB |
| Region | ap-south-1 (Mumbai) | Closest to NSE and the brokers |
| Network | Elastic IP; security group: 80/443 open, 22 only from your IP | Everything else listens on 127.0.0.1 |
| Roughly | t3.medium ≈ $30/month, t3.large ≈ $60/month + ~$3 disk, on-demand. Check the AWS calculator; prices move | A Lightsail 4 GB plan (~$24 flat) or a non-AWS VPS is cheaper still and works identically |

Add 2 GB of swap on a 4 GB machine (`fallocate -l 2G /swapfile && mkswap /swapfile && swapon /swapfile`).
Keep it running all day: the scheduler fires jobs overnight (screener.in at 02:00, reports at 02:30 IST).

## Oracle Cloud Free Tier (cheapest; needed for a fixed IP)

Flattrade API v2 only accepts orders from one **static IP that you enter when you create the API key** (changeable once a week).
So the order is: VM and fixed IP first, API key second.

1. **Instance:** Compute → Create instance → shape **VM.Standard.A1.Flex** (Always Free: up to 4 OCPU / 24 GB; pick 2 OCPU / 12 GB), image **Ubuntu 24.04 (aarch64)**, 50 GB boot volume, public subnet, add your SSH key.
   "Out of host capacity" is common: retry later, try another availability domain, or upgrade the account to Pay-As-You-Go (you are still not charged inside the free limits).
   Pick the home region carefully (Mumbai `ap-mumbai-1` or Hyderabad); it cannot be changed later.
2. **Fixed IP:** Networking → IP management → **Reserved public IP** → attach it to the instance's VNIC. Ephemeral IPs change if the instance is recreated.
3. **Open the ports twice** (Oracle blocks them in two places): add ingress rules for TCP 80 and 443 in the subnet's **security list**, and on the VM
   `sudo iptables -I INPUT 5 -p tcp -m multiport --dports 80,443 -j ACCEPT && sudo netfilter-persistent save`.
4. **Check the address the broker will see:** on the VM `curl -s ifconfig.me` must print the reserved IP. Enter that as the **primary static IP** when you create the Flattrade API key (redirect URL `http://127.0.0.1:5000/`).
5. Deploy as in *First deployment* below. All images used (postgres, python, node, caddy) have arm64 builds, so nothing changes.
6. Oracle may reclaim an Always Free instance whose CPU stays under ~20% for a week; Postgres plus the scheduler usually stay above that, but watch for the email.

**Every morning** (token expires about 05:30 IST): open `https://auth.flattrade.in/?app_key=<key>` on your laptop, log in, copy the address the browser ends on
(`http://127.0.0.1:5000/?code=...` - the page will not load, that is fine), then on the VM: `deploy.sh flattrade-login 'http://127.0.0.1:5000/?code=...'`.
At 08:15 IST the check job posts to Slack if the token is missing or rejected.

## First deployment

1. Start the VM (Ubuntu 24.04), ssh in as `ubuntu`.
2. Make sure the code is pushed: the VM pulls from GitHub, so everything built so far must be **committed and pushed to `main`** in all four repos (`trading-service/trading_service/terminal`, `trade/web` and the scheduler changes are not committed yet).
3. Create a GitHub fine-grained token with read access to the four repos (`trade_go`, `trading-service`, `data-store`, `strategy-screener`), then:
   ```bash
   sudo mkdir -p /opt/trading-pipeline && sudo chown ubuntu /opt/trading-pipeline && cd /opt/trading-pipeline
   git clone https://x-access-token:<TOKEN>@github.com/koushikreddy510/trading-service.git
   GH_TOKEN=<TOKEN> trading-service/deploy/deploy.sh init     # docker, other repos, .env, prints the dashboard login once
   trading-service/deploy/deploy.sh all
   ```
4. Open `http://<server-ip>/`, sign in, open **Data** and run, in this order: Sync symbol master → Sectors and market cap →
   Daily candles (needs a Fyers token in `data-store/fyers_access_token.txt`) → Latest results → Financials
   (`announcements_only`, then `incremental`/`full`). Then **Screener → Run daily scan**.

For HTTPS, point a DNS name at the Elastic IP and set `SITE_ADDRESS=your.domain.com` in `deploy/.env`
(then `deploy.sh web`). Caddy gets and renews the certificate itself.

## Day to day

```bash
deploy.sh web | trading | screener | data-store | all     # pull main, rebuild, restart
deploy.sh status        deploy.sh logs trading-service      deploy.sh migrate
```
`BRANCH=some-branch deploy.sh trading` deploys another branch (for testing). The script only fast-forwards, so a dirty or
diverged checkout on the VM stops it rather than being overwritten.

## GitHub Actions

`github-workflow.example.yml` is the whole thing: copy it into each repo as `.github/workflows/deploy.yml`, set `SERVICE`
to the value in its header, and add two secrets (`DEPLOY_HOST`, `DEPLOY_SSH_KEY`). A push to `main` in a repo then rebuilds only
that repo's containers; "Run workflow" with `all` redeploys everything. Concurrent deploys queue.

## Things to know

- **Login:** the Workspace has no user accounts yet and its Data screen can start scrapes, so Caddy puts one shared password in front of everything.
- **Live prices** need `TERMINAL_MODE=flattrade` plus `FLATTRADE_USER_ID` and `FLATTRADE_TOKEN`. That token and the Fyers token expire daily and nothing refreshes them on the VM yet; until then the terminal runs on synthetic prices (the page says DEMO DATA) and daily candles need the Fyers token file updated each day.
- **Scraping from a cloud IP:** screener.in and NSE sometimes throttle datacenter addresses. Watch the first runs on the Data screen.
- **Backups:** Postgres data lives in the `postgres_data` Docker volume. Take a daily `pg_dump` or EBS snapshot once the data matters.
- **Unpinned dependencies:** `requirements.txt` files are unpinned. The compose file names `postgresql+psycopg2://` for that reason; pin versions before relying on rebuilds being identical.
- The old systemd + nginx deployment of the strategy-screener at its current server is untouched; this kit is a separate, parallel target.
