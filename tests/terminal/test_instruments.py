"""MCX futures: the contract list, search, point value (P&L per lot), broker daily bars, and the MCX session."""
from datetime import date

import pytest

from tests.terminal.helpers import ist
from trading_service.terminal import instruments, symbols
from trading_service.terminal import timeframes as tfm
from trading_service.terminal.candles import Candle
from trading_service.terminal.feed.flattrade import parse_eod
from trading_service.terminal.instruments import Instruments
from trading_service.terminal.market import CandleService
from trading_service.terminal.notify import Notifier
from trading_service.terminal.paper import PaperBook
from trading_service.terminal.rules import Signal, SignalRepo

TODAY = date(2026, 10, 21)
SCRIP_MASTER = """Exchange,Token,Lotsize,Symbol,Tradingsymbol,Instrument,Expiry,Strike,Optiontype
MCX,562058,1,SILVERMIC,SILVERMIC30NOV26,FUTCOM,30-NOV-2026,0.00,XX
MCX,574843,1,SILVERMIC,SILVERMIC26FEB27,FUTCOM,26-FEB-2027,0.00,XX
MCX,562059,5,SILVERM,SILVERM30NOV26,FUTCOM,30-NOV-2026,0.00,XX
MCX,550001,100,GOLDM,GOLDM05NOV26,FUTCOM,05-NOV-2026,0.00,XX
MCX,540001,1,SILVERMIC,SILVERMIC31AUG26,FUTCOM,31-AUG-2026,0.00,XX
MCX,599999,1,SILVERMIC,SILVERMIC30NOV26P200000,OPTFUT,23-NOV-2026,200000.00,PE
MCX,560001,1,MCXBULLDEX,MCXBULLDEX24NOV26,FUTIDX,24-NOV-2026,0.00,XX
"""


@pytest.fixture
def loaded(db):
    return instruments.refresh(db, fetch=lambda url: SCRIP_MASTER, today=TODAY)


class TestContractList:
    def test_futures_that_have_not_expired_are_loaded_options_are_not(self, db, loaded):
        got = {r["symbol"] for r in db.rows("SELECT symbol FROM terminal_instruments")}
        assert got == {"MCX:SILVERMIC30NOV26", "MCX:SILVERMIC26FEB27", "MCX:SILVERM30NOV26", "MCX:GOLDM05NOV26", "MCX:MCXBULLDEX24NOV26"}
        assert loaded["contracts"] == 5

    def test_the_feed_gets_the_token_without_a_broker_lookup(self, db, loaded):
        assert db.row("SELECT exchange, token FROM terminal_broker_tokens WHERE symbol = 'MCX:SILVERM30NOV26'") == {"exchange": "MCX", "token": "562059"}

    def test_a_contract_that_has_expired_since_the_last_refresh_is_removed(self, db, loaded):
        later = SCRIP_MASTER.replace("MCX,550001,100,GOLDM,GOLDM05NOV26,FUTCOM,05-NOV-2026,0.00,XX\n", "")
        res = instruments.refresh(db, fetch=lambda url: later + "MCX,1,100,GOLDM,GOLDM04DEC26,FUTCOM,04-DEC-2026,0.00,XX\n", today=date(2026, 11, 6))
        names = {r["symbol"] for r in db.rows("SELECT symbol FROM terminal_instruments")}
        assert "MCX:GOLDM05NOV26" not in names and "MCX:GOLDM04DEC26" in names and res["expired_removed"] == 1

    def test_an_empty_download_keeps_the_current_list(self, db, loaded):
        with pytest.raises(RuntimeError):
            instruments.refresh(db, fetch=lambda url: "Exchange,Token\n", today=TODAY)
        assert db.scalar("SELECT count(*) FROM terminal_instruments") == 5


class TestSearch:
    def test_the_exact_underlying_comes_first_nearest_expiry_first(self, db, loaded):
        found = symbols.search(db, "silvermic")
        assert [r["symbol"] for r in found] == ["MCX:SILVERMIC30NOV26", "MCX:SILVERMIC26FEB27"]
        assert found[0] == {"symbol": "MCX:SILVERMIC30NOV26", "exchange": "MCX", "name": "SILVERMIC 30 Nov 2026 · lot 1", "sector": "Commodity future"}

    def test_a_shorter_name_lists_its_own_contracts_then_stocks_then_longer_names(self, db, loaded):
        db.execute("INSERT INTO symbols (tradingsymbol, name, sector) VALUES ('NSE:SILVERTUC-EQ', 'Silver Touch Technologies', 'IT')")
        assert [r["symbol"] for r in symbols.search(db, "SILVERM")] == ["MCX:SILVERM30NOV26", "MCX:SILVERMIC30NOV26", "MCX:SILVERMIC26FEB27"]
        assert [r["symbol"] for r in symbols.search(db, "silver")][0] == "NSE:SILVERTUC-EQ"

    def test_stock_search_is_unchanged(self, db, loaded):
        db.execute("INSERT INTO symbols (tradingsymbol, name, sector) VALUES ('NSE:SBIN-EQ', 'State Bank of India', 'Banks')")
        assert [r["symbol"] for r in symbols.search(db, "sbin")] == ["NSE:SBIN-EQ"]


class TestPointValue:
    def test_stocks_are_one_rupee_per_point_per_share(self, db, loaded):
        assert Instruments(db).point_value("NSE:SBIN-EQ") == 1.0

    def test_a_lot_is_lot_size_times_the_price_factor(self, db, loaded):
        inst = Instruments(db)
        inst.set_price_factor("MCX:GOLDM05NOV26", 0.1)      # quoted per 10 g, traded in grams
        inst.set_price_factor("MCX:SILVERM30NOV26", 1.0)
        assert inst.point_value("MCX:GOLDM05NOV26") == pytest.approx(10.0)
        assert inst.point_value("MCX:SILVERM30NOV26") == 5.0

    def test_until_the_price_factor_is_known_the_lot_size_is_used(self, db, loaded):
        inst = Instruments(db)
        assert inst.needs_price_factor("MCX:SILVERM30NOV26") and inst.point_value("MCX:SILVERM30NOV26") == 5.0
        assert not inst.needs_price_factor("NSE:SBIN-EQ")

    def test_paper_pnl_uses_the_point_value(self, db, loaded):
        inst = Instruments(db)
        inst.set_price_factor("MCX:SILVERM30NOV26", 1.0)
        sig = Signal("MCX:SILVERM30NOV26", "15m", "BUY", ist(2026, 10, 19, 10, 15), 211000.0, "open", stop=210000.0, target=213000.0)
        SignalRepo(db).insert_if_new(sig)
        book = PaperBook(db, inst.point_value)
        p = book.open_from_signal(sig, quantity=2)                       # 2 lots of 5 kg
        assert p.multiplier == 5.0 and p.unrealized(211100.0) == pytest.approx(1000.0)
        (closed,) = book.on_tick("MCX:SILVERM30NOV26", 213000.0, ist(2026, 10, 19, 11, 0))
        assert closed.realized_pnl == pytest.approx(20000.0)             # 2000 points x 2 lots x 5
        assert PaperBook(db, inst.point_value).reload() is None and db.scalar("SELECT multiplier FROM terminal_paper_positions") == 5.0

    def test_the_slack_exit_percentage_is_on_the_contract_value(self):
        sent = []
        Notifier(sender=lambda t, **kw: sent.append(t), background=False).send(Notifier._exit_text(
            {"side": "BUY", "quantity": 2, "symbol": "MCX:SILVERM30NOV26", "entry_price": 200000.0, "exit_price": 202000.0,
             "realized_pnl": 20000.0, "exit_reason": "target", "multiplier": 5.0}))
        assert "P&L +20,000.00 (+1.00%)" in sent[0]


class TestBrokerDailyBars:
    def test_eod_rows_become_daily_bars_at_the_mcx_open(self):
        rows = ['{"time":"20-OCT-2026", "into":"212420.00", "inth":"215960.00", "intl":"210541.00", "intc":"213901.00", "intv":"76330.00"}',
                '{"time":"19-OCT-2026", "into":"214049.00", "inth":"215500.00", "intl":"211790.00", "intc":"212443.00", "intv":"63100.00"}']
        (d1, b1), (d2, b2) = parse_eod(rows, "MCX")
        assert (d1, d2) == (date(2026, 10, 19), date(2026, 10, 20))
        assert b2.time == ist(2026, 10, 20, 9, 0) and b2.close == 213901.0 and b2.volume == 76330

    def test_backfill_fetches_daily_bars_and_the_price_factor_once_a_day(self, db, loaded):
        class History:
            calls = []

            def fetch_1m(self, symbol, start, end):
                return []

            def fetch_1d(self, symbol, start, end):
                self.calls.append((start, end))
                return [(date(2026, 10, 19), Candle(ist(2026, 10, 19, 9, 0), 1, 2, 0.5, 1.5, 10)),
                        (date(2026, 10, 20), Candle(ist(2026, 10, 20, 9, 0), 1.5, 3, 1, 2.5, 20))]

            def ensure_spec(self, symbol):
                Instruments(db).set_price_factor(symbol, 1.0)

        h = History()
        service = CandleService(db, h, clock=lambda: ist(2026, 10, 21, 11, 0))
        service.backfill("MCX:SILVERMIC30NOV26")
        service.backfill("MCX:SILVERMIC30NOV26")
        assert len(h.calls) == 1 and h.calls[0][1] == TODAY
        assert [b.close for b in service.candles("MCX:SILVERMIC30NOV26", "1D")] == [1.5, 2.5]
        assert not Instruments(db).needs_price_factor("MCX:SILVERMIC30NOV26")

    def test_stocks_do_not_ask_the_broker_for_daily_bars(self, db):
        class History:
            def fetch_1m(self, symbol, start, end):
                return []

            def fetch_1d(self, symbol, start, end):
                raise AssertionError("stocks use the data-store's daily bars")

        CandleService(db, History(), clock=lambda: ist(2026, 10, 21, 11, 0)).backfill("NSE:SBIN-EQ")


class TestMcxSession:
    def test_mcx_closes_at_23_30_while_the_us_is_on_summer_time(self):
        assert tfm.session_close(date(2026, 10, 30), "MCX") == ist(2026, 10, 30, 23, 30)

    def test_and_at_23_55_from_the_us_clock_change(self):
        assert tfm.session_close(date(2026, 11, 2), "MCX") == ist(2026, 11, 2, 23, 55)
        assert tfm.in_session(ist(2026, 11, 2, 23, 45), "MCX") and not tfm.in_session(ist(2026, 10, 30, 23, 45), "MCX")

    def test_nse_is_unaffected(self):
        assert tfm.session_close(date(2026, 11, 2), "NSE") == ist(2026, 11, 2, 15, 30)


class TestIndices:
    def test_indices_are_seeded_with_their_flattrade_name_and_token(self, db):
        assert instruments.seed_indices(db) == len(instruments.INDICES)
        assert db.row("SELECT exchange, token, tsym FROM terminal_broker_tokens WHERE symbol = 'NSE:NIFTYIT-INDEX'") == {"exchange": "NSE", "token": "26008", "tsym": "Nifty IT"}
        assert db.row("SELECT exchange, token FROM terminal_broker_tokens WHERE symbol = 'BSE:SENSEX-INDEX'") == {"exchange": "BSE", "token": "1"}

    def test_search_finds_indices_by_name_ahead_of_stocks(self, db):
        instruments.seed_indices(db)
        db.execute("INSERT INTO symbols (tradingsymbol, name, sector) VALUES ('NSE:NIFTYBEES-EQ', 'Nippon Nifty BeES', 'Mutual Fund Scheme')")
        assert symbols.search(db, "nifty it")[0] == {"symbol": "NSE:NIFTYIT-INDEX", "exchange": "NSE", "name": "NIFTY IT", "sector": "Index"}
        assert symbols.search(db, "sensex")[0]["symbol"] == "BSE:SENSEX-INDEX"
        found = [r["symbol"] for r in symbols.search(db, "nifty", limit=50)]
        assert found.index("NSE:NIFTY50-INDEX") < found.index("NSE:NIFTYBEES-EQ")
        assert symbols.search(db, "auto")[0]["symbol"] == "NSE:NIFTYAUTO-INDEX"      # by the sector word alone

    def test_a_refresh_of_mcx_contracts_keeps_the_indices(self, db, loaded):
        instruments.seed_indices(db)
        instruments.refresh(db, fetch=lambda url: SCRIP_MASTER, today=date(2026, 12, 1))
        assert db.scalar("SELECT count(*) FROM terminal_instruments WHERE instrument = 'INDEX'") == len(instruments.INDICES)

    def test_index_daily_bars_come_from_the_broker_by_its_own_name(self, db):
        from trading_service.terminal.feed.flattrade import FlattradeHistory, TokenResolver
        from trading_service.terminal.market import uses_broker_daily
        instruments.seed_indices(db)
        asked = []

        class Api:
            def eod_chart(self, sym, a, b):
                asked.append(sym)
                return []

        FlattradeHistory(Api(), TokenResolver(Api(), db)).fetch_1d("NSE:NIFTYIT-INDEX", date(2026, 1, 1), TODAY)
        assert asked == ["NSE:Nifty IT"]
        assert uses_broker_daily("NSE:NIFTYIT-INDEX") and uses_broker_daily("MCX:SILVERM30NOV26") and not uses_broker_daily("NSE:SBIN-EQ")

    def test_a_terminal_makes_the_indices_watchlist_once(self, db):
        from trading_service.terminal.feed.mock import MockFeed, MockHistory, SimClock
        from trading_service.terminal.runtime import Terminal
        clock = SimClock(ist(2026, 10, 19, 10, 0, 30), speed=0)
        t = Terminal(db, MockFeed(clock), MockHistory(), clock.now, mode="test", hub_interval=0)
        t._seed_indices()
        (wl,) = [w for w in t.watchlists.list() if w["name"] == "Indices"]
        assert wl["symbols"][:3] == ["NSE:NIFTY50-INDEX", "BSE:SENSEX-INDEX", "NSE:NIFTYBANK-INDEX"]
        t.watchlists.delete(wl["id"])
        t._seed_indices()                                  # the next start
        assert not [w for w in t.watchlists.list() if w["name"] == "Indices"]


class TestSectorIndex:
    def test_a_stock_is_judged_against_its_sector_index(self):
        from trading_service.terminal.sector_groups import sector_index
        assert sector_index("Private Sector Bank") == "NSE:NIFTYPVTBANK-INDEX"
        assert sector_index("Computers - Software & Consulting") == "NSE:NIFTYIT-INDEX"
        assert sector_index("Commercial Vehicles") == "NSE:NIFTYAUTO-INDEX"
        assert sector_index("Residential Commercial Projects") == "NSE:NIFTYREALTY-INDEX"
        assert sector_index("") == sector_index("Something new") == "NSE:NIFTY50-INDEX"

    def test_every_mapped_index_exists(self):
        from trading_service.terminal.sector_groups import GROUP_INDEX, INDUSTRY_INDEX
        ids = {i[0] for i in instruments.INDICES}
        assert set(GROUP_INDEX.values()) | set(INDUSTRY_INDEX.values()) <= ids
