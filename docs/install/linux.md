# Linux Installation Guide

## Binary Installation
1. Download the latest `.deb` or `.rpm` from GitHub Releases.
2. Install via package manager:
   ```bash
   sudo dpkg -i space-router-node_amd64.deb
   ```
3. Configure environment variables in `/etc/spacerouter/spacerouter.env`.
4. Start the service:
   ```bash
   sudo systemctl enable --now space-router-node
   ```
