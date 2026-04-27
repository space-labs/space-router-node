#!/bin/bash
set -e

# Reload systemd after unit file removal
systemctl daemon-reload || true

cat <<'EOF'
Space Router Home Node removed.

Configuration preserved at /etc/spacerouter/spacerouter.env.
Provider state preserved at /var/lib/spacerouter/.spacerouter/, including
the **identity key** that ties this node to its on-chain stake.

If you reinstall the package later, the same identity is reused
automatically and your stake stays attached.

To fully remove the package AND your identity (NOT recommended unless
you have unstaked first):
    sudo rm -rf /etc/spacerouter /opt/spacerouter /var/lib/spacerouter

WARNING: deleting /var/lib/spacerouter/.spacerouter/identity.key destroys
the keypair that signs your receipts. There is no recovery — re-staking
on a new key requires unstaking the old one on chain first.
EOF
