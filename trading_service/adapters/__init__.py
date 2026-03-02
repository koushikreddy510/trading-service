from trading_service.adapters.base import BrokerAdapter, OrderRequest, OrderResult, Position

__all__ = ["BrokerAdapter", "OrderRequest", "OrderResult", "Position", "get_adapter"]

def get_adapter(broker: str):
    if broker == "flattrade":
        from trading_service.adapters.flattrade_adapter import FlattradeAdapter
        return FlattradeAdapter
    if broker == "paytm_money":
        from trading_service.adapters.paytm_adapter import PaytmMoneyAdapter
        return PaytmMoneyAdapter
    if broker == "kotak":
        from trading_service.adapters.kotak_adapter import KotakAdapter
        return KotakAdapter
    raise ValueError(f"Unknown broker: {broker}")
