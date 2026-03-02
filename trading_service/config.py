"""
Trading service config: broker accounts, Slack webhook, feature flags.
Load from env; optional YAML later.
"""
import os
from typing import List, Optional
from dataclasses import dataclass, field

@dataclass
class BrokerAccount:
    broker: str  # flattrade | paytm_money | kotak
    account_id: str
    display_name: str = ""
    env_prefix: str = ""

@dataclass
class TradingConfig:
    slack_webhook_url: Optional[str] = None
    slack_enabled: bool = True
    accounts: List[BrokerAccount] = field(default_factory=list)

def load_config() -> TradingConfig:
    url = os.getenv("SLACK_WEBHOOK_URL") or os.getenv("TRADING_SLACK_WEBHOOK_URL")
    accounts: List[BrokerAccount] = []
    raw = os.getenv("TRADING_ACCOUNTS", "")
    for part in raw.split(","):
        part = part.strip()
        if ":" in part:
            broker, acc_id = part.split(":", 1)
            accounts.append(BrokerAccount(broker=broker.strip(), account_id=acc_id.strip()))
    return TradingConfig(slack_webhook_url=url, slack_enabled=bool(url), accounts=accounts)
