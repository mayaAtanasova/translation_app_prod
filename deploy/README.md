# Server Deployment Guide — translate.streamworks.no

Ubuntu 24.04 | user: `sw` | Python 3.12.3

---

## Stack

```
Internet (HTTP → HTTPS after certbot)
    ↓
Nginx :80/:443   — reverse proxy, static files, WebSocket upgrade
    ↓
uvicorn :8000    — FastAPI app (systemd service, 127.0.0.1 only)
    ↓
PostgreSQL       — installed, not yet wired (Phase 2)
```

---

## Files in this directory

| File | Deploy to |
|------|-----------|
| `nginx/translate.streamworks.no` | `/etc/nginx/sites-available/translate.streamworks.no` |
| `systemd/translation-app.service` | `/etc/systemd/system/translation-app.service` |
| `env.template` | Copy to `/home/sw/translation_app/backend/.env`, fill in values |
| `setup.sh` | Run once on the Ubuntu server |

---

## Phase 1: HTTP deployment (no DNS yet)

### Step 1 — Prepare files locally

```bash
# Update frontend to point at the new server (HTTP for now)
# In frontend/index.html, change:
#   const BACKEND_URL = 'ws://translate.streamworks.no';
# Then copy into the static directory:
cp frontend/index.html backend/static/index.html
```

### Step 2 — Copy deploy files to the server

```bash
# Copy nginx config
scp deploy/nginx/translate.streamworks.no \
    sw@SERVER_IP:/tmp/translate.streamworks.no
ssh sw@SERVER_IP "sudo mv /tmp/translate.streamworks.no \
    /etc/nginx/sites-available/translate.streamworks.no"

# Copy systemd unit
scp deploy/systemd/translation-app.service \
    sw@SERVER_IP:/tmp/translation-app.service
ssh sw@SERVER_IP "sudo mv /tmp/translation-app.service \
    /etc/systemd/system/translation-app.service"
```

### Step 3 — Run bootstrap script

```bash
# Edit PG_PASSWORD in setup.sh first!
scp deploy/setup.sh sw@SERVER_IP:~/
ssh sw@SERVER_IP "bash ~/setup.sh"
```

### Step 4 — Deploy application code

```bash
rsync -avz \
  --exclude='venv/' \
  --exclude='data/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.env' \
  ./backend/ sw@SERVER_IP:/home/sw/translation_app/backend/
```

### Step 5 — Deploy secrets (never via git)

```bash
# Google Cloud credentials
scp backend/config/service-account-credentials.json \
    sw@SERVER_IP:/home/sw/translation_app/backend/config/

ssh sw@SERVER_IP "chmod 600 /home/sw/translation_app/backend/config/service-account-credentials.json"
```

### Step 6 — Create .env on server

```bash
ssh sw@SERVER_IP
nano /home/sw/translation_app/backend/.env
# Paste and fill in values from deploy/env.template
chmod 600 /home/sw/translation_app/backend/.env
```

### Step 7 — Install dependencies and start

```bash
ssh sw@SERVER_IP

/home/sw/translation_app/backend/venv/bin/pip install \
    -r /home/sw/translation_app/backend/requirements.txt

sudo systemctl start translation-app
sudo systemctl status translation-app

# Watch live logs:
sudo journalctl -u translation-app -f
```

### Step 8 — Verify

```bash
# Health check (through Nginx)
curl http://SERVER_IP/

# Check static files served by Nginx (not FastAPI):
curl -I http://SERVER_IP/static/index.html
# Header should show: Server: nginx

# WebSocket (needs wscat: npm install -g wscat)
wscat -c ws://SERVER_IP/ws/sv-SE
# Send: ping  → Expect: pong
```

---

## Phase 2: HTTPS (after DNS is configured)

### DNS setup at registrar

Add an A record for `translate.streamworks.no` pointing to the server's public IP.

> **Static IP:** Check if the Ubuntu machine has a static public IP. If not, either assign one (check with your ISP/hosting provider) or the IP may change and break DNS. Confirm before pointing the domain.

Verify propagation:
```bash
dig translate.streamworks.no A
# Must return your server IP
```

### Get TLS certificate (Let's Encrypt — free)

```bash
ssh sw@SERVER_IP
sudo certbot --nginx -d translate.streamworks.no
```

Certbot will:
- Obtain the certificate automatically
- Rewrite the Nginx config to add HTTPS and redirect HTTP→HTTPS
- Set up auto-renewal via a systemd timer

Verify auto-renewal:
```bash
sudo certbot renew --dry-run
```

### After HTTPS is live

1. Update `CORS_ALLOWED_ORIGINS` in `/home/sw/translation_app/backend/.env`:
   ```
   CORS_ALLOWED_ORIGINS=https://translate.streamworks.no
   ```

2. Update `frontend/index.html`:
   ```javascript
   const BACKEND_URL = 'wss://translate.streamworks.no';
   ```

3. Redeploy frontend:
   ```bash
   cp frontend/index.html backend/static/index.html
   rsync backend/static/index.html sw@SERVER_IP:/home/sw/translation_app/backend/static/
   ```

4. Restart the app:
   ```bash
   ssh sw@SERVER_IP "sudo systemctl restart translation-app"
   ```

---

## Common operations

```bash
# View app logs
sudo journalctl -u translation-app -f

# Restart app
sudo systemctl restart translation-app

# Reload Nginx config (no downtime)
sudo nginx -t && sudo systemctl reload nginx

# Check listening ports
ss -tlnp | grep -E '80|443|8000'

# Check disk usage (audio files accumulate)
du -sh /home/sw/translation_app/backend/data/
```

---

## Important constraints

- **`--workers 1` is required** on the systemd service. The app uses in-process Python dicts for WebSocket connections and session state. Multiple workers = broken broadcasts. This will change in Phase 2 when state moves to PostgreSQL.
- **Port 8000 is NOT exposed externally** — firewall blocks it. All traffic goes through Nginx on 80/443.
- **Credentials files** (`service-account-credentials.json`, `.env`) are never committed to git. Always deploy manually.
