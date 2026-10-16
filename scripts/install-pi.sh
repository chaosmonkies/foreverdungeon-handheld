#!/bin/sh
# Install the standalone client as a systemd service (dedicated unprivileged user).
# For the LAFVIN 2.13" PiPanel device use scripts/install-pipanel.sh instead.
set -eu
PREFIX=/opt/foreverdungeon-handheld
SRC=$(cd "$(dirname "$0")/.." && pwd)

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
id foreverdungeon >/dev/null 2>&1 || useradd --system --home-dir /var/lib/foreverdungeon-handheld --shell /usr/sbin/nologin foreverdungeon
for g in gpio spi; do getent group "$g" >/dev/null && usermod -aG "$g" foreverdungeon || true; done
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade "$SRC"
[ -f /etc/foreverdungeon-handheld.toml ] || install -m 0644 "$SRC/config.example.toml" /etc/foreverdungeon-handheld.toml
install -m 0644 "$SRC/systemd/foreverdungeon-handheld.service" /etc/systemd/system/
systemctl daemon-reload
echo "Installed. Edit /etc/foreverdungeon-handheld.toml, then: systemctl enable --now foreverdungeon-handheld"
