import hashlib
import threading
import urllib.request

import pytest

from broker_auth import cli
from broker_auth.providers import PROVIDERS, AuthError, flattrade_exchange, paytm_exchange


def test_flattrade_sends_the_hashed_secret_and_returns_token_and_user():
    sent = {}

    def post(url, body):
        sent.update(url=url, body=body)
        return {"stat": "Ok", "token": "TOK", "client": "FZ1234"}

    out = flattrade_exchange("KEY", "SECRET", "CODE", post)
    assert out == {"FLATTRADE_TOKEN": "TOK", "FLATTRADE_USER_ID": "FZ1234"}
    assert sent["url"] == "https://authapi.flattrade.in/trade/apitoken"
    assert sent["body"] == {"api_key": "KEY", "request_code": "CODE", "api_secret": hashlib.sha256(b"KEYCODESECRET").hexdigest()}


def test_flattrade_rejection_is_an_error_with_the_brokers_message():
    with pytest.raises(AuthError, match="invalid code"):
        flattrade_exchange("K", "S", "C", lambda u, b: {"stat": "Not_Ok", "emsg": "invalid code"})


def test_paytm_returns_the_tokens_it_was_given_and_reports_errors():
    sent = {}
    out = paytm_exchange("K", "S", "RT", lambda u, b: (sent.update(b), {"access_token": "A", "public_access_token": "P"})[1])
    assert out == {"PAYTM_ACCESS_TOKEN": "A", "PAYTM_PUBLIC_ACCESS_TOKEN": "P"}
    assert sent == {"api_key": "K", "api_secret_key": "S", "request_token": "RT"}
    with pytest.raises(AuthError, match="bad secret"):
        paytm_exchange("K", "S", "RT", lambda u, b: {"errors": [{"message": "bad secret"}]})


def test_login_urls_carry_the_key():
    assert PROVIDERS["flattrade"].login_url("K1", "s") == "https://auth.flattrade.in/?app_key=K1"
    assert PROVIDERS["paytm"].login_url("K1", "s9") == "https://login.paytmmoney.com/merchant-login?apiKey=K1&state=s9"


def test_code_can_be_pasted_as_a_whole_redirect_url():
    assert cli.code_from("abc123", ("code",)) == "abc123"
    assert cli.code_from("http://127.0.0.1:5000/?code=abc&client=FZ1", ("code",)) == "abc"
    assert cli.code_from("http://127.0.0.1:5000/?requestToken=rt1&state=x", ("requestToken",)) == "rt1"
    with pytest.raises(AuthError):
        cli.code_from("http://127.0.0.1:5000/?foo=1", ("code",))


def test_the_local_server_catches_the_redirect_even_after_a_stray_request():
    server = cli.CallbackServer(0)
    port = server.server_port

    def browser():
        urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico").read()
        urllib.request.urlopen(f"http://127.0.0.1:{port}/?code=XYZ&client=FZ1").read()

    threading.Thread(target=browser).start()
    assert server.wait(timeout=10) == {"code": "XYZ", "client": "FZ1"}
    server.server_close()


def test_env_file_keys_are_replaced_not_duplicated(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PGUSER=x\nFLATTRADE_TOKEN=old\n")
    cli.update_env_file(env, {"FLATTRADE_TOKEN": "new", "FLATTRADE_USER_ID": "FZ1"})
    assert env.read_text() == "PGUSER=x\nFLATTRADE_TOKEN=new\nFLATTRADE_USER_ID=FZ1\n"


def test_saved_token_file_is_private(tmp_path):
    path = cli.save("flattrade", {"FLATTRADE_TOKEN": "T"}, tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600 and '"FLATTRADE_TOKEN": "T"' in path.read_text()


def test_flattrade_accepts_every_name_its_redirect_has_been_seen_with():
    names = PROVIDERS["flattrade"].code_params
    assert [cli.pick({n: "C"}, names) for n in ("code", "request_code", "request_token")] == ["C", "C", "C"]


# --- the daily token check ------------------------------------------------------
from broker_auth import check  # noqa: E402


def fake_http(table):
    return lambda method, url, **kw: next(v for k, v in table.items() if k in url)


def test_each_broker_is_valid_rejected_unknown_or_not_in_use():
    env = {"FLATTRADE_USER_ID": "FZ1", "FLATTRADE_TOKEN": "T", "PAYTM_ACCESS_TOKEN": "P", "STRATEGY_SCREENER_URL": "http://s:8000"}
    http = fake_http({
        "flattrade": (200, {"stat": "Not_Ok", "emsg": "Session Expired : Invalid Session Key"}),
        "paytmmoney": (200, {}),
        "fyers-token": (200, {"valid": False, "error": "Token file is empty"}),
    })
    r = check.check_all(env, http)
    assert r["flattrade"] == {"status": "rejected", "detail": "Session Expired : Invalid Session Key"}
    assert r["paytm"]["status"] == "valid"
    assert r["fyers"] == {"status": "rejected", "detail": "Token file is empty"}
    assert check.check_all({}, http) == {b: {"status": "not_configured", "detail": ""} for b in ("flattrade", "paytm", "fyers")}


def test_a_network_error_is_unknown_not_rejected_and_does_not_hide_other_brokers():
    def http(method, url, **kw):
        if "flattrade" in url:
            raise ConnectionError("no route")
        return 200, {}

    r = check.check_all({"FLATTRADE_TOKEN": "T", "PAYTM_ACCESS_TOKEN": "P"}, http)
    assert r["flattrade"]["status"] == "unknown" and "no route" in r["flattrade"]["detail"]
    assert r["paytm"]["status"] == "valid"


def test_slack_text_names_the_command_only_when_a_person_is_needed():
    ok = {b: {"status": "valid", "detail": ""} for b in ("flattrade", "paytm", "fyers")}
    assert check.alert_text(ok) is None
    bad = {**ok, "flattrade": {"status": "rejected", "detail": "expired"}, "fyers": {"status": "rejected", "detail": "x"}}
    text = check.alert_text(bad)
    assert "Broker logins" in text and "refreshes it automatically" in text and "paytm" not in text


def test_the_token_check_sends_flattrade_raw_json_not_url_encoded():
    from broker_auth.check import check_flattrade
    seen = {}
    def http(method, url, **kw):
        seen.update(kw)
        return 200, {"stat": "Ok"}
    assert check_flattrade({"FLATTRADE_USER_ID": "FT1", "FLATTRADE_TOKEN": "tok"}, http) == ("valid", "")
    assert seen["data"] == b'jData={"uid":"FT1"}&jKey=tok'
