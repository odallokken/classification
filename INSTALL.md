# Install guide — Ubuntu Server (step by step)

This guide walks through deploying the Pexip classification policy server
on a fresh **Ubuntu Server 22.04 LTS or 24.04 LTS** host. It is written
for someone who has never set up a Python web service before — every
command is shown in full, including the directory you should be in when
running it.

> **Time required:** about 20–30 minutes.
> **Prerequisites:** an Ubuntu server you can SSH into as a user with
> `sudo` rights, and network reachability between this server and your
> Pexip Conferencing Nodes (TCP 443 outbound to the nodes, and TCP 8080
> inbound from the nodes).

---

## 0. Decide the basics before you start

You will need to know:

| Setting | What to put here | Example |
|---|---|---|
| **Hostname / IP of this Ubuntu server** | Where Pexip will send policy requests. | `policy.example.com` or `10.0.0.50` |
| **Pexip Conferencing Node hostname** | Any one of your Pexip nodes. The Client API is reachable on every node. | `conf01.example.com` |
| **Default classification level** | Used when a caller's domain is not in the table yet. Must be an integer in the range `1`–`5`. | `1` |

Write these down — you'll paste them into a config file in step 5.

---

## 1. Update the operating system

Log in to the server and bring the package list up to date.

```bash
sudo apt update
sudo apt upgrade -y
```

If the kernel was upgraded, reboot:

```bash
sudo reboot
```

Log back in once it's up.

---

## 2. Install the system packages

The policy server needs Python 3, `pip`, the `venv` module, `git`, and
(optionally) Nginx as a TLS-terminating reverse proxy.

```bash
sudo apt install -y python3 python3-venv python3-pip git nginx ufw
```

Verify Python is at least 3.10:

```bash
python3 --version
```

Expected output (something like): `Python 3.10.12` or `Python 3.12.x`.

---

## 3. Create a dedicated service user

Running web services as your personal user is a bad habit. Create a
locked-down `pexippolicy` system user that owns the application files.

```bash
sudo useradd --system --create-home --home-dir /opt/pexip-policy \
             --shell /usr/sbin/nologin pexippolicy
```

Verify it exists:

```bash
id pexippolicy
```

You should see something like `uid=998(pexippolicy) gid=998(pexippolicy) ...`.

---

## 4. Get the code

Clone the repository into the service user's home directory.

```bash
sudo -u pexippolicy git clone https://github.com/odallokken/classification.git \
     /opt/pexip-policy/app
```

> **No internet access on the server?** Clone the repo on your laptop,
> tar it up (`tar czf classification.tgz classification/`), copy it
> over with `scp`, and `tar xzf` it into `/opt/pexip-policy/app` —
> then run `sudo chown -R pexippolicy:pexippolicy /opt/pexip-policy/app`.

---

## 5. Create a Python virtual environment and install dependencies

```bash
sudo -u pexippolicy python3 -m venv /opt/pexip-policy/venv
sudo -u pexippolicy /opt/pexip-policy/venv/bin/pip install --upgrade pip
sudo -u pexippolicy /opt/pexip-policy/venv/bin/pip install \
     -r /opt/pexip-policy/app/requirements.txt gunicorn
```

`gunicorn` is the production WSGI server we'll run the Flask app under.

Quick sanity check — the app should import without errors:

```bash
cd /opt/pexip-policy/app
sudo -u pexippolicy /opt/pexip-policy/venv/bin/python \
     -c "from app import create_app; create_app(); print('OK')"
```

If you see `OK`, you're good. (If you see `No module named app`,
double-check that you ran the command from inside
`/opt/pexip-policy/app`.)

---

## 6. Write the configuration file

We'll keep secrets out of the systemd unit by putting them in
`/etc/pexip-policy.env`. This file is owned by root and only the
service user can read it.

```bash
sudo tee /etc/pexip-policy.env > /dev/null <<'EOF'
# --- Database ---
POLICY_DB_PATH=/var/lib/pexip-policy/policy.db

# --- Default classification when caller's domain is not configured ---
# Must be an integer in the range 1..5 (1 = lowest / most permissive,
# 5 = highest / most restrictive). Defaults to 1 so unknown callers
# always pull the meeting down to the most permissive level.
DEFAULT_CLASSIFICATION_LEVEL=1

# --- Pexip Client API target ---
# Hostname of any one of your Pexip Conferencing Nodes.
PEXIP_NODE=conf01.example.com

# Display name shown in the participant list when the policy server joins.
# The bot stays in every meeting (refreshing its token automatically) so
# the classification banner and elapsed timer remain set for the whole
# conference. Host PINs are not configured here — the bot joins without a
# PIN, which works for unprotected meetings and PIN-protected meetings
# that allow guests.
PEXIP_PS_DISPLAY_NAME=Policy Server

# Set to false ONLY if your Pexip nodes use a self-signed certificate
# AND you accept the security implications.
PEXIP_VERIFY_TLS=true

# Client API HTTP timeout, seconds.
PEXIP_HTTP_TIMEOUT=10

# Master switch — set to false to disable Client API calls entirely
# (the policy server will still classify, but won't apply the timer
# or set_classification_level).
ENABLE_CLIENT_API=true
EOF

sudo chown root:pexippolicy /etc/pexip-policy.env
sudo chmod 640 /etc/pexip-policy.env
```

Edit the file with `sudo nano /etc/pexip-policy.env` and replace
`conf01.example.com` with your real value.

---

## 7. Create the data directory

The SQLite database lives outside the source tree so that a future
`git pull` upgrade never touches it.

```bash
sudo mkdir -p /var/lib/pexip-policy
sudo chown pexippolicy:pexippolicy /var/lib/pexip-policy
sudo chmod 750 /var/lib/pexip-policy
```

---

## 8. Create the systemd service

```bash
sudo tee /etc/systemd/system/pexip-policy.service > /dev/null <<'EOF'
[Unit]
Description=Pexip Infinity classification policy server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pexippolicy
Group=pexippolicy
WorkingDirectory=/opt/pexip-policy/app
EnvironmentFile=/etc/pexip-policy.env
ExecStart=/opt/pexip-policy/venv/bin/gunicorn \
          --workers 4 \
          --bind 127.0.0.1:8080 \
          --access-logfile - \
          --error-logfile - \
          app:app
Restart=on-failure
RestartSec=5

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/pexip-policy
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictNamespaces=true
RestrictRealtime=true
LockPersonality=true
MemoryDenyWriteExecute=true

[Install]
WantedBy=multi-user.target
EOF
```

Reload systemd, enable on boot, and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pexip-policy
```

Confirm it's running:

```bash
sudo systemctl status pexip-policy --no-pager
```

You should see `Active: active (running)` and four `gunicorn` worker
lines below.

Quick smoke test (Gunicorn is bound to localhost only at this stage):

```bash
curl -s http://127.0.0.1:8080/healthz
```

Expected: `{"status":"ok"}`.

---

## 9. Put Nginx in front (TLS termination)

Pexip will send policy requests over HTTPS, so we need Nginx in front of
Gunicorn with a real certificate. We'll use Let's Encrypt; if you have
your own certificate, skip the `certbot` step and drop your cert/key
into `/etc/ssl/...` instead.

### 9a. Replace the default Nginx site

```bash
sudo rm -f /etc/nginx/sites-enabled/default

sudo tee /etc/nginx/sites-available/pexip-policy > /dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name policy.example.com;       # <- change me

    # certbot will manage this block once we run it.
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name policy.example.com;       # <- change me

    # Filled in by certbot after step 9b.
    ssl_certificate     /etc/letsencrypt/live/policy.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/policy.example.com/privkey.pem;

    # Do NOT add `ssl_protocols` or `ssl_prefer_server_ciphers` here.
    # After step 9b, certbot adds `include /etc/letsencrypt/options-ssl-nginx.conf;`
    # which already sets both. Declaring them again makes `nginx -t` fail with
    # `"ssl_prefer_server_ciphers" directive is duplicate`. If you need to
    # override TLS settings, edit `/etc/letsencrypt/options-ssl-nginx.conf`
    # (or remove the include and set them here instead — pick one place).
    add_header Strict-Transport-Security "max-age=31536000" always;

    client_max_body_size 1m;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/pexip-policy /etc/nginx/sites-enabled/
```

Replace `policy.example.com` (twice) with the FQDN you want Pexip to
talk to.

### 9b. Get a TLS certificate

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d policy.example.com   # <- change me
```

Follow the prompts (email, accept ToS, redirect HTTP→HTTPS). Certbot
will edit the Nginx config and reload it.

### 9c. Test Nginx

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -sk https://policy.example.com/healthz
```

Expected: `{"status":"ok"}`.

---

## 10. Open the firewall

Allow SSH (so you don't lock yourself out) and HTTPS:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
sudo ufw status
```

> Lock down further if you can: replace `Nginx Full` with explicit
> `sudo ufw allow from <pexip-node-ip> to any port 443 proto tcp` rules
> for each Conferencing Node.

---

## 11. Configure the admin UX login (recommended)

The admin page at `https://policy.example.com/` has no built-in
authentication — anyone who can reach it can change classification
mappings. The simplest hardening is HTTP Basic Auth in Nginx,
restricted to the admin URLs (so Pexip's policy calls aren't blocked).

```bash
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/pexip-policy.htpasswd admin
# Enter and confirm a strong password when prompted.
sudo chown root:www-data /etc/nginx/pexip-policy.htpasswd
sudo chmod 640 /etc/nginx/pexip-policy.htpasswd
```

Edit `/etc/nginx/sites-available/pexip-policy` and add **inside the
`server { listen 443 ... }` block, before the existing `location /`**:

```nginx
location = / {
    auth_basic           "Pexip Policy Admin";
    auth_basic_user_file /etc/nginx/pexip-policy.htpasswd;
    proxy_pass           http://127.0.0.1:8080;
    include              /etc/nginx/proxy_params;
}
location /api/domains {
    auth_basic           "Pexip Policy Admin";
    auth_basic_user_file /etc/nginx/pexip-policy.htpasswd;
    proxy_pass           http://127.0.0.1:8080;
    include              /etc/nginx/proxy_params;
}
```

Reload:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Now `/` and `/api/domains` require a username and password, while
`/policy/v1/...` (called by Pexip) and `/healthz` remain open.

---

## 12. Tell Pexip Infinity about the policy server

In the Pexip Management Node admin UI:

1. Go to **Platform → Policy profiles → Add policy profile**
   (or **Platform → External policy** depending on Pexip version).
2. Name it `Domain classification`.
3. Set **Service URL** to:
   `https://policy.example.com/policy/v1`
   *(Pexip appends `/service/configuration` and
   `/participant/properties` automatically.)*
4. Tick the request types you want this server to handle:
   * **Service configuration** ✅
   * **Participant properties** ✅
   * Leave the others unticked.
5. **Save**.
6. Apply the policy profile to the relevant **System location(s)** or
   **Conferencing Node(s)**: edit the location/node, set
   **Policy profile** to `Domain classification`, save, and apply.

Make sure the **Classification levels** scheme on Pexip already defines
the integer levels `1`–`5` that this policy server uses — otherwise
`set_classification_level` will be rejected by the Client API. Those
levels are defined in the **theme** applied to the conference (the
`classification.levels` object inside `themeconfig.json`). A ready-to-use
example bundled with this repo is
[`examples/themeconfig.json`](examples/themeconfig.json) — zip it (so
`themeconfig.json` is at the top of the archive) and upload it via
**Services → Themes → Add theme**, then apply that theme to the relevant
VMRs / Call Routing Rules (or make it the global default theme). See the
[README "Pexip theme (classification banner)" section](README.md#pexip-theme-classification-banner)
for the full mapping and field reference.

> **How the meeting's level evolves.** The classification stored at
> meeting creation is determined by the first caller's domain. As
> additional participants join, the policy server re-evaluates the
> meeting's classification on each `participant/properties` callback
> and sets it to the **lowest** level across every joined participant's
> domain — so admitting a less-trusted party declassifies the meeting
> to their level. A more-trusted participant joining never silently
> raises the classification.

---

## 13. Add your first domain mapping

In a browser, open `https://policy.example.com/`. Log in with the admin
credentials from step 11. You should see the empty mappings table.

Add one entry, for example:

| Domain | Classification level | Label |
|---|---|---|
| `example.com` | `1` | `Official` |

Click **Save**. The row appears in the table.

---

## 14. Test end-to-end

1. Place a call from `alice@example.com` to a meeting on Pexip.
2. While the call is connecting, watch the policy server logs:

   ```bash
   sudo journalctl -u pexip-policy -f
   ```

   You should see a line like:

   ```
   service_configuration alias=meet1 caller_domain=example.com -> level=1 (Official)
   ```

3. Once the call connects, check the meeting:
   * The **classification banner** should show `Official` (or whatever
     label is configured in Pexip for level `1`).
   * An **elapsed timer** should appear on the stage and start counting
     up from `00:00:00`.
   * A participant called **`Policy Server`** appears briefly in the
     roster while the bot applies the settings, then leaves.

If anything's missing, see the troubleshooting section below.

---

## 15. Day-2 operations

### Update to a newer version

```bash
sudo systemctl stop pexip-policy
sudo -u pexippolicy git -C /opt/pexip-policy/app pull --ff-only
sudo -u pexippolicy /opt/pexip-policy/venv/bin/pip install \
     -r /opt/pexip-policy/app/requirements.txt
sudo systemctl start pexip-policy
sudo systemctl status pexip-policy --no-pager
```

### View logs

```bash
sudo journalctl -u pexip-policy -f                # live tail
sudo journalctl -u pexip-policy --since "1 hour ago"
```

### Back up the database

The mappings live in `/var/lib/pexip-policy/policy.db`. A simple cron
backup:

```bash
sudo tee /etc/cron.daily/pexip-policy-backup > /dev/null <<'EOF'
#!/bin/sh
mkdir -p /var/backups/pexip-policy
sqlite3 /var/lib/pexip-policy/policy.db ".backup /var/backups/pexip-policy/policy-$(date +\%F).db"
find /var/backups/pexip-policy -type f -mtime +14 -delete
EOF
sudo chmod +x /etc/cron.daily/pexip-policy-backup
sudo apt install -y sqlite3
```

### Restart / stop / start

```bash
sudo systemctl restart pexip-policy
sudo systemctl stop    pexip-policy
sudo systemctl start   pexip-policy
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `curl https://policy.example.com/healthz` times out | Firewall or DNS | Check `sudo ufw status`, verify the FQDN resolves to this server. |
| `502 Bad Gateway` from Nginx | Gunicorn isn't running | `sudo systemctl status pexip-policy`, then `sudo journalctl -u pexip-policy -n 100`. |
| Pexip logs show `404 Neither conference nor gateway found` after a call | Policy response was rejected (commonly an invalid `view` value or a PIN/`allow_guests` mismatch) | Tail the policy server logs and the Pexip support log; see gotchas in the `pexip-policy-server` skill. |
| Classification banner never appears | `PEXIP_NODE` wrong, or classification level not defined on Pexip | Verify `PEXIP_NODE` resolves and 443 is reachable; check the level is configured in Pexip's classification scheme. |
| Elapsed timer never appears | Same as above — the Client API call failed | Look for `set_clock (elapsed) failed` in `journalctl -u pexip-policy`. |
| `Policy Server` participant stays in the roster | A token/refresh edge case. Ending the meeting cleans it up. | Investigate by raising the log level (set `LOG_LEVEL=DEBUG` in `/etc/pexip-policy.env`, then `sudo systemctl restart pexip-policy`). |
| `PEXIP_VERIFY_TLS` warnings about self-signed certs | Lab Pexip nodes often use self-signed certs | Set `PEXIP_VERIFY_TLS=false` in `/etc/pexip-policy.env` for lab use only — never in production. |

---

## Uninstall

```bash
sudo systemctl disable --now pexip-policy
sudo rm /etc/systemd/system/pexip-policy.service
sudo systemctl daemon-reload

sudo rm -f /etc/nginx/sites-enabled/pexip-policy
sudo rm -f /etc/nginx/sites-available/pexip-policy
sudo systemctl reload nginx

sudo rm -rf /opt/pexip-policy /var/lib/pexip-policy /etc/pexip-policy.env
sudo userdel pexippolicy
```

That's it — the server is back to a vanilla Ubuntu install.
