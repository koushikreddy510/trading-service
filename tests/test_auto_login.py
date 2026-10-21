"""Unattended Flattrade login: the two calls the login page makes, then the usual code-for-token exchange."""
import hashlib

import pytest

from broker_auth.auto import auto_login_configured, flattrade_code, flattrade_login, totp
from broker_auth.providers import AuthError

ENV = {"FLATTRADE_USER_ID": "FT1", "FLATTRADE_PASSWORD": "pw", "FLATTRADE_TOTP_KEY": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
       "FLATTRADE_API_KEY": "key", "FLATTRADE_API_SECRET": "secret"}


class Page:
    def __init__(self, reply=None):
        self.sent = None
        self.reply = reply or {"emsg": "", "RedirectURL": "http://127.0.0.1:5000/?code=abc-123&client=FT1"}

    def session(self):
        return "SID1"

    def ftauth(self, body):
        self.sent = body
        return self.reply


def test_totp_matches_the_rfc_vectors():
    assert totp(ENV["FLATTRADE_TOTP_KEY"], 59, digits=8) == "94287082"
    assert totp(ENV["FLATTRADE_TOTP_KEY"], 1111111109, digits=8) == "07081804"


def test_the_page_gets_the_hashed_password_the_current_totp_and_the_session():
    page = Page()
    assert flattrade_code(ENV, page, clock=lambda: 59) == "abc-123"
    assert page.sent["UserName"] == "FT1" and page.sent["Sid"] == "SID1" and page.sent["APIKey"] == "key"
    assert page.sent["Password"] == hashlib.sha256(b"pw").hexdigest() and page.sent["PAN_DOB"] == totp(ENV["FLATTRADE_TOTP_KEY"], 59)


def test_the_code_is_exchanged_for_the_days_token():
    posted = {}

    def post(url, body):
        posted.update(body)
        return {"stat": "Ok", "token": "TOK", "client": "FT1"}

    assert flattrade_login(ENV, Page(), post=post) == {"FLATTRADE_TOKEN": "TOK", "FLATTRADE_USER_ID": "FT1"}
    assert posted["request_code"] == "abc-123"


def test_a_refusal_says_why():
    with pytest.raises(AuthError, match="Invalid TOTP"):
        flattrade_code(ENV, Page({"emsg": "Invalid TOTP"}))


def test_missing_settings_are_named_and_unattended_login_counts_as_not_set_up():
    env = {k: v for k, v in ENV.items() if k != "FLATTRADE_TOTP_KEY"}
    with pytest.raises(AuthError, match="FLATTRADE_TOTP_KEY"):
        flattrade_code(env, Page())
    assert not auto_login_configured("flattrade", env) and auto_login_configured("flattrade", ENV)
    assert not auto_login_configured("paytm", ENV)
