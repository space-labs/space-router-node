"""Tests for configuration, defaults, and validation."""

import os
import warnings

import pytest


class TestConfigDefaults:
    def test_default_port(self):
        from app.config import Settings
        s = Settings()
        assert s.NODE_PORT == 9090

    def test_default_buffer_size(self):
        from app.config import Settings
        s = Settings()
        assert s.BUFFER_SIZE == 65536

    def test_default_max_connections(self):
        from app.config import Settings
        s = Settings()
        assert s.MAX_CONNECTIONS == 256

    def test_default_bind_address(self):
        from app.config import Settings
        s = Settings()
        assert s.BIND_ADDRESS == "0.0.0.0"

    def test_default_upnp_enabled(self):
        from app.config import Settings
        s = Settings()
        assert s.UPNP_ENABLED is True

    def test_default_tls_paths(self):
        from app.config import Settings
        s = Settings()
        assert s.TLS_CERT_PATH == "certs/node.crt"
        assert s.TLS_KEY_PATH == "certs/node.key"


class TestConfigOverrides:
    def test_env_prefix(self):
        """Settings should read SR_ prefixed environment variables."""
        from app.config import Settings
        os.environ["SR_NODE_PORT"] = "8888"
        os.environ["SR_LOG_LEVEL"] = "DEBUG"
        try:
            s = Settings()
            assert s.NODE_PORT == 8888
            assert s.LOG_LEVEL == "DEBUG"
        finally:
            del os.environ["SR_NODE_PORT"]
            del os.environ["SR_LOG_LEVEL"]


class TestConfigHTTPWarning:
    def test_http_coordination_url_warns_for_remote(self):
        """Non-localhost HTTP Coordination API URL should emit a warning."""
        os.environ["SR_COORDINATION_API_URL"] = "http://remote-server.com:8000"
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                # Force re-evaluation by importing fresh
                import importlib
                import app.config
                importlib.reload(app.config)
                # Check if warning was issued
                http_warnings = [x for x in w if "plain HTTP" in str(x.message)]
                assert len(http_warnings) > 0
        finally:
            os.environ["SR_COORDINATION_API_URL"] = "http://localhost:8000"
            import importlib
            import app.config
            importlib.reload(app.config)

    def test_https_coordination_url_no_warning(self):
        """HTTPS Coordination API URL should not emit a warning."""
        os.environ["SR_COORDINATION_API_URL"] = "https://api.spacerouter.net"
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                import importlib
                import app.config
                importlib.reload(app.config)
                http_warnings = [x for x in w if "plain HTTP" in str(x.message)]
                assert len(http_warnings) == 0
        finally:
            os.environ["SR_COORDINATION_API_URL"] = "http://localhost:8000"
            import importlib
            import app.config
            importlib.reload(app.config)

    def test_localhost_http_no_warning(self):
        """localhost HTTP is acceptable for development — no warning."""
        os.environ["SR_COORDINATION_API_URL"] = "http://localhost:8000"
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                import importlib
                import app.config
                importlib.reload(app.config)
                http_warnings = [x for x in w if "plain HTTP" in str(x.message)]
                assert len(http_warnings) == 0
        finally:
            import importlib
            import app.config
            importlib.reload(app.config)
