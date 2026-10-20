"""How each broker turns a one-time login into a day's API token. One entry per broker; add Fyers/others here.

Every flow is the same shape: open `login_url` in a browser -> broker redirects to your registered redirect URL with a
one-time code (`code_params`) -> `exchange` trades key + secret + code for tokens. Tokens expire daily, so this is a daily step.
Endpoints are from the brokers' own SDKs/docs (links in README.md), not exercised against live accounts from here.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlencode

Post = Callable[[str, dict], dict]   # (url, json body) -> parsed JSON; tests replace it


class AuthError(Exception):
    pass


def _post(url: str, body: dict) -> dict:
    import requests

    r = requests.post(url, json=body, timeout=30)
    try:
        return r.json()
    except ValueError:
        raise AuthError(f"{url}: HTTP {r.status_code}, not JSON") from None


def flattrade_login_url(key: str, state: str) -> str:
    return "https://auth.flattrade.in/?" + urlencode({"app_key": key})     # Flattrade does not take a state


def flattrade_exchange(key: str, secret: str, code: str, post: Post = _post) -> dict[str, str]:
    # api_secret on the wire is sha256(api_key + request_code + api_secret), not the secret itself
    body = {"api_key": key, "request_code": code, "api_secret": hashlib.sha256(f"{key}{code}{secret}".encode()).hexdigest()}
    out = post("https://authapi.flattrade.in/trade/apitoken", body)
    if str(out.get("stat", "")).lower() != "ok" or not out.get("token"):
        raise AuthError(f"Flattrade refused the login: {out.get('emsg') or out}")
    return {"FLATTRADE_TOKEN": out["token"], "FLATTRADE_USER_ID": str(out.get("client", ""))}


def paytm_login_url(key: str, state: str) -> str:
    return "https://login.paytmmoney.com/merchant-login?" + urlencode({"apiKey": key, "state": state})


def paytm_exchange(key: str, secret: str, code: str, post: Post = _post) -> dict[str, str]:
    out = post("https://developer.paytmmoney.com/accounts/v2/gettoken", {"api_key": key, "api_secret_key": secret, "request_token": code})
    if not out.get("access_token"):
        errors = "; ".join(e.get("message", "") for e in out.get("errors", []) if isinstance(e, dict))
        raise AuthError(f"Paytm Money refused the login: {errors or out}")
    # access = place orders; public = live-price websocket; read = read-only calls
    return {f"PAYTM_{k.upper()}": out[k] for k in ("access_token", "public_access_token", "read_access_token") if out.get(k)}


@dataclass(frozen=True)
class Provider:
    name: str
    code_params: tuple[str, ...]                    # query parameter(s) the redirect may carry the one-time code in
    login_url: Callable[[str, str], str]
    exchange: Callable[..., dict[str, str]]


PROVIDERS = {
    "flattrade": Provider("flattrade", ("code", "request_code", "request_token"), flattrade_login_url, flattrade_exchange),
    "paytm": Provider("paytm", ("requestToken", "request_token"), paytm_login_url, paytm_exchange),
}
