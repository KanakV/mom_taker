#!/usr/bin/env bash
# mom_sync.sh — pull new recordings into the local inbox, push results back up.
# Phone saves recordings to Dropbox /mom-inbox; the done-archive is pushed to
# Google Drive. The gdrive inbox pull is kept so files dropped there still work.
set -eu

RCLONE="$HOME/.local/bin/rclone"
INBOX="$HOME/GoogleDrive/mom-inbox"
DONE="$HOME/GoogleDrive/mom-done"

# min-age keeps half-uploaded recordings remote until the phone finishes.
if "$RCLONE" lsd dropbox: >/dev/null 2>&1; then
    "$RCLONE" mkdir dropbox:mom-inbox
    "$RCLONE" move dropbox:mom-inbox "$INBOX" --min-age 1m
else
    echo "dropbox remote not configured/authenticated yet — run: rclone config"
fi

if "$RCLONE" lsd gdrive: >/dev/null 2>&1; then
    "$RCLONE" mkdir gdrive:mom-inbox
    "$RCLONE" move gdrive:mom-inbox "$INBOX" --min-age 1m
    if [ -d "$DONE" ]; then
        "$RCLONE" copy "$DONE" gdrive:mom-done
    fi
fi
