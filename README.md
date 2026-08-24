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
pip install -r requirements.txt
python -m app.main
```

On first launch the daemon creates `~/.spacerouter/settings.json` with safe
defaults, syncs the Leg 2 rate from the Coordination API (trust-on-first-use),
and generates a node identity key. The only thing you typically need to set
yourself is your **staking address** — via the GUI or by editing
`settings.json` directly. No environment variables are required.

On first run in an interactive terminal the wizard will prompt for:
1. **Identity key** — generate a new one (recommended) or import an existing hex private key
2. **Identity passphrase** (optional) — encrypts the key at rest using Web3 keystore JSON
3. **Staking address** (optional) — EVM wallet that earns staking rewards; defaults to identity address
4. **Collection address** (optional) — where traffic fees accumulate; defaults to staking address

In non-interactive / headless environments (CI, service startup) the wizard is skipped and the identity key is auto-generated (cryptographically random) and encrypted at rest with `SR_IDENTITY_PASSPHRASE` if set (plaintext by default).

## Configuration

As of v1.5, the canonical configuration store is **`~/.spacerouter/settings.json`**
on Linux, macOS, and Windows. The daemon auto-creates it on first launch with
safe defaults; the file is rewritten atomically and validated by a Pydantic
schema on load. Top-level sections:

- `node` — port, log level, mTLS, UPnP
- `wallet` — `staking_address`, `collection_address`, `settlement_key_path`
- `coordination` — `url` (Coordination API)
- `escrow` — `leg2_rate_per_gb`, `synced_from_coord_at` (synced from coord)
- `claim` — auto-claim toggle and thresholds (see below)
- `receipts` — retry caps and reaper intervals

Pre-v1.5 installs configured the daemon via `SR_*` environment variables. Those
are still honored: a legacy `~/.spacerouter/spacerouter.env` is auto-migrated
to JSON on first v1.5 launch (and renamed to `.migrated.bak`). After migration
the JSON file is the source of truth.

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

## Optional auto-claim

By default the daemon stores signed Leg 2 receipts locally and you submit them
on-chain manually via `--claim`. To have claims fire automatically, set
`claim.auto_claim_enabled` to `true` in `settings.json`. The monitor uses
**OR semantics**: a batch is submitted as soon as **either** threshold is
crossed (default: 10 SPACE accumulated **or** 10 unsubmitted receipts; set a
threshold to `0` to disable that side). Auto-claim is **off by default**.

## Claiming receipts manually

```bash
python -m app.main --claim
```

On macOS, the same CLI ships inside the GUI bundle, so you don't need a source
install: `"/Applications/SpaceRouter Proxy.app/Contents/MacOS/space-router-node" --claim`.
Useful flags: `--include-retryable`, `--uuid <UUID>`, `--receipts` (list).

## macOS launchd service

Install as a system service that starts at boot:

```bash
sudo cp launchd/com.spacerouter.homenode.plist /Library/LaunchDaemons/
sudo launchctl load /Library/LaunchDaemons/com.spacerouter.homenode.plist
```

## Pre-built binaries

Cross-platform binaries (macOS ARM64/x64, Windows x64, Linux x64) are built automatically and published as [GitHub Releases](https://github.com/space-labs/space-router-node/releases).

## Running tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

The desktop GUI frontend (`gui/assets/app.js`) has its own suite. It loads the
real `index.html` + `app.js` into jsdom and drives `updateStatus()` with status
payloads generated from the real `app.state.NodeStateMachine`:

```bash
npm install        # once — installs jsdom (dev-only, not shipped)
npm test           # == node --test "tests/js/**/*.test.mjs"
```

Regenerate the status fixtures after changing `NodeStatus.to_dict()`:

```bash
python tests/js/fixtures/gen_status_sequence.py
```

## API contract

The Home Node communicates with two components:

**Coordination API** (registration + config sync):
- `POST /nodes` — register on startup
- `PATCH /nodes/{id}/status` — set status to `offline` on shutdown
- `GET /config` — fetch escrow contract address, chain RPC, Leg 2 rate (trust-on-first-use)

**Proxy Gateway** (inbound proxy traffic):
- Accepts TLS TCP connections on `SR_NODE_PORT`
- Handles `CONNECT host:port` for HTTPS tunneling
- Handles `GET http://...` for HTTP forwarding
- Strips all `X-SpaceRouter-*` and `Proxy-Authorization` headers before forwarding to targets

Full component contracts and protocol specifications are maintained separately; contact the maintainers for access.

## FAQ

**Why is my Node ID different than yesterday?** In v1.5 the identity key
path is sticky across restarts, so this shouldn't happen on a stable install.
If it does, either the key file at `wallet.settlement_key_path` was moved /
deleted, or `BUILD_VARIANT` flipped (e.g. you swapped a `test` build for a
`production` build) and the daemon is now looking in a different directory.
Check the startup log for `build_variant=` to confirm.

**Where's my config?** `~/.spacerouter/settings.json` on Linux, macOS, and
Windows. Auto-created on first launch.

**How do I set my rate?** You don't. The Leg 2 rate syncs from the
Coordination API's `/config` endpoint on first launch and is frozen into
`settings.json` with a `synced_from_coord_at` timestamp. Providers and the
gateway must agree on the rate or settlements won't verify.

**Can I run two daemons on the same machine?** No. The daemon takes an
exclusive lock at `~/.spacerouter/daemon.lock` on startup; a second instance
will refuse to start. This is intentional — two daemons sharing one identity
key would race on receipt-claim submissions and corrupt the local SQLite
store. If you need multiple nodes on one box, use separate user accounts.

## License

MIT — see [LICENSE](LICENSE).
