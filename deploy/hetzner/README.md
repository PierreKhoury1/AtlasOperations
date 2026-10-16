# Atlas Desks on Hetzner Cloud

One VPS runs everything: portal (gunicorn) + real Hermes Agent runtime + PostgreSQL + Caddy (HTTPS).
Same shape as the BookliBookings `bookli-1` box. Oracle Always-Free (`deploy/oracle/`) was the previous plan but
signup never worked; Hetzner is paid (a few EUR/month) and provisions in a minute.

## Size

| Server | vCPU / RAM | Fits |
|---|---|---|
| CX23 | 2 / 4 GB | portal + Hermes + Postgres (no on-box vision) |
| **CX33** (recommended) | 4 / 8 GB | the above + `--vision` (torch CPU, YOLO, CLIP embeddings) |
| CAX21 (ARM) | 4 / 8 GB | same as CX33, cheaper, when in stock |

Location: Nuremberg or Falkenstein (same as bookli-1). Image: Ubuntu 24.04.

## Owner steps (console.hetzner.com)

1. New project **AtlasDesks** (keep it separate from BookliBookings).
2. Security -> SSH keys -> Add: paste the public half of `~/.ssh/atlas_hetzner` (name `pierre@atlas-hetzner`).
3. Create server: Ubuntu 24.04, CX33, that SSH key, no volumes, no Hetzner backups (we do our own).
4. Send back the public IPv4.

## Install (once)

```bash
ssh -i ~/.ssh/atlas_hetzner root@IP
curl -fsSL https://raw.githubusercontent.com/PierreKhoury1/AtlasOperations/main/deploy/hetzner/setup.sh -o setup.sh
bash setup.sh --openrouter-key sk-or-v1-XXXX --vision
```

Without `--host` the site is `https://IP.sslip.io` (real certificate, zero DNS). Once atlasdesks.com is bought,
point `A` records (`@` and `www`) at the IP and re-run `bash setup.sh --host atlasdesks.com` (keys are kept).

| Piece | Where | Check |
|---|---|---|
| Portal | `atlas.service`, user `atlas`, `/srv/atlas/src`, venv `/srv/atlas/venv` | `curl localhost:8094/api/health` |
| Hermes Agent | `hermes.service`, 127.0.0.1:8642, key in `/etc/atlas/atlas.env`; model calls via the metering proxy | `curl localhost:8642/health` |
| Postgres | local, `DATABASE_URL` in `/etc/atlas/atlas.env` | `sudo -u postgres psql -l` |
| Caddy | `/etc/caddy/Caddyfile`, access log `/var/log/caddy/access.log` | `systemctl status caddy` |
| Deploy | `atlas-deploy.timer` pulls `origin/main` every minute | `journalctl -u atlas-deploy` |
| Backup | `atlas-backup.timer` 03:40 UTC to `/srv/atlas/backups`, 30 days | `ls /srv/atlas/backups` |

## Updates

`git push` to `main` on PierreKhoury1/AtlasOperations. The box fast-forwards within a minute, reinstalls
requirements only if `requirements.txt` changed, restarts, health-checks, rolls back on failure. Never edit files on
the box except `/etc/atlas/atlas.env` (then `systemctl restart atlas`).

## Hermes model calls (the metering proxy)

Hermes runs its own tool loop and makes its own model calls, up to `max_turns` 150 per delegation. Atlas counts them
per account only when they go through the portal's metering proxy, `http://127.0.0.1:8094/internal/llm/v1`, which
checks each one against the account's limits and forwards it to OpenRouter with Atlas's key.

* A new install is wired to it: `~/.hermes/.env` holds `USAGE_HERMES_PROXY_TOKEN` (also in `/etc/atlas/atlas.env`)
  where the OpenRouter key would go, `~/.hermes/config.yaml` has `model.base_url` set to the proxy, and Caddy answers
  `/internal/*` with 404. The proxy itself answers only loopback requests with the token and no forwarding headers.
* `USAGE_HERMES_METER=off` is the default: Atlas does not use its Hermes instance and specialists run on the built-in
  engine (the proxy refuses every call). `proxy` turns Hermes on with every model call counted. `--hermes-proxy`
  installs straight into `proxy`.
* Before `proxy` on a box that matters, check on a test box with `USAGE_ENFORCE=0` (counted, never refused) that the
  inner calls reach the proxy for the default model and for Atlas's per-request model. The operator view's Hermes
  block shows the last inner call's model, whether it streamed and its header names; `journalctl -u hermes` shows
  what Hermes does with a 429.
* An install from before the proxy (atlas-1) keeps Hermes on its own key until switched by hand, as root: put
  `USAGE_HERMES_METER=proxy` and a random `USAGE_HERMES_PROXY_TOKEN` (`python3 -c "import secrets;
  print(secrets.token_urlsafe(32))"`) in `/etc/atlas/atlas.env`; the same token as `OPENROUTER_API_KEY` in
  `/home/atlas/.hermes/.env`; `model.base_url: http://127.0.0.1:8094/internal/llm/v1` in
  `/home/atlas/.hermes/config.yaml`; `respond /internal/* 404` in `/etc/caddy/Caddyfile` (a re-run of `setup.sh`
  writes it); then `systemctl restart hermes caddy atlas` and run one job on a Hermes desk: the operator view should
  show its inner requests and no self-check warning.

## Usage limits (operator)

Each account's allowances are shares of Atlas's free-model pool and paid cap; `/desk/usage` shows them. The
server-wide numbers are env settings in `/etc/atlas/atlas.env` (`USAGE_OWNER_SHARE`, `USAGE_AUTO_SHARE`,
`USAGE_PUBLIC_SHARE`, `USAGE_HOUSE_PAID_USD_MONTH`, ...), then `systemctl restart atlas`. One account's limits change
with the operator command, run as root on the box: the env file is root-only, and `--env-file` reads it the way
systemd does, so the command opens the service's own database.

```bash
cd /srv/atlas/src
U="/srv/atlas/venv/bin/python -m atlas usage --env-file /etc/atlas/atlas.env"
$U show                                   # the pool, the website's share, the paid cap, accounts with use today, overrides
$U show --email a@b.c                     # one account: limits, overrides, today's use, the last 7 days
$U set --email a@b.c --limit ai_requests_day --value 500 --note "pilot until 31 Oct"
$U clear --email a@b.c --limit ai_requests_day        # or --limit all
$U prune                                  # the hourly retention, once
```

* Limits an override can change: `ai_requests_day`, `ai_requests_auto_day`, `paid_usd_month`, `run_requests`,
  `runs_at_once`, `sms_segments_day`, `auto_sends_day`, `storage_mb`. `--value none` = no Atlas limit for that account
  (still counted). The note is shown to the owner on their Usage page.
* The service applies a change within 30 s. The command calls no provider and sends nothing.

## The house agent (one connected Claude Code / Codex for every account)

`HOUSE_BRIDGE_OWNER=<email>` in `/etc/atlas/atlas.env` names an account. While that account's runner is connected
(`py atlas_bridge.py --url https://... --token atb_... --agent claude --parallel 4` on any machine with the CLI
signed in), every desk that did not pick an AI of its own runs on it: each model call is one `claude -p` process
with every built-in tool off, so the machine is used as a model and nothing on it is read or run. It is house work:
every request counts on the account's `ai_requests_day` / `ai_requests_auto_day` and on `USAGE_HOUSE_AGENT_DAY`
(default 300 a day for everyone together; 0 = no cap). A desk that picks "Atlas models" in Desk setup (`house`)
opts out; the owner's own runner, when connected, still comes first. The named account's own desks keep the
BYO rule (metered, never limited). The runner's machine must stay on: when it is off, desks fall back to the house
models quietly, as before.

## Moving data off Supabase / Render (if anything worth keeping is there)

```bash
pg_dump "$SUPABASE_URL" --no-owner --no-privileges -Fc -f atlas.dump
scp -i ~/.ssh/atlas_hetzner atlas.dump root@IP:/tmp/
ssh -i ~/.ssh/atlas_hetzner root@IP 'systemctl stop atlas; sudo -u atlas pg_restore -d "$(grep ^DATABASE_URL= /etc/atlas/atlas.env | cut -d= -f2-)" --clean --if-exists /tmp/atlas.dump; systemctl start atlas'
```

Then delete the Render services and point camera hooks (`node.json` -> `hook`) at the new host.

## Security notes

* Accounts ON by default (`DESK_OPEN=0`). The 2 Sep 2026 audit still applies: `run_python` is unsandboxed and MCP
  connectors run as the `atlas` user. Desks for trusted customers only; no public signup yet.
* Hermes API server listens on localhost only. `/etc/atlas/atlas.env` (root:root 600, read by systemd as root) and
  `/home/atlas/.hermes/.env` (atlas 600) hold every key. Never commit them. The second file is still readable by
  `run_python` (same user): until snippets run in a sandbox with another uid, treat what it holds as exposed to any
  agent code. On an install wired to the metering proxy that is the proxy token, not the OpenRouter key: model calls
  made with it are counted and limited like Hermes's own. Caddy has its admin API off (`admin off`); reload with
  `systemctl restart caddy`.
* ssh is key-only, fail2ban bans after 5 failures, ufw allows 22/80/443 only, security updates unattended.
