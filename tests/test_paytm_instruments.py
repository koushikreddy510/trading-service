import pytest

from trading_service.adapters.paytm_instruments import PaytmInstruments, parse_master

MASTER = """security_id,symbol,name,exchange,instrument_type,lot_size,tick_size,expiry_date,strike_price
3045 ,SBIN,STATE BANK OF INDIA,NSE,ES,1,0.05,,
500112,SBIN,STATE BANK OF INDIA,BSE,ES,1,0.05,,
9999,SBIN,STATE BANK OF INDIA (DUP SERIES),NSE,ES,1,0.05,,
12345,NIFTYBEES,NIPPON NIFTY BEES,NSE,ETF,1,0.01,,
26000,NIFTY 50,Nifty 50,NSE,I,1,0.05,,
51000,SBIN,STATE BANK OF INDIA FUT,NSE,FUTSTK,750,0.05,2026-10-27,
"""


def instruments():
    calls = []
    inst = PaytmInstruments(loader=lambda: (calls.append(1), MASTER)[1])
    return inst, calls


def test_equities_resolve_to_exchange_segment_and_security_id():
    inst, _ = instruments()
    assert (lambda i: (i.exchange, i.segment, i.security_id, i.symbol))(inst.resolve("NSE:SBIN-EQ")) == ("NSE", "E", "3045", "SBIN")
    assert inst.resolve("BSE:SBIN-EQ").security_id == "500112"
    assert inst.resolve("NSE:NIFTYBEES-EQ").security_id == "12345"


def test_first_series_wins_and_derivatives_and_indices_are_not_equities():
    assert len(parse_master(MASTER)) == 3          # SBIN NSE, SBIN BSE, NIFTYBEES (NSE); the index, the future and the duplicate are out
    inst, _ = instruments()
    assert inst.resolve("NSE:SBIN-EQ").security_id == "3045"


def test_the_master_is_downloaded_once():
    inst, calls = instruments()
    inst.resolve("NSE:SBIN-EQ"), inst.resolve("BSE:SBIN-EQ")
    assert len(calls) == 1
    inst.reload()
    assert len(calls) == 2


@pytest.mark.parametrize("symbol, message", [("NSE:NOPE-EQ", "security master"), ("MCX:SILVER", "no MCX"), ("NSE:SBIN24OCTFUT", "plain equity")])
def test_what_cannot_be_resolved_says_why(symbol, message):
    inst, _ = instruments()
    with pytest.raises(ValueError, match=message):
        inst.resolve(symbol)
