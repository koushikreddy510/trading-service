"""Watchlists: named, ordered lists of symbols."""
import pytest

from trading_service.terminal.watchlists import MAX_ITEMS, WatchlistError, WatchlistRepo, normalise_symbol


@pytest.fixture
def repo(db):
    return WatchlistRepo(db)


class TestSymbols:
    def test_symbols_are_normalised_to_upper_case(self):
        assert normalise_symbol(" nse:sbin-eq ") == "NSE:SBIN-EQ"

    @pytest.mark.parametrize("bad", ["", "SBIN", "NSE:", ":SBIN-EQ", "NSE:SB IN", None, "NSE:SBIN;DROP TABLE x"])
    def test_malformed_symbols_are_rejected(self, bad):
        with pytest.raises(WatchlistError):
            normalise_symbol(bad)

    def test_special_characters_that_real_symbols_use_are_allowed(self):
        assert normalise_symbol("NSE:M&M-EQ") == "NSE:M&M-EQ"
        assert normalise_symbol("MCX:SILVERM25DECFUT") == "MCX:SILVERM25DECFUT"


class TestLists:
    def test_a_new_install_gets_a_default_watchlist_once(self, repo):
        first = repo.ensure_default()
        assert first["name"] == "Watchlist" and first["symbols"] == []
        assert repo.ensure_default()["id"] == first["id"]
        assert len(repo.list()) == 1

    def test_starter_symbols_are_added_only_when_the_default_list_is_first_created(self, repo):
        first = repo.ensure_default(["NSE:SBIN-EQ", "NSE:TCS-EQ"])
        assert first["symbols"] == ["NSE:SBIN-EQ", "NSE:TCS-EQ"]
        repo.remove(first["id"], "NSE:TCS-EQ")
        assert repo.ensure_default(["NSE:SBIN-EQ", "NSE:TCS-EQ"])["symbols"] == ["NSE:SBIN-EQ"]  # a restart does not bring it back

    def test_create_with_initial_symbols(self, repo):
        wl = repo.create("Banks", ["nse:sbin-eq", "NSE:HDFCBANK-EQ"])
        assert wl["symbols"] == ["NSE:SBIN-EQ", "NSE:HDFCBANK-EQ"]

    def test_names_must_be_present_and_unique_ignoring_case(self, repo):
        repo.create("Banks")
        with pytest.raises(WatchlistError, match="already exists"):
            repo.create("banks")
        with pytest.raises(WatchlistError, match="required"):
            repo.create("   ")

    def test_lists_come_back_in_creation_order(self, repo):
        for n in ["B", "A", "C"]:
            repo.create(n)
        assert [w["name"] for w in repo.list()] == ["B", "A", "C"]

    def test_rename_refuses_a_name_in_use_but_allows_a_case_change(self, repo):
        a, b = repo.create("A"), repo.create("B")
        with pytest.raises(WatchlistError):
            repo.rename(b["id"], "a")
        assert repo.rename(a["id"], "a") is True
        assert repo.get(a["id"])["name"] == "a"

    def test_delete_removes_the_list_and_its_items(self, repo, db):
        wl = repo.create("Tmp", ["NSE:SBIN-EQ"])
        assert repo.delete(wl["id"]) is True
        assert repo.get(wl["id"]) is None
        assert db.scalar("SELECT count(*) FROM terminal_watchlist_items") == 0
        assert repo.delete(wl["id"]) is False


class TestItems:
    def test_add_skips_duplicates_and_reports_what_was_added(self, repo):
        wl = repo.create("W", ["NSE:SBIN-EQ"])
        assert repo.add(wl["id"], ["NSE:SBIN-EQ", "NSE:TCS-EQ", "nse:tcs-eq"]) == ["NSE:TCS-EQ"]
        assert repo.get(wl["id"])["symbols"] == ["NSE:SBIN-EQ", "NSE:TCS-EQ"]

    def test_remove(self, repo):
        wl = repo.create("W", ["NSE:SBIN-EQ", "NSE:TCS-EQ"])
        assert repo.remove(wl["id"], "NSE:SBIN-EQ") is True
        assert repo.remove(wl["id"], "NSE:SBIN-EQ") is False
        assert repo.get(wl["id"])["symbols"] == ["NSE:TCS-EQ"]

    def test_reorder_puts_listed_symbols_first_and_keeps_the_rest(self, repo):
        wl = repo.create("W", ["NSE:A-EQ", "NSE:B-EQ", "NSE:C-EQ", "NSE:D-EQ"])
        order = repo.reorder(wl["id"], ["NSE:C-EQ", "NSE:A-EQ", "NSE:NOPE-EQ"])
        assert order == ["NSE:C-EQ", "NSE:A-EQ", "NSE:B-EQ", "NSE:D-EQ"]
        assert repo.get(wl["id"])["symbols"] == order

    def test_adding_to_a_missing_list_is_an_error(self, repo):
        with pytest.raises(WatchlistError, match="not found"):
            repo.add("nope", ["NSE:SBIN-EQ"])

    def test_a_list_has_a_size_limit(self, repo):
        wl = repo.create("Big")
        repo.add(wl["id"], [f"NSE:S{i}-EQ" for i in range(MAX_ITEMS)])
        with pytest.raises(WatchlistError, match="at most"):
            repo.add(wl["id"], ["NSE:ONE-MORE-EQ"])

    def test_all_symbols_unions_every_list_for_feed_subscription(self, repo):
        repo.create("One", ["NSE:A-EQ", "NSE:B-EQ"])
        repo.create("Two", ["NSE:B-EQ", "NSE:C-EQ"])
        assert repo.all_symbols() == {"NSE:A-EQ", "NSE:B-EQ", "NSE:C-EQ"}

    def test_users_cannot_see_each_others_lists(self, db):
        WatchlistRepo(db, "alice").create("Mine", ["NSE:A-EQ"])
        assert WatchlistRepo(db, "bob").list() == []
