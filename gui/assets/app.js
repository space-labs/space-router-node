/**
 * SpaceRouter Desktop App — Frontend
 *
 * Communicates with the Python backend via window.pywebview.api.*
 */

const EVM_RE = /^(0x)?[0-9a-fA-F]{40}$/;

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

function validateInputs() {
  const stakingInput = $("#staking-input");
  const stakingError = $("#staking-error");
  const collectionInput = $("#collection-input");
  const collectionError = $("#collection-error");
  const btn = $("#btn-start");

  const stakingVal = stakingInput.value.trim();
  const collectionVal = collectionInput.value.trim();

  let stakingValid = false;
  let collectionValid = true; // optional — valid when empty

  // Validate staking (required)
  if (!stakingVal) {
    stakingError.textContent = "";
    stakingInput.classList.remove("invalid");
  } else if (!EVM_RE.test(stakingVal)) {
    stakingError.textContent =
      "Invalid address — expected 0x followed by 40 hex characters";
    stakingInput.classList.add("invalid");
  } else {
    stakingError.textContent = "";
    stakingInput.classList.remove("invalid");
    stakingValid = true;
  }

  // Validate collection (optional)
  if (!collectionVal) {
    collectionError.textContent = "";
    collectionInput.classList.remove("invalid");
  } else if (!EVM_RE.test(collectionVal)) {
    collectionError.textContent =
      "Invalid address — expected 0x followed by 40 hex characters";
    collectionInput.classList.add("invalid");
    collectionValid = false;
  } else {
    collectionError.textContent = "";
    collectionInput.classList.remove("invalid");
  }

  btn.disabled = !(stakingValid && collectionValid);
}

function initOnboarding() {
  const stakingInput = $("#staking-input");
  const collectionInput = $("#collection-input");
  const stakingError = $("#staking-error");
  const btn = $("#btn-start");

  populateEnvSelector();

  stakingInput.addEventListener("input", validateInputs);
  collectionInput.addEventListener("input", validateInputs);

  btn.addEventListener("click", async function () {
    const stakingAddr = stakingInput.value.trim();
    const collectionAddr = collectionInput.value.trim();
    if (!EVM_RE.test(stakingAddr)) return;

    btn.disabled = true;
    btn.textContent = "Starting...";

    try {
      const result = await window.pywebview.api.save_wallet_and_start(
        stakingAddr,
        collectionAddr
      );
      if (result.ok) {
        hideAll();
        showStatus();
      } else {
        stakingError.textContent = result.error || "Unknown error";
        btn.disabled = false;
        btn.textContent = "Start SpaceRouter";
      }
    } catch (e) {
      stakingError.textContent = "Failed to connect to backend";
      btn.disabled = false;
      btn.textContent = "Start SpaceRouter";
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
  // Poll every 3 seconds
  if (statusPollId) clearInterval(statusPollId);
  statusPollId = setInterval(updateStatus, 3000);
}

async function updateStatus() {
  try {
    const status = await window.pywebview.api.get_status();

    const dot = $("#status-dot");
    const text = $("#status-text");
    const stakingEl = $("#staking-address");
    const collectionEl = $("#collection-address");
    const envBadge = $("#env-badge");
    const errorBanner = $("#error-banner");
    const errorText = $("#error-text");

    // Wallet addresses
    stakingEl.textContent = status.staking_address || status.wallet || "-";
    collectionEl.textContent = status.collection_address || "-";

    // Environment badge
    if (status.environment && status.environment !== "production") {
      envBadge.textContent = envLabel(status.environment);
      envBadge.style.display = "block";
    } else {
      envBadge.style.display = "none";
    }

    // Status indicator — use phase for granular state
    const phase = status.phase || "stopped";
    if (phase === "running") {
      dot.className = "dot dot-running";
      text.textContent = "SpaceRouter is running";
    } else if (phase === "registering") {
      dot.className = "dot dot-starting";
      text.textContent = "Registering with network...";
    } else if (phase === "starting") {
      dot.className = "dot dot-starting";
      text.textContent = "Starting...";
    } else if (status.error) {
      dot.className = "dot dot-stopped";
      text.textContent = "SpaceRouter is stopped";
    } else {
      dot.className = "dot dot-stopped";
      text.textContent = "Stopped";
    }

    // Error display
    if (status.error) {
      errorText.textContent = status.error;
      errorBanner.style.display = "block";
    } else {
      errorBanner.style.display = "none";
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

    // Go to network setup, then onboarding
    showNetworkSetup(function () {
      // Pre-fill addresses if kept
      hideAll();
      show("screen-onboarding");
      initOnboarding();

      if (keepAddresses) {
        // Reload addresses from config
        loadSavedAddresses();
      }
    });
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
    validateInputs();
  } catch (e) {}
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

    // Variant setup is non-blocking
    initTestVariant();

    // Fresh restart is always available
    initFreshRestart();

    if (needsOnboarding) {
      // First time: show network setup, then onboarding
      showNetworkSetup(function () {
        showOnboarding();
      });
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
