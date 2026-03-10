#!/bin/bash
set -e

# Create spacerouter system user if it doesn't exist
if ! id -u spacerouter >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin spacerouter
fi

# Ensure directories exist with correct ownership
mkdir -p /opt/spacerouter/certs
chown -R spacerouter:spacerouter /opt/spacerouter

mkdir -p /etc/spacerouter
# Don't overwrite existing config
if [ ! -f /etc/spacerouter/spacerouter.env ]; then
    cp /opt/spacerouter/spacerouter.env.default /etc/spacerouter/spacerouter.env
fi
chown -R spacerouter:spacerouter /etc/spacerouter

# Reload systemd and enable service
systemctl daemon-reload
systemctl enable space-router-node.service
systemctl start space-router-node.service || true

echo "Space Router Home Node installed and started."
echo "Configure: /etc/spacerouter/spacerouter.env"
echo "Status:    systemctl status space-router-node"
echo "Logs:      journalctl -u space-router-node -f"
