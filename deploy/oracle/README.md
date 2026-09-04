# Atlas Desks on Oracle Cloud (Always Free, Ampere A1)

One VM runs everything: portal (gunicorn) + real Hermes Agent runtime + PostgreSQL + Caddy (HTTPS).
The camera **vision node** (`node/`) is a separate container and can run on the same VM.

## What only the account owner can do

1. **Sign up** at cloud.oracle.com (card + phone; some accounts are rejected at signup, retry with another card).
   Pick the **home region** carefully: it cannot be changed later. `il-jerusalem-1` is closest to the customers and
   less contested than Frankfurt/London; Amsterdam, Marseille, Stockholm and Zurich are other quiet choices.
2. **Upgrade to Pay As You Go** (Billing -> Upgrade). Still $0 inside the Always-Free limits, but stops Oracle
   reclaiming "idle" free instances. Set a budget alert at $1.
3. **Create the instance**: Compute -> Instances -> Create. Image *Ubuntu 24.04 (aarch64)*, shape *VM.Standard.A1.Flex*,
   4 OCPU / 24 GB, boot volume 100-200 GB. Paste the SSH public key you were given. If it says
   *Out of host capacity*, run `python node/oracle/launch_a1.py` (retries every AD until one succeeds) or try again later.
4. **Open the ports**: Networking -> Virtual cloud networks -> your VCN -> subnet -> Default security list ->
   Add ingress rules: source `0.0.0.0/0`, TCP, destination ports `80` and `443`.
5. Send back the **public IP**. Everything below is scripted.

## Install (once, on the VM)

```bash
ssh -i ~/.ssh/atlas_oracle ubuntu@PUBLIC_IP
curl -fsSL https://raw.githubusercontent.com/PierreKhoury1/hermes-ops/master/deploy/oracle/setup_portal.sh | bash -s -- \
     --openrouter-key sk-or-v1-XXXX --domain atlasdesks.com
```

Omit `--domain` until the domain is bought: Caddy then serves `http://PUBLIC_IP`. Re-run with `--domain` later,
after the DNS `A` record (`atlasdesks.com` and `www`) points at the VM; Caddy fetches the certificate itself.
Add `--open` only for a demo box (no login at all). Add `--vision` to install the local CV stack on the VM.

Result:

| Piece | Where | Check |
|---|---|---|
| Portal | `atlas.service`, 127.0.0.1:8094 behind Caddy | `curl localhost:8094/api/health` -> `live_ready: true`, `hermes_agent: true` |
| Hermes Agent | `hermes.service`, 127.0.0.1:8642, key in `/etc/atlas/atlas.env` | `curl localhost:8642/health` |
| Postgres | local, `DATABASE_URL` in `/etc/atlas/atlas.env` | `sudo -u postgres psql -l` |
| Caddy | `/etc/caddy/Caddyfile` | `sudo systemctl status caddy` |
| Logs | `sudo journalctl -fu atlas` / `-fu hermes` | |

## Updates

* Manual: `ssh ubuntu@VM bash /opt/atlas/src/deploy/oracle/deploy.sh`
* Automatic on push: copy `deploy/oracle/deploy-oracle.yml` to `.github/workflows/` (GitHub web UI; the local git
  token lacks the `workflow` scope) and add repo secrets `ORACLE_HOST` and `ORACLE_SSH_KEY` (the private key).
  It then runs `deploy.sh` after every push to master.

## Moving data off Render/Supabase

```bash
pg_dump "$SUPABASE_URL" --no-owner --no-privileges -Fc -f atlas.dump
scp -i ~/.ssh/atlas_oracle atlas.dump ubuntu@PUBLIC_IP:/tmp/
ssh ubuntu@PUBLIC_IP 'sudo systemctl stop atlas && pg_restore -d "$(sudo grep ^DATABASE_URL= /etc/atlas/atlas.env | cut -d= -f2-)" --clean --if-exists /tmp/atlas.dump; sudo systemctl start atlas'
```

Then delete the Render services (they hold no data any more) and point the desk hook URLs in camera configs
(`node.json` -> `hook`) at the new host.

## Vision node on the same VM

```bash
bash /opt/atlas/src/node/oracle/setup.sh https://atlasdesks.com/hook/DESK_TOKEN/vision
```

## Security notes

* Accounts are ON by default (`DESK_OPEN=0`). The audit of 2 Sep 2026 still applies: `run_python` is unsandboxed and
  MCP connectors run as the portal user. Keep desks per trusted customer; do not hand out signup to the public yet.
* The Hermes API server listens on localhost only. Nothing but the portal can reach it.
* `/etc/atlas/atlas.env` and `~/.hermes/.env` hold every key. `chmod 600`, never commit.
