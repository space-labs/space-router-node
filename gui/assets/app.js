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
let isTestBuild = false;

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
  ]) {
    hide(id);
  }
  if (statusPollId) {
    clearInterval(statusPollId);
    statusPollId = null;
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

  // ── Form-level enable/disable ──
  function validateForm() {
    const importValid = radioGenerate.checked ||
      (radioImport.checked && HEX_KEY_RE.test(identityKeyInput.value.trim()));
    const stakingValid = validateAddress(stakingInput, stakingError);
    const collectionValid = validateAddress(collectionInput, collectionError);
    btn.disabled = !(importValid && stakingValid && collectionValid);
  }

  // Enable button immediately for generate mode
  validateForm();

  // ── Submit ──
  btn.addEventListener("click", async function () {
    btn.disabled = true;
    btn.textContent = "Starting...";

    const passphrase = $("#passphrase-input").value;
    const staking = stakingInput.value.trim();
    const collection = collectionInput.value.trim();
    const identityKeyHex = radioImport.checked ? identityKeyInput.value.trim() : "";

    // Save network mode from advanced section
    const networkMode = document.querySelector('input[name="onboard-network-mode"]:checked');
    const mode = networkMode ? networkMode.value : "upnp";
    const tunnelHost = mode === "tunnel" ? ($("#onboard-tunnel-host").value.trim() || "") : "";
    const tunnelPort = mode === "tunnel" ? ($("#onboard-tunnel-port").value.trim() || "") : "";

    try {
      await window.pywebview.api.save_network_mode(mode, tunnelHost, tunnelPort);
      const result = await window.pywebview.api.save_onboarding_and_start(
        passphrase, staking, collection, identityKeyHex,
      );
      if (result.ok) {
        hideAll();
        showStatus();
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

// ── Status Dashboard ──

function showStatus() {
  show("screen-status");
  updateStatus();
  if (statusPollId) clearInterval(statusPollId);
  statusPollId = setInterval(updateStatus, 3000);
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
    const btnStop = $("#btn-stop");

    // Wallet addresses (truncated, full on hover)
    const fullStaking = status.staking_address || status.wallet || "";
    const fullCollection = status.collection_address || "";
    stakingEl.textContent = truncateAddress(fullStaking) || "-";
    stakingEl.title = fullStaking;
    collectionEl.textContent = truncateAddress(fullCollection) || "-";
    collectionEl.title = fullCollection;

    // State-based display
    const state = status.state || "idle";

    // Passphrase required — show unlock dialog immediately
    if (state === "passphrase_required") {
      showUnlockDialog();
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
        break;
      case "initializing":
        dot.className = "dot dot-starting";
        text.textContent = "Initializing...";
        detail.textContent = status.detail || "Loading certificates";
        break;
      case "binding":
        dot.className = "dot dot-starting";
        text.textContent = "Starting server...";
        detail.textContent = status.detail || "";
        break;
      case "registering":
        dot.className = "dot dot-starting";
        text.textContent = "Registering...";
        detail.textContent = status.detail || "";
        break;
      case "running":
        dot.className = "dot dot-running";
        text.textContent = "SpaceRouter is running";
        detail.textContent = status.detail || "";
        break;
      case "reconnecting":
        dot.className = "dot dot-reconnecting";
        text.textContent = "Reconnecting...";
        detail.textContent = status.detail || "";
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
        // Use error_code for user-friendly messages
        if (status.error_code === "identity_key_locked") {
          showUnlockDialog();
          return;
        } else if (status.error_code === "registration_rejected") {
          detail.textContent = "Registration rejected. Check your staking balance and wallet address.";
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

    // Error display
    if (status.error && state !== "error_transient" && state !== "passphrase_required") {
      errorText.textContent = status.error;
      errorBanner.style.display = "block";
    } else {
      errorBanner.style.display = "none";
    }

    // Cert expiry warning
    certWarning.style.display = status.cert_expiry_warning ? "block" : "none";

    // Action buttons
    if (state === "error_permanent") {
      btnRetry.style.display = "block";
      btnStop.style.display = "none";
    } else if (state === "idle") {
      btnRetry.style.display = "none";
      btnStop.style.display = "none";
    } else {
      btnRetry.style.display = "none";
      btnStop.style.display = "block";
    }
  } catch (e) {
    // Backend not ready yet — ignore
  }
}

// ── Fresh Restart ──

function initFreshRestart() {
  $("#btn-fresh-restart").addEventListener("click", function () {
    // Reset button states
    $("#btn-restart-keep").disabled = false;
    $("#btn-restart-keep").textContent = "Keep Addresses";
    $("#btn-restart-clear").disabled = false;
    $("#btn-restart-clear").textContent = "Clear Everything";
    hideAll();
    show("screen-fresh-restart");
  });

  $("#btn-restart-cancel").addEventListener("click", function () {
    hideAll();
    showStatus();
  });

  $("#btn-restart-keep").addEventListener("click", async function () {
    await doFreshRestart(true);
  });

  $("#btn-restart-clear").addEventListener("click", async function () {
    await doFreshRestart(false);
  });
}

async function doFreshRestart(keepAddresses) {
  const btn = keepAddresses ? $("#btn-restart-keep") : $("#btn-restart-clear");
  btn.disabled = true;
  btn.textContent = "Resetting...";

  try {
    const result = await window.pywebview.api.fresh_restart(keepAddresses);
    if (!result.ok) {
      btn.disabled = false;
      btn.textContent = keepAddresses ? "Keep Addresses" : "Clear Everything";
      return;
    }

    // Go directly to onboarding
    hideAll();
    show("screen-onboarding");
    initOnboarding();

    if (keepAddresses) {
      loadSavedAddresses();
    }
  } catch (e) {
    btn.disabled = false;
    btn.textContent = keepAddresses ? "Keep Addresses" : "Clear Everything";
  }
}

async function loadSavedAddresses() {
  try {
    const status = await window.pywebview.api.get_status();
    if (status.staking_address) {
      $("#staking-input").value = status.staking_address;
    }
    if (
      status.collection_address &&
      status.collection_address !== status.staking_address
    ) {
      $("#collection-input").value = status.collection_address;
    }
  } catch (e) {}
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

  $("#btn-stop").addEventListener("click", async function () {
    const btn = $("#btn-stop");
    btn.disabled = true;
    btn.textContent = "Stopping...";
    try {
      await window.pywebview.api.stop_node();
    } catch (e) {}
    btn.disabled = false;
    btn.textContent = "Stop";
  });
}

// ── Settings Panel (test builds only) ──

function initSettings() {
  const envSelect = $("#settings-env");
  const customUrl = $("#settings-custom-url");
  const mtlsToggle = $("#settings-mtls");
  const mtlsLabel = $("#mtls-label");
  const mtlsWarning = $("#mtls-warning");
  const saveBtn = $("#btn-save-settings");
  const statusEl = $("#settings-status");

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
    // Load current settings
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
    let url = envSelect.value;
    if (url === "custom") {
      url = customUrl.value.trim();
      if (!url) {
        statusEl.textContent = "Please enter a custom URL";
        statusEl.style.color = "#e74c3c";
        return;
      }
    }

    const mtlsEnabled = mtlsToggle.checked;

    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";
    statusEl.textContent = "";

    try {
      const result = await window.pywebview.api.save_settings(url, mtlsEnabled);
      if (!result.ok) {
        statusEl.textContent = result.error || "Failed to save";
        statusEl.style.color = "#e74c3c";
        saveBtn.disabled = false;
        saveBtn.textContent = "Save & Restart Node";
        return;
      }

      // Restart node with new settings
      statusEl.textContent = "Restarting node...";
      statusEl.style.color = "#8080a0";

      await window.pywebview.api.stop_node();
      await window.pywebview.api.start_node();

      // Update test banner env label
      updateTestBannerLabel(url);

      saveBtn.disabled = false;
      saveBtn.textContent = "Save & Restart Node";

      // Go back to status
      hideAll();
      showStatus();
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

function showUnlockDialog() {
  show("dialog-overlay");
  if (statusPollId) {
    clearInterval(statusPollId);
    statusPollId = null;
  }

  const btn = $("#btn-unlock");
  const input = $("#unlock-passphrase");
  const errEl = $("#unlock-error");

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

      // Show settings button
      $("#btn-settings").style.display = "block";

      // Load current env for banner label
      try {
        const settings = await window.pywebview.api.get_settings();
        updateTestBannerLabel(settings.coordination_api_url);
      } catch (e) {}

      // Init settings panel
      initSettings();
    }
  } catch (e) {
    // Variant check failed — continue as production
  }
}

async function init() {
  try {
    const needsOnboarding = await window.pywebview.api.needs_onboarding();

    // Determine build variant before showing any screens
    await initTestVariant();

    // Action buttons
    initFreshRestart();
    initActionButtons();

    if (needsOnboarding) {
      showOnboarding();
    } else {
      // Already configured — start node and show status
      await window.pywebview.api.start_node();
      showStatus();
    }
  } catch (e) {
    // pywebview.api not ready — retry
    setTimeout(init, 200);
  }
}

// Wait for pywebview to be ready
window.addEventListener("pywebviewready", init);
