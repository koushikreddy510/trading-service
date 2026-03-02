"""
Trading service API: accounts, orders, positions, PnL.
Run: uvicorn main:app --host 0.0.0.0 --port 8001
"""
import os
from datetime import date
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

app = FastAPI(title="Trading Service", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/trading/accounts")
def trading_accounts():
    try:
        from trading_service.db import get_accounts
        return {"accounts": get_accounts()}
    except Exception as e:
        return {"accounts": [], "error": str(e)}


@app.get("/trading/orders")
def trading_orders(
    account_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    try:
        from trading_service.db import get_orders
        orders = get_orders(account_id=account_id, status=status, limit=limit, offset=offset)
        return {"orders": orders, "total": len(orders)}
    except Exception as e:
        return {"orders": [], "total": 0, "error": str(e)}


@app.get("/trading/positions")
def trading_positions(account_id: Optional[int] = Query(None)):
    try:
        from trading_service.db import get_positions
        return {"positions": get_positions(account_id=account_id)}
    except Exception as e:
        return {"positions": [], "error": str(e)}


@app.get("/trading/pnl")
def trading_pnl(
    account_id: Optional[int] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    limit: int = Query(90, ge=1, le=365),
):
    try:
        from trading_service.db import get_pnl_snapshots
        fd = date.fromisoformat(from_date) if from_date else None
        td = date.fromisoformat(to_date) if to_date else None
        snapshots = get_pnl_snapshots(account_id=account_id, from_date=fd, to_date=td, limit=limit)
        return {"snapshots": snapshots}
    except Exception as e:
        return {"snapshots": [], "error": str(e)}
