#!/usr/bin/env bash
# =============================================================================
# setup.sh — One-time server bootstrap for translate.streamworks.no
# Ubuntu 24.04 LTS | user: sw | Python 3.12 already installed
#
# Run as sw (with sudo rights):
#   bash setup.sh
#
# Edit PG_PASSWORD below before running.
# =============================================================================
set -euo pipefail

APP_USER="sw"
APP_DIR="/home/${APP_USER}/translation_app"
BACKEND_DIR="${APP_DIR}/backend"
DOMAIN="translate.streamworks.no"
PG_DB="translation_app"
PG_USER="translation_app"
PG_PASSWORD="CHANGE_TO_STRONG_PASSWORD"   # <-- change this before running

# =============================================================================

echo "=== [1/7] Install system packages ==="
sudo apt-get update -y
sudo apt-get install -y \
    nginx \
    postgresql \
    postgresql-contrib \
    certbot \
    python3-certbot-nginx \
    ufw \
    python3.12-venv \
    python3-pip \
    libpq-dev \
    git \
    curl \
    htop

echo "=== [2/7] Create directory structure ==="
mkdir -p "${BACKEND_DIR}/data/sessions"
mkdir -p "${BACKEND_DIR}/static"
mkdir -p "${BACKEND_DIR}/config"
mkdir -p "${BACKEND_DIR}/logs"

# Nginx (www-data) needs read access to static and data dirs
sudo usermod -aG "${APP_USER}" www-data
chmod 750 "${BACKEND_DIR}"
chmod -R 755 "${BACKEND_DIR}/static"
chmod -R 755 "${BACKEND_DIR}/data"

echo "=== [3/7] Python virtual environment ==="
cd "${BACKEND_DIR}"
python3.12 -m venv venv
./venv/bin/pip install --upgrade pip
echo "Venv ready at ${BACKEND_DIR}/venv"
echo "(Run: ./venv/bin/pip install -r requirements.txt after deploying code)"

echo "=== [4/7] PostgreSQL setup ==="
sudo systemctl enable --now postgresql

sudo -u postgres psql <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${PG_USER}') THEN
        CREATE ROLE ${PG_USER} LOGIN PASSWORD '${PG_PASSWORD}';
    END IF;
END
\$\$;
SELECT 'CREATE DATABASE' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${PG_DB}')\gexec
ALTER DATABASE ${PG_DB} OWNER TO ${PG_USER};
REVOKE ALL ON DATABASE ${PG_DB} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${PG_DB} TO ${PG_USER};
SQL

# Lock PostgreSQL to localhost only
PG_VER=$(ls /etc/postgresql/)
PG_CONF="/etc/postgresql/${PG_VER}/main/postgresql.conf"
sudo sed -i "s/^#*listen_addresses.*/listen_addresses = 'localhost'/" "${PG_CONF}"
sudo systemctl restart postgresql
echo "PostgreSQL ready. DB: ${PG_DB}, User: ${PG_USER}"

echo "=== [5/7] Nginx configuration ==="
sudo rm -f /etc/nginx/sites-enabled/default

NGINX_CONF="/etc/nginx/sites-available/${DOMAIN}"
if [ -f "${NGINX_CONF}" ]; then
    sudo ln -sf "${NGINX_CONF}" "/etc/nginx/sites-enabled/${DOMAIN}"
    sudo nginx -t
    sudo systemctl enable --now nginx
    sudo systemctl reload nginx
    echo "Nginx configured for ${DOMAIN}"
else
    echo "WARNING: ${NGINX_CONF} not found."
    echo "Deploy deploy/nginx/translate.streamworks.no to /etc/nginx/sites-available/ first, then:"
    echo "  sudo ln -s /etc/nginx/sites-available/${DOMAIN} /etc/nginx/sites-enabled/${DOMAIN}"
    echo "  sudo nginx -t && sudo systemctl reload nginx"
fi

echo "=== [6/7] Systemd service ==="
UNIT_FILE="/etc/systemd/system/translation-app.service"
if [ -f "${UNIT_FILE}" ]; then
    sudo systemctl daemon-reload
    sudo systemctl enable translation-app
    echo "Service registered. Start with: sudo systemctl start translation-app"
else
    echo "WARNING: ${UNIT_FILE} not found."
    echo "Deploy deploy/systemd/translation-app.service to /etc/systemd/system/, then:"
    echo "  sudo systemctl daemon-reload && sudo systemctl enable translation-app"
fi

echo "=== [7/7] Firewall ==="
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw allow 'Nginx Full'
sudo ufw allow 7070/tcp
sudo ufw allow 7070/udp
sudo ufw --force enable
sudo ufw status verbose

echo ""
echo "============================================================"
echo " Bootstrap complete. Manual steps remaining:"
echo ""
echo " 1. Deploy code:"
echo "    rsync -avz --exclude='venv/' --exclude='data/' --exclude='__pycache__/' \\"
echo "      ./backend/ sw@SERVER_IP:${BACKEND_DIR}/"
echo ""
echo " 2. Deploy Google credentials:"
echo "    scp backend/config/service-account-credentials.json \\"
echo "      sw@SERVER_IP:${BACKEND_DIR}/config/"
echo "    ssh sw@SERVER_IP 'chmod 600 ${BACKEND_DIR}/config/service-account-credentials.json'"
echo ""
echo " 3. Create .env (use deploy/env.template as guide):"
echo "    nano ${BACKEND_DIR}/.env"
echo "    chmod 600 ${BACKEND_DIR}/.env"
echo ""
echo " 4. Install Python deps:"
echo "    ${BACKEND_DIR}/venv/bin/pip install -r ${BACKEND_DIR}/requirements.txt"
echo ""
echo " 5. Start the app:"
echo "    sudo systemctl start translation-app"
echo "    sudo journalctl -u translation-app -f"
echo ""
echo " 6. When DNS for ${DOMAIN} is live:"
echo "    sudo certbot --nginx -d ${DOMAIN}"
echo "    Update BACKEND_URL in frontend/index.html to wss://translate.streamworks.no"
echo "    Redeploy: cp frontend/index.html ${BACKEND_DIR}/static/index.html"
echo "============================================================"
