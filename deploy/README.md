# Hosting ValorLink on a DigitalOcean droplet

This runs **both** the Discord bot and the web UI on one small droplet,
sharing a single SQLite database file, with Caddy terminating HTTPS in front
of the web app. Total cost: about **$6/month** for the droplet plus a domain
name (~$1–12/year).

```
                    ┌──────────────── one droplet ────────────────┐
Discord gateway ⇄  │  valorlink-bot.service   (bot.py + bridge)   │
                    │             │  shared /opt/valorlink/valorlink.db
browsers ⇄ :443 ⇄ Caddy ⇄ :8000 │  valorlink-web.service   (uvicorn)          │
                    └──────────────────────────────────────────────┘
```

Files in this folder:

| File | Where it goes |
|---|---|
| `valorlink-bot.service` | `/etc/systemd/system/` (via `install.sh`) |
| `valorlink-web.service` | `/etc/systemd/system/` (via `install.sh`) |
| `install.sh` | run in place with `sudo` |
| `Caddyfile` | `/etc/caddy/Caddyfile` |
| `.env.production.example` | copy to `/opt/valorlink/.env` |

The units assume the app lives at **`/opt/valorlink`**, runs as a
**`valorlink`** system user, and uses a virtualenv at
`/opt/valorlink/.venv`. The steps below set that up. If you use different
paths, edit the two `.service` files to match.

---

## 1. Create the droplet

In the DigitalOcean control panel: **Create → Droplets**.

- **Image:** Ubuntu 24.04 (LTS)
- **Type:** Basic → Regular. The **$6/mo** (1 GB RAM) size is comfortable;
  the $4/mo (512 MB) works too but leaves little headroom for updates.
- **Authentication:** add your SSH key (not a password).
- Create it, and note the droplet's public IP.

## 2. Point a domain at it

The web UI needs a real hostname for HTTPS and Discord sign-in. Create a
DNS **A record** for e.g. `hq.yourregiment.com` pointing at the droplet IP.
(You can use DigitalOcean's own DNS under **Networking → Domains**, or your
registrar's.) DNS can take a few minutes to propagate.

## 3. First login and firewall

```bash
ssh root@YOUR_DROPLET_IP

# Basic firewall: SSH + web only.
ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw --force enable
```

## 4. Install system packages

```bash
apt update && apt upgrade -y
apt install -y python3-venv python3-pip git

# Caddy (official apt repo)
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

## 5. Create the app user and clone the repo

```bash
# Clone first, then create the service user and hand it ownership.
git clone https://github.com/Uncreatedlemon91/ValorLink.git /opt/valorlink
useradd --system --home-dir /opt/valorlink --shell /usr/sbin/nologin valorlink
chown -R valorlink:valorlink /opt/valorlink
```

## 6. Virtualenv and dependencies

```bash
cd /opt/valorlink
sudo -u valorlink python3 -m venv .venv
sudo -u valorlink .venv/bin/pip install --upgrade pip
sudo -u valorlink .venv/bin/pip install -r requirements.txt -r web/requirements.txt
```

## 7. Configure the environment

```bash
sudo -u valorlink cp deploy/.env.production.example .env
# generate a session secret to paste into the file:
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
sudo -u valorlink nano .env      # fill everything in (see notes below)
chmod 600 .env
```

Fill in `.env`:
- `DISCORD_BOT_TOKEN`, `GUILD_ID` — from your Discord application (see the
  main [`SETUP.md`](../SETUP.md) if you haven't created the bot yet).
- `WEB_SESSION_SECRET` — the value you just generated.
- `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` — from the app's **OAuth2**
  page.
- `DISCORD_OAUTH_REDIRECT` — `https://hq.yourregiment.com/auth/discord/callback`
  with **your** domain. Then, in the Discord app's **OAuth2 → Redirects**,
  add that exact URL.
- Leave `DATABASE_URL` as the absolute SQLite path unless you have a reason
  to change it. Leave `WEB_DEV_LOGIN` unset.

## 8. Create the database

```bash
cd /opt/valorlink
sudo -u valorlink .venv/bin/alembic upgrade head
```

## 9. Start the bot and web services

```bash
sudo bash deploy/install.sh
```

This copies both unit files, enables them on boot, and starts them. Check:

```bash
systemctl status valorlink-bot valorlink-web
journalctl -u valorlink-web -f      # Ctrl-C to stop tailing
```

## 10. Point Caddy at the web app

```bash
cp /opt/valorlink/deploy/Caddyfile /etc/caddy/Caddyfile
nano /etc/caddy/Caddyfile      # replace hq.example.com with your domain
systemctl reload caddy
```

Caddy will fetch a Let's Encrypt certificate automatically. Visit
`https://hq.yourregiment.com` — you should see the ValorLink Headquarters
site. Sign in with Discord; your permission tier is read from your regiment
roles.

## 11. Configure the regiment

If this is a fresh install, set roles/channels/ranks/companies either from
Discord (`/config`, `/rank`, `/company` — see [`SETUP.md`](../SETUP.md)) or,
once an admin role is set, from the web **Command Tent**. Officer sign-in on
the site maps admin/officer/recruiter Discord roles to what each person can
do, so make sure those role IDs are configured.

---

## Updating later

```bash
cd /opt/valorlink
sudo -u valorlink git pull
sudo -u valorlink .venv/bin/pip install -r requirements.txt -r web/requirements.txt
sudo -u valorlink .venv/bin/alembic upgrade head
sudo systemctl restart valorlink-bot valorlink-web
```

## Troubleshooting

- **Web up but "sign in" fails** — the `DISCORD_OAUTH_REDIRECT` in `.env`
  must exactly match a redirect registered in the Discord app, and the bot
  must be in the guild (`GUILD_ID`) so it can read your roles.
- **Actions on the site don't change Discord** — the **bot** service applies
  those. Confirm `valorlink-bot` is running and that both services point at
  the same `DATABASE_URL` (`journalctl -u valorlink-bot`).
- **Certificate errors** — DNS must resolve to the droplet before Caddy can
  issue a cert; give it a few minutes, then `systemctl reload caddy`.
- **Backups** — the whole regiment lives in `/opt/valorlink/valorlink.db`.
  Copy it somewhere safe periodically (DigitalOcean weekly droplet backups,
  a cron `cp`, or `rsync` off-box).
