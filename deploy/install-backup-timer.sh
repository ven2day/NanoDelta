#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root: sudo deploy/install-backup-timer.sh" >&2
  exit 2
fi

install -m 0644 deploy/systemd/nanodelta-backup.service /etc/systemd/system/nanodelta-backup.service
install -m 0644 deploy/systemd/nanodelta-backup.timer /etc/systemd/system/nanodelta-backup.timer
systemctl daemon-reload
systemctl enable --now nanodelta-backup.timer
systemctl list-timers nanodelta-backup.timer --no-pager

