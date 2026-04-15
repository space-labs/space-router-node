"""SpaceRouter node payment modules for escrow-based proxy billing (v0.2.x).

The module-level ``_settlement_manager`` is set by main.py during startup
and used by proxy_handler.py to store signed receipts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.payment.settlement import SettlementManager

_settlement_manager: SettlementManager | None = None
