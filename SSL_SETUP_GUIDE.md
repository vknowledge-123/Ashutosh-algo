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

---

## Step 6: Deploy the App Code

Clone the repository and install Python dependencies:

```bash
cd /opt
sudo git clone <YOUR_REPO_URL> trading-app
sudo chown -R $USER:$USER /opt/trading-app
cd /opt/trading-app

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

If your app uses a `.env` file, create it now.

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

Check logs if needed:

```bash
sudo journalctl -u trading -f
```

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
