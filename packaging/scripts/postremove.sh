#!/bin/bash
set -e

# Reload systemd after unit file removal
systemctl daemon-reload || true

echo "Space Router Home Node removed."
echo "Configuration preserved at /etc/spacerouter/"
echo "To fully remove: rm -rf /etc/spacerouter /opt/spacerouter"
