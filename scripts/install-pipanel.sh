#!/bin/sh
# Copy the package next to PiPanel so a PiPanel app can `run(disp, btn)` it.
# Usage (from a workstation): scripts/install-pipanel.sh root@orangepizero2w
# Does NOT restart pipanel and does NOT edit PiPanel's app list: wiring it into
# PiPanel's menu is a separate, deliberate step (see docs/HARDWARE.md).
set -eu
HOST=${1:?usage: install-pipanel.sh user@host}
SRC=$(cd "$(dirname "$0")/.." && pwd)
ssh "$HOST" 'mkdir -p /root/panel/fdungeon/lib && chmod 700 /root/panel/fdungeon'
scp -r "$SRC/src/foreverdungeon_handheld" "$HOST:/root/panel/fdungeon/lib/"
echo "Copied to $HOST:/root/panel/fdungeon/lib. Entry point: foreverdungeon_handheld.displays.pipanel.run"
