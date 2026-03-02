"""
Flattrade broker adapter (NSE/MCX).
Uses NorenApiPy from flattrade/pythonAPI when available.
Credentials: env TRADING_FLATTRADE_<acc>_USER, TRADING_FLATTRADE_<acc>_TOKEN.
"""
from typing import List, Optional
from trading_service.adapters.base import BrokerAdapter, OrderRequest, OrderResult, Position

try:
    from NorenApi import NorenApi as NorenApiPy
    FLATTRADE_AVAILABLE = True
except ImportError:
    try:
        from api_helper import NorenApiPy
        FLATTRADE_AVAILABLE = True
    except ImportError:
        FLATTRADE_AVAILABLE = False
        NorenApiPy = None


class FlattradeAdapter(BrokerAdapter):
    def __init__(self):
        self._api = None

    def _get_api(self, user_id: str, token: str):
        if not FLATTRADE_AVAILABLE or NorenApiPy is None:
            return None
        if self._api is None:
            self._api = NorenApiPy()
            ret = self._api.set_session(userid=user_id, password="", usertoken=token)
            if ret is not None:
                return None
        return self._api

    def place_order(self, account_id: str, req: OrderRequest, credentials_ref: str) -> OrderResult:
        import os
        user = os.getenv(f"TRADING_FLATTRADE_{credentials_ref}_USER") or os.getenv("TRADING_FLATTRADE_USER")
        token = os.getenv(f"TRADING_FLATTRADE_{credentials_ref}_TOKEN") or os.getenv("TRADING_FLATTRADE_TOKEN")
        if not user or not token:
            return OrderResult(success=False, message="Flattrade credentials not set (TRADING_FLATTRADE_*_USER, TOKEN)")
        api = self._get_api(user, token)
        if api is None and FLATTRADE_AVAILABLE and NorenApiPy:
            return OrderResult(success=False, message="Flattrade session failed")
        if not FLATTRADE_AVAILABLE:
            return OrderResult(success=False, message="Flattrade SDK not installed (pip install flattrade or clone pythonAPI)")

        buy_or_sell = "B" if req.side.upper() == "BUY" else "S"
        price_type = "LMT" if req.order_type.upper() == "LIMIT" else "MKT"
        tradingsymbol = req.symbol if "-" in req.symbol else f"{req.symbol}-EQ"
        if req.exchange.upper() == "MCX":
            tradingsymbol = req.symbol

        try:
            ret = api.place_order(
                buy_or_sell=buy_or_sell,
                product_type="M" if req.order_type.upper() in ("SL", "SL-M") else "C",
                exchange=req.exchange.upper(),
                tradingsymbol=tradingsymbol,
                quantity=req.quantity,
                discloseqty=0,
                price_type=price_type,
                price=req.price or 0,
                trigger_price=req.trigger_price or 0,
                retention="DAY",
                remarks="ts",
            )
            if isinstance(ret, dict) and ret.get("stat") == "Ok":
                return OrderResult(success=True, broker_order_id=ret.get("norenordno"))
            return OrderResult(success=False, message=str(ret))
        except Exception as e:
            return OrderResult(success=False, message=str(e))

    def cancel_order(self, account_id: str, broker_order_id: str, credentials_ref: str) -> bool:
        if not FLATTRADE_AVAILABLE:
            return False
        import os
        user = os.getenv(f"TRADING_FLATTRADE_{credentials_ref}_USER") or os.getenv("TRADING_FLATTRADE_USER")
        token = os.getenv(f"TRADING_FLATTRADE_{credentials_ref}_TOKEN") or os.getenv("TRADING_FLATTRADE_TOKEN")
        api = self._get_api(user, token) if user and token else None
        if not api:
            return False
        try:
            ret = api.cancel_order(orderno=broker_order_id)
            return isinstance(ret, dict) and ret.get("stat") == "Ok"
        except Exception:
            return False

    def get_positions(self, account_id: str, credentials_ref: str) -> List[Position]:
        if not FLATTRADE_AVAILABLE:
            return []
        import os
        user = os.getenv(f"TRADING_FLATTRADE_{credentials_ref}_USER") or os.getenv("TRADING_FLATTRADE_USER")
        token = os.getenv(f"TRADING_FLATTRADE_{credentials_ref}_TOKEN") or os.getenv("TRADING_FLATTRADE_TOKEN")
        api = self._get_api(user, token) if user and token else None
        if not api:
            return []
        try:
            raw = api.get_positions()
            if not raw:
                return []
            out = []
            for r in raw:
                qty = int(float(r.get("netqty", 0)))
                if qty == 0:
                    continue
                side = "BUY" if qty > 0 else "SELL"
                out.append(Position(
                    symbol=r.get("tsym", ""),
                    exchange=r.get("exch", "NSE"),
                    side=side,
                    quantity=abs(qty),
                    avg_price=float(r.get("avgprc", 0) or 0),
                    current_price=float(r.get("lp", 0) or 0) or None,
                    unrealized_pnl=float(r.get("urmtom", 0) or 0) or None,
                ))
            return out
        except Exception:
            return []

    def get_orders(self, account_id: str, credentials_ref: str, status: Optional[str] = None) -> List[dict]:
        if not FLATTRADE_AVAILABLE:
            return []
        import os
        user = os.getenv(f"TRADING_FLATTRADE_{credentials_ref}_USER") or os.getenv("TRADING_FLATTRADE_USER")
        token = os.getenv(f"TRADING_FLATTRADE_{credentials_ref}_TOKEN") or os.getenv("TRADING_FLATTRADE_TOKEN")
        api = self._get_api(user, token) if user and token else None
        if not api:
            return []
        try:
            raw = api.get_order_book()
            return raw if isinstance(raw, list) else []
        except Exception:
            return []
