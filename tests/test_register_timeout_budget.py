"""The register call must outwait the coordination server's own budget.

`POST /nodes/register` is not a normal request. The coordination server verifies
our inbound endpoint INSIDE it: a CONNECT challenge back to this node, an
egress-IP check routed through our proxy, then an IP classification. Each step
carries its own multi-second timeout, so the honest answer can legitimately
arrive tens of seconds in.

At 15s we frequently gave up before it arrived. When we did, we reported
CONNECTION_LOST -- "Connection to coordination server interrupted" -- and then
retried a registration the server had often already completed.

Deliberately NOT changed: a register ReadTimeout still classifies as
CONNECTION_LOST. An earlier proposal relabelled it ENDPOINT_UNREACHABLE with
copy telling the operator to forward their port. Production data killed that
idea -- all 96 connection_lost reports on record are this same ReadTimeout, and
8 of them landed during a server-side database outage in which the coordination
server had ALREADY completed the CONNECT challenge successfully before hanging.
The copy would have sent those operators to their routers to fix something the
server had just proven was working. A timeout is the one signal that carries no
information about the operator's port, so it must not be attributed to it.
"""
from __future__ import annotations

import httpx

import app.registration as reg
from app.errors import NodeErrorCode, classify_error

# The coordination server's own worst realistic register budget, from its
# configured timeouts: CONNECT challenge (connect + response line, ~10s each)
# plus the egress-IP verification (~10s each for connect and read).
_COORD_WORST_REALISTIC_BUDGET_S = 40.0


def test_register_budget_outwaits_the_coordination_server():
    assert hasattr(reg, "REGISTER_TIMEOUT_SECONDS"), (
        "REGISTER_TIMEOUT_SECONDS is missing — the register budget must be a "
        "named constant, not a literal buried in the call"
    )
    assert reg.REGISTER_TIMEOUT_SECONDS >= _COORD_WORST_REALISTIC_BUDGET_S, (
        f"register budget {reg.REGISTER_TIMEOUT_SECONDS}s is below the "
        f"coordination server's worst realistic budget "
        f"({_COORD_WORST_REALISTIC_BUDGET_S}s), so we can still give up before "
        f"its honest answer arrives and then retry work it already did"
    )


def test_register_call_uses_the_constant_not_a_literal():
    """The POST must carry the vetted budget."""
    import inspect

    source = inspect.getsource(reg._do_register)
    assert "timeout=REGISTER_TIMEOUT_SECONDS" in source, (
        "the register POST does not use REGISTER_TIMEOUT_SECONDS — a literal "
        "here is how the budget silently drifted out of step with the server"
    )
    assert "timeout=15.0" not in source, "the old 15s literal is still present"


def test_register_read_timeout_is_still_connection_lost():
    """Regression guard on a deliberate decision, not an oversight.

    If someone reclassifies this as ENDPOINT_UNREACHABLE, operators get told to
    reconfigure their router during OUR outages. Read this module's docstring
    before changing it.
    """
    err = classify_error(httpx.ReadTimeout("read timeout"))
    assert err.code is NodeErrorCode.CONNECTION_LOST, (
        f"a register read timeout now classifies as {err.code} — a timeout "
        f"carries no information about the operator's inbound port, so it must "
        f"not be attributed to it"
    )
    assert "port" not in err.user_message.lower(), (
        f"the timeout message mentions the operator's port: "
        f"{err.user_message!r}. During a server-side outage this is false."
    )
    assert err.is_transient, "a timeout must remain retryable"
