"""
Paytm Money broker adapter (stocks, MTF).
TODO: Integrate Paytm Money API when available.
Credentials: env TRADING_PAYTM_*
"""
from typing import List, Optional
from trading_service.adapters.base import BrokerAdapter, OrderRequest, OrderResult, Position


class PaytmMoneyAdapter(BrokerAdapter):
    def place_order(self, account_id: str, req: OrderRequest, credentials_ref: str) -> OrderResult:
        return OrderResult(success=False, message="Paytm Money adapter not yet implemented")

    def cancel_order(self, account_id: str, broker_order_id: str, credentials_ref: str) -> bool:
        return False

    def get_positions(self, account_id: str, credentials_ref: str) -> List[Position]:
        return []

    def get_orders(self, account_id: str, credentials_ref: str, status: Optional[str] = None) -> List[dict]:
        return []
