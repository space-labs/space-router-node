#!/usr/bin/env bash
# Build a macOS .pkg installer for Space Router Home Node.
#
# Usage:
#   bash packaging/macos/build_pkg.sh \
#     --binary dist/space-router-node-macos-arm64 \
#     --version 1.0.0 \
#     --output dist/space-router-node-macos-arm64.pkg \
#     [--sign "Developer ID Installer: ..."]
#
# Requires: pkgbuild, productbuild (pre-installed on macOS)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

BINARY=""
VERSION=""
OUTPUT=""
SIGN_IDENTITY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --binary)   BINARY="$2";        shift 2 ;;
        --version)  VERSION="$2";       shift 2 ;;
        --output)   OUTPUT="$2";        shift 2 ;;
        --sign)     SIGN_IDENTITY="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [[ -z "$BINARY" || -z "$VERSION" || -z "$OUTPUT" ]]; then
    echo "Usage: build_pkg.sh --binary <path> --version <ver> --output <path> [--sign <identity>]"
    exit 1
fi

BINARY="$(cd "$(dirname "$BINARY")" && pwd)/$(basename "$BINARY")"
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

echo "=== Building macOS .pkg installer ==="
echo "Binary:  $BINARY"
echo "Version: $VERSION"
echo "Output:  $OUTPUT"

# --- Stage the install payload ---
echo "Staging install payload..."
mkdir -p "$STAGING/payload/opt/spacerouter"
mkdir -p "$STAGING/payload/opt/spacerouter/certs"
mkdir -p "$STAGING/payload/Library/LaunchDaemons"
mkdir -p "$STAGING/payload/etc/spacerouter"

cp "$BINARY" "$STAGING/payload/opt/spacerouter/space-router-node"
chmod 755 "$STAGING/payload/opt/spacerouter/space-router-node"

cp "$PROJECT_ROOT/launchd/com.spacerouter.homenode.plist" \
   "$STAGING/payload/Library/LaunchDaemons/"

cp "$PROJECT_ROOT/packaging/spacerouter.env" \
   "$STAGING/payload/opt/spacerouter/spacerouter.env.default"

# --- Copy install scripts ---
echo "Preparing install scripts..."
mkdir -p "$STAGING/scripts"
cp "$SCRIPT_DIR/scripts/preinstall"  "$STAGING/scripts/"
cp "$SCRIPT_DIR/scripts/postinstall" "$STAGING/scripts/"
chmod 755 "$STAGING/scripts/preinstall"
chmod 755 "$STAGING/scripts/postinstall"

# --- Build component package ---
echo "Building component package..."
pkgbuild \
    --root "$STAGING/payload" \
    --scripts "$STAGING/scripts" \
    --identifier "com.spacerouter.homenode" \
    --version "$VERSION" \
    --install-location "/" \
    "$STAGING/component.pkg"

# --- Generate distribution XML from template ---
echo "Generating distribution descriptor..."
sed "s/{{VERSION}}/$VERSION/g" "$SCRIPT_DIR/distribution.xml" \
    > "$STAGING/distribution.xml"

# --- Build final product package ---
echo "Building product package..."
SIGN_ARGS=()
if [[ -n "$SIGN_IDENTITY" ]]; then
    SIGN_ARGS=(--sign "$SIGN_IDENTITY")
    echo "Signing with: $SIGN_IDENTITY"
fi

productbuild \
    --distribution "$STAGING/distribution.xml" \
    --resources "$SCRIPT_DIR/resources" \
    --package-path "$STAGING" \
    ${SIGN_ARGS[@]+"${SIGN_ARGS[@]}"} \
    "$OUTPUT"

echo ""
echo "=== .pkg built successfully: $OUTPUT ==="
ls -lh "$OUTPUT"
