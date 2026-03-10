#!/bin/bash
set -e

# Stop and disable the service before removal
systemctl stop space-router-node.service || true
systemctl disable space-router-node.service || true
