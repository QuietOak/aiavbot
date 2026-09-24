#!/usr/bin/env bash
# AIAVBOT update: pull the latest code for an instance's branch, refresh packages, back up, restart.
# Installed as /usr/local/bin/aiavbot-update by setup_vps.sh.
#
#   sudo aiavbot-update test      # test instance  <- branch dev
#   sudo aiavbot-update live      # live instance  <- branch main
#   sudo aiavbot-update all       # every instance
#
# Roll back one instance:  sudo -u aiavbot git -C /opt/aiavbot/<name> checkout <commit>
#                          sudo systemctl restart aiavbot@<name>
# Copyright (C) 2026 Oak. SPDX-License-Identifier: AGPL-3.0-or-later
set -euo pipefail
BOT_USER=aiavbot
ROOT=/opt/aiavbot
[[ $EUID -eq 0 ]] || exec sudo bash "$0" "$@"
as_bot() { sudo -H -u "$BOT_USER" "$@"; }

instances() { for d in "$ROOT"/*/.git; do [[ -d "$d" ]] && basename "$(dirname "$d")"; done; }

if [[ $# -ne 1 ]]; then
    echo "Usage: sudo aiavbot-update <instance|all>"
    echo "Instances: $(instances | tr '\n' ' ')"
    exit 1
fi
if [[ "$1" == "all" ]]; then targets=$(instances); else targets="$1"; fi

for INSTANCE in $targets; do
    APP_DIR="$ROOT/$INSTANCE"
    SERVICE="aiavbot@$INSTANCE"
    [[ -d "$APP_DIR/.git" ]] || { echo "No instance '$INSTANCE' in $ROOT"; exit 1; }
    BRANCH="$(as_bot git -C "$APP_DIR" config --get aiavbot.branch || echo main)"
    echo "=== $INSTANCE (branch $BRANCH) ==="

    before="$(as_bot git -C "$APP_DIR" rev-parse --short HEAD)"
    as_bot git -C "$APP_DIR" fetch --quiet origin
    as_bot git -C "$APP_DIR" checkout --quiet "$BRANCH"     # also undoes an earlier rollback
    as_bot git -C "$APP_DIR" pull --ff-only --quiet
    after="$(as_bot git -C "$APP_DIR" rev-parse --short HEAD)"
    if [[ "$before" == "$after" ]]; then
        echo "Already up to date ($after). Restarting anyway."
    else
        echo "Updating $before -> $after:"
        as_bot git -C "$APP_DIR" --no-pager log --oneline "$before..$after"
    fi

    as_bot "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
    # Shared files come from the LIVE instance when it exists, so a test branch can't change them for live.
    SHARED="$ROOT/live"; [[ -d "$SHARED/.git" ]] || SHARED="$APP_DIR"
    install -m 644 "$SHARED/deploy/aiavbot@.service" /etc/systemd/system/aiavbot@.service
    install -m 755 "$SHARED/deploy/aiavbot-backup" /etc/cron.daily/aiavbot-backup
    install -m 755 "$SHARED/deploy/update.sh" /usr/local/bin/aiavbot-update
    systemctl daemon-reload

    /etc/cron.daily/aiavbot-backup "$INSTANCE" || echo "(backup skipped)"
    systemctl restart "$SERVICE"
    sleep 6
    systemctl --no-pager --lines=8 status "$SERVICE" || true
    echo "Previous version of $INSTANCE was $before. To roll back:"
    echo "  sudo -u $BOT_USER git -C $APP_DIR checkout $before && sudo systemctl restart $SERVICE"
    echo
done
