"""
Trading service: multi-broker, multi-account order placement,
position tracking, trailing stops, PnL, and Slack notifications.
"""

__all__ = ["get_orders", "get_positions", "get_pnl_snapshots", "get_accounts"]

from trading_service.db import get_orders, get_positions, get_pnl_snapshots, get_accounts
