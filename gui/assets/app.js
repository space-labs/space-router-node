/**
 * SpaceRouter Desktop App — Frontend
 *
 * Communicates with the Python backend via window.pywebview.api.*
 */

const EVM_RE = /^(0x)?[0-9a-fA-F]{40}$/;
const HEX_KEY_RE = /^(0x)?[0-9a-fA-F]{64}$/;

const ENV_URLS = {
  "https://spacerouter-coordination-api.fly.dev": "Production",
  "https://spacerouter-coordination-api-test.fly.dev": "Test",
};

let statusPollId = null;
let receiptsPollId = null;
let incidentPollId = null;
let currentClaimTaskId = null;
let isTestBuild = false;
let versionModalDismissed = false;  // reset on each node start

// G6 — local transitional flag suppresses the polling-loop button
// label flicker between click and backend-confirmed state change.
// The flag clears once the backend reports the new state OR after a
// hard timeout so a stuck request never freezes the UI.
let nodeTransition = null;  // 'starting' | 'stopping' | null
let nodeTransitionTimer = null;
function setNodeTransition(kind) {
  nodeTransition = kind;
  if (nodeTransitionTimer) clearTimeout(nodeTransitionTimer);
  if (kind) {
    // After 20s, assume the backend won't catch up cleanly and let
    // the polling loop take over. Same upper bound as stop()'s
    // default join timeout.
    nodeTransitionTimer = setTimeout(() => { nodeTransition = null; }, 20000);
  }
}

// ── Helpers ──

function $(selector) {
  return document.querySelector(selector);
}

function show(id) {
  document.getElementById(id).style.display = "flex";
}

function hide(id) {
  document.getElementById(id).style.display = "none";
}

function hideAll() {
  for (const id of [
    "screen-onboarding",
    "screen-status",
    "screen-settings",
    "screen-fresh-restart",
    "screen-network",
    "screen-receipts",
  ]) {
    hide(id);
  }
  if (statusPollId) {
    clearInterval(statusPollId);
    statusPollId = null;
  }
  if (receiptsPollId) {
    clearInterval(receiptsPollId);
    receiptsPollId = null;
  }
}

function truncateAddress(addr) {
  if (!addr || addr.length < 12) return addr || "-";
  return addr.slice(0, 6) + "..." + addr.slice(-4);
}

// ── Environment Selector ──

async function populateEnvSelector() {
  const select = $("#env-select");
  try {
    const envs = await window.pywebview.api.get_environments();
    select.innerHTML = "";
    for (const env of envs) {
      const opt = document.createElement("option");
      opt.value = env.key;
      opt.textContent = env.label;
      if (env.active) opt.selected = true;
      select.appendChild(opt);
    }
    select.addEventListener("change", async function () {
      await window.pywebview.api.set_environment(select.value);
    });
  } catch (e) {
    // Fallback if API not ready
  }
}

function envLabel(envKey) {
  const labels = {
    production: "Production",
    test: "Test (CC Testnet)",
    staging: "Staging",
    local: "Local",
  };
  return labels[envKey] || envKey;
}

// ── Network Setup Screen ──

function initNetworkSetup(onComplete) {
  // Strip old listeners by replacing elements
  for (const sel of ["#btn-network-continue"]) {
    const el = $(sel);
    el.replaceWith(el.cloneNode(true));
  }

  const radios = document.querySelectorAll('input[name="network-mode"]');
  const tunnelConfig = $("#tunnel-config");
  const tunnelHost = $("#tunnel-host");
  const tunnelPort = $("#tunnel-port");
  const continueBtn = $("#btn-network-continue");

  // Show/hide tunnel config
  for (const radio of radios) {
    radio.addEventListener("change", function () {
      tunnelConfig.style.display = this.value === "tunnel" ? "block" : "none";
    });
  }

  continueBtn.addEventListener("click", async function () {
    const selected = document.querySelector('input[name="network-mode"]:checked');
    const mode = selected ? selected.value : "upnp";

    let publicHost = "";
    let port = "";
    if (mode === "tunnel") {
      publicHost = tunnelHost.value.trim();
      if (!publicHost) {
        tunnelHost.classList.add("invalid");
        return;
      }
      tunnelHost.classList.remove("invalid");
      port = tunnelPort.value.trim();

      // F4: if the user pasted "host:port" in the hostname field,
      // split it client-side and populate the port field. Saves the
      // most common bore.pub copy-paste mistake.
      const parsed = parseHostPort(publicHost);
      if (parsed.error) {
        tunnelHost.classList.add("invalid");
        showInlineError(tunnelHost, parsed.error);
        return;
      }
      publicHost = parsed.host;
      if (parsed.port) {
        port = parsed.port;
        tunnelPort.value = port;
      }
      tunnelHost.classList.remove("invalid");
      clearInlineError(tunnelHost);
    }

    continueBtn.disabled = true;
    continueBtn.textContent = "Saving...";

    try {
      const result = await window.pywebview.api.save_network_mode(mode, publicHost, port);
      if (result.ok) {
        onComplete();
      }
    } catch (e) {
      // ignore
    }

    continueBtn.disabled = false;
    continueBtn.textContent = "Continue";
  });
}

// F4 helpers — parse "host:port" into a {host, port} pair, validating
// the port is numeric in 1..65535 and rejecting raw IPv6 addresses
// (which would also contain colons and need bracket-quoting we don't
// support yet).
function parseHostPort(raw) {
  const value = (raw || "").trim();
  if (!value) return { host: "", port: "" };
  // Multiple colons → likely IPv6 without brackets; ambiguous.
  const colonCount = (value.match(/:/g) || []).length;
  if (colonCount > 1) {
    return { error: "Use a hostname or IPv4 IP address (IPv6 not supported here)" };
  }
  if (colonCount === 0) {
    return { host: value, port: "" };
  }
  const [host, portStr] = value.split(":");
  if (!host) {
    return { error: "Hostname is empty" };
  }
  const portNum = parseInt(portStr, 10);
  if (!Number.isFinite(portNum) || String(portNum) !== portStr || portNum < 1 || portNum > 65535) {
    return { error: "Port must be an integer between 1 and 65535" };
  }
  return { host, port: String(portNum) };
}

// rc.6 MIN-1: shared helper to wire a "host:port" blur listener that
// auto-splits the host field on focus-out and populates the matching
// port field. Errors are silent on blur — only the submit path
// surfaces them, so the UX is forgiving of mid-typing values.
function wireHostPortBlur(hostSelector, portSelector) {
  const hostEl = document.querySelector(hostSelector);
  const portEl = document.querySelector(portSelector);
  if (!hostEl || !portEl) return;
  hostEl.addEventListener("blur", function () {
    const raw = hostEl.value.trim();
    if (!raw) return;
    const parsed = parseHostPort(raw);
    if (!parsed.error && parsed.port) {
      hostEl.value = parsed.host;
      portEl.value = parsed.port;
    }
    // Don't show errors on blur — let submit do that.
  });
}

function showInlineError(inputEl, message) {
  // Try to find a sibling .error span; if none, append one once.
  let err = inputEl.parentElement && inputEl.parentElement.querySelector(".error");
  if (!err && inputEl.parentElement) {
    err = document.createElement("span");
    err.className = "error";
    inputEl.parentElement.appendChild(err);
  }
  if (err) err.textContent = message;
}

function clearInlineError(inputEl) {
  const err = inputEl.parentElement && inputEl.parentElement.querySelector(".error");
  if (err) err.textContent = "";
}

async function showNetworkSetup(onComplete) {
  // Pre-fill with current settings
  try {
    const net = await window.pywebview.api.get_network_mode();
    const radio = document.querySelector(
      'input[name="network-mode"][value="' + net.mode + '"]'
    );
    if (radio) radio.checked = true;
    $("#tunnel-config").style.display = net.mode === "tunnel" ? "block" : "none";
    if (net.public_host) {
      $("#tunnel-host").value = net.public_host;
    }
    if (net.port) {
      $("#tunnel-port").value = net.port;
    }
  } catch (e) {}

  initNetworkSetup(onComplete);
  hideAll();
  show("screen-network");
}

// ── Onboarding Screen ──

function initOnboarding() {
  // Strip old listeners by replacing elements
  for (const sel of ["#btn-start"]) {
    const el = $(sel);
    el.replaceWith(el.cloneNode(true));
  }

  const radioGenerate = $("#radio-generate");
  const radioImport = $("#radio-import");
  const importSection = $("#import-key-section");
  const identityKeyInput = $("#identity-key-input");
  const identityKeyError = $("#identity-key-error");
  const stakingInput = $("#staking-input");
  const stakingError = $("#staking-error");
  const collectionInput = $("#collection-input");
  const collectionError = $("#collection-error");
  const btn = $("#btn-start");
  const advancedToggle = $("#advanced-toggle");
  const advancedSection = $("#advanced-section");
  const advancedArrow = $("#advanced-arrow");

  // Environment selector: test builds only
  const envGroup = $("#env-select").parentElement;
  if (isTestBuild) {
    populateEnvSelector();
    envGroup.style.display = "";
  } else {
    envGroup.style.display = "none";
  }

  // ── Identity key mode toggle ──
  function updateKeyMode() {
    if (radioImport.checked) {
      importSection.style.display = "block";
    } else {
      importSection.style.display = "none";
      identityKeyError.textContent = "";
      identityKeyInput.classList.remove("invalid");
    }
    validateForm();
  }

  radioGenerate.addEventListener("change", updateKeyMode);
  radioImport.addEventListener("change", updateKeyMode);

  // ── Import key validation ──
  identityKeyInput.addEventListener("input", function () {
    const val = identityKeyInput.value.trim();
    if (!val) {
      identityKeyError.textContent = "";
      identityKeyInput.classList.remove("invalid");
    } else if (!HEX_KEY_RE.test(val)) {
      identityKeyError.textContent = "Expected 64 hex characters (with or without 0x prefix)";
      identityKeyInput.classList.add("invalid");
    } else {
      identityKeyError.textContent = "";
      identityKeyInput.classList.remove("invalid");
    }
    validateForm();
  });

  // ── Advanced toggle ──
  advancedToggle.addEventListener("click", function () {
    const open = advancedSection.style.display !== "none";
    advancedSection.style.display = open ? "none" : "block";
    advancedArrow.textContent = open ? "▸" : "▾";
  });

  // rc.6 MIN-1: parse pasted "host:port" on blur, not just on submit.
  // The most common bore.pub copy-paste mistake is pasting the whole
  // "subdomain.example.com:1234" string into the host field, then
  // staring at the port "30000" thinking it's correct. Auto-split on
  // blur so the operator sees the corrected fields immediately.
  // Don't show errors on blur — let the submit path render those.
  wireHostPortBlur("#onboard-tunnel-host", "#onboard-tunnel-port");

  // ── Passphrase show/hide toggles ──
  // Two fields, each with its own visibility toggle. We never touch the
  // value — only the input ``type``. This mirrors the CLI wizard's
  // confirm-twice behaviour.
  function wirePassphraseToggle(inputId, toggleId) {
    const input = document.getElementById(inputId);
    const toggle = document.getElementById(toggleId);
    if (!input || !toggle) return;
    toggle.addEventListener("click", function () {
      if (input.type === "password") {
        input.type = "text";
        toggle.textContent = "Hide";
      } else {
        input.type = "password";
        toggle.textContent = "Show";
      }
    });
  }
  wirePassphraseToggle("passphrase-input", "passphrase-show-toggle");
  wirePassphraseToggle("passphrase-confirm-input", "passphrase-confirm-show-toggle");

  // ── Passphrase confirm validation ──
  const passphraseInput = $("#passphrase-input");
  const passphraseConfirmInput = $("#passphrase-confirm-input");
  const passphraseError = $("#passphrase-error");
  function validatePassphrase() {
    const a = passphraseInput.value;
    const b = passphraseConfirmInput.value;
    // Empty is allowed (passphrase is optional). Once the user types
    // anything in the primary, both fields must match.
    if (!a && !b) {
      passphraseError.textContent = "";
      passphraseConfirmInput.classList.remove("invalid");
      return true;
    }
    if (a !== b) {
      passphraseError.textContent = "Passphrases do not match";
      passphraseConfirmInput.classList.add("invalid");
      return false;
    }
    passphraseError.textContent = "";
    passphraseConfirmInput.classList.remove("invalid");
    return true;
  }
  passphraseInput.addEventListener("input", function () {
    validatePassphrase();
    validateForm();
  });
  passphraseConfirmInput.addEventListener("input", function () {
    validatePassphrase();
    validateForm();
  });

  // ── Network mode toggle (in advanced section) ──
  const onboardNetworkRadios = document.querySelectorAll('input[name="onboard-network-mode"]');
  const onboardTunnelConfig = $("#onboard-tunnel-config");
  for (const radio of onboardNetworkRadios) {
    radio.addEventListener("change", function () {
      onboardTunnelConfig.style.display = this.value === "tunnel" ? "block" : "none";
    });
  }

  // ── Optional address validation ──
  function validateAddress(input, errorEl) {
    const val = input.value.trim();
    if (!val) {
      errorEl.textContent = "";
      input.classList.remove("invalid");
      return true;
    }
    if (!EVM_RE.test(val)) {
      errorEl.textContent = "Invalid address — expected 0x followed by 40 hex characters";
      input.classList.add("invalid");
      return false;
    }
    errorEl.textContent = "";
    input.classList.remove("invalid");
    return true;
  }

  stakingInput.addEventListener("input", function () {
    validateAddress(stakingInput, stakingError);
    validateForm();
  });
  collectionInput.addEventListener("input", function () {
    validateAddress(collectionInput, collectionError);
    validateForm();
  });
  $("#referral-input").addEventListener("input", function () {
    const v = this.value.trim();
    const err = $("#referral-error");
    if (!v || (/^[a-zA-Z0-9_-]+$/.test(v) && v.length >= 3 && v.length <= 50)) {
      err.textContent = "";
    } else {
      err.textContent = "Must be 3-50 chars: letters, numbers, hyphens, underscores";
    }
  });

  // ── Form-level enable/disable ──
  function validateForm() {
    const importValid = radioGenerate.checked ||
      (radioImport.checked && HEX_KEY_RE.test(identityKeyInput.value.trim()));
    const stakingValid = validateAddress(stakingInput, stakingError);
    const collectionValid = validateAddress(collectionInput, collectionError);
    const passphraseValid = validatePassphrase();
    btn.disabled = !(importValid && stakingValid && collectionValid && passphraseValid);
  }

  // Enable button immediately for generate mode
  validateForm();

  // ── Submit ──
  btn.addEventListener("click", async function () {
    btn.disabled = true;
    btn.textContent = "Starting...";

    const passphrase = $("#passphrase-input").value;
    const passphraseConfirm = $("#passphrase-confirm-input").value;
    if (passphrase !== passphraseConfirm) {
      // Belt-and-suspenders — validateForm already gates the button,
      // but a programmatic submit could bypass that.
      $("#passphrase-error").textContent = "Passphrases do not match";
      btn.disabled = false;
      btn.textContent = "Start Node";
      return;
    }
    const staking = stakingInput.value.trim();
    const collection = collectionInput.value.trim();
    const identityKeyHex = radioImport.checked ? identityKeyInput.value.trim() : "";

    const referral = $("#referral-input").value.trim();
    if (referral && (referral.length < 3 || referral.length > 50 || !/^[a-zA-Z0-9_-]+$/.test(referral))) {
        $("#referral-error").textContent = "Must be 3-50 chars: letters, numbers, hyphens, underscores";
        btn.disabled = false;
        btn.textContent = "Start Node";
        return;
    }

    // Save network mode from advanced section
    const networkMode = document.querySelector('input[name="onboard-network-mode"]:checked');
    const mode = networkMode ? networkMode.value : "upnp";
    let tunnelHost = mode === "tunnel" ? ($("#onboard-tunnel-host").value.trim() || "") : "";
    let tunnelPort = mode === "tunnel" ? ($("#onboard-tunnel-port").value.trim() || "") : "";
    if (mode === "tunnel" && tunnelHost) {
      // F4: parse "host:port" client-side.
      const parsed = parseHostPort(tunnelHost);
      if (parsed.error) {
        const hostEl = $("#onboard-tunnel-host");
        hostEl.classList.add("invalid");
        showInlineError(hostEl, parsed.error);
        btn.disabled = false;
        btn.textContent = "Start Node";
        return;
      }
      tunnelHost = parsed.host;
      if (parsed.port) {
        tunnelPort = parsed.port;
        $("#onboard-tunnel-port").value = tunnelPort;
      }
    }

    try {
      await window.pywebview.api.save_network_mode(mode, tunnelHost, tunnelPort);
      const result = await window.pywebview.api.save_onboarding_and_start(
        passphrase, staking, collection, identityKeyHex, referral,
      );
      if (result.ok) {
        showStakingModal(function () {
          hideAll();
          showStatus();
        });
      } else {
        // Show error inline (remove any previous error first)
        btn.parentNode.querySelectorAll("p.error").forEach(el => el.remove());
        const errEl = document.createElement("p");
        errEl.className = "error";
        errEl.style.marginTop = "12px";
        errEl.textContent = result.error || "Unknown error";
        btn.parentNode.insertBefore(errEl, btn.nextSibling);
        btn.disabled = false;
        btn.textContent = "Start Node";
      }
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "Start Node";
    }
  });
}

function showOnboarding() {
  hideAll();
  show("screen-onboarding");
  initOnboarding();
}

// ── Staking Modal (overlay) ──

async function showStakingModal(onContinue) {
  const overlay = $("#staking-modal-overlay");

  // Fetch min staking amount from coordination API
  try {
    const minAmount = await window.pywebview.api.get_min_staking_amount();
    $("#staking-modal-body").textContent =
      `Stake at least ${minAmount} $SPACE to start operating your node.`;
  } catch (_) {}

  overlay.style.display = "flex";

  // Strip old listeners
  for (const sel of ["#btn-start-staking", "#btn-staking-skip"]) {
    const el = $(sel);
    el.replaceWith(el.cloneNode(true));
  }

  $("#btn-start-staking").addEventListener("click", function () {
    window.pywebview.api.open_url("https://penguinbase.com/dapp/spacestaking");
    overlay.style.display = "none";
    if (onContinue) onContinue();
  });

  $("#btn-staking-skip").addEventListener("click", function () {
    overlay.style.display = "none";
    if (onContinue) onContinue();
  });
}

// ── Error Report Modal ──

let errorReportShownForKey = null;  // track to show only once per error

function showErrorReportModal() {
  const overlay = $("#error-report-overlay");
  // Reset to initial state
  $("#error-report-title").textContent = "Send Error Report?";
  $("#error-report-body").textContent =
    "Help improve Space Router by sharing this error with the team. " +
    "The report includes error details, node state, and network config. " +
    "No private keys or personal data are sent.";
  overlay.style.display = "flex";

  // Strip old listeners
  for (const sel of ["#btn-send-report", "#btn-skip-report"]) {
    const el = $(sel);
    el.replaceWith(el.cloneNode(true));
  }

  const sendBtn = $("#btn-send-report");
  sendBtn.textContent = "Send Report";
  sendBtn.disabled = false;
  sendBtn.style.display = "";

  $("#btn-skip-report").textContent = "Dismiss";

  sendBtn.addEventListener("click", async function () {
    sendBtn.disabled = true;
    sendBtn.textContent = "Sending...";
    try {
      const result = await window.pywebview.api.send_error_report();
      if (result.ok) {
        $("#error-report-title").textContent = "Report Sent";
        $("#error-report-body").textContent = "Thank you! The report has been sent successfully.";
        sendBtn.style.display = "none";
        $("#btn-skip-report").textContent = "Close";
      } else {
        $("#error-report-body").textContent = "Failed to send report: " + (result.error || "Unknown error");
        sendBtn.textContent = "Retry";
        sendBtn.disabled = false;
      }
    } catch (e) {
      $("#error-report-body").textContent = "Failed to send report.";
      sendBtn.textContent = "Retry";
      sendBtn.disabled = false;
    }
  });

  $("#btn-skip-report").addEventListener("click", function () {
    overlay.style.display = "none";
  });
}

// ── Version Check Modal ──

function handleVersionCheck(vc) {
  if (!vc || versionModalDismissed) return;
  if (vc.status !== "soft_update" && vc.status !== "hard_update") return;

  const overlay = $("#version-modal-overlay");
  const modal = $("#version-modal");
  const title = $("#version-modal-title");
  const body = $("#version-modal-body");
  const info = $("#version-modal-info");
  const downloadUrl = vc.download_url || "https://github.com/space-labs/space-router-node/releases/latest";

  if (vc.status === "hard_update") {
    modal.className = "staking-modal version-modal-hard";
    title.textContent = "Update Required";
    body.textContent =
      "Your version is no longer supported. Please update to continue running your node.";
    info.textContent =
      "Current: " + (vc.current_version || "?") + " \u00b7 Min required: " + (vc.min_version || "?");
  } else {
    modal.className = "staking-modal version-modal-soft";
    title.textContent = "New Version Available";
    body.textContent =
      (vc.latest_version || "A new version") +
      " is now available. Update now to get the latest improvements.";
    info.textContent =
      "Current: " + (vc.current_version || "?") + " \u00b7 Min required: " + (vc.min_version || "any");
  }

  overlay.style.display = "flex";

  // Strip old listeners
  for (const sel of ["#btn-version-download", "#btn-version-dismiss"]) {
    const el = document.querySelector(sel);
    el.replaceWith(el.cloneNode(true));
  }

  document.querySelector("#btn-version-download").addEventListener("click", function () {
    window.pywebview.api.open_url(downloadUrl);
    overlay.style.display = "none";
    versionModalDismissed = true;
  });

  document.querySelector("#btn-version-dismiss").addEventListener("click", function () {
    overlay.style.display = "none";
    versionModalDismissed = true;
  });
}

function updateSettingsVersionStatus(vc) {
  const el = $("#settings-version-status");
  if (!el) return;
  if (!vc || vc.status === "unknown") {
    el.textContent = "";
    return;
  }
  if (vc.status === "up_to_date") {
    el.textContent = "You're on the latest version (" + (vc.current_version || "") + ")";
    el.className = "version-status";
  } else if (vc.status === "soft_update") {
    el.textContent = "Update available: " + (vc.latest_version || "");
    el.className = "version-status has-update";
  } else if (vc.status === "hard_update") {
    el.textContent = "Update required \u2014 minimum " + (vc.min_version || "");
    el.className = "version-status needs-update";
  }
}

// ── Status Dashboard ──

// Coord-recovery sub-line — surfaces the daemon's self-probe loop state so
// the operator can see recovery progress when local says "running" but
// coord considers the node offline/draining/unknown. Only renders while
// the local state is "running"; cleared on every tick by updateStatus().
function _coordHintEl() {
  let el = document.getElementById("coord-recovery-hint");
  if (!el) {
    const card = document.getElementById("status-card");
    const detail = document.getElementById("status-detail");
    if (!card || !detail) return null;
    el = document.createElement("div");
    el.id = "coord-recovery-hint";
    el.className = "status-detail text-muted";
    // Smaller, dimmer than the main detail line.
    el.style.fontSize = "11px";
    el.style.marginTop = "2px";
    detail.parentNode.insertBefore(el, detail.nextSibling);
  }
  return el;
}

function clearCoordRecoveryHint() {
  const el = _coordHintEl();
  if (el) {
    el.textContent = "";
    el.style.display = "none";
  }
}

function renderCoordRecoveryHint(status) {
  const el = _coordHintEl();
  if (!el) return;

  const cs = status.coord_status;
  // Coord agrees, no payload yet, or coord is happy — nothing to show.
  if (!cs || cs === "—" || cs === "online" || cs === "active") {
    el.textContent = "";
    el.style.display = "none";
    return;
  }

  let suffix;
  const outcome = status.last_probe_outcome;
  const nextAt = status.next_probe_attempt_at;
  if (outcome === "rate_limited") {
    suffix = "last probe rate-limited";
  } else if (outcome === "escalated") {
    suffix = "escalating to reconnect";
  } else if (typeof nextAt === "number" && nextAt > 0) {
    const secs = Math.max(0, Math.round(nextAt - Date.now() / 1000));
    suffix = "next probe in " + secs + "s";
  } else if (outcome === "cooldown") {
    suffix = "probe in cooldown";
  } else {
    // Default fallback — coord_status only.
    el.textContent = "Coord sees: " + cs;
    el.style.display = "block";
    return;
  }

  el.textContent = "Coord sees: " + cs + " · " + suffix;
  el.style.display = "block";
}

function showStatus() {
  show("screen-status");
  updateStatus();
  updateEarningsRow();
  if (statusPollId) clearInterval(statusPollId);
  statusPollId = setInterval(function () {
    updateStatus();
    // Earnings row refreshes on a slower cadence (10s vs 3s) — store
    // queries are cheap but the row doesn't need to be real-time.
    if (Date.now() % 10000 < 3100) updateEarningsRow();
  }, 3000);
}

async function updateStatus() {
  try {
    const status = await window.pywebview.api.get_status();

    const dot = $("#status-dot");
    const text = $("#status-text");
    const detail = $("#status-detail");
    const stakingEl = $("#staking-address");
    const collectionEl = $("#collection-address");
    const envBadge = $("#env-badge");
    const errorBanner = $("#error-banner");
    const errorText = $("#error-text");
    const certWarning = $("#cert-warning");
    const btnRetry = $("#btn-retry");
    const btnStartNode = $("#btn-start-node");
    const btnStop = $("#btn-stop");

    // Wallet addresses (truncated, full on hover)
    const fullStaking = status.staking_address || status.wallet || "";
    const fullCollection = status.collection_address || "";
    const fullIdentity = status.identity_address || status.node_id || "";
    stakingEl.textContent = truncateAddress(fullStaking) || "-";
    stakingEl.title = fullStaking;
    collectionEl.textContent = truncateAddress(fullCollection) || "-";
    collectionEl.title = fullCollection;
    // F2: surface the identity address (the wallet derived from the
    // local key) so operators can verify it matches what they staked
    // against without digging through logs.
    const identityEl = $("#identity-address");
    if (identityEl) {
      identityEl.textContent = truncateAddress(fullIdentity) || "-";
      identityEl.title = fullIdentity;
    }

    // Staking status display
    const stakingStatusEl = $("#staking-status");
    const ss = status.staking_status || "—";
    // F3 — surface a clear label for the unstaked state. Pre-rc.5 the
    // raw "unstaked" lower-case token leaked into the GUI; map to a
    // capitalised "Unstaked — stake required" so it reads as a
    // status rather than a typo.
    let stakingStatusLabel;
    if (ss === "unstaked") {
      stakingStatusLabel = "Unstaked — stake required";
    } else if (ss === "earning" || ss === "qualifying") {
      // Capitalise for consistency with the new "Unstaked" label.
      stakingStatusLabel = ss.charAt(0).toUpperCase() + ss.slice(1);
    } else {
      stakingStatusLabel = ss;
    }
    stakingStatusEl.textContent = stakingStatusLabel;
    stakingStatusEl.className = "wallet-value"
      + (ss === "earning" ? " staking-earning"
        : ss === "qualifying" ? " staking-qualifying"
        : ss === "unstaked" ? " staking-unstaked"
        : "");

    // State-based display
    const state = status.state || "idle";

    // Clear the coord-recovery hint by default; the running branch
    // re-renders it when coord disagrees with the local state.
    clearCoordRecoveryHint();

    // Passphrase required — show unlock dialog immediately. Surface the
    // state machine's detail (e.g. "incorrect" after a failed attempt)
    // so the user knows their previous try landed but wasn't accepted.
    if (state === "passphrase_required") {
      showUnlockDialog(status.detail || "");
      return;
    }

    // Environment badge
    if (status.environment && status.environment !== "production") {
      envBadge.textContent = envLabel(status.environment);
      envBadge.style.display = "block";
    } else {
      envBadge.style.display = "none";
    }

    switch (state) {
      case "idle":
        dot.className = "dot dot-idle";
        text.textContent = "Node is stopped";
        detail.textContent = "";
        errorReportShownForKey = null;
        break;
      case "initializing":
        dot.className = "dot dot-starting";
        text.textContent = "Initializing...";
        detail.textContent = status.detail || "Loading certificates";
        errorReportShownForKey = null;
        break;
      case "binding":
        dot.className = "dot dot-starting";
        text.textContent = "Starting server...";
        detail.textContent = status.detail || "";
        errorReportShownForKey = null;
        break;
      case "registering":
        dot.className = "dot dot-starting";
        text.textContent = "Registering...";
        detail.textContent = status.detail || "";
        errorReportShownForKey = null;
        break;
      case "running":
        dot.className = "dot dot-running";
        text.textContent = "SpaceRouter is running";
        detail.textContent = status.detail || "";
        // Surface the gap between local "running" and coord-side state so an
        // operator can see the daemon's self-probe recovery in flight. Only
        // render when coord disagrees (offline/draining/unknown/etc).
        renderCoordRecoveryHint(status);
        errorReportShownForKey = null;
        break;
      case "reconnecting":
        dot.className = "dot dot-reconnecting";
        text.textContent = "Reconnecting...";
        detail.textContent = status.detail || "";
        errorReportShownForKey = null;
        break;
      case "error_transient":
        dot.className = "dot dot-reconnecting";
        text.textContent = "Retrying...";
        // Show countdown if next_retry_at is set
        if (status.next_retry_at) {
          const secsLeft = Math.max(0, Math.ceil(status.next_retry_at - Date.now() / 1000));
          detail.textContent = secsLeft > 0
            ? status.detail + " (" + secsLeft + "s)"
            : status.detail;
        } else {
          detail.textContent = status.detail || "";
        }
        break;
      case "error_permanent":
        dot.className = "dot dot-stopped";
        text.textContent = "Error";
        // Use error_code for user-friendly messages.
        // For codes where the server provides a specific detail (e.g. exact stake
        // amounts), prefer status.error_message over canned text.
        if (status.error_code === "identity_key_locked") {
          showUnlockDialog(status.error || status.detail || "");
          return;
        } else if (status.error_code === "version_too_old") {
          detail.textContent = status.error_message || "This version is outdated. Please download the latest update.";
        } else if (status.error_code === "ip_conflict") {
          detail.textContent = status.error_message || "Another node is already using this IP address. Only one node per IP is allowed.";
        } else if (status.error_code === "wallet_conflict") {
          detail.textContent = status.error_message || "Wallet address is already registered to another node.";
        } else if (status.error_code === "registration_rejected") {
          detail.textContent = status.error_message || "Registration rejected. Check your staking balance and wallet address.";
        } else if (status.error_code === "staking_insufficient") {
          detail.textContent = status.error_message || "Insufficient SPACE staked. Check your staking balance.";
        } else if (status.error_code === "staking_locked") {
          detail.textContent = status.error_message || "Staking account is locked. Unlock your stake on-chain.";
        } else if (status.error_code === "anonymous_ip") {
          detail.textContent = status.error_message || "Anonymous IP detected. VPN, proxy, and Tor connections are not allowed.";
        } else if (status.error_code === "ip_classification_unavailable") {
          detail.textContent = status.error_message || "IP classification service temporarily unavailable.";
        } else if (status.error_code === "timestamp_expired") {
          detail.textContent = status.error_message || "Request timestamp expired. Check your system clock.";
        } else if (status.error_code === "endpoint_unreachable") {
          detail.textContent = status.error_message || "Coordination server cannot reach this node.";
        } else if (status.error_code === "rate_limited") {
          detail.textContent = "Too many requests. Waiting before retry...";
        } else if (status.error_code === "connection_lost") {
          detail.textContent = "Connection to coordination server interrupted. Retrying...";
        } else if (status.error_code === "network_unreachable") {
          detail.textContent = "Cannot reach coordination server. Check your internet connection.";
        } else if (status.error_code === "invalid_wallet") {
          detail.textContent = "Invalid wallet address. Use Fresh Restart to reconfigure.";
        } else if (status.error_code === "port_permission") {
          detail.textContent = "Port permission denied. Use a port above 1024.";
        } else if (status.error_code === "port_in_use") {
          detail.textContent = "Port is already in use by another application.";
        } else {
          detail.textContent = status.error_message || status.error || "";
        }
        break;
      case "stopping":
        dot.className = "dot dot-starting";
        text.textContent = "Shutting down...";
        detail.textContent = "";
        break;
      default:
        dot.className = "dot dot-stopped";
        text.textContent = "Stopped";
        detail.textContent = "";
    }

    // Show error report modal:
    // - Immediately on permanent errors
    // - After 3+ retries for persistent transient errors (once per error type)
    if (status.error_report_available) {
      const showNow =
        state === "error_permanent" ||
        (state === "error_transient" && (status.retry_count || 0) >= 3);

      if (showNow) {
        const reportKey = status.error_code || "unknown";
        if (errorReportShownForKey !== reportKey) {
          errorReportShownForKey = reportKey;
          showErrorReportModal();
        }
      }
    }

    // Version check modal + settings status
    if (status.version_check) {
      handleVersionCheck(status.version_check);
      updateSettingsVersionStatus(status.version_check);
    }

    // Error display
    if (status.error && state !== "error_transient" && state !== "passphrase_required") {
      errorText.textContent = status.error;
      errorBanner.style.display = "block";
    } else {
      errorBanner.style.display = "none";
    }

    // Cert expiry warning
    certWarning.style.display = status.cert_expiry_warning ? "block" : "none";

    // Action buttons.
    //
    // G6 fix: while a click-driven transition is in flight (the user
    // has just hit Start or Stop and we haven't seen the backend
    // confirm yet) we keep the *clicked* button visible with its
    // "Starting…/Stopping…" disabled label rather than letting the
    // poll-driven branch flip it back to the opposite state for one
    // tick. The transition flag clears as soon as the backend state
    // changes (or after a hard 20s timeout, see setNodeTransition).
    if (nodeTransition === "starting" && (state === "idle" || !state)) {
      btnRetry.style.display = "none";
      btnStartNode.style.display = "block";
      btnStartNode.disabled = true;
      btnStartNode.textContent = "Starting...";
      btnStop.style.display = "none";
      return;
    }
    if (nodeTransition === "stopping" && state !== "idle"
        && state !== "error_permanent") {
      btnRetry.style.display = "none";
      btnStartNode.style.display = "none";
      btnStop.style.display = "block";
      btnStop.disabled = true;
      btnStop.textContent = "Stopping...";
      return;
    }
    // Backend has caught up — clear the flag and render the
    // canonical button for the current state.
    if (nodeTransition === "starting" && state !== "idle") {
      setNodeTransition(null);
    } else if (nodeTransition === "stopping"
               && (state === "idle" || state === "error_permanent")) {
      setNodeTransition(null);
    }

    if (state === "error_permanent") {
      btnRetry.style.display = "block";
      btnStartNode.style.display = "none";
      btnStop.style.display = "none";
    } else if (state === "idle") {
      btnRetry.style.display = "none";
      btnStartNode.style.display = "block";
      btnStartNode.disabled = false;
      btnStartNode.textContent = "Start";
      btnStop.style.display = "none";
    } else {
      btnRetry.style.display = "none";
      btnStartNode.style.display = "none";
      btnStop.style.display = "block";
      btnStop.disabled = false;
      btnStop.textContent = "Stop";
    }
  } catch (e) {
    // Backend not ready yet — ignore
  }
}

// ── Fresh Restart ──

function initFreshRestart() {
  const confirmBtn = $("#btn-restart-confirm");
  const confirmInput = $("#reset-confirm-input");

  $("#btn-fresh-restart").addEventListener("click", function () {
    // Reset button state — RESET-typing gating starts disabled.
    confirmBtn.disabled = true;
    confirmBtn.textContent = "Confirm Reset";
    if (confirmInput) {
      confirmInput.value = "";
    }
    hideAll();
    show("screen-fresh-restart");
    if (confirmInput) {
      // Focus the input so the user lands ready to type.
      setTimeout(function () { confirmInput.focus(); }, 50);
    }
  });

  $("#btn-restart-cancel").addEventListener("click", function () {
    hideAll();
    showStatus();
  });

  if (confirmInput) {
    // The confirm button is gated behind the user typing exactly "RESET".
    // Case-sensitive on purpose — typo-protection. Whitespace tolerated.
    confirmInput.addEventListener("input", function () {
      const matches = confirmInput.value.trim() === "RESET";
      confirmBtn.disabled = !matches;
    });
  }

  confirmBtn.addEventListener("click", async function () {
    if (confirmBtn.disabled) return;
    await doFreshRestart();
  });
}

async function doFreshRestart() {
  const btn = $("#btn-restart-confirm");
  btn.disabled = true;
  btn.textContent = "Resetting...";

  try {
    const result = await window.pywebview.api.fresh_restart();
    if (!result.ok) {
      btn.disabled = false;
      btn.textContent = "Confirm Reset";
      return;
    }

    // Go directly to onboarding
    versionModalDismissed = false;
    hideAll();
    show("screen-onboarding");
    initOnboarding();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Confirm Reset";
  }
}

// ── Action Buttons (Retry / Stop) ──

function initActionButtons() {
  $("#btn-retry").addEventListener("click", async function () {
    const btn = $("#btn-retry");
    btn.disabled = true;
    btn.textContent = "Retrying...";
    try {
      await window.pywebview.api.retry_node();
    } catch (e) {}
    btn.disabled = false;
    btn.textContent = "Retry";
  });

  // G6: suppress the Start → Starting → Start → Stop flicker. We hold
  // the "Starting…" label until the polling loop sees a non-IDLE
  // state, at which point the action-buttons block in updateStatus()
  // hides this button entirely.
  $("#btn-start-node").addEventListener("click", async function () {
    const btn = $("#btn-start-node");
    setNodeTransition("starting");
    btn.disabled = true;
    btn.textContent = "Starting...";
    versionModalDismissed = false;
    try {
      const result = await window.pywebview.api.start_node();
      // rc.7 BLK-new: the daemon's pre-flight passphrase gate (added in
      // rc.5) returns early without spawning the node thread when the
      // keystore is encrypted but no passphrase is in env. The state
      // machine therefore never enters PASSPHRASE_REQUIRED — the poll
      // loop's state-driven dialog won't fire. Surface it explicitly.
      if (result && result.error_code === "PASSPHRASE_REQUIRED") {
        setNodeTransition(null);
        btn.disabled = false;
        btn.textContent = "Start";
        showUnlockDialog(result.error || "");
        return;
      }
    } catch (e) {}
    // Don't reset the label here. The polling loop will drive the
    // visible button based on the new state once the backend
    // transitions out of IDLE; setNodeTransition keeps Start hidden
    // in the meantime.
  });

  // Same for Stop. We blank the staking_status synchronously
  // server-side (G5) and hold the "Stopping…" label until the loop
  // confirms the transition.
  $("#btn-stop").addEventListener("click", async function () {
    const btn = $("#btn-stop");
    setNodeTransition("stopping");
    btn.disabled = true;
    btn.textContent = "Stopping...";
    try {
      await window.pywebview.api.stop_node();
    } catch (e) {}
  });
}

// ── Settings Panel ──

function initSettings() {
  const envSelect = $("#settings-env");
  const customUrl = $("#settings-custom-url");
  const mtlsToggle = $("#settings-mtls");
  const mtlsLabel = $("#mtls-label");
  const mtlsWarning = $("#mtls-warning");
  const saveBtn = $("#btn-save-settings");
  const statusEl = $("#settings-status");
  const networkRadios = document.querySelectorAll('input[name="settings-network-mode"]');
  const tunnelConfig = $("#settings-tunnel-config");
  const tunnelHost = $("#settings-tunnel-host");
  const tunnelPort = $("#settings-tunnel-port");

  // Show test-only settings groups
  if (isTestBuild) {
    $("#settings-env-group").style.display = "";
    $("#settings-mtls-group").style.display = "";
  }

  // Show/hide tunnel config
  for (const radio of networkRadios) {
    radio.addEventListener("change", function () {
      tunnelConfig.style.display = this.value === "tunnel" ? "block" : "none";
    });
  }

  // rc.6 MIN-1: parse pasted "host:port" on blur (settings dialog).
  // Same UX nicety as the onboarding wizard.
  wireHostPortBlur("#settings-tunnel-host", "#settings-tunnel-port");

  // Show/hide custom URL input based on dropdown
  envSelect.addEventListener("change", function () {
    if (envSelect.value === "custom") {
      customUrl.style.display = "block";
      customUrl.focus();
    } else {
      customUrl.style.display = "none";
    }
  });

  // mTLS toggle warning
  mtlsToggle.addEventListener("change", function () {
    const enabled = mtlsToggle.checked;
    mtlsLabel.textContent = enabled ? "Enabled" : "Disabled";
    mtlsWarning.style.display = enabled ? "none" : "block";
  });

  // Open settings
  $("#btn-settings").addEventListener("click", async function () {
    // Load current network mode
    try {
      const net = await window.pywebview.api.get_network_mode();
      const radio = document.querySelector(
        'input[name="settings-network-mode"][value="' + net.mode + '"]'
      );
      if (radio) radio.checked = true;
      tunnelConfig.style.display = net.mode === "tunnel" ? "block" : "none";
      tunnelHost.value = net.public_host || "";
      tunnelPort.value = net.port || "";
    } catch (e) {}

    // Load current settings (test builds)
    if (isTestBuild) {
      try {
        const settings = await window.pywebview.api.get_settings();
        const url = settings.coordination_api_url;

        // Set dropdown value
        if (ENV_URLS[url]) {
          envSelect.value = url;
          customUrl.style.display = "none";
        } else {
          envSelect.value = "custom";
          customUrl.value = url;
          customUrl.style.display = "block";
        }

        // Set mTLS toggle
        mtlsToggle.checked = settings.mtls_enabled;
        mtlsLabel.textContent = settings.mtls_enabled ? "Enabled" : "Disabled";
        mtlsWarning.style.display = settings.mtls_enabled ? "none" : "block";
      } catch (e) {
        // Use defaults
      }
    }

    // Auto-claim panel — load current config + status into the form.
    await loadAutoClaimPanel();

    statusEl.textContent = "";
    hideAll();
    show("screen-settings");
  });

  // Back button
  $("#btn-back").addEventListener("click", function () {
    hideAll();
    showStatus();
  });

  // Save settings
  saveBtn.addEventListener("click", async function () {
    // Validate tunnel config
    const selectedMode = document.querySelector('input[name="settings-network-mode"]:checked');
    const mode = selectedMode ? selectedMode.value : "upnp";
    if (mode === "tunnel" && !tunnelHost.value.trim()) {
      statusEl.textContent = "Please enter a public hostname for tunnel mode";
      statusEl.style.color = "#e74c3c";
      tunnelHost.classList.add("invalid");
      return;
    }
    tunnelHost.classList.remove("invalid");

    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";
    statusEl.textContent = "";

    try {
      // Save network mode (all builds)
      await window.pywebview.api.save_network_mode(
        mode,
        mode === "tunnel" ? tunnelHost.value.trim() : "",
        mode === "tunnel" ? tunnelPort.value.trim() : "",
      );

      // Save auto-claim config (all builds; harmless when escrow off)
      const acResult = await saveAutoClaimPanel();
      if (acResult && !acResult.ok) {
        statusEl.textContent = acResult.error || "Auto-claim save failed";
        statusEl.style.color = "#e74c3c";
        saveBtn.disabled = false;
        saveBtn.textContent = "Save & Restart Node";
        return;
      }

      // Save API URL and mTLS (test builds only)
      if (isTestBuild) {
        let url = envSelect.value;
        if (url === "custom") {
          url = customUrl.value.trim();
          if (!url) {
            statusEl.textContent = "Please enter a custom URL";
            statusEl.style.color = "#e74c3c";
            saveBtn.disabled = false;
            saveBtn.textContent = "Save & Restart Node";
            return;
          }
        }

        const mtlsEnabled = mtlsToggle.checked;
        const result = await window.pywebview.api.save_settings(url, mtlsEnabled);
        if (!result.ok) {
          statusEl.textContent = result.error || "Failed to save";
          statusEl.style.color = "#e74c3c";
          saveBtn.disabled = false;
          saveBtn.textContent = "Save & Restart Node";
          return;
        }

        // Update test banner env label
        updateTestBannerLabel(url);
      }

      // Restart node with new settings
      statusEl.textContent = "Restarting node...";
      statusEl.style.color = "#8080a0";

      await window.pywebview.api.stop_node();
      const startResult = await window.pywebview.api.start_node();

      saveBtn.disabled = false;
      saveBtn.textContent = "Save & Restart Node";

      // Go back to status
      hideAll();
      showStatus();

      // rc.7 BLK-new: pre-flight passphrase gate may have returned early
      // without starting the node. Surface the unlock dialog so the
      // status screen doesn't sit on "Starting..." indefinitely.
      if (startResult && startResult.error_code === "PASSPHRASE_REQUIRED") {
        showUnlockDialog(startResult.error || "");
      }
    } catch (e) {
      statusEl.textContent = "Failed to save settings";
      statusEl.style.color = "#e74c3c";
      saveBtn.disabled = false;
      saveBtn.textContent = "Save & Restart Node";
    }
  });
}

function updateTestBannerLabel(url) {
  const label = $("#test-env-label");
  if (!label) return;
  const envName = ENV_URLS[url];
  label.textContent = envName ? "— " + envName : "— Custom";
}

// ── Passphrase Unlock Dialog ──

function showUnlockDialog(hint) {
  show("dialog-overlay");
  if (statusPollId) {
    clearInterval(statusPollId);
    statusPollId = null;
  }

  const btn = $("#btn-unlock");
  const input = $("#unlock-passphrase");
  const errEl = $("#unlock-error");

  // Pre-populate the error label when we re-enter this dialog after a
  // wrong-passphrase attempt — the state machine's transition reason
  // contains "incorrect" so the user gets visible feedback that their
  // prior try landed but didn't decrypt the keystore.
  if (errEl) {
    errEl.textContent = hint && /incorrect/i.test(hint) ? hint : "";
  }

  // Prevent duplicate listeners
  const newBtn = btn.cloneNode(true);
  btn.parentNode.replaceChild(newBtn, btn);

  newBtn.addEventListener("click", async function () {
    const passphrase = input.value;
    if (!passphrase) {
      errEl.textContent = "Passphrase is required";
      return;
    }
    newBtn.disabled = true;
    newBtn.textContent = "Unlocking...";
    errEl.textContent = "";

    try {
      const result = await window.pywebview.api.unlock_and_start(passphrase);
      if (result.ok) {
        hide("dialog-overlay");
        input.value = "";
        showStatus();
      } else {
        errEl.textContent = result.error || "Incorrect passphrase";
        newBtn.disabled = false;
        newBtn.textContent = "Unlock";
      }
    } catch (e) {
      errEl.textContent = "Failed to connect to backend";
      newBtn.disabled = false;
      newBtn.textContent = "Unlock";
    }
  });
}

// ── Initialisation ──

async function initTestVariant() {
  try {
    const variant = await window.pywebview.api.get_build_variant();
    isTestBuild = variant === "test";

    if (isTestBuild) {
      // Show test banner
      const banner = document.getElementById("test-banner");
      banner.style.display = "block";
      document.body.classList.add("has-test-banner");

      // Load current env for banner label
      try {
        const settings = await window.pywebview.api.get_settings();
        updateTestBannerLabel(settings.coordination_api_url);
      } catch (e) {}
    }

    // Init settings panel for all builds (network mode is always editable)
    initSettings();
  } catch (e) {
    // Variant check failed — continue as production, still init settings
    initSettings();
  }
}

async function init() {
  try {
    const needsOnboarding = await window.pywebview.api.needs_onboarding();

    // Determine build variant before showing any screens
    await initTestVariant();

    // Display build version on every surface that has a version-label slot
    // (status footer + onboarding screen — F1).
    try {
      const version = await window.pywebview.api.get_build_version();
      if (version) {
        for (const id of ["version-label", "version-label-onboarding"]) {
          const el = document.getElementById(id);
          if (el) el.textContent = version;
        }
      }
    } catch (e) {}

    // Action buttons
    initFreshRestart();
    initActionButtons();
    initReceiptsScreen();

    // Sticky incident banner (auto-claim failures persist to disk).
    startIncidentPoll();

    if (needsOnboarding) {
      showOnboarding();
    } else {
      // Already configured — show status, then maybe overlay staking modal.
      const startResult = await window.pywebview.api.start_node();
      showStatus();
      // rc.7 BLK-new: when the keystore is encrypted (operator set a
      // passphrase during onboarding), start_node's pre-flight gate
      // returns PASSPHRASE_REQUIRED without spawning the daemon. The
      // state machine stays at IDLE so the status poll never fires the
      // unlock dialog — the GUI used to sit on "Starting..." forever
      // (Woojung's rc.5/rc.6 regression). Surface the dialog explicitly
      // and skip the staking modal until the daemon is actually up.
      if (startResult && startResult.error_code === "PASSPHRASE_REQUIRED") {
        showUnlockDialog(startResult.error || "");
        return;
      }
      // rc.6 MIN-3: pre-rc.6 we showed the staking modal on every startup,
      // even for wallets that were already earning rewards — operators
      // had to click past it every single launch. Only nag when the wallet
      // hasn't actually staked yet ("unstaked"/"inactive"/"—" sentinel
      // value before the first probe lands).
      try {
        const status = await window.pywebview.api.get_status();
        const ss = status && status.staking_status;
        if (ss !== "earning" && ss !== "qualifying") {
          showStakingModal();
        }
      } catch (e) {
        // Status fetch failed — fall back to old behaviour so the
        // operator can still see the modal if they truly haven't staked.
        showStakingModal();
      }
    }
  } catch (e) {
    // pywebview.api not ready — retry
    setTimeout(init, 200);
  }
}

// ─────────────────────────────────────────────────────────────────
// Error catalog — friendly modal for known daemon-side error codes.
// Codes match the constants in gui/api.py. The Python side detects
// the pattern; we render the human strings here so we can edit them
// without a daemon redeploy.
// ─────────────────────────────────────────────────────────────────

const FAUCET_URL = "https://faucet.creditcoin.org/";
const SUPPORT_URL = "https://docs.spacerouter.io/troubleshooting";

const ERROR_CATALOG = {
  insufficient_gas: {
    title: "Out of CTC for gas",
    body: "Your wallet doesn't have enough CTC to pay the network fee for this claim.",
    remediationHtml:
      "Get CTC from the <a href=\"#\" data-faucet>faucet</a>. After funding the wallet, click Retry.",
    primaryLabel: "Open faucet",
    primaryAction: () => window.pywebview.api.open_url(FAUCET_URL),
  },
  coord_unreachable: {
    title: "Cannot reach coordination API",
    body: "The node can't talk to the coordination service right now.",
    remediationHtml: "Trying to reconnect every 30 seconds. Check your internet connection if this persists.",
  },
  chain_rpc_unreachable: {
    title: "Cannot reach chain RPC",
    body: "The node can't talk to the Creditcoin RPC endpoint right now.",
    remediationHtml: "Trying to reconnect every 30 seconds. Check your internet connection if this persists.",
  },
  identity_key_missing: {
    title: "Identity key missing or unreadable",
    body: "The node identity key file could not be loaded.",
    remediationHtml: "Re-run setup (Reset Node) to regenerate it. Back up your wallet first.",
  },
  rate_mismatch: {
    title: "Rate config out of sync",
    body: "The configured price-per-GB doesn't match what the gateway is reporting.",
    remediationHtml: "Restart the node to re-sync the rate from the gateway.",
  },
  receipt_db_locked: {
    title: "Receipt database is locked",
    body: "The local receipt database can't be opened — another process may be holding the file.",
    remediationHtml: "Stop and restart the node. If the error persists, back up <code>~/.spacerouter/receipts.db</code> and restart.",
  },
  stake_not_approved: {
    title: "Stake awaiting approval",
    body: "Your stake has been registered but isn't approved yet.",
    remediationHtml: "Approval typically takes 5–30 minutes. The node will start automatically once it goes through.",
  },
  disk_full: {
    title: "Disk full",
    body: "There's no space left on the device the node is writing to.",
    remediationHtml: "Free up space and restart the node.",
  },
  upnp_nat_blocked: {
    title: "Node not reachable from internet",
    body: "UPnP failed and we couldn't auto-detect a working public IP.",
    remediationHtml: "Switch to <strong>Manual / Tunnel</strong> mode in Settings, or configure port forwarding on your router.",
  },
  sleep_resume: {
    title: "Resumed from sleep",
    body: "Reconnecting after the system woke from sleep.",
    remediationHtml: "No action required — the node will reconnect automatically.",
  },
};

// Mirror of the Python-side classify_error_text — used when an error
// surfaces via the task-error path (no error_code attached).
const ERROR_PATTERNS = [
  [/insufficient funds for gas|intrinsic gas too low/i, "insufficient_gas"],
  [/coordination[- ]api|coord(ination)?\s+(api\s+)?unreachable/i, "coord_unreachable"],
  [/chain rpc|rpc.*unreachable|cc3-testnet|max retries.*rpc/i, "chain_rpc_unreachable"],
  [/identity key.*(not found|missing|corrupt)|cannot load identity/i, "identity_key_missing"],
  [/rate.*(mismatch|out of sync|differs)/i, "rate_mismatch"],
  [/database is locked|sqlite.*locked|disk i\/o error/i, "receipt_db_locked"],
  [/stake.*(not.*approved|awaiting approval|pending approval)/i, "stake_not_approved"],
  [/no space left on device|disk full|enospc/i, "disk_full"],
  [/upnp.*(failed|unavailable|blocked|nat)|cannot detect public ip/i, "upnp_nat_blocked"],
];

function classifyErrorTextLocal(text) {
  if (!text) return "unknown";
  for (const [re, code] of ERROR_PATTERNS) {
    if (re.test(text)) return code;
  }
  return "unknown";
}

function showErrorCatalogModal(code, contextMessage) {
  const entry = ERROR_CATALOG[code];
  const overlay = $("#error-catalog-overlay");
  const titleEl = $("#error-catalog-title");
  const bodyEl = $("#error-catalog-body");
  const remediationEl = $("#error-catalog-remediation");
  const primaryBtn = $("#btn-error-primary");
  const secondaryBtn = $("#btn-error-secondary");

  if (!entry) {
    titleEl.textContent = "Something went wrong";
    bodyEl.textContent = contextMessage || "An unexpected error occurred.";
    remediationEl.innerHTML =
      "Try again, or restart the node. If the problem persists, "
      + "<a href=\"#\" data-support>contact support</a>.";
    primaryBtn.style.display = "none";
  } else {
    titleEl.textContent = entry.title;
    bodyEl.textContent = entry.body;
    remediationEl.innerHTML = entry.remediationHtml || "";
    if (entry.primaryLabel && entry.primaryAction) {
      primaryBtn.style.display = "block";
      primaryBtn.textContent = entry.primaryLabel;
      // Replace listener safely.
      const fresh = primaryBtn.cloneNode(true);
      primaryBtn.parentNode.replaceChild(fresh, primaryBtn);
      fresh.addEventListener("click", () => {
        try { entry.primaryAction(); } catch (e) {}
        overlay.style.display = "none";
      });
    } else {
      primaryBtn.style.display = "none";
    }
  }

  // Wire any [data-faucet] / [data-support] anchors in remediation.
  remediationEl.querySelectorAll("a[data-faucet]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      window.pywebview.api.open_url(FAUCET_URL);
    });
  });
  remediationEl.querySelectorAll("a[data-support]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      window.pywebview.api.open_url(SUPPORT_URL);
    });
  });

  // Strip and rebind the close button.
  const freshClose = secondaryBtn.cloneNode(true);
  secondaryBtn.parentNode.replaceChild(freshClose, secondaryBtn);
  freshClose.addEventListener("click", () => {
    overlay.style.display = "none";
  });

  overlay.style.display = "flex";
}

// ─────────────────────────────────────────────────────────────────
// Sticky incident banner — auto-claim failure UX, persists across
// GUI restarts via ~/.spacerouter/incidents.json (written by the
// daemon when an auto-claim attempt raises).
// ─────────────────────────────────────────────────────────────────

let incidentBannerVisibleId = null;

async function refreshIncidentBanner() {
  let resp;
  try {
    resp = await window.pywebview.api.get_incidents();
  } catch (e) {
    return;
  }
  if (!resp || !resp.ok) return;

  const items = resp.incidents || [];
  const last = [...items].reverse().find(
    (i) => i && i.kind === "auto_claim_failed" && !i.acknowledged,
  );

  const banner = document.getElementById("incident-banner");
  if (!banner) return;

  if (!last) {
    banner.style.display = "none";
    document.body.classList.remove("has-incident-banner");
    incidentBannerVisibleId = null;
    return;
  }

  // Render once per incident id so we don't reset its state on every
  // poll (which would re-bind listeners and steal focus).
  if (incidentBannerVisibleId === last.id) return;
  incidentBannerVisibleId = last.id;

  const when = last.at
    ? new Date(last.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : "";
  $("#incident-banner-title").textContent =
    "Auto-claim failed" + (when ? " at " + when : "");
  $("#incident-banner-text").textContent =
    "Reason: " + (last.message || last.code || "unknown");

  banner.style.display = "flex";
  document.body.classList.add("has-incident-banner");

  // Strip & rebind action listeners.
  const ids = ["btn-incident-log", "btn-incident-retry",
               "btn-incident-disable", "btn-incident-dismiss"];
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) continue;
    const fresh = el.cloneNode(true);
    el.parentNode.replaceChild(fresh, el);
  }

  document.getElementById("btn-incident-log").addEventListener("click", showLogViewer);
  document.getElementById("btn-incident-retry").addEventListener(
    "click", async () => {
      try {
        const r = await window.pywebview.api.receipts_claim_all();
        if (r && r.ok && r.task_id) {
          showToast("Claim started", "success");
          await window.pywebview.api.acknowledge_incident(last.id);
          incidentBannerVisibleId = null;
          refreshIncidentBanner();
        }
      } catch (e) {}
    },
  );
  document.getElementById("btn-incident-disable").addEventListener(
    "click", async () => {
      try {
        await window.pywebview.api.set_auto_claim_config(false, "", 0);
        await window.pywebview.api.acknowledge_incident(last.id);
        showToast("Auto-claim disabled. Restart the node to apply.", "warn");
        incidentBannerVisibleId = null;
        refreshIncidentBanner();
      } catch (e) {}
    },
  );
  document.getElementById("btn-incident-dismiss").addEventListener(
    "click", async () => {
      try {
        await window.pywebview.api.acknowledge_incident(last.id);
        incidentBannerVisibleId = null;
        refreshIncidentBanner();
      } catch (e) {}
    },
  );
}

async function showLogViewer() {
  const overlay = $("#log-viewer-overlay");
  const body = $("#log-viewer-body");
  body.textContent = "Loading…";
  overlay.style.display = "flex";

  let r;
  try {
    r = await window.pywebview.api.get_recent_logs(50);
  } catch (e) {
    body.textContent = "Could not load logs.";
    return;
  }
  if (!r || !r.ok || !r.lines || !r.lines.length) {
    body.textContent = "No log lines available.";
  } else {
    body.textContent = r.lines.join("\n");
  }

  const close = $("#btn-log-close");
  const fresh = close.cloneNode(true);
  close.parentNode.replaceChild(fresh, close);
  fresh.addEventListener("click", () => { overlay.style.display = "none"; });
}

function startIncidentPoll() {
  if (incidentPollId) return;
  refreshIncidentBanner();
  // Slow poll — incidents are rare; 30s keeps it cheap. The banner
  // also refreshes immediately after a successful manual claim.
  incidentPollId = setInterval(refreshIncidentBanner, 30000);
}

// ─────────────────────────────────────────────────────────────────
// Auto-claim Settings panel — checkbox + thresholds + status line.
// ─────────────────────────────────────────────────────────────────

function _spaceWeiToHuman(weiStr) {
  if (!weiStr) return "0";
  // Use BigInt where available — wei amounts can overflow a double.
  try {
    const wei = BigInt(weiStr);
    const whole = wei / 1000000000000000000n;
    const frac = wei % 1000000000000000000n;
    if (frac === 0n) return whole.toString();
    const fracStr = frac.toString().padStart(18, "0").replace(/0+$/, "");
    return whole.toString() + "." + fracStr;
  } catch (e) {
    return String(weiStr);
  }
}

function _humanToSpaceWei(human) {
  if (human === "" || human == null) return "0";
  const m = String(human).trim().match(/^(\d+)(?:\.(\d+))?$/);
  if (!m) return null;
  const whole = m[1];
  const frac = (m[2] || "").padEnd(18, "0").slice(0, 18);
  // Strip leading zeros to get a clean integer string. BigInt handles
  // anything realistic comfortably.
  try {
    const wei = BigInt(whole) * 1000000000000000000n + BigInt(frac || "0");
    return wei.toString();
  } catch (e) {
    return null;
  }
}

async function loadAutoClaimPanel() {
  const group = $("#settings-autoclaim-group");
  const enabledEl = $("#settings-autoclaim-enabled");
  const labelEl = $("#autoclaim-label");
  const thresholdsEl = $("#autoclaim-thresholds");
  const spaceEl = $("#autoclaim-threshold-space");
  const countEl = $("#autoclaim-threshold-count");
  const statusEl = $("#autoclaim-status");
  if (!group) return;

  let cfg;
  try {
    cfg = await window.pywebview.api.get_auto_claim_config();
  } catch (e) {
    return;
  }
  if (!cfg || !cfg.ok) return;

  // Show the panel for all builds — the daemon ignores it harmlessly
  // when escrow isn't configured, but ops want to set thresholds
  // before they switch envs.
  group.style.display = "";
  enabledEl.checked = !!cfg.enabled;
  labelEl.textContent = cfg.enabled ? "Enabled" : "Disabled";
  thresholdsEl.style.display = cfg.enabled ? "block" : "none";
  spaceEl.value = _spaceWeiToHuman(cfg.threshold_space_wei);
  countEl.value = cfg.threshold_count;

  // Live show/hide thresholds when toggled.
  enabledEl.onchange = () => {
    labelEl.textContent = enabledEl.checked ? "Enabled" : "Disabled";
    thresholdsEl.style.display = enabledEl.checked ? "block" : "none";
  };

  // Status line below the form.
  try {
    const st = await window.pywebview.api.get_auto_claim_status();
    if (st && st.ok) {
      const outcome = st.last_attempt_outcome || "none";
      let line;
      if (outcome === "success") {
        statusEl.className = "autoclaim-status has-success";
        line = "Last attempt: success";
        if (st.last_attempt_at) line += " at " + st.last_attempt_at;
      } else if (outcome === "failed") {
        statusEl.className = "autoclaim-status has-error";
        line = "Last attempt: failed";
        if (st.last_attempt_at) line += " at " + st.last_attempt_at;
        if (st.last_error) line += " (" + st.last_error + ")";
      } else {
        statusEl.className = "autoclaim-status";
        line = "No claim attempts yet.";
      }
      const claimable = _spaceWeiToHuman(st.current_claimable_wei || "0");
      line += " · " + claimable + " SPACE accumulated"
              + " (" + (st.current_claimable_count || 0) + " receipts).";
      statusEl.textContent = line;
    } else {
      statusEl.textContent = "";
    }
  } catch (e) {
    statusEl.textContent = "";
  }
}

async function saveAutoClaimPanel() {
  const enabledEl = $("#settings-autoclaim-enabled");
  const spaceEl = $("#autoclaim-threshold-space");
  const countEl = $("#autoclaim-threshold-count");
  if (!enabledEl) return { ok: true };

  const wei = _humanToSpaceWei(spaceEl.value);
  if (wei === null) {
    return { ok: false, error: "Invalid SPACE threshold (must be a positive number)" };
  }
  const count = parseInt(countEl.value, 10);
  if (isNaN(count) || count < 0) {
    return { ok: false, error: "Invalid receipt threshold (must be a non-negative integer)" };
  }
  return await window.pywebview.api.set_auto_claim_config(
    enabledEl.checked, wei, count,
  );
}

// ─────────────────────────────────────────────────────────────────
// Earnings / Payments — PR 5
// ─────────────────────────────────────────────────────────────────

function formatSpace(wei) {
  // 18-decimal token; show up to 6 decimal places, strip trailing zeros.
  if (!wei && wei !== 0) return "—";
  const n = Number(wei) / 1e18;
  if (!isFinite(n)) return "—";
  let s = n.toFixed(6);
  if (s.includes(".")) s = s.replace(/\.?0+$/, "");
  return s || "0";
}

function formatBytes(bytes) {
  if (bytes == null) return "—";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
}

function humanAge(sec) {
  if (sec < 60) return sec + "s ago";
  if (sec < 3600) return Math.floor(sec / 60) + "m ago";
  if (sec < 86400) return Math.floor(sec / 3600) + "h ago";
  return Math.floor(sec / 86400) + "d ago";
}

async function updateEarningsRow() {
  const btn = $("#btn-earnings");
  if (!btn) return;

  let resp;
  try {
    resp = await window.pywebview.api.receipts_summary();
  } catch (e) {
    btn.style.display = "none";
    return;
  }

  if (!resp || !resp.ok || !resp.escrow_configured) {
    btn.style.display = "none";
    return;
  }

  const s = resp.summary || {};
  const outstanding = (s.claimable || 0) + (s.pending_sign || 0);
  const failed = (s.failed_retryable || 0) + (s.failed_terminal || 0);
  const summaryEl = $("#earnings-summary");

  if (outstanding === 0 && failed === 0 && (s.claimed || 0) === 0) {
    summaryEl.textContent = "No earnings yet";
    summaryEl.className = "wallet-summary text-muted";
  } else if (s.failed_retryable > 0) {
    summaryEl.innerHTML =
      "&#9888; " + s.failed_retryable + " need attention";
    summaryEl.className = "wallet-summary text-warn";
  } else if (s.failed_terminal > 0 && s.claimable === 0 && s.pending_sign === 0) {
    summaryEl.textContent = s.failed_terminal + " locked";
    summaryEl.className = "wallet-summary text-error";
  } else if (s.pending_sign > 0) {
    summaryEl.textContent =
      s.pending_sign + " pending · " +
      formatSpace(s.claimable_total_price) + " SPACE";
    summaryEl.className = "wallet-summary";
  } else {
    summaryEl.textContent =
      formatSpace(s.claimable_total_price) + " SPACE ready";
    summaryEl.className = "wallet-summary text-success";
  }

  btn.style.display = "flex";
}

async function showReceipts() {
  hideAll();
  show("screen-receipts");
  await refreshReceipts();
  if (receiptsPollId) clearInterval(receiptsPollId);
  receiptsPollId = setInterval(refreshReceipts, 10000);
}

async function refreshReceipts() {
  let resp;
  try {
    resp = await window.pywebview.api.receipts_list("all", 500, 0);
  } catch (e) {
    showToast("Could not load receipts: " + e, "error");
    return;
  }
  if (!resp || !resp.ok) {
    showToast("Could not load receipts: " + (resp && resp.error), "error");
    return;
  }

  const rows = resp.receipts || [];
  const summary = resp.summary || {};

  const claimable = rows.filter(r => r.view === "claimable");
  const retryable = rows.filter(r => r.view === "failed_retryable");
  const pending = rows.filter(r => r.view === "pending_sign");
  const locked = rows.filter(r => r.view === "failed_terminal");
  // Claimed rows stay visible as an audit record (most recent first).
  const history = rows
    .filter(r => r.view === "claimed")
    .sort((a, b) => (b.claimed_at || 0) - (a.claimed_at || 0));

  renderClaimableCard(summary, claimable);
  renderRowList("receipts-retryable-list", retryable, "failed_retryable");
  renderRowList("receipts-pending-list", pending, "pending_sign");
  renderRowList("receipts-locked-list", locked, "failed_terminal");
  renderHistoryList("receipts-history-list", history);

  $("#receipts-retryable-card").style.display = retryable.length ? "block" : "none";
  $("#receipts-retryable-title").textContent =
    "Needs attention · " + retryable.length;

  // Surface a hint above the list if any failed_retryable receipt has
  // CLAIM_INSUFFICIENT_GAS as its last error — the operator action is
  // "send CTC to the claim wallet" rather than just "click retry".
  const hint = $("#receipts-retryable-hint");
  const gasFailed = retryable.some(
    (r) => r.last_error_code === "CLAIM_INSUFFICIENT_GAS",
  );
  if (gasFailed) {
    hint.textContent =
      "Identity wallet has no CTC for gas. Send CTC to the claim wallet " +
      "shown above, then click Retry all.";
    hint.style.display = "block";
  } else {
    hint.style.display = "none";
  }

  // Claim wallet — surface the auto-derived identity address so the
  // operator can fund it for gas. Hidden when we couldn't read the
  // keystore (encrypted + no passphrase).
  const claimWallet = resp.claim_wallet_address || null;
  if (claimWallet) {
    $("#claim-wallet-address").textContent = claimWallet;
    $("#claim-wallet-card").style.display = "block";
  } else {
    $("#claim-wallet-card").style.display = "none";
  }

  $("#receipts-pending-card").style.display = pending.length ? "block" : "none";
  $("#receipts-locked-card").style.display = locked.length ? "block" : "none";
  $("#receipts-history-card").style.display = history.length ? "block" : "none";
  $("#receipts-history-title").textContent =
    "Claim history · " + history.length;

  const empty =
    claimable.length === 0 && retryable.length === 0 &&
    pending.length === 0 && locked.length === 0 && history.length === 0;
  $("#receipts-empty").style.display = empty ? "block" : "none";
}

function renderHistoryList(containerId, rows) {
  const container = $("#" + containerId);
  container.innerHTML = "";
  const now = Math.floor(Date.now() / 1000);

  for (const r of rows) {
    const row = document.createElement("div");
    row.className = "receipt-row";

    const header = document.createElement("div");
    header.className = "receipt-row-header";
    const uuid = document.createElement("span");
    uuid.className = "uuid";
    uuid.textContent = r.request_uuid.slice(0, 8) + "…";
    // G7 — visible badge so the operator can tell at a glance whether
    // a row settled via a local tx (Blockscout link works) or was
    // reconciled by the gateway (no local tx was submitted).
    const isExternal = r.claim_tx_hash === "external";
    const badge = document.createElement("span");
    badge.className = "history-badge " + (isExternal ? "external" : "tx");
    badge.textContent = isExternal ? "Reconciled" : "On-chain";
    badge.title = isExternal
      ? "The gateway auto-settled this receipt on-chain before this node submitted a claim. No local tx was sent."
      : "This node submitted a claim tx; the badge links to Blockscout via Details.";
    uuid.appendChild(badge);
    const price = document.createElement("span");
    price.textContent = formatSpace(r.total_price) + " SPACE";
    header.appendChild(uuid);
    header.appendChild(price);
    row.appendChild(header);

    const meta = document.createElement("div");
    meta.className = "receipt-meta";
    const when = r.claimed_at
      ? humanAge(now - r.claimed_at)
      : "just now";
    const source = isExternal
      ? "Gateway auto-settled (no local tx)"
      : "On-chain tx";
    meta.textContent = formatBytes(r.data_amount) + " · " + when + " · " + source;
    row.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "receipt-actions";
    const detailBtn = document.createElement("button");
    detailBtn.className = "btn-text-subtle";
    detailBtn.textContent = "Details";
    detailBtn.addEventListener("click", () => showReceiptDetail(r.request_uuid));
    actions.appendChild(detailBtn);

    // Quick-link Blockscout straight from the row when we have a real
    // tx hash. Saves the user a Details-modal hop.
    if (!isExternal && r.claim_tx_hash) {
      const txBtn = document.createElement("button");
      txBtn.className = "btn-text-subtle";
      txBtn.textContent = "Blockscout";
      txBtn.addEventListener("click", () =>
        window.pywebview.api.receipts_open_explorer(r.claim_tx_hash));
      actions.appendChild(txBtn);
    }

    row.appendChild(actions);

    container.appendChild(row);
  }
}

function renderClaimableCard(summary, claimable) {
  const card = $("#receipts-claimable-card");
  const count = summary.claimable || 0;
  if (count === 0) {
    card.style.display = "none";
    return;
  }
  $("#receipts-claimable-count").textContent = count;
  $("#receipts-claimable-plural").textContent = count === 1 ? "" : "s";
  $("#receipts-claimable-total").textContent =
    formatSpace(summary.claimable_total_price);
  card.style.display = "block";

  const btn = $("#btn-claim-all");
  // Don't let a click drop duplicate claims — the backend also
  // serialises via flock, but disabling here keeps the UI honest.
  btn.disabled = !!currentClaimTaskId;
  btn.textContent = currentClaimTaskId
    ? "Claiming..." : "Claim All Outstanding";
}

function renderRowList(containerId, rows, view) {
  const container = $("#" + containerId);
  container.innerHTML = "";
  const now = Math.floor(Date.now() / 1000);

  for (const r of rows) {
    const row = document.createElement("div");
    row.className = "receipt-row" + (r.locked ? " locked" : "");

    const header = document.createElement("div");
    header.className = "receipt-row-header";
    const uuid = document.createElement("span");
    uuid.className = "uuid";
    uuid.textContent = r.request_uuid.slice(0, 8) + "…";
    const price = document.createElement("span");
    price.textContent = formatSpace(r.total_price) + " SPACE";
    header.appendChild(uuid);
    header.appendChild(price);
    row.appendChild(header);

    const meta = document.createElement("div");
    meta.className = "receipt-meta";
    const triesText = r.view === "failed_retryable"
      ? "try " + (r.claim_attempts || r.sign_attempts) + " of " +
        (r.view.startsWith("failed") && r.sign_attempts
          ? r.max_sign_attempts : r.max_claim_attempts)
      : r.view === "failed_terminal" ? "locked" : "";
    meta.textContent = formatBytes(r.data_amount)
      + " · " + humanAge(now - r.created_at)
      + (triesText ? " · " + triesText : "");
    row.appendChild(meta);

    if (r.last_error_message) {
      const reason = document.createElement("div");
      reason.className = "receipt-reason";
      reason.textContent = r.last_error_message;
      row.appendChild(reason);
    }

    const actions = document.createElement("div");
    actions.className = "receipt-actions";

    // Per-row Retry buttons were removed in favor of a single
    // "Retry All" at the top of the failed-retryable card. Each
    // per-row click used to fire its own claimBatch tx (one receipt
    // per tx), wasting gas and causing the test.105 "5 separate retry
    // txs" bug. The batched retry chunks up to CLAIM_BATCH_SIZE (50)
    // receipts into a single tx.

    const detailBtn = document.createElement("button");
    detailBtn.className = "btn-text-subtle";
    detailBtn.textContent = "Details";
    detailBtn.addEventListener("click", () => showReceiptDetail(r.request_uuid));
    actions.appendChild(detailBtn);

    row.appendChild(actions);
    container.appendChild(row);
  }
}

async function onClaimAll() {
  if (currentClaimTaskId) return;
  const btn = $("#btn-claim-all");
  btn.disabled = true;
  btn.textContent = "Starting claim...";

  let resp;
  try {
    resp = await window.pywebview.api.receipts_claim_all();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Claim All Outstanding";
    showToast("Claim failed: " + e, "error");
    return;
  }

  if (!resp || !resp.ok) {
    btn.disabled = false;
    btn.textContent = "Claim All Outstanding";
    showToast("Claim failed: " + (resp && resp.error), "error");
    return;
  }
  currentClaimTaskId = resp.task_id;
  btn.textContent = "Claiming...";
  pollClaimTask(resp.task_id);
}

async function onRetryAll() {
  if (currentClaimTaskId) return;
  const btn = $("#btn-retry-all");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Retrying...";
  }
  let resp;
  try {
    resp = await window.pywebview.api.receipts_retry_all();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "Retry all"; }
    showToast("Retry failed: " + e, "error");
    return;
  }
  if (!resp.ok) {
    if (btn) { btn.disabled = false; btn.textContent = "Retry all"; }
    showToast("Retry failed: " + resp.error, "error");
    return;
  }
  currentClaimTaskId = resp.task_id;
  refreshReceipts();
  pollClaimTask(resp.task_id);
}

async function onCopyClaimWallet() {
  const addr = $("#claim-wallet-address").textContent;
  if (!addr || addr === "—") return;
  try {
    await navigator.clipboard.writeText(addr);
    showToast("Copied: " + addr, "info");
  } catch (e) {
    showToast("Copy failed: " + e, "error");
  }
}

async function pollClaimTask(taskId) {
  const poll = async () => {
    let st;
    try {
      st = await window.pywebview.api.receipts_claim_status(taskId);
    } catch (e) {
      // Retry once on pywebview hiccup.
      setTimeout(poll, 1500);
      return;
    }
    if (!st.ok) {
      finishClaimTask();
      showToast("Claim task lost: " + st.error, "error");
      return;
    }
    if (st.state === "done") {
      finishClaimTask();
      renderClaimOutcome(st.result);
      await refreshReceipts();
      await updateEarningsRow();
      return;
    }
    if (st.state === "error") {
      finishClaimTask();
      // The error is the raw exception text; classify locally to
      // catch web3 "insufficient funds for gas" and the like.
      const code = classifyErrorTextLocal(st.error || "");
      if (code !== "unknown" && ERROR_CATALOG[code]) {
        showErrorCatalogModal(code, st.error);
      } else {
        showToast("Claim failed: " + st.error, "error");
      }
      await refreshReceipts();
      return;
    }
    setTimeout(poll, 1500);
  };
  poll();
}

function finishClaimTask() {
  currentClaimTaskId = null;
  const btn = $("#btn-claim-all");
  if (btn) {
    btn.disabled = false;
    btn.textContent = "Claim All Outstanding";
  }
}

function renderClaimOutcome(result) {
  if (!result) return;
  if (result.noop) {
    showToast("Another claim was already running — skipped.", "warn");
    return;
  }
  if (!result.ok) {
    // A7 + error catalog: when the daemon attaches a known
    // ``error_code`` (e.g. insufficient_gas), surface the friendly
    // modal with the right remediation. Fall back to the toast for
    // unknown errors so we never swallow the failure silently.
    const code = result.error_code;
    if (code && code !== "unknown" && ERROR_CATALOG[code]) {
      showErrorCatalogModal(code, result.error);
    } else {
      showToast("Claim failed: " + (result.error || "unknown"), "error");
    }
    return;
  }
  const s = result.summary || {};
  const parts = [];
  if (s.submitted) parts.push(s.submitted + " claimed on-chain");
  // "reconciled" = the gateway had already auto-settled these receipts
  // before our node submitted a claim tx, so the reaper just marks them
  // as landed without a local tx. Spell this out so the user doesn't
  // look for a Blockscout entry that doesn't exist.
  if (s.reconciled) parts.push(s.reconciled + " already settled by gateway");
  if (s.failed_batches) parts.push(s.failed_batches + " failed");
  if (s.locked_after_failure) parts.push(s.locked_after_failure + " locked");
  if (parts.length === 0) parts.push("Nothing to claim");
  const tone = s.failed_batches ? "warn" : "success";
  showToast("Claim: " + parts.join(" · "), tone);
}

async function showReceiptDetail(uuid) {
  let resp;
  try {
    resp = await window.pywebview.api.receipts_detail(uuid);
  } catch (e) {
    showToast("Could not load detail: " + e, "error");
    return;
  }
  if (!resp || !resp.ok) {
    showToast("Could not load detail: " + (resp && resp.error), "error");
    return;
  }
  const r = resp.receipt;

  const body = $("#receipt-detail-body");
  const rows = [
    ["UUID",         r.request_uuid],
    ["Status",       r.view],
    ["Bytes",        formatBytes(r.data_amount)],
    ["Price",        formatSpace(r.total_price) + " SPACE (" + r.total_price + " wei)"],
    ["Client",       r.client_address],
    ["Node",         r.node_address],
    ["Created",      new Date(r.created_at * 1000).toLocaleString()],
    ["Sign attempts", r.sign_attempts + " / " + r.max_sign_attempts],
    ["Claim attempts", r.claim_attempts + " / " + r.max_claim_attempts],
  ];
  if (r.last_error_code) {
    rows.push(["Last error", r.last_error_code]);
    rows.push(["Details", r.last_error_detail || r.last_error_message]);
  }
  if (r.claim_tx_hash === "external") {
    rows.push([
      "Settlement",
      "Gateway auto-settled on-chain; no local tx was submitted.",
    ]);
  } else if (r.claim_tx_hash) {
    rows.push(["Tx hash", r.claim_tx_hash]);
  }

  body.innerHTML = rows.map(([k, v]) =>
    "<div style='margin-bottom:6px;'>" +
    "<span style='color:#8080a0; display:inline-block; width:110px;'>" +
    k + "</span>" +
    "<span style='color:#e0e0e0; word-break:break-all;'>" +
    escapeHtml(String(v)) + "</span></div>"
  ).join("");

  const txBtn = $("#btn-receipt-detail-tx");
  if (r.claim_tx_hash && r.claim_tx_hash !== "external") {
    txBtn.style.display = "block";
    txBtn.onclick = () => window.pywebview.api.receipts_open_explorer(r.claim_tx_hash);
  } else {
    txBtn.style.display = "none";
    txBtn.onclick = null;
  }

  $("#receipt-detail-overlay").style.display = "flex";
}

function hideReceiptDetail() {
  $("#receipt-detail-overlay").style.display = "none";
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;",
  })[c]);
}

let toastTimer = null;
function showToast(msg, tone) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast visible " + (tone || "");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    t.className = "toast " + (tone || "");
  }, 4000);
}

function initReceiptsScreen() {
  const earningsBtn = $("#btn-earnings");
  if (earningsBtn) earningsBtn.addEventListener("click", showReceipts);

  // G4 — Settings' Back calls hideAll() before showStatus() so the
  // current screen is properly torn down (interval polls cleared,
  // display flipped). The Earnings Back was missing the hideAll()
  // wrapper, so the receipts screen stayed visible underneath the
  // status screen and the receipts poll kept running. Match the
  // Settings pattern exactly here.
  const backBtn = $("#btn-receipts-back");
  if (backBtn) backBtn.addEventListener("click", function () {
    hideAll();
    showStatus();
  });

  const claimBtn = $("#btn-claim-all");
  if (claimBtn) claimBtn.addEventListener("click", onClaimAll);

  const retryAllBtn = $("#btn-retry-all");
  if (retryAllBtn) retryAllBtn.addEventListener("click", onRetryAll);

  const copyClaimBtn = $("#btn-copy-claim-wallet");
  if (copyClaimBtn) copyClaimBtn.addEventListener("click", onCopyClaimWallet);

  const closeDetailBtn = $("#btn-receipt-detail-close");
  if (closeDetailBtn) closeDetailBtn.addEventListener("click", hideReceiptDetail);
}

// Wait for pywebview to be ready
window.addEventListener("pywebviewready", init);
