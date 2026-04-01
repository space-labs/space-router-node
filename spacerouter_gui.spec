# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SpaceRouter Desktop GUI."""

import os
import sys
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

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
    ],
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
        codesign_identity=None,
        entitlements_file=None,
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
        codesign_identity=None,
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
            name="SpaceRouter.app",
            icon="packaging/macos/SpaceRouter.icns",
            bundle_identifier="com.spacerouter.desktop",
            info_plist={
                "CFBundleShortVersionString": _bundle_version,
                "CFBundleName": "SpaceRouter",
                "NSHighResolutionCapable": True,
            },
        )
