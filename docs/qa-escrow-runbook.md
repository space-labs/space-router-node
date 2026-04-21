# QA Runbook — Escrow Payment Receipt Lifecycle (v1.5)

How to exercise the full receipt lifecycle (`pending_sign` →
`claimable` → `failed_retryable` → `failed_terminal` → `claimed`) on a
test environment without waiting for real traffic or real chain
failures.

## Prerequisites

1. Test provider node running against `spacerouter-coordination-api-test.fly.dev`
   with `SR_PAYMENT_ENABLED=true` and `SR_ESCROW_*` configured for CC3
   testnet (contract `0xC5740e4e9175301a24FB6d22bA184b8ec0762852`).
2. Shell access to the provider host (for the `scripts/qa_escrow_inject.py`
   helper). Only needed to seed failure states; the CLI / GUI surfaces can
   be exercised without it once a failure state exists.
3. At least one signed receipt in the local store (run some traffic
   through the provider first — any `spacerouter request get <url>` call
   works).

## Injecting failure states

`scripts/qa_escrow_inject.py` mutates the local receipt store directly.
It refuses to run unless `SR_ALLOW_TEST_FIXTURES=1` and rejects a mainnet
RPC URL outright. Pick a scenario from this list:

| Scenario | What it simulates | Surface it exercises |
|---|---|---|
| `unregistered-node` | Gateway refused to sign: node not registered | `failed_retryable` with `SIGN_REJECTED_UNREGISTERED_NODE` |
| `byte-mismatch` | Gateway refused to sign: byte count mismatch | `failed_retryable` with `SIGN_REJECTED_BYTE_MISMATCH` |
| `price-cap` | Gateway refused to sign: over price cap | `failed_retryable` with `SIGN_REJECTED_PRICE_CAP` |
| `claim-revert` | `claimBatch` tx reverted on-chain | `failed_retryable` with `CLAIM_REVERTED` |
| `claim-timeout` | Tx broadcast but confirmation timed out | Reaper reconciles via `isNonceUsed` |
| `lock-now` | Force row into terminal state | `failed_terminal` |
| `clear` | Reset a uuid back to clean state | — |

### Running

```
SR_ALLOW_TEST_FIXTURES=1 \
SR_RECEIPT_STORE_PATH=~/.spacerouter/receipts.db \
SR_ESCROW_CHAIN_RPC=https://rpc.cc3-testnet.creditcoin.network \
.venv/bin/python scripts/qa_escrow_inject.py --scenario byte-mismatch
```

Target a specific UUID (otherwise the script picks the oldest suitable
row):

```
python scripts/qa_escrow_inject.py --scenario claim-revert --uuid 4f2a-...
```

Clean up:

```
python scripts/qa_escrow_inject.py --scenario clear --uuid 4f2a-...
```

## Verifying surfaces

After injecting a scenario, verify each surface shows the expected state:

### Provider CLI (node side, local DB)

```
python -m app.main --receipts --failed
```

Expected: a table entry for the injected uuid with
- `Try` column showing `1/2`
- `Status` column showing the corresponding retry reason message

Pretty JSON for automation:

```
python -m app.main --receipts --json | jq '.receipts[] | select(.view == "failed_retryable")'
```

### Provider GUI (desktop)

1. Click the "Earnings" row on the status screen.
2. The "Needs attention" card shows the injected uuid with:
   - `try 1 of 2` or similar tries metadata
   - A retry reason (amber text)
   - A `Retry` button (purple ghost)
   - A `Details` link

Clicking `Retry` fires `receipts_retry`. On second failure, the row
moves to the locked card with no Retry button.

### SDK CLI (on-chain view)

```
spacerouter receipts is-settled <client_address> <uuid> \
  --rpc-url https://rpc.cc3-testnet.creditcoin.network \
  --contract-address 0xC5740e4e9175301a24FB6d22bA184b8ec0762852
```

Expected: `settled_on_chain: false` for injected / unclaimed rows.

## End-to-end flow to exercise

1. Start fresh: provider store has only `claimable` / `pending_sign` rows.
2. Open GUI → Earnings tab. Summary shows "X SPACE ready" (green).
3. Inject `byte-mismatch` on one uuid.
4. Earnings row flips to "⚠ 1 need attention" (amber) within 10s.
5. Click into the tab; the needs-attention card shows the row.
6. Click Retry — counter goes to 2/2 — then second failure locks it.
7. Row moves to the Locked card. Retry gone. Details still work.
8. Inject `clear` on the uuid to reset.
9. Click "Claim All Outstanding" — progress spinner → toast on completion.

## Idempotency checks

- Click "Claim All Outstanding" 10× in rapid succession → exactly one
  claim tx (flock serialisation).
- Run `python -m app.main --claim` in a terminal while the GUI claim is
  running → CLI exits with `claim in progress` (or similar), no second
  tx.
- Kill provider mid-claim via `kill -9`, restart → reaper resolves any
  `CLAIM_TX_TIMEOUT` rows within 5 minutes (configurable via
  `SR_RECEIPT_REAPER_GRACE_SECONDS`).

## Known gotchas

- The provider must be running to keep the Leg 2 poller alive for
  `rejection_reason` sync. You can still inject scenarios while the
  node is off — they'll persist and the next poll will reconcile.
- On a fresh install with no history, the Earnings row is hidden — this
  is correct. Route some traffic first.
- On the test env, `claimable_total_price` is intentionally tiny
  (fractional SPACE) because the test rate is low. This is expected.

## Rollback

Everything this runbook touches is local provider state. To completely
reset: stop the node, `mv ~/.spacerouter/receipts.db ~/.spacerouter/receipts.db.bak`,
restart. The v3 migration recreates a fresh store on first use.
