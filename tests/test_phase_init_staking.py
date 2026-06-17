"""Mandatory staking-address gate in ``_phase_init``.

Pre-change, ``_phase_init`` silently substituted the auto-generated
identity address when ``SR_STAKING_ADDRESS`` was empty. The node then
registered with coord using a wallet the operator had never seen, the
on-chain stake check failed, and the GUI surfaced a confusing
"Insufficient SPACE staked" error.

Post-change: empty staking address is rejected up front with
``NodeErrorCode.MISSING_WALLET`` before any network or identity work
happens, so the operator gets an actionable error instead of a
mis-classified one.
"""

import pytest

from app.errors import NodeError, NodeErrorCode
from app.main import _NodeContext, _phase_init


@pytest.mark.asyncio
async def test_phase_init_raises_when_staking_address_empty(settings):
    """Empty STAKING_ADDRESS → MISSING_WALLET before identity load."""
    settings.STAKING_ADDRESS = ""
    settings.UPNP_ENABLED = False
    # PUBLIC_IP is preset on the fixture, so detect_public_ip is bypassed.
    ctx = _NodeContext(settings, http_client=None)

    with pytest.raises(NodeError) as exc_info:
        await _phase_init(ctx)
    assert exc_info.value.code == NodeErrorCode.MISSING_WALLET
    # The error must not have mutated the context — identity key was
    # never loaded, so the staking field stays empty.
    assert ctx.staking_address == ""
    assert ctx.identity_address == ""


@pytest.mark.asyncio
async def test_phase_init_raises_when_staking_address_whitespace(settings):
    """Whitespace-only STAKING_ADDRESS is treated the same as empty."""
    settings.STAKING_ADDRESS = "   "
    settings.UPNP_ENABLED = False
    ctx = _NodeContext(settings, http_client=None)

    with pytest.raises(NodeError) as exc_info:
        await _phase_init(ctx)
    assert exc_info.value.code == NodeErrorCode.MISSING_WALLET
