"""Unattended daily login: what a person does on the broker's login page, done by the server each morning.

Flattrade: auth.flattrade.in is a page over authapi.flattrade.in. We open a session (/auth/session), send user id,
sha256(password) and the current TOTP code (/ftauth), and get back the redirect URL with the one-time `code`, which
providers.flattrade_exchange turns into the day's token exactly as a browser login would.

Needs, in the environment: FLATTRADE_USER_ID, FLATTRADE_PASSWORD, FLATTRADE_TOTP_KEY (the base32 secret shown when
TOTP was set up, not a 6-digit code), FLATTRADE_API_KEY, FLATTRADE_API_SECRET.
The user chose unattended logins knowing brokers expect a daily manual login; a captcha or a changed page breaks this,
and the morning check (broker_auth.check) then tells Slack to log in by hand.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qs, urlparse

from .providers import AuthError, flattrade_exchange

AUTH_API = "https://authapi.flattrade.in"
PAGE_HEADERS = {"Referer": "https://auth.flattrade.in/", "Origin": "https://auth.flattrade.in", "User-Agent": "Mozilla/5.0"}
FLATTRADE_AUTO_VARS = ("FLATTRADE_USER_ID", "FLATTRADE_PASSWORD", "FLATTRADE_TOTP_KEY", "FLATTRADE_API_KEY", "FLATTRADE_API_SECRET")


def totp(secret: str, at: Optional[float] = None, digits: int = 6, step: int = 30) -> str:
    """RFC 6238 code for a base32 secret (what an authenticator app shows)."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return str((struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def auto_login_configured(broker: str, env: Mapping[str, str]) -> bool:
    return broker == "flattrade" and all(env.get(v) for v in FLATTRADE_AUTO_VARS)


class _Http:
    """The two calls the login page makes, on one cookie session. Tests replace it."""

    def __init__(self):
        import requests

        self.s = requests.Session()

    def session(self) -> str:
        r = self.s.post(f"{AUTH_API}/auth/session", headers=PAGE_HEADERS, timeout=20)
        if r.status_code != 200 or not r.text.strip():
            raise AuthError(f"Flattrade login page: HTTP {r.status_code}")
        return r.text.strip()

    def ftauth(self, body: dict) -> dict:
        r = self.s.post(f"{AUTH_API}/ftauth", json=body, headers=PAGE_HEADERS, timeout=20)
        try:
            return r.json()
        except ValueError:
            raise AuthError(f"Flattrade login: HTTP {r.status_code}, not JSON (a captcha or a changed page?)") from None


def flattrade_code(env: Mapping[str, str], http: Any = None, clock: Callable[[], float] = time.time) -> str:
    """Log in like the page does and return the one-time request code."""
    missing = [v for v in FLATTRADE_AUTO_VARS if not env.get(v)]
    if missing:
        raise AuthError(f"unattended Flattrade login needs {', '.join(missing)}")
    http = http or _Http()
    sid = http.session()
    out = http.ftauth({
        "UserName": env["FLATTRADE_USER_ID"], "Password": hashlib.sha256(env["FLATTRADE_PASSWORD"].encode()).hexdigest(),
        "PAN_DOB": totp(env["FLATTRADE_TOTP_KEY"], clock()), "App": "", "ClientID": "", "Key": "",
        "APIKey": env["FLATTRADE_API_KEY"], "Sid": sid, "Override": "Y", "Source": "AUTHPAGE",
    })
    if out.get("emsg"):
        raise AuthError(f"Flattrade refused the login: {out['emsg']}")
    redirect = out.get("RedirectURL") or ""
    code = (parse_qs(urlparse(redirect).query).get("code") or [""])[0]
    if not code:
        raise AuthError(f"Flattrade login gave no code: {out}")
    return code


def flattrade_login(env: Mapping[str, str], http: Any = None, post: Any = None, clock: Callable[[], float] = time.time) -> dict[str, str]:
    """The day's token, with no browser: {"FLATTRADE_TOKEN": ..., "FLATTRADE_USER_ID": ...}."""
    code = flattrade_code(env, http, clock)
    kwargs = {"post": post} if post else {}
    return flattrade_exchange(env["FLATTRADE_API_KEY"], env["FLATTRADE_API_SECRET"], code, **kwargs)
