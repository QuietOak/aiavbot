#!/usr/bin/env bash
# AIAVBOT VPS setup: installs ONE instance (a separate copy of the bot) on Ubuntu 24.04 / Debian 12.
#
#   sudo bash setup_vps.sh test     # branch "dev"  -> your test server
#   sudo bash setup_vps.sh live     # branch "main" -> Dreamers
#   sudo bash setup_vps.sh <name> <branch>   # any other instance/branch
#
# Each instance gets its own code (/opt/aiavbot/<name>), settings (/etc/aiavbot/<name>.env),
# data (/var/lib/aiavbot/<name>/) and service (aiavbot@<name>). They share nothing.
# Safe to run again: it keeps an existing token/settings and just updates everything else.
# Copyright (C) 2026 Oak. SPDX-License-Identifier: AGPL-3.0-or-later
set -euo pipefail

INSTANCE="${1:-}"
case "$INSTANCE" in
    live) DEFAULT_BRANCH=main ;;
    test) DEFAULT_BRANCH=dev ;;
    "")   echo "Usage: sudo bash $0 <test|live|name> [branch]"; exit 1 ;;
    *)    DEFAULT_BRANCH=main ;;
esac
BRANCH="${2:-$DEFAULT_BRANCH}"
if ! [[ "$INSTANCE" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]]; then
    echo "Instance name must be lowercase letters, digits or '-' (e.g. live, test)."; exit 1
fi

REPO_URL="${REPO_URL:-https://github.com/QuietOak/aiavbot.git}"
TZ_NAME="${TZ_NAME:-America/Chicago}"      # nightly update posts at midnight in this timezone
BOT_USER=aiavbot
APP_DIR="/opt/aiavbot/$INSTANCE"
DATA_ROOT=/var/lib/aiavbot
ENV_DIR=/etc/aiavbot
ENV_FILE="$ENV_DIR/$INSTANCE.env"
SERVICE="aiavbot@$INSTANCE"

say()  { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
as_bot() { sudo -H -u "$BOT_USER" "$@"; }

[[ $EUID -eq 0 ]] || { echo "Please run with sudo:  sudo bash $0 $*"; exit 1; }
grep -qiE 'ubuntu|debian' /etc/os-release || warn "This script is written for Ubuntu/Debian. Continuing anyway."
echo "Setting up instance '$INSTANCE' from branch '$BRANCH'."

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git sqlite3 ufw unattended-upgrades ca-certificates curl sudo

say "Setting timezone to $TZ_NAME and keeping the clock synced"
timedatectl set-timezone "$TZ_NAME" || warn "Couldn't set timezone"
timedatectl set-ntp true || true

say "Service account '$BOT_USER' (no login)"
if ! id "$BOT_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_ROOT" --shell /usr/sbin/nologin "$BOT_USER"
fi
install -d -o "$BOT_USER" -g "$BOT_USER" -m 750 "$DATA_ROOT"
install -d -o "$BOT_USER" -g "$BOT_USER" -m 755 /opt/aiavbot
install -d -o "$BOT_USER" -g "$BOT_USER" -m 750 "$DATA_ROOT/$INSTANCE" "$DATA_ROOT/$INSTANCE/backups"

say "Code for '$INSTANCE' ($BRANCH) in $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
    as_bot git -C "$APP_DIR" fetch --quiet origin
    as_bot git -C "$APP_DIR" checkout --quiet "$BRANCH"
    as_bot git -C "$APP_DIR" pull --ff-only --quiet
else
    if ! git ls-remote --exit-code --heads "$REPO_URL" "$BRANCH" >/dev/null 2>&1; then
        echo "Branch '$BRANCH' doesn't exist on GitHub yet. Create and push it first, e.g.:"
        echo "  git checkout -b $BRANCH && git push -u origin $BRANCH"
        exit 1
    fi
    as_bot git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
as_bot git -C "$APP_DIR" config aiavbot.branch "$BRANCH"     # remembered for aiavbot-update

say "Python packages for '$INSTANCE' (own virtual environment)"
as_bot python3 -m venv "$APP_DIR/.venv"
as_bot "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
as_bot "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

say "Token and server for '$INSTANCE'"
install -d -m 700 -o root -g root "$ENV_DIR"
if [[ -f "$ENV_FILE" ]] && grep -qE '^DISCORD_TOKEN=.+' "$ENV_FILE"; then
    echo "Keeping the existing settings in $ENV_FILE (edit it to change them)."
else
    echo "Paste the token of the Discord bot for '$INSTANCE' (Developer Portal > Bot > Reset Token)."
    [[ "$INSTANCE" == "live" ]] && echo "This must be a DIFFERENT bot than the test instance."
    echo "It won't be shown while you paste. Press Enter when done."
    TOKEN=""
    while [[ -z "$TOKEN" ]]; do
        read -rs -p "DISCORD_TOKEN: " TOKEN < /dev/tty; echo
        TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
        if [[ -n "$TOKEN" && "$(printf '%s' "$TOKEN" | tr -cd '.' | wc -c)" -ne 2 ]]; then
            warn "That doesn't look like a bot token (it should have two dots). Try again."; TOKEN=""
        fi
        for other in "$ENV_DIR"/*.env; do
            [[ -f "$other" && "$other" != "$ENV_FILE" && -n "$TOKEN" ]] || continue
            if grep -qxF "DISCORD_TOKEN=$TOKEN" "$other"; then
                warn "That token is already used by $(basename "$other" .env). Each instance needs its own bot."; TOKEN=""
            fi
        done
    done
    echo
    echo "Server ID this instance runs in, for instant slash commands"
    echo "(right-click the server icon > Copy Server ID). Leave blank for global commands."
    read -r -p "Server ID: " GUILD < /dev/tty
    GUILD="$(printf '%s' "$GUILD" | tr -cd '0-9')"
    ( umask 077
      { echo "# AIAVBOT '$INSTANCE' settings. Root-only. Edit, then: sudo systemctl restart $SERVICE"
        echo "DISCORD_TOKEN=$TOKEN"
        echo "DEV_GUILD_ID=$GUILD"
      } > "$ENV_FILE" )
    unset TOKEN
    echo "Saved to $ENV_FILE (readable by root only)."
fi
chmod 600 "$ENV_FILE"; chown root:root "$ENV_FILE"

say "Service, daily backups, update command and firewall"
install -m 644 "$APP_DIR/deploy/aiavbot@.service" /etc/systemd/system/aiavbot@.service
install -m 755 "$APP_DIR/deploy/aiavbot-backup" /etc/cron.daily/aiavbot-backup
install -m 755 "$APP_DIR/deploy/update.sh" /usr/local/bin/aiavbot-update
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null

ufw allow OpenSSH >/dev/null          # the bots only make outgoing connections
ufw --force enable >/dev/null
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

say "Starting $SERVICE"
systemctl restart "$SERVICE"
sleep 8
if systemctl is-active --quiet "$SERVICE"; then echo "$SERVICE is running."
else warn "$SERVICE isn't running yet. Recent log lines:"; fi
journalctl -u "$SERVICE" -n 15 --no-pager || true

cat <<MSG

'$INSTANCE' is set up (branch $BRANCH). Useful commands:
  sudo systemctl status $SERVICE        # is it running?
  sudo journalctl -u $SERVICE -f        # live log (Ctrl+C to leave)
  sudo systemctl restart $SERVICE       # restart
  sudo aiavbot-update $INSTANCE              # pull latest '$BRANCH' from GitHub and restart
  sudo nano $ENV_FILE             # change token / server ID, then restart
Data, log and backups: $DATA_ROOT/$INSTANCE
MSG
