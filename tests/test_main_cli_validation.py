"""CLI argument validation — Phase A findings #6 / #7.

The Phase A real-user sweep showed the daemon happily starting with
``--port 0`` and ``--staking-address bogus``. Both produced "ghost
provider" registrations that surfaced as far-downstream gateway errors
hours later. ``app.main._validate_cli_args`` is the early-rejection
layer that catches these at the CLI boundary instead.

These tests exercise the validator directly so we don't have to pay
the full ``app.main`` import + asyncio setup cost on every assertion.
"""

import pytest

from app.main import _build_arg_parser, _validate_cli_args


GOOD_ADDR = "0x" + "ab" * 20


@pytest.fixture
def parser():
    return _build_arg_parser()


# ── Port validation ─────────────────────────────────────────────────


def test_port_zero_rejected(parser, capsys):
    args = parser.parse_args(["--port", "0"])
    with pytest.raises(SystemExit) as exc:
        _validate_cli_args(args)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--port must be in 1..65535" in err


def test_port_negative_rejected(parser, capsys):
    args = parser.parse_args(["--port", "-1"])
    with pytest.raises(SystemExit) as exc:
        _validate_cli_args(args)
    assert exc.value.code == 2
    assert "--port must be in 1..65535" in capsys.readouterr().err


def test_port_above_65535_rejected(parser, capsys):
    args = parser.parse_args(["--port", "70000"])
    with pytest.raises(SystemExit) as exc:
        _validate_cli_args(args)
    assert exc.value.code == 2
    assert "--port must be in 1..65535" in capsys.readouterr().err


def test_port_in_range_accepted(parser):
    args = parser.parse_args(["--port", "9090"])
    _validate_cli_args(args)  # must not raise


def test_port_default_passes(parser):
    args = parser.parse_args([])
    _validate_cli_args(args)


def test_public_port_above_range_rejected(parser, capsys):
    args = parser.parse_args(["--public-port", "99999"])
    with pytest.raises(SystemExit):
        _validate_cli_args(args)
    assert "--public-port must be in 1..65535" in capsys.readouterr().err


# ── Staking / collection address validation ────────────────────────


def test_staking_address_garbage_rejected(parser, capsys):
    args = parser.parse_args(["--staking-address", "bogus"])
    with pytest.raises(SystemExit) as exc:
        _validate_cli_args(args)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--staking-address" in err
    assert "Invalid EVM wallet address" in err


def test_staking_address_too_short_rejected(parser):
    args = parser.parse_args(["--staking-address", "0x" + "ab" * 19])
    with pytest.raises(SystemExit):
        _validate_cli_args(args)


def test_staking_address_non_hex_rejected(parser):
    args = parser.parse_args(["--staking-address", "0x" + "zz" * 20])
    with pytest.raises(SystemExit):
        _validate_cli_args(args)


def test_staking_address_valid_passes(parser):
    args = parser.parse_args(["--staking-address", GOOD_ADDR])
    _validate_cli_args(args)


def test_collection_address_garbage_rejected(parser, capsys):
    args = parser.parse_args(["--collection-address", "not-a-hex-address"])
    with pytest.raises(SystemExit):
        _validate_cli_args(args)
    err = capsys.readouterr().err
    assert "--collection-address" in err


# ── Multiple errors surface together ───────────────────────────────


def test_all_errors_reported_together(parser, capsys):
    """Operator should see every problem in one run, not one-at-a-time."""
    args = parser.parse_args([
        "--port", "0",
        "--public-port", "70000",
        "--staking-address", "garbage",
    ])
    with pytest.raises(SystemExit):
        _validate_cli_args(args)
    err = capsys.readouterr().err
    assert "--port must be in 1..65535" in err
    assert "--public-port must be in 1..65535" in err
    assert "--staking-address" in err


# ── load_settings() in cwd-restricted contexts ────────────────────


def test_load_settings_does_not_probe_cwd_dotenv(tmp_path, monkeypatch):
    """Phase A E2E surfaced this: ``--reset`` crashed under
    ``sudo -u spacerouter`` from ``/root`` because pydantic-settings
    called ``Path('.env').is_file()`` which raised ``PermissionError``
    when ``/root`` was 0700. The fix passes ``_env_file=None`` so the
    explicit-kwargs construction skips dotenv probing entirely.
    """
    from app.config import _settings_from_provider_settings
    from app.settings_v2 import Settings as V2Settings

    # Create a directory we can chdir into that contains a `.env` we
    # cannot read — simulate the /root scenario without needing root.
    bad_dir = tmp_path / "no-read"
    bad_dir.mkdir()
    bad_env = bad_dir / ".env"
    bad_env.write_text("SR_NODE_PORT=99\n")
    bad_env.chmod(0o000)

    try:
        monkeypatch.chdir(bad_dir)

        v = V2Settings()
        s = _settings_from_provider_settings(v)

        # Default port from the v2 settings, not the unreadable .env.
        assert s.NODE_PORT == 9090
    finally:
        bad_env.chmod(0o600)  # make tmp_path teardown happy
