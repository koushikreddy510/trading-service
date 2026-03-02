from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class OrderRequest:
    symbol: str
    exchange: str
    side: str  # BUY | SELL
    quantity: int
    order_type: str  # LIMIT | MARKET | SL | SL-M
    price: Optional[float] = None
    trigger_price: Optional[float] = None

@dataclass
class OrderResult:
    success: bool
    broker_order_id: Optional[str] = None
    message: Optional[str] = None

@dataclass
class Position:
    symbol: str
    exchange: str
    side: str
    quantity: int
    avg_price: float
    current_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None

class BrokerAdapter(ABC):
    @abstractmethod
    def place_order(self, account_id: str, req: OrderRequest, credentials_ref: str) -> OrderResult:
        pass

    @abstractmethod
    def cancel_order(self, account_id: str, broker_order_id: str, credentials_ref: str) -> bool:
        pass

    @abstractmethod
    def get_positions(self, account_id: str, credentials_ref: str) -> List[Position]:
        pass

    @abstractmethod
    def get_orders(self, account_id: str, credentials_ref: str, status: Optional[str] = None) -> List[dict]:
        pass
