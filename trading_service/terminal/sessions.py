"""Broker login sessions: the day's tokens, kept in the database and applied to the running terminal without a restart.

Sources of a token, newest wins:
* `auto`   - the unattended morning login (broker_auth.auto), run by a scheduled job or the "Log in now" button;
* `manual` - someone logged in on the broker's page and pasted the URL it redirected to;
* `env`    - FLATTRADE_TOKEN / PAYTM_* in the environment (the older deploy.sh flattrade-login path still works).

A token is only ever replaced by a newer login; nothing here touches orders or positions.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from .db import Database, jdump

log = logging.getLogger(__name__)

BROKERS = ("flattrade", "paytm")
TOKEN_VARS = {"flattrade": ("FLATTRADE_TOKEN",), "paytm": ("PAYTM_ACCESS_TOKEN", "PAYTM_PUBLIC_ACCESS_TOKEN", "PAYTM_READ_ACCESS_TOKEN")}


class SessionStore:
    def __init__(self, db: Database):
        self.db = db

    def save(self, broker: str, tokens: Mapping[str, str], source: str) -> None:
        self.db.execute(
            """
            INSERT INTO terminal_broker_sessions (broker, tokens, source, obtained_at) VALUES (:b, CAST(:t AS JSONB), :s, now())
            ON CONFLICT (broker) DO UPDATE SET tokens = EXCLUDED.tokens, source = EXCLUDED.source, obtained_at = EXCLUDED.obtained_at
            """,
            b=broker, t=jdump(dict(tokens)), s=source,
        )

    def get(self, broker: str) -> Optional[dict]:
        row = self.db.row("SELECT tokens, source, obtained_at FROM terminal_broker_sessions WHERE broker = :b", b=broker)
        return dict(row) if row else None

    def env(self, base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        """The environment with stored tokens laid over it: what the broker clients and checks should use."""
        out = dict(os.environ if base is None else base)
        for broker in BROKERS:
            s = self.get(broker)
            if s:
                out.update({k: str(v) for k, v in (s["tokens"] or {}).items() if v})
        return out


class BrokerLogins:
    """Status and login for each broker. `apply(broker, tokens)` hands a new token to whatever is running."""

    def __init__(self, db: Database, apply: Optional[Callable[[str, dict], Any]] = None,
                 login: Optional[Callable[[str, Mapping[str, str]], dict]] = None,
                 check: Optional[Callable[[Mapping[str, str]], dict]] = None):
        self.store = SessionStore(db)
        self.apply = apply
        self._login = login
        self._check = check

    def status(self) -> list[dict]:
        from broker_auth.auto import auto_login_configured

        env = self.store.env()
        checks = (self._check or _check_all)(env)
        out = []
        for broker in BROKERS:
            s = self.store.get(broker)
            out.append({
                "broker": broker,
                "status": checks.get(broker, {}).get("status", "unknown"),
                "detail": checks.get(broker, {}).get("detail", ""),
                "auto_login": auto_login_configured(broker, env),
                "has_api_key": bool(env.get(f"{broker.upper()}_API_KEY")),
                "obtained_at": s["obtained_at"].isoformat() if s else None,
                "source": s["source"] if s else ("env" if any(env.get(v) for v in TOKEN_VARS[broker]) else None),
                "login_url": _login_url(broker, env),
            })
        return out

    async def login(self, broker: str, code: Optional[str] = None) -> dict:
        """Unattended login (no code) or a pasted code / redirect URL. Saves, applies, and reports."""
        import asyncio

        if broker not in BROKERS:
            raise ValueError(f"unknown broker {broker!r}")
        env = self.store.env()
        tokens = await asyncio.to_thread(self._login or _do_login, broker, env, code)
        self.store.save(broker, tokens, "manual" if code else "auto")
        if self.apply is not None:
            res = self.apply(broker, tokens)
            if asyncio.iscoroutine(res):
                await res
        return {"broker": broker, "source": "manual" if code else "auto", "obtained_at": datetime.now(timezone.utc).isoformat()}

    async def login_all(self) -> dict:
        """Every broker that has unattended login set up (the morning job). Failures are reported, not raised."""
        from broker_auth.auto import auto_login_configured

        env = self.store.env()
        results = {}
        for broker in BROKERS:
            if not auto_login_configured(broker, env):
                results[broker] = {"ok": None, "detail": "unattended login not set up"}
                continue
            try:
                await self.login(broker)
                results[broker] = {"ok": True, "detail": "logged in"}
            except Exception as e:  # noqa: BLE001 - one broker failing must not stop the next
                log.warning("auto login %s failed: %s", broker, e)
                results[broker] = {"ok": False, "detail": str(e)}
        return results


def _do_login(broker: str, env: Mapping[str, str], code: Optional[str]) -> dict:
    from broker_auth.auto import flattrade_login
    from broker_auth.cli import code_from
    from broker_auth.providers import PROVIDERS

    if code:
        p = PROVIDERS[broker]
        key, secret = env.get(f"{broker.upper()}_API_KEY"), env.get(f"{broker.upper()}_API_SECRET")
        if not key or not secret:
            raise ValueError(f"{broker.upper()}_API_KEY and {broker.upper()}_API_SECRET are not set on the server")
        return p.exchange(key, secret, code_from(code, p.code_params))
    if broker == "flattrade":
        return flattrade_login(env)
    raise ValueError(f"unattended login is not available for {broker}; use its login page and paste the redirect URL")


def _check_all(env: Mapping[str, str]) -> dict:
    from broker_auth.check import check_all

    return check_all(env)


def _login_url(broker: str, env: Mapping[str, str]) -> Optional[str]:
    key = env.get(f"{broker.upper()}_API_KEY")
    if not key:
        return None
    from broker_auth.providers import PROVIDERS

    return PROVIDERS[broker].login_url(key, "atlas")
