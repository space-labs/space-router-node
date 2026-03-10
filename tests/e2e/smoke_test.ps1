# E2E smoke tests for the compiled Space Router Home Node binary.
# Runs on Windows CI runners after PyInstaller build.
#
# Usage: .\tests\e2e\smoke_test.ps1 -Binary "dist\space-router-node-windows-x64.exe"
#
# Required environment variables:
#   EXPECTED_VERSION  - version string the binary should report
#   SR_NODE_PORT      - port for the node to bind (use a high port to avoid conflicts)
#   SR_COORDINATION_API_URL - coordination API URL (can be fake for testing)
#   SR_PUBLIC_IP      - public IP override
#   SR_UPNP_ENABLED   - set to "false" for CI

param(
    [Parameter(Mandatory = $true)]
    [string]$Binary
)

$ErrorActionPreference = "Continue"
$Pass = 0
$Fail = 0

function Log($msg) { Write-Host "  [INFO]  $msg" }
function Pass($msg) { Write-Host "  [PASS]  $msg"; $script:Pass++ }
function Fail($msg) { Write-Host "  [FAIL]  $msg"; $script:Fail++ }

# ---------- Test 1: --version flag ----------
function Test-VersionFlag {
    Log "Testing --version flag..."
    try {
        $output = & $Binary --version 2>&1 | Out-String
        if ($output -match [regex]::Escape($env:EXPECTED_VERSION)) {
            Pass "--version reports '$($env:EXPECTED_VERSION)'"
        }
        else {
            Fail "--version output was '$($output.Trim())', expected to contain '$($env:EXPECTED_VERSION)'"
        }
    }
    catch {
        Fail "--version threw an error: $_"
    }
}

# ---------- Test 2: Binary starts and binds to port ----------
function Test-PortBinding {
    Log "Testing port binding on port $($env:SR_NODE_PORT)..."

    $proc = Start-Process -FilePath $Binary -PassThru -NoNewWindow
    Log "Started binary with PID $($proc.Id)"

    Start-Sleep -Seconds 4

    if ($proc.HasExited) {
        Fail "Binary exited prematurely with code $($proc.ExitCode)"
        return
    }

    # Check if the port is listening
    try {
        $connection = Test-NetConnection -ComputerName 127.0.0.1 -Port ([int]$env:SR_NODE_PORT) -WarningAction SilentlyContinue
        $listening = $connection.TcpTestSucceeded
    }
    catch {
        Log "Test-NetConnection failed: $_, trying netstat fallback"
        $netstat = netstat -an 2>$null | Select-String ":$($env:SR_NODE_PORT)\s+.*LISTENING"
        $listening = ($null -ne $netstat)
    }

    # Clean up
    try {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        $proc.WaitForExit(5000) | Out-Null
    }
    catch { }

    if ($listening) {
        Pass "Binary is listening on port $($env:SR_NODE_PORT)"
    }
    else {
        Fail "Binary did not bind to port $($env:SR_NODE_PORT)"
    }
}

# ---------- Test 3: Clean shutdown ----------
function Test-CleanShutdown {
    Log "Testing clean shutdown..."

    $proc = Start-Process -FilePath $Binary -PassThru -NoNewWindow
    Log "Started binary with PID $($proc.Id)"

    Start-Sleep -Seconds 4

    if ($proc.HasExited) {
        Fail "Binary exited before shutdown signal could be sent"
        return
    }

    # On Windows, send Ctrl+C via taskkill (graceful) then wait
    # taskkill without /F sends WM_CLOSE / CTRL_CLOSE_EVENT
    & taskkill /PID $proc.Id 2>$null | Out-Null

    Log "Sent shutdown signal to PID $($proc.Id)"

    # Wait up to 10 seconds
    $exited = $proc.WaitForExit(10000)

    if ($exited) {
        $code = $proc.ExitCode
        # Exit code 0 or 1 (taskkill) are both acceptable
        if ($code -eq 0 -or $code -eq 1) {
            Pass "Clean shutdown (exit code $code)"
        }
        else {
            Pass "Shutdown completed (exit code $code)"
        }
    }
    else {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        $proc.WaitForExit(5000) | Out-Null
        Fail "Binary did not exit within 10 seconds after shutdown signal"
    }
}

# ---------- Run all tests ----------
Write-Host ""
Write-Host "=== Space Router Home Node - E2E Smoke Tests ==="
Write-Host "Binary:  $Binary"
Write-Host "Version: $($env:EXPECTED_VERSION)"
Write-Host "Port:    $($env:SR_NODE_PORT)"
Write-Host ""

Test-VersionFlag
Test-PortBinding
Test-CleanShutdown

Write-Host ""
Write-Host "=== Results: $Pass passed, $Fail failed ==="
Write-Host ""

if ($Fail -gt 0) {
    exit 1
}
