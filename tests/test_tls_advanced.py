"""Advanced TLS tests — certificate properties, key size, expiry, rotation."""

import datetime
import os
import ssl
import stat

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from app.tls import create_server_ssl_context, ensure_certificates


class TestCertificateProperties:
    def test_key_is_rsa_4096(self, tmp_path):
        """Generated key should be RSA 4096-bit."""
        cert = str(tmp_path / "test.crt")
        key = str(tmp_path / "test.key")
        ensure_certificates(cert, key)

        with open(key, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)
        assert isinstance(private_key, rsa.RSAPrivateKey)
        assert private_key.key_size == 4096

    def test_cert_common_name(self, tmp_path):
        """Certificate CN should be 'SpaceRouter Home Node'."""
        cert_path = str(tmp_path / "test.crt")
        key_path = str(tmp_path / "test.key")
        ensure_certificates(cert_path, key_path)

        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
        assert cn == "SpaceRouter Home Node"

    def test_cert_validity_365_days(self, tmp_path):
        """Certificate should be valid for approximately 365 days."""
        cert_path = str(tmp_path / "test.crt")
        key_path = str(tmp_path / "test.key")
        ensure_certificates(cert_path, key_path)

        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())

        now = datetime.datetime.now(datetime.timezone.utc)
        # not_valid_before should be roughly now
        assert abs((cert.not_valid_before_utc - now).total_seconds()) < 60
        # not_valid_after should be roughly 365 days from now
        delta = cert.not_valid_after_utc - cert.not_valid_before_utc
        assert 364 <= delta.days <= 366

    def test_cert_is_self_signed(self, tmp_path):
        """Issuer and subject should be identical (self-signed)."""
        cert_path = str(tmp_path / "test.crt")
        key_path = str(tmp_path / "test.key")
        ensure_certificates(cert_path, key_path)

        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        assert cert.issuer == cert.subject

    def test_cert_uses_sha256(self, tmp_path):
        """Certificate should be signed with SHA-256."""
        cert_path = str(tmp_path / "test.crt")
        key_path = str(tmp_path / "test.key")
        ensure_certificates(cert_path, key_path)

        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        assert "sha256" in cert.signature_algorithm_oid._name.lower()

    def test_cert_file_permissions(self, tmp_path):
        """Certificate file should be world-readable (0644)."""
        cert_path = str(tmp_path / "test.crt")
        key_path = str(tmp_path / "test.key")
        ensure_certificates(cert_path, key_path)

        cert_mode = stat.S_IMODE(os.stat(cert_path).st_mode)
        assert cert_mode == 0o644


class TestSSLContextCiphers:
    def test_weak_ciphers_rejected(self, tmp_path):
        """SSL context should not include RC4, DES, or NULL ciphers."""
        cert = str(tmp_path / "test.crt")
        key = str(tmp_path / "test.key")
        ensure_certificates(cert, key)

        ctx = create_server_ssl_context(cert, key)
        ciphers = [c["name"] for c in ctx.get_ciphers()]

        for cipher_name in ciphers:
            assert "RC4" not in cipher_name
            assert "DES" not in cipher_name.replace("ECDSA", "")  # avoid false positive
            assert "NULL" not in cipher_name
            assert "EXPORT" not in cipher_name

    def test_only_aead_ciphers(self, tmp_path):
        """All allowed ciphers should use AEAD (GCM or CHACHA20)."""
        cert = str(tmp_path / "test.crt")
        key = str(tmp_path / "test.key")
        ensure_certificates(cert, key)

        ctx = create_server_ssl_context(cert, key)
        ciphers = [c["name"] for c in ctx.get_ciphers()]

        for cipher_name in ciphers:
            assert "GCM" in cipher_name or "CHACHA20" in cipher_name, \
                f"Non-AEAD cipher found: {cipher_name}"
