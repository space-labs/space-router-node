# SpaceRouter Home Node

A daemon that runs on residential machines and acts as a proxy exit point for the [SpaceRouter](https://spacerouter.org) network.

Traffic from AI agents flows through the SpaceRouter Proxy Gateway to this Home Node, which forwards requests from a residential IP address.

## How it works

```
AI Agent → Proxy Gateway (cloud) → Home Node (your machine) → Target website
```

The Home Node:
- Generates or imports a secp256k1 identity key on first run for ownership verification
- Registers with the Coordination API on startup (proving ownership via cryptographic signature)
- Accepts TLS-encrypted proxy connections from the Proxy Gateway
- Forwards traffic to target servers from your residential IP
- Auto-configures your router via UPnP for port forwarding
- Deregisters on shutdown

## Quick start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure the Coordination API URL (required for production)
export SR_COORDINATION_API_URL=https://spacerouter-coordination-api.fly.dev

# Run — first-time setup wizard starts automatically in a terminal
python -m app.main
```

On first run in an interactive terminal the wizard will prompt for:
1. **Identity key** — generate a new one (recommended) or import an existing hex private key
2. **Identity passphrase** (optional) — encrypts the key at rest using Web3 keystore JSON
3. **Staking address** (optional) — EVM wallet that earns staking rewards; defaults to identity address
4. **Collection address** (optional) — where traffic fees accumulate; defaults to staking address

In non-interactive / headless environments (CI, service startup) the wizard is skipped and the identity key is auto-generated (cryptographically random) and encrypted at rest with `SR_IDENTITY_PASSPHRASE` if set (plaintext by default).

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `SR_COORDINATION_API_URL` | `http://localhost:8000` | Coordination API URL |
| `SR_NODE_PORT` | `9090` | Port for incoming proxy connections |
| `SR_NODE_LABEL` | `""` | Human-readable label for this node |
| `SR_BIND_ADDRESS` | `0.0.0.0` | Interface address to bind the proxy listener |
| `SR_MAX_CONNECTIONS` | `256` | Maximum concurrent proxy connections (DoS limit) |
| `SR_STAKING_ADDRESS` | identity address | EVM wallet that earns staking rewards |
| `SR_COLLECTION_ADDRESS` | staking address | EVM wallet that collects traffic fees |
| `SR_IDENTITY_KEY_PATH` | `certs/node-identity.key` | Path to identity private key file |
| `SR_IDENTITY_PASSPHRASE` | `""` | Passphrase to encrypt/decrypt the identity key |
| `SR_PUBLIC_IP` | auto-detected | Public IP (auto-detected if empty) |
| `SR_UPNP_ENABLED` | `true` | Enable UPnP port forwarding |
| `SR_UPNP_LEASE_DURATION` | `3600` | UPnP lease duration in seconds |
| `SR_TLS_CERT_PATH` | `certs/node.crt` | TLS certificate path (auto-generated) |
| `SR_TLS_KEY_PATH` | `certs/node.key` | TLS key path (auto-generated) |
| `SR_MTLS_ENABLED` | `true` | Require mutual TLS authentication from the Gateway |
| `SR_GATEWAY_CA_CERT_PATH` | `certs/gateway-ca.crt` | Path to Gateway CA certificate for mTLS verification |
| `SR_REGISTRATION_MODE` | `v1` | Registration protocol: `v1`, `v2`, or `auto` |
| `SR_BUFFER_SIZE` | `65536` | TCP relay buffer size |
| `SR_REQUEST_TIMEOUT` | `30.0` | Connection timeout in seconds |
| `SR_RELAY_TIMEOUT` | `300.0` | Bidirectional relay timeout in seconds |
| `SR_LOG_LEVEL` | `INFO` | Log level |

> **Upgrading from v0.1.x:** `SR_WALLET_ADDRESS` is accepted as a backward-compatible alias for `SR_STAKING_ADDRESS`. No config changes are required.

### Identity key storage

The identity key is stored at `SR_IDENTITY_KEY_PATH` in one of two formats:

- **Plaintext** (no passphrase): raw hex private key — simple, no extra prompt on startup
- **Keystore JSON** (passphrase set): Web3 standard encrypted keystore — requires `SR_IDENTITY_PASSPHRASE` to be set, or will prompt on startup

If a plaintext key file exists and `SR_IDENTITY_PASSPHRASE` is later configured, the file is **automatically migrated** to keystore JSON on next startup.

> **Note:** When the wizard saves a passphrase, it is written in plaintext to `.env` as `SR_IDENTITY_PASSPHRASE`. This means the encrypted key and its passphrase are co-located on the filesystem; passphrase encryption primarily protects against accidental key file exposure, not against an adversary with full filesystem access.

## macOS launchd service

Install as a system service that starts at boot:

```bash
sudo cp launchd/com.spacerouter.homenode.plist /Library/LaunchDaemons/
sudo launchctl load /Library/LaunchDaemons/com.spacerouter.homenode.plist
```

## Pre-built binaries

Cross-platform binaries (macOS ARM64/x64, Windows x64, Linux x64) are built automatically and published as [GitHub Releases](https://github.com/gluwa/space-router-node/releases).

## Running tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

## API contract

The Home Node communicates with two components:

**Coordination API** (registration):
- `POST /nodes` — register on startup
- `PATCH /nodes/{id}/status` — set status to `offline` on shutdown

**Proxy Gateway** (inbound proxy traffic):
- Accepts TLS TCP connections on `SR_NODE_PORT`
- Handles `CONNECT host:port` for HTTPS tunneling
- Handles `GET http://...` for HTTP forwarding
- Strips all `X-SpaceRouter-*` and `Proxy-Authorization` headers before forwarding to targets

See [component-contracts.md](https://github.com/space-labs/space-router-protocol/blob/main/component-contracts.md) for full specifications.
