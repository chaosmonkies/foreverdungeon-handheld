#!/bin/sh
# Install the device provisioning key on a PiPanel handheld so it can sign up /
# sign in on-device. The key is copied server .env -> device over two ssh pipes;
# it is never printed, logged, passed on a command line or stored locally.
#
# Usage: scripts/install-provision-key.sh root@orangepizero2w [infra-host]
#   infra-host (default: infra-server) holds /opt/sites/foreverdungeon/.env
#   with DEVICE_PROVISION_KEYS=key1[,key2...]; the first key is installed.
set -eu
HOST=${1:?usage: install-provision-key.sh user@device [infra-host]}
INFRA=${2:-infra-server}
ENV_PATH=${FD_ENV_PATH:-/opt/sites/foreverdungeon/.env}
DEST=/root/panel/fdungeon/provision_key

ssh "$INFRA" "sudo sed -n 's/^DEVICE_PROVISION_KEYS=//p' '$ENV_PATH' | cut -d, -f1 | tr -d '\r\n'" |
ssh "$HOST" "umask 077; mkdir -p /root/panel/fdungeon && chmod 700 /root/panel/fdungeon &&
  k=\$(cat) && [ -n \"\$k\" ] || { echo 'no DEVICE_PROVISION_KEYS on server' >&2; exit 1; }
  printf '%s\n' \"\$k\" > $DEST.tmp && chmod 600 $DEST.tmp && mv $DEST.tmp $DEST && echo 'provision key installed (0600)'"
