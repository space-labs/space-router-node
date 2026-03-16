"""Tests for EVM wallet key management and challenge signing."""

import os

import pytest

from app.wallet import (
    ensure_wallet_key,
    private_key_to_address,
    sign_challenge,
    verify_challenge,
)

# Well-known test private key (from Ethereum dev docs — DO NOT use in production)
_TEST_KEY = "4c0883a69102937d6231471b5dbb6204fe512961708279f40aad9b5e2ec3699d"
_TEST_ADDRESS = "0xcF53850b0674E149F95A942f4f311CB1CD0F4958"


class TestPrivateKeyToAddress:
    def test_known_vector(self):
        assert private_key_to_address(_TEST_KEY).lower() == _TEST_ADDRESS.lower()

    def test_accepts_0x_prefix(self):
        assert private_key_to_address(f"0x{_TEST_KEY}").lower() == _TEST_ADDRESS.lower()


class TestSignChallenge:
    def test_returns_130_hex_chars(self):
        sig = sign_challenge(_TEST_KEY, "93.184.216.34")
        assert len(sig) == 130
        # Verify it's valid hex
        bytes.fromhex(sig)

    def test_deterministic(self):
        sig1 = sign_challenge(_TEST_KEY, "93.184.216.34")
        sig2 = sign_challenge(_TEST_KEY, "93.184.216.34")
        assert sig1 == sig2

    def test_different_ip_different_signature(self):
        sig1 = sign_challenge(_TEST_KEY, "1.2.3.4")
        sig2 = sign_challenge(_TEST_KEY, "5.6.7.8")
        assert sig1 != sig2


class TestVerifyChallenge:
    def test_roundtrip(self):
        ip = "93.184.216.34"
        sig = sign_challenge(_TEST_KEY, ip)
        address = private_key_to_address(_TEST_KEY)
        assert verify_challenge(sig, ip, address) is True

    def test_wrong_address(self):
        ip = "93.184.216.34"
        sig = sign_challenge(_TEST_KEY, ip)
        assert verify_challenge(sig, ip, "0x0000000000000000000000000000000000000001") is False

    def test_wrong_ip(self):
        sig = sign_challenge(_TEST_KEY, "1.2.3.4")
        address = private_key_to_address(_TEST_KEY)
        assert verify_challenge(sig, "5.6.7.8", address) is False

    def test_ipv6(self):
        ip = "2001:db8::1"
        sig = sign_challenge(_TEST_KEY, ip)
        address = private_key_to_address(_TEST_KEY)
        assert verify_challenge(sig, ip, address) is True


class TestEnsureWalletKey:
    def test_generates_new_key(self, tmp_path):
        key_path = str(tmp_path / "wallet.key")
        hex_key = ensure_wallet_key(key_path)

        assert len(hex_key) == 64
        assert os.path.isfile(key_path)
        # Verify restrictive permissions
        assert oct(os.stat(key_path).st_mode & 0o777) == "0o600"

    def test_loads_existing_key(self, tmp_path):
        key_path = str(tmp_path / "wallet.key")
        with open(key_path, "w") as f:
            f.write(_TEST_KEY)

        loaded = ensure_wallet_key(key_path)
        assert loaded == _TEST_KEY

    def test_loads_key_with_0x_prefix(self, tmp_path):
        key_path = str(tmp_path / "wallet.key")
        with open(key_path, "w") as f:
            f.write(f"0x{_TEST_KEY}\n")

        loaded = ensure_wallet_key(key_path)
        assert loaded == _TEST_KEY

    def test_generates_valid_signing_key(self, tmp_path):
        key_path = str(tmp_path / "wallet.key")
        hex_key = ensure_wallet_key(key_path)

        # Should produce a valid address and signature
        address = private_key_to_address(hex_key)
        assert address.startswith("0x")
        sig = sign_challenge(hex_key, "1.2.3.4")
        assert verify_challenge(sig, "1.2.3.4", address) is True

    def test_same_key_across_calls(self, tmp_path):
        key_path = str(tmp_path / "wallet.key")
        key1 = ensure_wallet_key(key_path)
        key2 = ensure_wallet_key(key_path)
        assert key1 == key2

    def test_creates_parent_directories(self, tmp_path):
        key_path = str(tmp_path / "nested" / "dir" / "wallet.key")
        hex_key = ensure_wallet_key(key_path)
        assert len(hex_key) == 64
        assert os.path.isfile(key_path)
