# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SpaceRouter Home Node."""

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
_codesign_identity = os.environ.get("CODESIGN_IDENTITY")

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
    # Rich TUI
    "rich",
    "rich.console",
    "rich.live",
    "rich.panel",
    "rich.prompt",
    "rich.table",
    "rich.text",
]

# Collect all pydantic submodules to handle dynamic imports
hiddenimports += collect_submodules("pydantic")
hiddenimports += collect_submodules("pydantic_core")
hiddenimports += collect_submodules("eth_account")
hiddenimports += collect_submodules("web3")
hiddenimports += collect_submodules("rich")

# C4 (QA Build 129, finding 9): force the CI-stamped build-identity modules
# into the bundle. app/version.py and app/variant.py import them via
# try/except at runtime, which PyInstaller's static analysis can miss; a
# missing app/_build_variant flips the frozen binary to the fallback variant.
# Conditional so local dev builds (no stamped files) don't warn.
_app_dir = os.path.join(
    os.path.abspath(SPECPATH if "SPECPATH" in dir() else "."), "app",
)
for _mod, _fname in (("app._build_version", "_build_version.py"),
                     ("app._build_variant", "_build_variant.py")):
    if os.path.exists(os.path.join(_app_dir, _fname)):
        hiddenimports.append(_mod)

a = Analysis(
    ["app/main.py"],
    pathex=[],
    binaries=[],
    datas=[
        # TokenPaymentEscrow ABI — the file is read from two different
        # runtime paths because PyInstaller flattens the entry-point
        # module's package root onto ``_MEIPASS`` *and* keeps sibling
        # packages at their imported path:
        #   - app/main.py               → _MEIPASS/main.py, so the startup
        #     check at app/main.py:604 resolves to _MEIPASS/payment/…
        #   - app/payment/settlement.py → _MEIPASS/app/payment/..., so its
        #     _ABI_PATH resolves to _MEIPASS/app/payment/…
        # Ship the JSON at BOTH destinations or one of the two paths
        # fails on ``--claim``/startup with FileNotFoundError.
        ("app/payment/escrow_abi.json", "payment"),
        ("app/payment/escrow_abi.json", "app/payment"),
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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="space-router-node",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=_codesign_identity,
    entitlements_file=None,
)
