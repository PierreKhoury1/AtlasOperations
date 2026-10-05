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
| Hermes Agent | `hermes.service`, 127.0.0.1:8642, key in `/etc/atlas/atlas.env` | `curl localhost:8642/health` |
| Postgres | local, `DATABASE_URL` in `/etc/atlas/atlas.env` | `sudo -u postgres psql -l` |
| Caddy | `/etc/caddy/Caddyfile`, access log `/var/log/caddy/access.log` | `systemctl status caddy` |
| Deploy | `atlas-deploy.timer` pulls `origin/main` every minute | `journalctl -u atlas-deploy` |
| Backup | `atlas-backup.timer` 03:40 UTC to `/srv/atlas/backups`, 30 days | `ls /srv/atlas/backups` |

## Updates

`git push` to `main` on PierreKhoury1/AtlasOperations. The box fast-forwards within a minute, reinstalls
requirements only if `requirements.txt` changed, restarts, health-checks, rolls back on failure. Never edit files on
the box except `/etc/atlas/atlas.env` (then `systemctl restart atlas`).

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
* Hermes API server listens on localhost only. `/etc/atlas/atlas.env` (root:atlas 640) and `/home/atlas/.hermes/.env`
  hold every key. Never commit them.
* ssh is key-only, fail2ban bans after 5 failures, ufw allows 22/80/443 only, security updates unattended.
