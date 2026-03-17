"""Tests for EVM wallet address validation."""

import pytest

from app.wallet import validate_wallet_address


class TestValidateWalletAddress:
    def test_valid_address_with_0x(self):
        result = validate_wallet_address("0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
        assert result == "0x742d35cc6634c0532925a3b844bc9e7595f2bd18"

    def test_valid_address_without_0x(self):
        result = validate_wallet_address("742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
        assert result == "0x742d35cc6634c0532925a3b844bc9e7595f2bd18"

    def test_uppercase_address(self):
        result = validate_wallet_address("0xABCDEF1234567890ABCDEF1234567890ABCDEF12")
        assert result == "0xabcdef1234567890abcdef1234567890abcdef12"

    def test_already_lowercase(self):
        addr = "0xabcdef1234567890abcdef1234567890abcdef12"
        assert validate_wallet_address(addr) == addr

    def test_empty_string(self):
        with pytest.raises(ValueError, match="Invalid EVM wallet address"):
            validate_wallet_address("")

    def test_too_short(self):
        with pytest.raises(ValueError, match="Invalid EVM wallet address"):
            validate_wallet_address("0x1234")

    def test_too_long(self):
        with pytest.raises(ValueError, match="Invalid EVM wallet address"):
            validate_wallet_address("0x" + "a" * 41)

    def test_non_hex_chars(self):
        with pytest.raises(ValueError, match="Invalid EVM wallet address"):
            validate_wallet_address("0xGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG")

    def test_only_0x_prefix(self):
        with pytest.raises(ValueError, match="Invalid EVM wallet address"):
            validate_wallet_address("0x")
