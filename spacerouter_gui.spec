# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SpaceRouter Desktop GUI."""

import os
import sys
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

block_cipher = None
_codesign_identity = os.environ.get("CODESIGN_IDENTITY")

# Read build version from app/_build_version.py (written by CI)
_bundle_version = "0.1.0"
_build_version_path = os.path.join(
    os.path.abspath(SPECPATH if "SPECPATH" in dir() else "."),
    "app", "_build_version.py",
)
if os.path.exists(_build_version_path):
    _ns = {}
    with open(_build_version_path) as _f:
        exec(_f.read(), _ns)  # noqa: S102 — reads our own CI-generated file
    _bundle_version = _ns.get("BUILD_VERSION", _bundle_version).lstrip("v").split("-")[0]

# Distribution METADATA, not just module code. eth-keyfile >= 0.10 performs an
# importlib.metadata lookup at import time, so a frozen build that bundles the
# module but not its .dist-info dies with:
#   PackageNotFoundError: No package metadata was found for py_ecc
# ...from `from eth_account import Account`, which the node does on every cold
# start to load or create its identity key. The build stayed green for months
# and then broke with zero code change, because eth-account/eth-keyfile are
# range-pinned (`>=0.13,<1`) and 0.14.0 / 0.10.0 landed upstream.
#
# Tolerant of absence so the spec does not hard-fail if a package is dropped.
_metadata_packages = [
    "py_ecc",
    "eth_account",
    "eth_keyfile",
    "eth_keys",
    "eth_hash",
    "eth_utils",
    "hexbytes",
    "web3",
]
metadatas = []
for _pkg in _metadata_packages:
    try:
        metadatas += copy_metadata(_pkg)
    except Exception:
        pass

hiddenimports = [
    # Conditionally imported at runtime
    "miniupnpc",
    # pydantic v2 uses a Rust-compiled core loaded dynamically
    "pydantic",
    "pydantic_core",
    "pydantic_settings",
    "pydantic_settings.main",
    # dotenv loaded by pydantic-settings
    "dotenv",
    # httpx transport stack
    "httpx",
    "httpcore",
    "h11",
    "certifi",
    "idna",
    "sniffio",
    "anyio",
    "anyio._backends._asyncio",
    # cryptography internals sometimes missed
    "cryptography.hazmat.primitives.asymmetric.rsa",
    "cryptography.hazmat.primitives.hashes",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.x509",
    "cryptography.x509.oid",
    # Identity signing (eth-account / web3)
    "eth_account",
    "eth_account.messages",
    "eth_keys",
    "eth_hash",
    "web3",
    # pywebview
    "webview",
]

# C4 (QA Build 129, finding 9): force the CI-stamped build-identity modules
# into the bundle. They're imported via try/except at runtime (app/version.py,
# app/variant.py, settings_v2._seed_build_variant), so PyInstaller's static
# analysis can miss them. A missing app/_build_variant silently flips the
# frozen app to the fallback variant — which is what made the clean-install
# Environment default differ across platforms (macOS → Production, Windows →
# Test). Conditional so local dev builds (no stamped files) don't warn.
_app_dir = os.path.dirname(_build_version_path)
for _mod, _fname in (("app._build_version", "_build_version.py"),
                     ("app._build_variant", "_build_variant.py")):
    if os.path.exists(os.path.join(_app_dir, _fname)):
        hiddenimports.append(_mod)

# Platform-specific webview backends
if sys.platform == "darwin":
    hiddenimports += [
        "webview.platforms.cocoa",
        "objc",
        "Foundation",
        "AppKit",
        "WebKit",
    ]
elif sys.platform == "win32":
    hiddenimports += [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr",
        # System tray (pystray + Pillow)
        "pystray",
        "pystray._win32",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
    ]

# Collect all pydantic submodules to handle dynamic imports
hiddenimports += collect_submodules("pydantic")
hiddenimports += collect_submodules("pydantic_core")
hiddenimports += collect_submodules("eth_account")
hiddenimports += collect_submodules("web3")

a = Analysis(
    ["gui/app.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("gui/assets", "gui/assets"),
        # TokenPaymentEscrow ABI — ship at both bundle paths because the
        # GUI entry point (gui/app.py) lands at the bundle root while
        # app/payment/settlement.py keeps its package path. See homenode.spec
        # for the full explanation.
        ("app/payment/escrow_abi.json", "payment"),
        ("app/payment/escrow_abi.json", "app/payment"),
    ] + metadatas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "pytest_asyncio",
        "respx",
        "_pytest",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if sys.platform == "win32":
    # Windows: single-file executable (no _internal/ directory needed)
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="SpaceRouter",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=_codesign_identity,
        entitlements_file=None,
        icon="packaging/windows/SpaceRouter.ico",
    )
else:
    # macOS/Linux: COLLECT mode (required for macOS .app bundle)
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="SpaceRouter",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=_codesign_identity,
        entitlements_file=None,
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="SpaceRouter",
    )

    if sys.platform == "darwin":
        app = BUNDLE(
            coll,
            name="SpaceRouter Proxy.app",
            icon="packaging/macos/SpaceRouter.icns",
            bundle_identifier="com.spacerouter.desktop",
            info_plist={
                "CFBundleShortVersionString": _bundle_version,
                "CFBundleName": "SpaceRouter",
                "NSHighResolutionCapable": True,
            },
        )
