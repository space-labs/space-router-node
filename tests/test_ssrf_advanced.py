"""Advanced SSRF tests — IPv4-mapped IPv6, edge cases, boundary IPs."""

import pytest

from app.proxy_handler import _is_private_target


class TestSSRFAdvanced:
    """Test edge cases in SSRF protection that attackers commonly exploit."""

    # IPv4-mapped IPv6 addresses (common SSRF bypass)
    @pytest.mark.parametrize("host,expected", [
        ("::ffff:127.0.0.1", True),       # loopback
        ("::ffff:10.0.0.1", True),         # private
        ("::ffff:192.168.1.1", True),      # private
        ("::ffff:169.254.169.254", True),  # cloud metadata
        ("::ffff:8.8.8.8", False),         # public (should pass)
    ])
    def test_ipv4_mapped_ipv6(self, host, expected):
        assert _is_private_target(host, 80) is expected

    # Boundary addresses
    @pytest.mark.parametrize("host,expected", [
        ("9.255.255.255", False),   # just before 10.0.0.0/8
        ("10.0.0.0", True),         # start of 10.0.0.0/8
        ("10.255.255.255", True),   # end of 10.0.0.0/8
        ("11.0.0.0", False),        # just after 10.0.0.0/8
        ("172.15.255.255", False),  # just before 172.16.0.0/12
        ("172.16.0.0", True),       # start
        ("172.31.255.255", True),   # end
        ("172.32.0.0", False),      # just after
        ("192.167.255.255", False), # just before 192.168.0.0/16
        ("192.168.0.0", True),      # start
        ("192.168.255.255", True),  # end
        ("192.169.0.0", False),     # just after
    ])
    def test_boundary_addresses(self, host, expected):
        assert _is_private_target(host, 80) is expected

    # Zero IP and broadcast
    @pytest.mark.parametrize("host,expected", [
        ("0.0.0.0", True),
        ("0.0.0.1", True),
        ("255.255.255.255", True),
    ])
    def test_special_addresses(self, host, expected):
        assert _is_private_target(host, 80) is expected

    # Hostname patterns
    @pytest.mark.parametrize("host,expected", [
        ("LOCALHOST", True),             # case insensitive
        ("LocalHost", True),             # mixed case
        ("my-device.LOCAL", True),       # .local suffix
        ("router.local", True),          # common mDNS
        ("not-local.example.com", False), # contains 'local' but not .local
    ])
    def test_hostname_patterns(self, host, expected):
        assert _is_private_target(host, 80) is expected

    # Port edge cases
    @pytest.mark.parametrize("port,expected", [
        (22, True),      # SSH
        (23, True),      # Telnet
        (25, True),      # SMTP
        (80, False),     # HTTP — allowed
        (443, False),    # HTTPS — allowed
        (445, True),     # SMB
        (3306, True),    # MySQL
        (5432, True),    # PostgreSQL
        (6379, True),    # Redis
        (8080, False),   # alt HTTP — allowed
        (8443, False),   # alt HTTPS — allowed
        (11211, True),   # Memcached
        (27017, True),   # MongoDB
    ])
    def test_port_restrictions(self, port, expected):
        # Use a public IP so only port restriction matters
        assert _is_private_target("93.184.216.34", port) is expected
