"""python -m broker_auth <flattrade|paytm|all|status> [--code CODE_OR_REDIRECT_URL] [--port 5000] [--env-file PATH]

Reads <BROKER>_API_KEY and <BROKER>_API_SECRET from the environment (or asks), opens the broker's login page, catches the
redirect on http://127.0.0.1:<port>/ (register exactly that as the app's redirect URL), exchanges the code for tokens and
saves them to ~/.broker_tokens/<broker>.json (and into --env-file as KEY=value lines, which docker compose / deploy.sh read).
On a server with no browser, log in on your laptop and pass the whole URL it redirected to as --code.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .providers import PROVIDERS, AuthError, Provider


class CallbackServer(HTTPServer):
    """Waits for one browser redirect and keeps its query parameters."""

    def __init__(self, port: int):
        super().__init__(("127.0.0.1", port), _Handler)
        self.params: dict[str, str] = {}

    def wait(self, timeout: float = 300) -> dict[str, str]:
        self.timeout = 1                            # poll, so a stray request (favicon) does not end the wait
        deadline = time.monotonic() + timeout
        while not self.params and time.monotonic() < deadline:
            self.handle_request()
        return self.params


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        self.server.params.update(q)                # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Login received. You can close this tab." if q else b"Waiting for the broker redirect.")

    def log_message(self, *a):
        pass


def pick(params: dict, names: tuple[str, ...]) -> str | None:
    return next((params[n] for n in names if params.get(n)), None)


def code_from(text: str, names: tuple[str, ...]) -> str:
    """The one-time code from a pasted code, or from a whole redirect URL."""
    text = text.strip()
    if "?" in text or text.startswith("http"):
        code = pick({k: v[0] for k, v in parse_qs(urlparse(text).query).items()}, names)
        if not code:
            raise AuthError(f"no {' / '.join(names)} in that URL")
        return code
    return text


def update_env_file(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    for k, v in values.items():
        line = f"{k}={v}"
        for i, existing in enumerate(lines):
            if existing.startswith(f"{k}="):
                lines[i] = line
                break
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n")


def save(broker: str, tokens: dict[str, str], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{broker}.json"
    path.write_text(json.dumps({"saved_at": datetime.now(timezone.utc).isoformat(), **tokens}, indent=2))
    path.chmod(0o600)
    return path


def run(p: Provider, key: str, secret: str, code: str | None, port: int) -> dict[str, str]:
    state = secrets.token_urlsafe(8)
    if code is None:
        server = CallbackServer(port)               # bind first so a busy port fails before the browser opens
        url = p.login_url(key, state)
        print(f"Log in here (redirect URL registered for the app must be http://127.0.0.1:{port}/):\n  {url}")
        webbrowser.open(url)
        params = server.wait()
        server.server_close()
        code = pick(params, p.code_params)
        if not code:
            raise AuthError("no login arrived within 5 minutes")
        if p.name == "paytm" and params.get("state") not in (None, state):
            raise AuthError("the login came back with a different 'state' than we sent; start again")
    else:
        code = code_from(code, p.code_params)
    return p.exchange(key, secret, code)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m broker_auth", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("broker", choices=[*sorted(PROVIDERS), "all", "status"], help="'all' logs in to every broker whose <BROKER>_API_KEY and _API_SECRET are set")
    ap.add_argument("--code", help="the one-time code, or the whole URL the broker redirected to")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--env-file", type=Path, help="also write the tokens into this .env file")
    ap.add_argument("--out", type=Path, default=Path.home() / ".broker_tokens")
    args = ap.parse_args(argv)
    if args.broker == "status":
        from .check import check_all

        results = check_all()
        for name, r in results.items():
            print(f"{name:10s} {r['status']:15s} {r['detail']}")
        return 0 if all(r["status"] in ("valid", "not_configured") for r in results.values()) else 1
    if args.broker == "all":
        if args.code:
            ap.error("--code belongs to one broker")
        names = [b for b in sorted(PROVIDERS) if os.getenv(f"{b.upper()}_API_KEY") and os.getenv(f"{b.upper()}_API_SECRET")]
        if not names:
            ap.error("set <BROKER>_API_KEY and <BROKER>_API_SECRET for at least one broker")
    else:
        names = [args.broker]
    failed = 0
    for name in names:
        prefix = name.upper()
        key = os.getenv(f"{prefix}_API_KEY") or input(f"{prefix} API key: ").strip()
        secret = os.getenv(f"{prefix}_API_SECRET") or getpass.getpass(f"{prefix} API secret: ").strip()
        try:
            tokens = run(PROVIDERS[name], key, secret, args.code, args.port)
        except AuthError as e:
            print(f"{name} failed: {e}", file=sys.stderr)
            failed += 1
            continue
        print(f"{name}: saved {save(name, tokens, args.out)}  ({', '.join(tokens)})")
        if args.env_file:
            update_env_file(args.env_file, tokens)
            print(f"{name}: updated {args.env_file}")
    return 1 if failed else 0
