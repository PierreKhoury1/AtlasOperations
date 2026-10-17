#!/usr/bin/env bash
# Atlas Desks on one Hetzner Cloud VPS (Ubuntu 24.04, x86 CX/CPX or ARM CAX). Run ONCE as root on a fresh box:
#
#   bash setup.sh --openrouter-key sk-or-v1-XXXX [--host atlasdesks.com] [--vision] [--open] [--hermes-proxy]
#
# Installs and wires, on the same box:
#   * Atlas portal (gunicorn, 127.0.0.1:8094)                      systemd atlas.service     user atlas
#   * real Hermes Agent (Nous Research) API server 127.0.0.1:8642  systemd hermes.service    user atlas
#     its model calls go through the portal's metering proxy        127.0.0.1:8094/internal/llm/v1 (usage spec 1.8)
#   * PostgreSQL (accounts / desks / approvals / vision vectors)    DATABASE_URL in /etc/atlas/atlas.env
#   * Caddy automatic HTTPS                                        /etc/caddy/Caddyfile
#   * pull-based auto-deploy: origin/main checked every minute     atlas-deploy.timer (no GitHub Action needed)
#   * nightly pg_dump + data tarball, 30 days                      atlas-backup.timer
#   * ufw 22/80/443, fail2ban, key-only ssh, unattended upgrades
#
#   --host     public hostname. Default = <public-ip>.sslip.io, which gets a real certificate with no DNS work.
#              Re-run with --host atlasdesks.com once the A record points here; Caddy fetches the new cert.
#   --vision   also install the on-box CV stack (torch CPU + ultralytics + open_clip, ~2.5 GB). Wants >= 8 GB RAM.
#   --open     DESK_OPEN=1 (no login, every desk public). Default = accounts ON; this box is on the internet.
#   --hermes-proxy  USAGE_HERMES_METER=proxy: Atlas uses its Hermes instance and every Hermes model call counts per
#              account. Default = off (specialists on the built-in engine) until the README's Hermes checks are done.
#              A new install is wired to the proxy either way: Hermes holds a proxy token, never the OpenRouter key.
# Re-running is safe: keeps keys/passwords in /etc/atlas/atlas.env, updates the checkout, restarts services.
set -euo pipefail

REPO="${ATLAS_REPO:-https://github.com/PierreKhoury1/AtlasOperations.git}"
BRANCH="${ATLAS_BRANCH:-main}"
HOST=""; OPENROUTER=""; OPEN="0"; VISION="0"; HERMES_METER="off"
while [ $# -gt 0 ]; do
  case "$1" in
    --openrouter-key) OPENROUTER="$2"; shift 2 ;;
    --host|--domain) HOST="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --open) OPEN="1"; shift ;;
    --vision) VISION="1"; shift ;;
    --hermes-proxy) HERMES_METER="proxy"; shift ;;
    *) echo "unknown arg $1"; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

APP_USER=atlas
ROOT=/srv/atlas
ENVF=/etc/atlas/atlas.env
NEW_INSTALL=0; [ -f "$ENVF" ] || NEW_INSTALL=1
PROXY_URL=http://127.0.0.1:8094/internal/llm/v1          # the portal's metering proxy (atlas.service binds :8094)
HOME_DIR=/home/$APP_USER
export DEBIAN_FRONTEND=noninteractive
rand() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
setenv() { if grep -q "^$2=" "$1"; then sed -i "s|^$2=.*|$2=$3|" "$1"; else echo "$2=$3" >> "$1"; fi; }

PUBIP="$(curl -s -m 5 -4 ifconfig.me || hostname -I | awk '{print $1}')"
[ -n "$HOST" ] || HOST="$PUBIP.sslip.io"

echo "== packages"
apt-get update -q
apt-get install -y -q git curl ca-certificates gnupg ufw fail2ban unattended-upgrades ffmpeg \
  python3 python3-venv python3-pip postgresql postgresql-contrib \
  debian-keyring debian-archive-keyring apt-transport-https >/dev/null

if ! command -v caddy >/dev/null 2>&1; then
  echo "== caddy"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy >/dev/null
fi

echo "== app user"
id -u $APP_USER >/dev/null 2>&1 || useradd -r -m -d $HOME_DIR -s /bin/bash $APP_USER
install -d -o $APP_USER -g $APP_USER $ROOT $ROOT/data $ROOT/backups
mkdir -p /etc/atlas
# the deploy timer runs as root inside the atlas user's checkout; git refuses that ("dubious ownership")
# unless the path is marked safe system-wide (systemd units do not read root's ~/.gitconfig)
git config --system --add safe.directory $ROOT/src >/dev/null 2>&1 || true

echo "== postgres"
systemctl enable --now postgresql >/dev/null
if [ -f "$ENVF" ] && grep -q '^DATABASE_URL=' "$ENVF"; then
  PGPASS="$(grep '^DATABASE_URL=' "$ENVF" | sed -E 's#.*://atlas:([^@]+)@.*#\1#')"
else
  PGPASS="$(rand 24)"
fi
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='atlas'" | grep -q 1 || \
  sudo -u postgres psql -qc "CREATE ROLE atlas LOGIN PASSWORD '$PGPASS'"
sudo -u postgres psql -qc "ALTER ROLE atlas PASSWORD '$PGPASS'"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='atlas'" | grep -q 1 || \
  sudo -u postgres psql -qc "CREATE DATABASE atlas OWNER atlas"

echo "== checkout + venv"
if [ -d $ROOT/src/.git ]; then
  sudo -u $APP_USER git -C $ROOT/src fetch -q origin "$BRANCH"
  sudo -u $APP_USER git -C $ROOT/src reset -q --hard "origin/$BRANCH"
else
  sudo -u $APP_USER git clone -q --branch "$BRANCH" "$REPO" $ROOT/src
fi
[ -d $ROOT/venv ] || sudo -u $APP_USER python3 -m venv $ROOT/venv
sudo -u $APP_USER $ROOT/venv/bin/pip install -q --upgrade pip
sudo -u $APP_USER $ROOT/venv/bin/pip install -q -r $ROOT/src/requirements.txt
if [ "$VISION" = "1" ]; then
  echo "== vision stack (CPU torch)"
  sudo -u $APP_USER $ROOT/venv/bin/pip install -q torch torchvision --index-url https://download.pytorch.org/whl/cpu
  sudo -u $APP_USER $ROOT/venv/bin/pip install -q -r $ROOT/src/requirements-vision.txt
fi

echo "== hermes agent (Nous Research), as $APP_USER"
HERMES_KEY=""
[ -f "$ENVF" ] && HERMES_KEY="$(grep '^HERMES_AGENT_KEY=' "$ENVF" | cut -d= -f2- || true)"
[ -n "$HERMES_KEY" ] || HERMES_KEY="atlas-$(rand 18)"
if [ ! -x $HOME_DIR/.local/bin/hermes ]; then
  # download first: `curl | bash </dev/null` would hand bash /dev/null instead of the script (curl exit 23)
  sudo -u $APP_USER -H bash -c "curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh && bash /tmp/hermes-install.sh --non-interactive </dev/null" \
    || echo "hermes installer returned non-zero - check output above"
fi
sudo -u $APP_USER -H bash -c "mkdir -p ~/.hermes && touch ~/.hermes/.env && chmod 600 ~/.hermes/.env"
HENV=$HOME_DIR/.hermes/.env
# Hermes's model calls go through the portal's metering proxy, so each one counts for the account whose run asked
# (usage spec section 1.8). Its .env holds the proxy token where a model key would go: Atlas's OpenRouter key never
# sits in the Hermes process (which runs shell tools), and a model call that bypasses the proxy fails with a 401
# instead of spending quietly. An install from before the proxy keeps its own key until it is switched (README).
PROXY_TOKEN=""
[ -f "$ENVF" ] && PROXY_TOKEN="$(grep '^USAGE_HERMES_PROXY_TOKEN=' "$ENVF" | cut -d= -f2- || true)"
if [ "$NEW_INSTALL" = 1 ] && [ -z "$PROXY_TOKEN" ]; then PROXY_TOKEN="$(rand 32)"; fi
if [ -n "$PROXY_TOKEN" ]; then
  setenv $HENV OPENROUTER_API_KEY "$PROXY_TOKEN"
elif [ -n "$OPENROUTER" ]; then
  setenv $HENV OPENROUTER_API_KEY "$OPENROUTER"
fi
setenv $HENV API_SERVER_ENABLED true
setenv $HENV API_SERVER_KEY "$HERMES_KEY"
setenv $HENV API_SERVER_HOST 127.0.0.1
setenv $HENV API_SERVER_PORT 8642
if [ ! -f $HOME_DIR/.hermes/config.yaml ]; then
  sudo -u $APP_USER tee $HOME_DIR/.hermes/config.yaml >/dev/null <<YAML
model:
  default: anthropic/claude-haiku-4.5
  provider: openrouter
  base_url: $PROXY_URL
agent:
  max_turns: 150
  verbose: false
terminal:
  backend: local
  timeout: 180
YAML
elif [ "$NEW_INSTALL" = 1 ] && ! grep -q "base_url: $PROXY_URL" $HOME_DIR/.hermes/config.yaml; then
  echo "   WARNING: ~/.hermes/config.yaml already existed: set model.base_url to $PROXY_URL by hand"
fi

echo "== $ENVF"
if [ ! -f "$ENVF" ]; then
  cat > "$ENVF" <<EOT
# Atlas portal runtime (read by atlas.service). Keep private.
DESK_MODE=live
DESK_PROVIDER=openrouter
OPENROUTER_API_KEY=$OPENROUTER
VISION_DEMO_KEY=$OPENROUTER
DESK_OPEN=$OPEN
DESK_SECRET=$(rand 32)
DESK_TEMPLATE=sales_desk
DESK_DEFAULT_ENGINE=hermes_agent
HERMES_AGENT_URL=http://127.0.0.1:8642
HERMES_AGENT_KEY=$HERMES_KEY
DATABASE_URL=postgresql://atlas:$PGPASS@127.0.0.1:5432/atlas
ATLAS_DATA_DIR=$ROOT/data
SPEND_CAP_USD=10
PORT=8094
# Hermes's own model calls (usage spec section 1.8). off: Atlas does not use its Hermes instance; specialists run on
# the built-in engine. proxy: Hermes's model calls go through /internal/llm/v1 and count per account. Hermes is wired
# to the proxy either way (~/.hermes/.env holds this token, config.yaml points at the proxy); switch to proxy after
# the README's Hermes checks, then systemctl restart atlas.
USAGE_HERMES_METER=$HERMES_METER
USAGE_HERMES_PROXY_TOKEN=$PROXY_TOKEN
# sign-in extras (atlas/desk/auth.py). Google: OAuth web client, redirect URI https://$HOST/auth/google/callback.
# Mail: the portal's own sender for 2FA codes (Gmail app password or Resend). TWO_FACTOR=auto|required|off.
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
ATLAS_SMTP_HOST=
ATLAS_SMTP_PORT=587
ATLAS_SMTP_USER=
ATLAS_SMTP_PASSWORD=
ATLAS_MAIL_FROM=
ATLAS_MAIL_FROM_NAME=Atlas Desks
TWO_FACTOR=auto
PUBLIC_URL=https://$HOST
PYTHONIOENCODING=utf-8
PYTHONUNBUFFERED=1
EOT
else
  echo "   keeping existing $ENVF"
  [ -z "$OPENROUTER" ] || setenv "$ENVF" OPENROUTER_API_KEY "$OPENROUTER"
  setenv "$ENVF" PUBLIC_URL "https://$HOST"
  if [ "$HERMES_METER" = proxy ] && [ -n "$PROXY_TOKEN" ]; then
    setenv "$ENVF" USAGE_HERMES_METER proxy
  elif [ "$HERMES_METER" = proxy ]; then
    echo "   --hermes-proxy: this install predates the metering proxy; switch Hermes by hand (README)"
  fi
fi
# root-only: systemd reads EnvironmentFile as root, so the service user never needs the file itself. Left group-readable,
# a model-written run_python script (which runs as that user) could read every key in it.
chown root:root "$ENVF"; chmod 600 "$ENVF"

# run_python sandbox: bubblewrap, plus the AppArmor profile Ubuntu 24.04 needs before an unprivileged user may
# create user namespaces (without it bwrap dies with "loopback: Failed RTM_NEWADDR: Operation not permitted")
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq bubblewrap >/dev/null
cat > /etc/apparmor.d/bwrap <<'EOF_AA'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF_AA
apparmor_parser -r /etc/apparmor.d/bwrap || true
grep -q '^ATLAS_SANDBOX=' "$ENVF" || echo 'ATLAS_SANDBOX=bwrap' >> "$ENVF"

# the session pool (atlas/desk/pool.py): Claude Code on the box, one runner per account, billed to ANTHROPIC_API_KEY in $ENVF
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs npm >/dev/null
command -v claude >/dev/null || npm i -g @anthropic-ai/claude-code >/dev/null 2>&1 || true
mkdir -p /srv/atlas/data/pool && chown atlas:atlas /srv/atlas/data/pool

# systemd sandbox for the portal process itself (live on atlas-1 since 17 Oct 2026): read-only system and home,
# writes only under /srv/atlas (+ gunicorn's control dir), no new privileges, private /tmp
mkdir -p /etc/systemd/system/atlas.service.d
cat > /etc/systemd/system/atlas.service.d/hardening.conf <<'EOF_HD'
[Service]
ProtectSystem=strict
ReadWritePaths=/srv/atlas /home/atlas/.gunicorn
ProtectHome=read-only
PrivateTmp=yes
NoNewPrivileges=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
EOF_HD
systemctl daemon-reload

echo "== systemd"
cat > /etc/systemd/system/hermes.service <<EOT
[Unit]
Description=Hermes Agent gateway (API server on 127.0.0.1:8642)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Environment=HOME=$HOME_DIR
Environment=PATH=$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin
WorkingDirectory=$HOME_DIR
ExecStart=$HOME_DIR/.local/bin/hermes gateway
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOT
cat > /etc/systemd/system/atlas.service <<EOT
[Unit]
Description=Atlas Desks portal (gunicorn on 127.0.0.1:8094)
After=network-online.target postgresql.service hermes.service
Wants=network-online.target

[Service]
User=$APP_USER
EnvironmentFile=$ENVF
WorkingDirectory=$ROOT/src
ExecStart=$ROOT/venv/bin/gunicorn -w 1 --threads 32 --timeout 120 -b 127.0.0.1:8094 atlas.desk.app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOT

echo "== auto-deploy (pull $BRANCH every minute, restart only when it moved)"
install -m 755 $ROOT/src/deploy/hetzner/deploy.sh /usr/local/bin/atlas-deploy
cat > /etc/systemd/system/atlas-deploy.service <<EOT
[Unit]
Description=Atlas pull-based deploy
[Service]
Type=oneshot
Environment=ATLAS_BRANCH=$BRANCH
ExecStart=/usr/local/bin/atlas-deploy
EOT
cat > /etc/systemd/system/atlas-deploy.timer <<EOT
[Unit]
Description=Check GitHub $BRANCH every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s
[Install]
WantedBy=timers.target
EOT

echo "== nightly backup (03:40 UTC, 30 days)"
install -m 755 $ROOT/src/deploy/hetzner/backup.sh /usr/local/bin/atlas-backup
cat > /etc/systemd/system/atlas-backup.service <<EOT
[Unit]
Description=Atlas nightly backup
[Service]
Type=oneshot
User=$APP_USER
EnvironmentFile=$ENVF
ExecStart=/usr/local/bin/atlas-backup
EOT
cat > /etc/systemd/system/atlas-backup.timer <<EOT
[Unit]
Description=Nightly Atlas backup
[Timer]
OnCalendar=*-*-* 03:40:00
Persistent=true
[Install]
WantedBy=timers.target
EOT

echo "== caddy ($HOST)"
install -d -o caddy -g caddy /var/log/caddy
cat > /etc/caddy/Caddyfile <<EOT
{
	# no admin API on loopback: it answers any local request with config read/write. Reload = systemctl restart caddy.
	admin off
}
$HOST {
	encode zstd gzip
	log {
		output file /var/log/caddy/access.log {
			roll_size 20MiB
			roll_keep 5
		}
	}
	# the Hermes metering proxy answers Hermes on this box only (usage spec section 1.8); the route refuses forwarded
	# requests too, this keeps it off the internet whatever the mode
	respond /internal/* 404
	reverse_proxy 127.0.0.1:8094 {
		flush_interval -1
		transport http {
			read_timeout 10m
		}
	}
	header {
		Strict-Transport-Security "max-age=31536000"
		-Server
	}
}
EOT

echo "== firewall + ssh hardening"
ufw --force reset >/dev/null
ufw default deny incoming; ufw default allow outgoing
ufw allow 22/tcp; ufw allow 80/tcp; ufw allow 443/tcp
ufw --force enable
cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'EOT'
PasswordAuthentication no
PermitRootLogin prohibit-password
KbdInteractiveAuthentication no
EOT
systemctl reload ssh || systemctl reload sshd || true
cat > /etc/fail2ban/jail.local <<'EOT'
[sshd]
enabled = true
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOT
systemctl enable --now fail2ban >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== start"
systemctl daemon-reload
systemctl enable --now hermes atlas atlas-deploy.timer atlas-backup.timer >/dev/null
systemctl restart hermes atlas
systemctl enable --now caddy >/dev/null; systemctl reload caddy || systemctl restart caddy
sleep 5
echo
echo "================================================================"
echo " portal: $(curl -s -m 10 http://127.0.0.1:8094/api/health || echo 'not answering yet - journalctl -u atlas -n 50')"
echo " hermes: $(curl -s -m 10 http://127.0.0.1:8642/health || echo 'not up yet - journalctl -u hermes -n 50')"
HMODE="$(grep '^USAGE_HERMES_METER=' "$ENVF" | cut -d= -f2- || true)"
HWIRE="own OpenRouter key (an install from before the proxy)"
[ -z "$PROXY_TOKEN" ] || HWIRE="metering proxy $PROXY_URL"
echo "         model calls: $HWIRE, USAGE_HERMES_METER=${HMODE:-off}"
echo " url:    https://$HOST   (cert fetched on first request)"
echo " env:    $ENVF   hermes: $HENV"
echo " logs:   journalctl -fu atlas | journalctl -fu hermes | journalctl -u atlas-deploy"
echo " deploy: git push to $BRANCH; the box pulls within a minute"
echo "================================================================"
