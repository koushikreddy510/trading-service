"""
Slack notifications for order fill, cancel, and alerts.
"""
import os
import requests
from typing import Optional

CHANNEL_ENV = {"jobs": "SLACK_JOBS_WEBHOOK_URL", "data": "SLACK_DATA_WEBHOOK_URL"}  # "orders" uses the main webhook


def webhook_for(account: str = "default", channel: str = "orders") -> Optional[str]:
    """The webhook for a message: jobs / data go to their own channel when one is set; orders go to
    SLACK_WEBHOOK_URL_<ACCOUNT> if set. Anything without its own webhook falls back to the shared one."""
    own = os.getenv(CHANNEL_ENV[channel]) if channel in CHANNEL_ENV else None
    if own:
        return own
    if channel == "orders" and account and account != "default":
        specific = os.getenv(f"SLACK_WEBHOOK_URL_{account.upper()}")
        if specific:
            return specific
    return os.getenv("SLACK_WEBHOOK_URL") or os.getenv("TRADING_SLACK_WEBHOOK_URL")


def send_slack(text: str, webhook_url: Optional[str] = None, account: str = "default", channel: str = "orders") -> bool:
    url = webhook_url or webhook_for(account, channel)
    if not url:
        return False
    try:
        r = requests.post(url, json={"text": text}, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def notify_order_placed(account: str, symbol: str, side: str, qty: int, order_type: str, broker_order_id: Optional[str] = None):
    msg = f"*Order placed* | {account} | {side} {qty} {symbol} ({order_type})"
    if broker_order_id:
        msg += f" | ID: `{broker_order_id}`"
    send_slack(msg)


def notify_order_filled(account: str, symbol: str, side: str, qty: int, price: float, broker_order_id: Optional[str] = None):
    msg = f"*Order filled* | {account} | {side} {qty} {symbol} @ {price}"
    if broker_order_id:
        msg += f" | ID: `{broker_order_id}`"
    send_slack(msg)


def notify_order_cancelled(account: str, symbol: str, reason: Optional[str] = None):
    msg = f"*Order cancelled* | {account} | {symbol}"
    if reason:
        msg += f" | {reason}"
    send_slack(msg)


def notify_alert(title: str, body: str):
    send_slack(f"*{title}*\n{body}")
