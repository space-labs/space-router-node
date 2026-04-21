# Windows Provider Node — Agent Guideline

**Audience:** Claude Code running on a Windows 10 / 11 machine for a teammate who wants to run a Space Router provider node against the test environment (CC3 testnet, Escrow payment v1.5).

**Goal:** Download the latest `dev` Windows GUI build, install it, configure it for the test environment, launch it, and verify it's registering + routing.

**Build selected:** GitHub release tag `dev-latest` on `space-labs/space-router-node`, asset `spacerouter-gui-test-windows-x64.exe`. This is the **test variant** (`BUILD_VARIANT=test`) which defaults to `spacerouter-coordination-api-test.fly.dev` and exposes the environment selector + mTLS toggle. Includes the full v1.5 lifecycle UX (schema v3 receipt store, 2-try retry cap, reaper, GUI Earnings screen, all edge-case fixes, Leg 1 post-tunnel settlement).

---

## 0. Hard prerequisites — check these first, stop and ask if missing

Ask the user for the following **before** touching anything. Don't proceed until you have all four:

| Item | Why | How to get |
|---|---|---|
| **CC3 testnet staking wallet address** | Proves the operator staked ≥ 1 SPC. Required for registration. | User should have this from team onboarding. If not: ask engineering for a pre-funded test wallet, OR run `cast wallet new`, fund ≥ 1.5 CTC, stake 1 via the Staking contract, wait for approval. |
| **(Optional) Collection wallet address** | Receives Leg 2 SPC payouts. Defaults to staking address if unset. | Same wallet as staking is fine for QA. |
| **Identity key** (either an existing `identity.key` file **or** an agreement to let the node generate a fresh one) | Required for the node's EIP-191 signing against the coord API. | If fresh: node creates it on first run. If importing: user provides a 64-hex private key. |
| **Whether to allow Windows Defender + SmartScreen to run an unsigned binary** | Test builds are not code-signed. SmartScreen will warn on first launch. | Explain this out loud; user must click "More info → Run anyway" once. The agent CAN'T dismiss that dialog. |

**If the user cannot provide a staked testnet wallet, stop.** Point them at the team lead with: "We need a staked CC3 wallet (min 1 SPC on test) and an optional collection wallet to run the provider. I can't generate one that works without engineering action."

---

## 1. Workspace setup

Use a single working directory so everything is self-contained:

```powershell
$WorkDir = "$env:USERPROFILE\Desktop\SpaceRouter-Test"
New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null
Set-Location $WorkDir
```

Tell the user which directory you're using. This is where the .exe, logs, and config backups live.

---

## 2. Download the latest dev Windows GUI artifact

The `dev-latest` GitHub release is a **draft prerelease**. Plain anonymous HTTP won't work. Prefer the **workflow-artifact path** (fresher, always latest `dev`) over the release path (which needs all platforms to build clean — Windows CLI smoke test occasionally flakes and skips the release promotion).

### 2A. `gh` CLI is a hard prerequisite

```powershell
gh --version
gh auth status
```

- Missing: `winget install --id GitHub.cli -e --accept-source-agreements --accept-package-agreements`
- Not authenticated: `gh auth login --web --scopes repo` — opens a browser for OAuth. Tell the user to complete the sign-in there.

### 2B. Preferred — workflow-artifact download (always latest `dev`)

```powershell
# Find the latest successful GUI build for the dev branch
$runId = gh run list `
  --repo space-labs/space-router-node `
  --workflow build-test.yml `
  --branch dev `
  --limit 5 `
  --json databaseId,conclusion,headSha,jobs `
  --jq '[.[] | select(.jobs[]? | .name | contains("spacerouter-gui-test-windows-x64")) | select(.jobs[] | .name == "build-gui (windows-latest, spacerouter-gui-test-windows-x64)" and .conclusion == "success")][0].databaseId'

if (-not $runId) { throw "No recent dev build has a successful Windows GUI job; ping the team" }
Write-Host "Downloading from run $runId"

# Download the artifact zip; extract the single .exe inside
gh run download $runId `
  --repo space-labs/space-router-node `
  --name spacerouter-gui-test-windows-x64 `
  --dir $WorkDir

# The artifact is a zip; gh run download extracts it automatically
Get-ChildItem $WorkDir -Filter "*.exe" | Select Name, Length
```

Expected: `$WorkDir\spacerouter-gui-test-windows-x64.exe`, ~36 MB.

### 2C. Fallback — `dev-latest` GitHub release

If 2B fails (no recent successful run):

```powershell
gh release download dev-latest `
  --repo space-labs/space-router-node `
  --pattern "spacerouter-gui-test-windows-x64.exe" `
  --dir $WorkDir `
  --clobber
```

⚠️ The release can lag a few commits behind when the Windows CLI smoke test fails — it's gated on all platforms succeeding. Verify the embedded version string **after launch** matches what the team expects (see §5).

### Verify the download

```powershell
$Exe = "$WorkDir\spacerouter-gui-test-windows-x64.exe"
Get-Item $Exe | Select-Object Name, Length, LastWriteTime
# Expect Length around 36_000_000–38_000_000 bytes.
# A few KB = HTML error page; stop and investigate auth.
```

---

## 3. First-launch handshake — SmartScreen

The binary is **unsigned**. On first launch, Windows SmartScreen will show a blue popup: _"Windows protected your PC"_.

**Tell the user, in this exact flow:**
1. Double-click the .exe (or ask the agent to `Start-Process` it — same effect).
2. When the SmartScreen blue popup appears, click **More info**.
3. Click **Run anyway**.
4. If Windows Defender flags it with a red popup, the user must add an exclusion:
   - Open Windows Security → Virus & threat protection → Manage settings → Exclusions → Add → File → select `$WorkDir\spacerouter-gui-test-windows-x64.exe`.

The agent **cannot** bypass these dialogs. Verify the user says "it's running now" before moving on.

Launch command (agent-visible):

```powershell
Start-Process -FilePath "$WorkDir\spacerouter-gui-test-windows-x64.exe"
```

---

## 4. Onboarding inside the GUI (user action; agent coaches)

A pywebview window opens with the onboarding screen. Guide the user through the fields:

| Field | What to enter |
|---|---|
| **Generate new key** / **Import existing key** | Generate is recommended unless the user has a specific identity key they've used before. |
| **Environment** (dropdown) | Pick **"Test (CC Testnet)"** — the default for test builds. |
| **Advanced → Passphrase** | Optional. If set, encrypts the identity key file; user must re-enter on restart. |
| **Advanced → Staking address** | Paste the user's staked wallet address. |
| **Advanced → Collection address** | Same as staking, unless they want a separate payout wallet. |
| **Advanced → Referral code** | Blank. |
| **Advanced → Network** | Pick **UPnP (Automatic)** if the router supports it. If the machine is behind CGNAT, pick **Manual / Tunnel** and provide a `bore.pub` hostname + port (see step 4b). |

Click **Start Node**. The GUI may flip to a staking-modal ("Stake at least 1 SPC…"); if the user already has a valid stake, click "I've staked. Continue".

### 4b. Tunnel setup (only if behind CGNAT / no port-forward)

Install `bore` and start a tunnel in a second terminal:

```powershell
# Download bore (small Rust TCP tunnel)
$BoreZip = "$env:TEMP\bore.zip"
Invoke-WebRequest -Uri "https://github.com/ekzhang/bore/releases/latest/download/bore-v0.6.0-x86_64-pc-windows-msvc.zip" -OutFile $BoreZip
Expand-Archive -Path $BoreZip -DestinationPath "$WorkDir\bore" -Force
$Bore = "$WorkDir\bore\bore.exe"

# Run in a separate window; capture the assigned remote port
Start-Process -FilePath $Bore -ArgumentList "local", "9090", "--to", "bore.pub" -NoNewWindow
```

The `bore local 9090 --to bore.pub` output prints a line like:
```
listening at bore.pub:57123
```
Put `bore.pub` in the **Public hostname** field and `57123` in the **Port** field.

---

## 5. Verify the node is running

The GUI's status screen should show, within ~15 seconds:
- Green pulsing dot + "Running"
- Your staking + collection addresses
- **Staking Status** row — `earning` (approved + staked) or `qualifying` (pending health approval)
- **Earnings** row (new in v1.5) — initially "No earnings yet"
- Bottom-of-screen version label like `v0.0.0-test.<run>`

### Agent-side checks

Tail the provider log (written by the pywebview app):

```powershell
$LogDir = "$env:LOCALAPPDATA\SpaceRouter-Test"
Get-ChildItem $LogDir -Recurse -Include *.log | Select-Object FullName
# There should be at least one log file. Tail the most recent:
Get-Content (Get-ChildItem $LogDir -Recurse -Include *.log | Sort LastWriteTime -Desc | Select -First 1).FullName -Tail 40
```

Look for:
- `Acquired daemon lock at …\daemon.lock` (P4 single-instance lock)
- `Registered as node <uuid>` with status `updated` or `new`
- `Leg 2 receipt poller started (interval=10s)`
- `Claim reaper started (interval=300s, grace=300s)`
- `Leg 2 submitter ready — payer=0xd35d00aF... node_wallet=0x<collection>... rate=…/GB (poller every 10s, reaper enabled=True)`
- `Node is RUNNING Listening on port 9090`

If instead you see `NODE NOT REGISTERED in escrow contract` ERROR → the staking address has not been registered on the escrow contract's node registry. The user needs to ping engineering in the team Slack to run `registerNode(bytes32, address)` for their collection address. Receipts will accumulate but `--claim` will silently skip them until registered.

### Coord API sanity check

```powershell
$CoordApi = "https://spacerouter-coordination-api-test.fly.dev"
$Resp = Invoke-RestMethod "$CoordApi/nodes"
$Resp | Where-Object { $_.collection_address -ieq "<user's collection address>" } | `
  Select id, collection_address, status, staking_status, last_seen_at
```

Expected: exactly one row with `status = online`, `staking_status` in (`earning`, `qualifying`).

---

## 6. Smoke test — route a request through this node

Ask the user's teammate to route a SPACE-paid request through them and confirm the provider logs it. Alternatively, drive a request ourselves if a consumer wallet is available.

Report back to the user: "Your node is online, registered, and visible at `<coord_url>/nodes/<your_id>`. QA engineering can now route paid traffic through you."

---

## 7. Post-launch expectations & pointers

Tell the user the following, all once:

- **GUI Earnings tab**: After some paid traffic routes through them, the Earnings row lights up. Click it for the Payments screen (claim-outstanding button, retry, details).
- **CLI mirror**: Power-user commands available from a PowerShell in `$WorkDir`:
  ```powershell
  .\spacerouter-gui-test-windows-x64.exe --receipts --json
  .\spacerouter-gui-test-windows-x64.exe --claim
  ```
  Note: the GUI .exe also accepts CLI flags thanks to the unified entry point.
- **Config file**: Stored under `%LOCALAPPDATA%\SpaceRouter-Test`. Identity key, cert, and `.env` live there. To fully reset, close the GUI, delete that folder, reopen the .exe.
- **Stop the node**: Right-click the tray icon (coloured dot near the system clock) → Quit. Or close via `Stop-Process -Name spacerouter-gui-test-windows-x64 -Force`.
- **Upgrade to a newer build**: re-run step 2 (the gh download overwrites the .exe). Config is preserved in `%LOCALAPPDATA%`.

---

## 8. Troubleshooting — the three things that break most often

| Symptom | Likely cause | Fix |
|---|---|---|
| SmartScreen refuses even after "Run anyway", or the .exe immediately closes | Defender quarantined the file | Windows Security → Protection history → find the quarantine event → **Restore**. Then add the .exe to Exclusions as in §3. |
| Status stuck at "Registering" for > 1 minute | Coord API unreachable, or staking address not found on-chain | `Test-NetConnection spacerouter-coordination-api-test.fly.dev -Port 443`. If that's fine, the staking address isn't staked. User must stake ≥ 1 SPC. |
| Status "Running" but Coord API never sees it come online | Inbound port 9090 not reachable | Windows Firewall may have blocked it on first launch. Allow the app: Windows Security → Firewall & network protection → Allow an app → Add → point at the .exe → check **Private** and **Public**. |
| Green dot + "Running" but no receipts accumulate under traffic | The gateway doesn't see this node as an eligible provider | Coord API's `/nodes` shows `status=online` but `staking_status != earning` and `last_seen_at` is fresh? Then health probes are failing — check log for probe errors. |
| Earnings row shows "⚠ N need attention" | Some receipts got rejected. Click into the Payments screen for the reason strings. | Usual culprit: the `registerNode` dance wasn't done. Ping engineering with the staking wallet address. |

---

## 9. When you're done, report back

Tell the user, in this order:

1. **Node ID** (from Coord API check or from the log line `Registered as node <uuid>`).
2. **Status** (earning / qualifying / offline).
3. **Public reachable at** (`<SR_PUBLIC_IP>:<SR_PUBLIC_PORT>`).
4. **Config path** so they can share logs if something breaks later: `%LOCALAPPDATA%\SpaceRouter-Test`.
5. Which of the v1.5 lifecycle pieces you saw at startup (daemon lock, poller, reaper, submitter).

That's enough context for QA to route traffic through the new provider.

---

## Never do any of these without explicit user approval

- Don't disable Windows Defender globally. Exclusions for the single .exe only.
- Don't upload the identity key file anywhere. It's the wallet's signing key.
- Don't commit or paste the identity key or passphrase into chats, PRs, or logs.
- Don't run `--claim` until the user says so — it spends gas on-chain.
- Don't pass the user's private key or passphrase as a CLI argument or environment variable that gets persisted in PowerShell history. Prompt for it at the GUI's password field instead.

---

## Appendix — What's in this build (key files for operator debugging)

All paths relative to `%LOCALAPPDATA%\SpaceRouter-Test` unless noted.

| File | What it is | Safe to delete? |
|---|---|---|
| `receipts.db` | SQLite store of Leg 2 receipts (v3 schema). | No — losing this means losing unclaimed earnings. |
| `daemon.lock` | `fcntl.flock` single-instance guard. | Auto-created; OK to remove only if the daemon isn't running. |
| `claim.lock` | Serialises concurrent `--claim` across GUI + CLI. | Same. |
| `identity.key` | The wallet's secp256k1 private key (plaintext or passphrase-encrypted). | **Never**. Back up to a password manager. |
| `certs/node.crt` + `certs/node.key` | TLS cert for the proxy port. Auto-rotated. | Can be regenerated; takes a daemon restart. |
| `spacerouter.env` | Config. | OK to edit if the user knows what they're doing. |

If the user asks "is this the build with the Leg 1 Escrow fix?" — answer: "Yes if the version label is `v0.0.0-test.<run>` where `<run>` corresponds to a build of commit `ba5acef` or later on `dev`. Earlier `dev-latest` builds (around commit `70f150d`) are missing the Leg 1 settlement flow." They can verify via `Get-Content (Get-ChildItem "$env:LOCALAPPDATA\SpaceRouter-Test" -Recurse -Include *.log | Sort LastWriteTime -Desc | Select -First 1).FullName | Select-String "Leg 1 post-tunnel"`.
