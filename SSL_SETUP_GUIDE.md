# GCP Deployment Guide

This guide shows how to deploy the FastAPI trading app on Google Cloud without a domain name, using:

- A Compute Engine VM
- A reserved static external IP
- HTTPS on the public IP address
- nginx as reverse proxy
- `systemd` for process management

You will access the app with an IP URL such as:

```text
https://YOUR_STATIC_IP/?user_id=1
```

Your webhook URL will be:

```text
https://YOUR_STATIC_IP/webhook/chartink?user_id=1
```

## Beginner Defaults

If you are just starting, use these exact values:

- GCP region: `asia-south1` (Mumbai)
- GCP zone: `asia-south1-a`
- VM name: `trading-vm`
- Static IP name: `trading-ip`
- Firewall tag: `trading-server`
- Machine type: `e2-medium`
- OS image: `Ubuntu 22.04 LTS`
- App folder on server: `/opt/trading-app`

Your GitHub clone URL for this project:

```text
https://github.com/vknowledge-123/saurabmisra.git
```

If you follow this guide exactly, you only need to replace these values later:

- `YOUR_STATIC_IP`
- `YOUR_PUBLIC_IP`
- `YOUR_EMAIL`
- `YOUR_LINUX_USER`

## Important Notes

1. You do not need a domain for this setup.
2. As of January 15, 2026, Let's Encrypt supports public IP address certificates.
3. IP certificates are short-lived, about 6 days, so auto-renewal is mandatory.
4. Google-managed SSL certificates for load balancers are still domain/DNS based. For an IP-only setup, terminate TLS directly on the VM with nginx.
5. If a third-party webhook provider refuses bare-IP URLs for policy reasons, you will still need a domain even if HTTPS itself is valid.

---

## Recommended Architecture

```text
Internet
   |
HTTPS :443
   |
GCP Static External IP
   |
nginx
   |
http://127.0.0.1:8000
   |
uvicorn app.main:app
```

---

## Step 1: Reserve a Static External IP

If you are a beginner, the easiest path is:

1. Open GCP Console.
2. Create a project if you do not already have one.
3. Enable billing for the project.
4. Open `Compute Engine`.
5. Click `VM instances` and enable the API if prompted.
6. Open `VPC network` -> `IP addresses`.
7. Reserve a new static external IP named `trading-ip` in region `asia-south1`.

Choose your region first. Example:

- Region: `asia-south1`
- Zone: `asia-south1-a`

Reserve the IP:

```bash
gcloud compute addresses create trading-ip \
  --region=asia-south1
```

Get the reserved IP:

```bash
gcloud compute addresses describe trading-ip \
  --region=asia-south1 \
  --format="get(address)"
```

Save that value as `YOUR_STATIC_IP`.

---

## Step 2: Create the VM and Attach the Static IP

### GCP Console Method

1. Open `Compute Engine` -> `VM instances`.
2. Click `Create Instance`.
3. Set `Name` to `trading-vm`.
4. Set `Region` to `Mumbai (asia-south1)`.
5. Set `Zone` to `asia-south1-a`.
6. Set `Machine configuration` to `E2`.
7. Set `Machine type` to `e2-medium`.
8. Under `Boot disk`, click `Change`.
9. Choose `Ubuntu`.
10. Choose `Ubuntu 22.04 LTS`.
11. Set disk size to `30 GB`.
12. Under `Networking`, choose the reserved external IP `trading-ip`.
13. In `Network tags`, add `trading-server`.
14. Click `Create`.

Wait until the VM status shows `Running`.

Create an Ubuntu VM:

```bash
gcloud compute instances create trading-vm \
  --zone=asia-south1-a \
  --machine-type=e2-medium \
  --address=YOUR_STATIC_IP \
  --tags=trading-server \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB
```

If the VM already exists, you can reassign the reserved IP later from the GCP console or by removing the old access config and adding the reserved address.

---

## Step 3: Open Firewall Ports

### GCP Console Method

1. Open `VPC network` -> `Firewall`.
2. Click `Create Firewall Rule`.
3. Create one rule named `trading-allow-web`.
4. Direction: `Ingress`.
5. Targets: `Specified target tags`.
6. Target tags: `trading-server`.
7. Source IPv4 ranges: `0.0.0.0/0`.
8. Protocols and ports: choose `Specified protocols and ports`.
9. Enter `tcp:80,tcp:443`.
10. Save.

Now create another rule for SSH:

1. Click `Create Firewall Rule`.
2. Name: `trading-allow-ssh`.
3. Direction: `Ingress`.
4. Targets: `Specified target tags`.
5. Target tags: `trading-server`.
6. Source IPv4 ranges: your own internet IP with `/32`.
7. Protocols and ports: `tcp:22`.
8. Save.

Allow web traffic:

```bash
gcloud compute firewall-rules create trading-allow-web \
  --allow=tcp:80,tcp:443 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=trading-server
```

Allow SSH. Replace `YOUR_PUBLIC_IP` with your office/home public IP:

```bash
gcloud compute firewall-rules create trading-allow-ssh \
  --allow=tcp:22 \
  --source-ranges=YOUR_PUBLIC_IP/32 \
  --target-tags=trading-server
```

If you need temporary open SSH access for setup, you can widen it and tighten it later.

---

## Step 4: SSH Into the VM

### GCP Console Method

1. Go to `Compute Engine` -> `VM instances`.
2. Find `trading-vm`.
3. Click the `SSH` button in the row.
4. A browser terminal will open.

From now on, almost everything below runs inside that SSH terminal.

```bash
gcloud compute ssh trading-vm --zone=asia-south1-a
```

---

## Step 5: Install System Packages

On the VM:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git nginx redis-server snapd
```

Install Certbot from snap so you get a recent enough version for IP certificates:

```bash
sudo snap install core
sudo snap refresh core
sudo snap install --classic certbot
sudo ln -sf /snap/bin/certbot /usr/bin/certbot
certbot --version
```

You can also check Redis and nginx status:

```bash
sudo systemctl status redis-server
sudo systemctl status nginx
```

---

## Step 6: Deploy the App Code

Clone the repository and install Python dependencies:

```bash
cd /opt
sudo git clone https://github.com/vknowledge-123/saurabmisra.git trading-app
sudo chown -R $USER:$USER /opt/trading-app
cd /opt/trading-app

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

If your app uses a `.env` file, create it now.

Create a starter `.env` file:

```bash
cat > /opt/trading-app/.env <<'EOF'
ALLOWED_HOSTS=YOUR_STATIC_IP,localhost,127.0.0.1
EOF
```

You can edit it later with:

```bash
nano /opt/trading-app/.env
```

For IP-based access, if you set `ALLOWED_HOSTS`, include the public IP:

```env
ALLOWED_HOSTS=YOUR_STATIC_IP,localhost,127.0.0.1
```

If you leave `ALLOWED_HOSTS` unset, the app currently defaults to `*`.

---

## Step 7: Test the App on Localhost

Start the app once to verify it boots:

```bash
cd /opt/trading-app
source .venv/bin/activate
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

From the VM:

```bash
curl http://127.0.0.1:8000/
```

Stop it after the test.

To stop it, press:

```text
Ctrl+C
```

---

## Step 8: Create a systemd Service

Create the service file:

```bash
sudo tee /etc/systemd/system/trading.service > /dev/null <<'EOF'
[Unit]
Description=Trading FastAPI App
After=network.target redis-server.service

[Service]
User=YOUR_LINUX_USER
Group=YOUR_LINUX_USER
WorkingDirectory=/opt/trading-app
EnvironmentFile=-/opt/trading-app/.env
ExecStart=/opt/trading-app/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

Replace `YOUR_LINUX_USER` with your Linux username, for example:

```ini
User=ubuntu
Group=ubuntu
```

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable redis-server
sudo systemctl enable trading
sudo systemctl start trading
sudo systemctl status trading
```

If the service does not start, inspect the logs:

```bash
sudo journalctl -u trading -n 100 --no-pager
```

Check logs if needed:

```bash
sudo journalctl -u trading -f
```

## Step 8A: Make `REFRESH SYSTEM` Work on GCP

On GCP, the dashboard button calls:

```text
POST /api/service/restart
```

If you want that button to restart your real Linux services, do not leave it on the default fallback mode. Configure it explicitly.

### 1. Enable restart from the app environment

Add these lines to `/opt/trading-app/.env`:

```env
ENABLE_SERVICE_RESTART=1
SERVICE_RESTART_TOKEN=replace-with-a-long-random-secret
TRADING_RESTART_CMD=/usr/bin/sudo /usr/local/bin/restart-trading-stack.sh
```

If you need to restart more than one unit, also add:

```env
TRADING_STACK_UNITS=trading.service redis-server.service
```

### 2. Install the helper script as a root-owned file

Copy the repository helper script into a root-owned location:

```bash
sudo cp /opt/trading-app/restart_trading_stack.sh /usr/local/bin/restart-trading-stack.sh
sudo chown root:root /usr/local/bin/restart-trading-stack.sh
sudo chmod 750 /usr/local/bin/restart-trading-stack.sh
```

Important:

- Do not grant `sudo` access to a script that is still writable by your app user.
- Keeping it root-owned prevents privilege escalation.

### 3. Allow only that one command through sudo

Replace `YOUR_LINUX_USER` with the same Linux user used in `trading.service`:

```bash
echo 'YOUR_LINUX_USER ALL=(root) NOPASSWD: /usr/local/bin/restart-trading-stack.sh' | sudo tee /etc/sudoers.d/trading-restart
sudo chmod 440 /etc/sudoers.d/trading-restart
sudo visudo -cf /etc/sudoers.d/trading-restart
```

### 4. Reload and restart the trading app once

```bash
sudo systemctl daemon-reload
sudo systemctl restart trading.service
```

### 5. Test the same command manually before using the button

Run this as your app user:

```bash
sudo /usr/local/bin/restart-trading-stack.sh
```

If you configured multiple units:

```bash
sudo TRADING_STACK_UNITS="trading.service redis-server.service" /usr/local/bin/restart-trading-stack.sh
```

Only after this works should you use `REFRESH SYSTEM` in the dashboard.

### 6. What the button now tells you

The backend now checks whether the restart command exited successfully. If `systemctl` or `sudo` is denied, the API response will return an error instead of a false success message.

--- 

## Step 9: Configure nginx for HTTP First

Create a folder for ACME challenges:

```bash
sudo mkdir -p /var/www/certbot
sudo chown -R www-data:www-data /var/www/certbot
```

Create an HTTP-only nginx config first:

```bash
sudo tee /etc/nginx/sites-available/trading > /dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name YOUR_STATIC_IP;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF
```

Replace `YOUR_STATIC_IP` in the file, then enable the site:

```bash
sudo ln -sf /etc/nginx/sites-available/trading /etc/nginx/sites-enabled/trading
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
```

Now test:

```bash
curl http://YOUR_STATIC_IP/
```

If this works in your browser, your app is publicly reachable on normal HTTP.

---

## Step 10: Obtain a Trusted SSL Certificate for the IP Address

Use Certbot in `webroot` mode.

Important:

- Use a recent Certbot version.
- For IP certificates, the `nginx` installer plugin is not the path to use here.
- Keep port 80 open during validation.

Run:

```bash
sudo certbot certonly \
  --webroot \
  --webroot-path /var/www/certbot \
  --ip-address YOUR_STATIC_IP \
  --preferred-profile shortlived \
  -m YOUR_EMAIL \
  --agree-tos
```

Certificates will be stored at:

```text
/etc/letsencrypt/live/YOUR_STATIC_IP/fullchain.pem
/etc/letsencrypt/live/YOUR_STATIC_IP/privkey.pem
```

If issuance fails:

- Make sure port `80` is reachable from the internet.
- Make sure nginx is serving `/.well-known/acme-challenge/`.
- Make sure `certbot --version` is recent enough.

If Certbot prints an error, do not continue to HTTPS until that step succeeds.

---

## Step 11: Switch nginx to HTTPS

Replace the nginx config with:

```bash
sudo tee /etc/nginx/sites-available/trading > /dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name YOUR_STATIC_IP;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name YOUR_STATIC_IP;

    ssl_certificate /etc/letsencrypt/live/YOUR_STATIC_IP/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/YOUR_STATIC_IP/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_timeout 10m;
    ssl_session_cache shared:SSL:10m;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;

    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
        proxy_buffering off;
    }
}
EOF
```

Replace `YOUR_STATIC_IP` in the file, then reload nginx:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

## Step 12: Verify HTTPS

Test in browser:

```text
https://YOUR_STATIC_IP/?user_id=1
```

Test from terminal:

```bash
curl -I https://YOUR_STATIC_IP/
```

Open in browser:

```text
https://YOUR_STATIC_IP/?user_id=1
```

If the browser opens without a certificate warning, SSL is working.

If you want to inspect the presented certificate:

```bash
echo | openssl s_client -connect YOUR_STATIC_IP:443 -servername YOUR_STATIC_IP
```

---

## Step 13: Configure Automatic Certificate Renewal

IP certificates are short-lived, so do not skip this step.

Create a deploy hook so nginx reloads after renewal:

```bash
sudo mkdir -p /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh > /dev/null <<'EOF'
#!/bin/sh
systemctl reload nginx
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
```

Enable Certbot's timer:

```bash
sudo systemctl enable --now snap.certbot.renew.timer
sudo systemctl status snap.certbot.renew.timer
```

Test renewal:

```bash
sudo certbot renew --dry-run
```

---

## URLs to Use

Dashboard:

```text
https://YOUR_STATIC_IP/?user_id=1
```

Webhook:

```text
https://YOUR_STATIC_IP/webhook/chartink?user_id=1
```

API examples:

```text
https://YOUR_STATIC_IP/api/alerts?user_id=1
https://YOUR_STATIC_IP/api/positions?user_id=1
```

---

## Troubleshooting

### 1. Browser says certificate is invalid

Check:

- `sudo certbot certificates`
- `sudo nginx -t`
- `sudo systemctl status nginx`
- `echo | openssl s_client -connect YOUR_STATIC_IP:443 -servername YOUR_STATIC_IP`

### 2. Certbot cannot validate the IP

Check:

- Port `80` is allowed in GCP firewall
- nginx is running
- `curl http://YOUR_STATIC_IP/.well-known/acme-challenge/test`
- The instance is actually using the reserved static IP

### 3. Dashboard opens but API/websocket fails

Check:

- `sudo journalctl -u trading -f`
- `sudo tail -f /var/log/nginx/error.log`
- App is listening on `127.0.0.1:8000`

### 4. Host header blocked

If you configure `ALLOWED_HOSTS`, include:

```env
ALLOWED_HOSTS=YOUR_STATIC_IP,localhost,127.0.0.1
```

### 5. Third-party webhook still rejects IP URL

That is usually a provider policy issue, not a TLS issue. In that case use:

- A real domain name
- The same nginx reverse proxy flow
- A normal domain-based Let's Encrypt certificate

---

## Simple Fallback Option

If you only want browser testing and do not need a publicly trusted certificate, you can still use the local self-signed flow from this repository:

```bash
python generate_ssl_cert.py
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --ssl-keyfile=ssl_key.pem --ssl-certfile=ssl_cert.pem
```

Use this only for testing. For production webhooks, prefer a trusted certificate.

---

## Beginner Checklist

Use this as your quick checklist:

1. Create GCP project.
2. Enable billing.
3. Reserve static IP `trading-ip` in Mumbai region `asia-south1`.
4. Create VM `trading-vm` in zone `asia-south1-a`.
5. Attach static IP to VM.
6. Add network tag `trading-server`.
7. Open firewall ports `80`, `443`, and `22`.
8. SSH into the VM.
9. Install packages.
10. Clone `https://github.com/vknowledge-123/saurabmisra.git` into `/opt/trading-app`.
11. Create Python virtual environment.
12. Install Python requirements.
13. Create `.env` with `ALLOWED_HOSTS=YOUR_STATIC_IP,localhost,127.0.0.1`.
14. Test app locally with Uvicorn.
15. Create `systemd` service.
16. Configure nginx for HTTP.
17. Test `http://YOUR_STATIC_IP/`.
18. Run Certbot for the IP certificate.
19. Configure nginx for HTTPS.
20. Test `https://YOUR_STATIC_IP/?user_id=1`.
21. Enable certificate auto-renewal.
