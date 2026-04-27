#!/bin/bash
# Post-install for the .deb / .rpm package.
#
# v1.5 unified all provider state under ~/.spacerouter (settings.json,
# identity.key, receipts.db, daemon.lock). For a system-service install
# we run as the `spacerouter` user with HOME=/var/lib/spacerouter, so
# the canonical state dir is /var/lib/spacerouter/.spacerouter — the
# hidden ".spacerouter" sub-directory matches what the same daemon
# writes when run interactively under any other user account.
#
# We do NOT auto-start the service on a fresh install: v1.5 needs a
# wallet address before it can register, and starting in a half-
# configured state just produces an immediately-restarting unit. On
# upgrades we restart the existing service so the new binary takes
# effect.
set -e

# ── Create the spacerouter system user ─────────────────────────────
# --home-dir sets pw_dir so Path.home() resolves correctly even if
# someone runs the binary manually via `sudo -u spacerouter`. We keep
# --no-create-home so we don't seed bash dotfiles into /var/lib.
if ! id -u spacerouter >/dev/null 2>&1; then
    useradd --system \
        --home-dir /var/lib/spacerouter \
        --no-create-home \
        --shell /usr/sbin/nologin \
        spacerouter
elif ! getent passwd spacerouter | cut -d: -f6 | grep -qx /var/lib/spacerouter; then
    # Pre-existing user from a v1.4 install pointed at /home/spacerouter
    # (or empty pw_dir). Repoint at /var/lib/spacerouter without changing
    # uid/gid so existing chown'd files keep working.
    usermod --home /var/lib/spacerouter spacerouter || true
fi

# ── Binary directory ───────────────────────────────────────────────
mkdir -p /opt/spacerouter/certs
chown -R spacerouter:spacerouter /opt/spacerouter
chmod 0700 /opt/spacerouter/certs

# ── Writable state dir (~/.spacerouter for the spacerouter user) ──
# This is where settings.json, identity.key, receipts.db, daemon.lock
# all live at runtime. Match the systemd unit's ReadWritePaths.
mkdir -p /var/lib/spacerouter/.spacerouter
chown -R spacerouter:spacerouter /var/lib/spacerouter
chmod 0700 /var/lib/spacerouter /var/lib/spacerouter/.spacerouter

# ── Optional one-shot env-var seed file ────────────────────────────
# v1.4 users edited /etc/spacerouter/spacerouter.env. v1.5 still
# accepts that file via the systemd unit's EnvironmentFile= directive:
# the daemon reads the SR_* vars on first start and seeds settings.json
# from them, then never touches the env file again. Newcomers should
# edit /var/lib/spacerouter/.spacerouter/settings.json after the first
# start instead — the env file is for backwards compat only.
mkdir -p /etc/spacerouter
if [ ! -f /etc/spacerouter/spacerouter.env ]; then
    cp /opt/spacerouter/spacerouter.env.default /etc/spacerouter/spacerouter.env
fi
chown -R spacerouter:spacerouter /etc/spacerouter

# ── Install + (conditional) start the service ──────────────────────
systemctl daemon-reload
systemctl enable space-router-node.service

# On upgrades the service is already running and has a configured
# settings.json — restart so the new binary takes effect. On fresh
# installs we leave it stopped: starting without a wallet address only
# burns log lines and confuses the operator.
if [ -f /var/lib/spacerouter/.spacerouter/settings.json ]; then
    if systemctl is-active --quiet space-router-node.service; then
        systemctl restart space-router-node.service || true
        echo "Space Router Home Node restarted."
    else
        systemctl start space-router-node.service || true
        echo "Space Router Home Node started."
    fi
else
    cat <<'EOF'
Space Router Home Node installed.

Next steps:
  1. Edit /etc/spacerouter/spacerouter.env to set SR_WALLET_ADDRESS (and
     any other overrides). On first start the daemon reads this file,
     seeds /var/lib/spacerouter/.spacerouter/settings.json, and from
     then on settings.json is the canonical config — edit it directly.
  2. sudo systemctl start space-router-node.service
  3. journalctl -u space-router-node.service -f   # follow startup logs
EOF
fi
