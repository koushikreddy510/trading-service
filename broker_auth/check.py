"""Is each broker's token for today still accepted? One cheap authenticated call per broker.

Used by the scheduler's morning check (POST /terminal/notify/broker-tokens, which tells Slack what to do) and by
`python -m broker_auth status`. A broker whose token variable is not set is simply not in use ("not_configured").
Statuses: valid | rejected (the broker said no) | unknown (we could not reach it) | not_configured.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Mapping

Http = Callable[..., tuple[int, Any]]   # (method, url, **requests kwargs) -> (status code, parsed JSON or None)

LOGIN = {"flattrade": "python -m broker_auth flattrade --env-file deploy/.env", "paytm": "python -m broker_auth paytm --env-file deploy/.env"}


def _http(method: str, url: str, **kw) -> tuple[int, Any]:
    import requests

    r = requests.request(method, url, timeout=15, **kw)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def check_flattrade(env: Mapping[str, str], http: Http) -> tuple[str, str]:
    user, token = env.get("FLATTRADE_USER_ID", ""), env.get("FLATTRADE_TOKEN", "")
    if not token:
        return "not_configured", ""
    code, body = http("POST", "https://piconnect.flattrade.in/PiConnectAPI/UserDetails", data={"jData": json.dumps({"uid": user}), "jKey": token})
    if isinstance(body, dict) and str(body.get("stat", "")).lower() == "ok":
        return "valid", ""
    if isinstance(body, dict) and body.get("emsg"):
        return "rejected", str(body["emsg"])
    return "unknown", f"HTTP {code}"


def check_paytm(env: Mapping[str, str], http: Http) -> tuple[str, str]:
    token = env.get("PAYTM_READ_ACCESS_TOKEN") or env.get("PAYTM_ACCESS_TOKEN", "")
    if not token:
        return "not_configured", ""
    code, _ = http("GET", "https://developer.paytmmoney.com/accounts/v1/user/details", headers={"x-jwt-token": token})
    if code == 200:
        return "valid", ""
    return ("rejected", f"HTTP {code}") if code in (401, 403) else ("unknown", f"HTTP {code}")


def check_fyers(env: Mapping[str, str], http: Http) -> tuple[str, str]:
    """The strategy-screener owns the Fyers token file and can log in by itself; ask it."""
    base = env.get("STRATEGY_SCREENER_URL", "").rstrip("/")
    if not base:
        return "not_configured", ""
    code, body = http("GET", f"{base}/admin/fyers-token/validate")
    if isinstance(body, dict) and "valid" in body:
        return ("valid", "") if body["valid"] else ("rejected", str(body.get("error", "")))
    return "unknown", f"HTTP {code}"


CHECKS = {"flattrade": check_flattrade, "paytm": check_paytm, "fyers": check_fyers}


def check_all(env: Mapping[str, str] | None = None, http: Http | None = None) -> dict[str, dict[str, str]]:
    env, http = os.environ if env is None else env, http or _http
    out = {}
    for name, fn in CHECKS.items():
        try:
            status, detail = fn(env, http)
        except Exception as e:  # noqa: BLE001 - a network error must not hide the other brokers
            status, detail = "unknown", f"{type(e).__name__}: {e}"
        out[name] = {"status": status, "detail": detail}
    return out


def alert_text(results: dict[str, dict[str, str]]) -> str | None:
    """The Slack message, or None when nothing needs a person."""
    lines = []
    for name, r in results.items():
        if r["status"] in ("valid", "not_configured"):
            continue
        why = f" ({r['detail']})" if r["detail"] else ""
        if r["status"] == "unknown":
            lines.append(f"• *{name}*: could not check{why}")
        elif name in LOGIN:
            lines.append(f"• *{name}*: today's token is not accepted{why}. Log in: `{LOGIN[name]}` then restart the service (`deploy.sh trading`)")
        else:
            lines.append(f"• *{name}*: token is not valid{why}. The scheduler refreshes it automatically at 08:00; if this persists check FYERS_PIN / FYERS_TOTP_KEY")
    return ("⚠️ *Broker logins need attention before the market opens*\n" + "\n".join(lines)) if lines else None
