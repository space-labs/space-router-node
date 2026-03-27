# Home Node → Coordination API Contracts

Expected API contracts for both protocol versions. The Home Node supports
both v0.1.2 and v0.2.0 payloads, selected via `SR_REGISTRATION_MODE`.

---

## Common: Public IP Detection (pre-registration)

Tries these services in order (skipped if `SR_PUBLIC_IP` is set):

1. `GET https://httpbin.org/ip` → `{"origin": "1.2.3.4"}`
2. `GET https://api.ipify.org?format=json` → `{"ip": "1.2.3.4"}`
3. `GET https://ifconfig.me/ip` → plain text `1.2.3.4`

---

## Common: Challenge Probe Response

When the Coordination API probes the node via `CONNECT challenge.spacerouter.internal`,
the Home Node responds:

```
HTTP/1.1 200 Connection Established\r\n
X-SpaceRouter-Address: 0x{wallet_address}\r\n
\r\n
```

The server verifies that `X-SpaceRouter-Address` matches the registered address:
- **v0.1.2:** matches `wallet_address`
- **v0.2.0:** matches `identity_address`

---

## v0.1.2 API Contract

### Registration — `POST /nodes/register`

Request:
```json
{
  "wallet_address": "0x{ctc_address}",
  "endpoint_url": "https://{public_ip}:{port}",
  "identity_signature": "0x{eip191_signature_hex}",
  "timestamp": 1700000000,
  "label": "optional-label"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `wallet_address` | string | yes | CTC wallet address that owns the node |
| `endpoint_url` | string | yes | HTTPS URL where the node listens |
| `identity_signature` | string | yes | EIP-191 signature (see below) |
| `timestamp` | integer | yes | Unix timestamp (must be within 5 min of server time) |
| `label` | string | no | Human-readable node label |

**EIP-191 signature message:**
```
space-router:register:{wallet_address}:{timestamp}
```

The server recovers the signer address from the signature. This becomes
the node's `node_address` (secp256k1 identity).

**Server-side verification:**
1. Recover signer from `identity_signature`
2. Probe `endpoint_url` via CONNECT — verify `X-SpaceRouter-Address` matches `wallet_address`
3. If `SR_STAKING_CONTRACT_ADDRESS` configured, check on-chain stake (insufficient → 403)
4. Classify IP via IPinfo Core API

Response 200:
```json
{
  "status": "registered|updated|rekeyed",
  "node_id": "uuid",
  "wallet_address": "0x...",
  "node_address": "0x...recovered_identity",
  "endpoint_url": "https://1.2.3.4:9090",
  "gateway_ca_cert": "-----BEGIN CERTIFICATE-----\n...|null"
}
```

- `status`: `"registered"` for new, `"updated"` for re-registration, `"rekeyed"` when identity key changed
- `gateway_ca_cert`: null if CA not configured

Errors:
- 400: invalid address
- 403: insufficient on-chain stake
- 409: duplicate wallet_address (UNIQUE constraint)
- 422: endpoint probe failed
- 424: IPinfo unavailable

### Health Probe Request — `POST /nodes/{node_id}/request-probe`

Request:
```json
{
  "wallet_address": "0x...",
  "signature": "0x{eip191_hex}",
  "timestamp": 1700000000
}
```

**Signature message:** `space-router:request_probe:{node_id}:{timestamp}`

- 200: probe queued
- 400: node already online

### Deregistration — `PATCH /nodes/{node_id}/status`

Request:
```json
{
  "status": "offline",
  "wallet_address": "0x...",
  "signature": "0x{eip191_hex}",
  "timestamp": 1700000000
}
```

**Signature message:** `space-router:update_status:{node_id}:{timestamp}`

Only `"offline"` and `"draining"` are accepted. Best-effort on shutdown —
failures are logged but not raised.

**Staking side-effect:** If `is_staking_approved=True`, setting status to
`"offline"` or `"draining"` triggers an async `disapproveStaker()` on-chain.

### Node Deletion — `DELETE /nodes/{node_id}`

Request:
```json
{
  "wallet_address": "0x...",
  "signature": "0x{eip191_hex}",
  "timestamp": 1700000000
}
```

**Signature message:** `space-router:delete_node:{node_id}:{timestamp}`

Response 204: no body.

---

## v0.2.0 API Contract

v0.2.0 introduces a multi-wallet model: identity, staking, and collection
wallets. Any wallet address can equal any other ("wallet collapsing").

### Registration — `POST /nodes/register` (same endpoint, new payload)

Request:
```json
{
  "identity_address": "0x{identity_wallet_address}",
  "staking_address": "0x{staking_wallet_address}",
  "collection_address": "0x{collection_wallet_address}",
  "vouching_signature": "0x{eip191_hex}",
  "identity_signature": "0x{eip191_hex}",
  "endpoint_url": "https://{public_ip}:{port}",
  "timestamp": 1700000000,
  "label": "optional-label"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `identity_address` | string | yes | Identity wallet address (derived from node's keypair) |
| `staking_address` | string | yes | Staking wallet address (may equal identity_address) |
| `collection_address` | string | yes | Fee collection wallet address (may equal identity_address) |
| `vouching_signature` | string | yes | Identity vouches for staking + collection wallets |
| `identity_signature` | string | yes | EIP-191 registration signature |
| `endpoint_url` | string | yes | HTTPS URL where the node listens |
| `timestamp` | integer | yes | Unix timestamp (must be within 5 min of server time) |
| `label` | string | no | Human-readable node label |

**identity_signature message:**
```
space-router:register:{identity_address}:{timestamp}
```

**vouching_signature message (no timestamp — one-time binding):**
```
space-router:vouch:{staking_address}:{collection_address}:{timestamp}
```

Both signatures are signed by the identity wallet's private key.

**Server-side verification:**
1. Recover signer from `identity_signature` — must match `identity_address`
2. Recover signer from `vouching_signature` — must match `identity_address`
3. Probe `endpoint_url` via CONNECT — verify `X-SpaceRouter-Address` matches `identity_address`
4. Check on-chain stake for `staking_address`
5. Classify IP via IPinfo

Response 200:
```json
{
  "status": "registered|updated|rekeyed",
  "node_id": "uuid",
  "identity_address": "0x...",
  "staking_address": "0x...",
  "collection_address": "0x...",
  "endpoint_url": "https://1.2.3.4:9090",
  "gateway_ca_cert": "-----BEGIN CERTIFICATE-----\n...|null"
}
```

Errors: same as v0.1.2

### Health Probe Request, Deregistration, Node Deletion

Same as v0.1.2 — endpoints, payloads, and signature formats are unchanged.
