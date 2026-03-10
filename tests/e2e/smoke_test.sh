#!/usr/bin/env bash
# E2E smoke tests for the compiled Space Router Home Node binary.
# Runs on macOS and Linux CI runners after PyInstaller build.
#
# Usage: bash tests/e2e/smoke_test.sh <path-to-binary>
#
# Required environment variables:
#   EXPECTED_VERSION  - version string the binary should report
#   SR_NODE_PORT      - port for the node to bind (use a high port to avoid conflicts)
#   SR_COORDINATION_API_URL - coordination API URL (can be fake for testing)
#   SR_PUBLIC_IP      - public IP override
#   SR_UPNP_ENABLED   - set to "false" for CI

set -euo pipefail

BINARY="$1"
PASS=0
FAIL=0

log()  { echo "  [INFO]  $*"; }
pass() { echo "  [PASS]  $*"; PASS=$((PASS + 1)); }
fail() { echo "  [FAIL]  $*"; FAIL=$((FAIL + 1)); }

# ---------- Test 1: --version flag ----------
test_version_flag() {
    log "Testing --version flag..."
    VERSION_OUTPUT=$("$BINARY" --version 2>&1) || true

    if echo "$VERSION_OUTPUT" | grep -q "$EXPECTED_VERSION"; then
        pass "--version reports '$EXPECTED_VERSION'"
    else
        fail "--version output was '$VERSION_OUTPUT', expected to contain '$EXPECTED_VERSION'"
    fi
}

# ---------- Test 2: Binary starts and binds to port ----------
test_port_binding() {
    log "Testing port binding on port $SR_NODE_PORT..."

    # Start the binary in the background. It will fail to register with the
    # coordination API but should still bind the TLS listener first.
    "$BINARY" &
    PID=$!
    log "Started binary with PID $PID"

    # Give it time to start and bind
    sleep 3

    if ! kill -0 "$PID" 2>/dev/null; then
        fail "Binary exited prematurely"
        return
    fi

    # Check if the port is listening
    if command -v ss &>/dev/null; then
        LISTENING=$(ss -tlnp 2>/dev/null | grep ":${SR_NODE_PORT}" || true)
    elif command -v lsof &>/dev/null; then
        LISTENING=$(lsof -iTCP:"${SR_NODE_PORT}" -sTCP:LISTEN 2>/dev/null || true)
    elif command -v netstat &>/dev/null; then
        LISTENING=$(netstat -an 2>/dev/null | grep "LISTEN" | grep ":${SR_NODE_PORT}" || true)
    else
        log "No port-checking tool available, skipping port binding check"
        kill "$PID" 2>/dev/null || true
        wait "$PID" 2>/dev/null || true
        pass "Binary started (port check skipped — no ss/lsof/netstat)"
        return
    fi

    # Clean up
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true

    if [ -n "$LISTENING" ]; then
        pass "Binary is listening on port $SR_NODE_PORT"
    else
        fail "Binary did not bind to port $SR_NODE_PORT"
    fi
}

# ---------- Test 3: Clean shutdown via SIGTERM ----------
test_clean_shutdown() {
    log "Testing clean shutdown via SIGTERM..."

    "$BINARY" &
    PID=$!
    log "Started binary with PID $PID"

    sleep 3

    if ! kill -0 "$PID" 2>/dev/null; then
        fail "Binary exited before SIGTERM could be sent"
        return
    fi

    kill -TERM "$PID"
    log "Sent SIGTERM to PID $PID"

    # Wait up to 10 seconds for clean exit
    for i in $(seq 1 10); do
        if ! kill -0 "$PID" 2>/dev/null; then
            wait "$PID" 2>/dev/null
            EXIT_CODE=$?
            if [ "$EXIT_CODE" -eq 0 ] || [ "$EXIT_CODE" -eq 143 ]; then
                pass "Clean shutdown (exit code $EXIT_CODE)"
            else
                fail "Exited with unexpected code $EXIT_CODE after SIGTERM"
            fi
            return
        fi
        sleep 1
    done

    # Force kill if still running
    kill -9 "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
    fail "Binary did not exit within 10 seconds after SIGTERM"
}

# ---------- Run all tests ----------
echo ""
echo "=== Space Router Home Node — E2E Smoke Tests ==="
echo "Binary:  $BINARY"
echo "Version: $EXPECTED_VERSION"
echo "Port:    $SR_NODE_PORT"
echo ""

test_version_flag
test_port_binding
test_clean_shutdown

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
echo ""

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
