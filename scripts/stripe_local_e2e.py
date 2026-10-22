#!/usr/bin/env python3
"""Run Watchman's Stripe billing against a local server and stripe-mock.

Checkout is a real Stripe SDK call to stripe-mock. Webhooks are signed here
with the test secret (HMAC-SHA256 over ``t.payload``) and posted to the API.
Nothing in this script talks to Stripe or Supabase.

Usage (from the repo root):
    python scripts/stripe_local_e2e.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote_plus

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

API_PORT = 8765
MOCK_PORT = 12111
PROXY_PORT = 12112
USER_ID = "11111111-1111-1111-1111-111111111111"
WEBHOOK_SECRET = "whsec_test_secret"
PRICE_MONTH = "price_month_test"
PRICE_YEAR = "price_year_test"
PRICE_FOUNDING = "price_founding_test"
CLIENT = "https://trywatchman.app"

# Set before importing the app. get_settings() is cached on first use.
os.environ["APP_ENV"] = "development"
os.environ["DEBUG"] = "false"
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_ANON_KEY"] = ""
os.environ["SUPABASE_SERVICE_KEY"] = ""
os.environ["SUPABASE_JWT_SECRET"] = ""
os.environ["STRIPE_SECRET_KEY"] = "sk_test_watchmanlocal"
os.environ["STRIPE_WEBHOOK_SECRET"] = WEBHOOK_SECRET
os.environ["STRIPE_PRICE_MONTHLY"] = PRICE_MONTH
os.environ["STRIPE_PRICE_YEARLY"] = PRICE_YEAR
os.environ["STRIPE_PRICE_FOUNDING"] = PRICE_FOUNDING
os.environ["STRIPE_API_BASE"] = f"http://127.0.0.1:{PROXY_PORT}"
os.environ["CLIENT_BASE_URL"] = CLIENT

RECORDED: list[dict] = []


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


class _Proxy(BaseHTTPRequestHandler):
    def _forward(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        upstream = urllib.request.Request(
            f"http://127.0.0.1:{MOCK_PORT}{self.path}",
            data=body if self.command != "GET" else None,
            method=self.command,
        )
        for key, value in self.headers.items():
            if key.lower() in {"host", "content-length"}:
                continue
            upstream.add_header(key, value)
        try:
            with urllib.request.urlopen(upstream, timeout=20) as response:
                payload = response.read()
                status = response.status
                content_type = response.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status = exc.code
            content_type = exc.headers.get("Content-Type", "application/json")
        RECORDED.append({
            "method": self.command,
            "path": self.path.split("?", 1)[0],
            "status": status,
            "body": body.decode("utf-8", "replace"),
        })
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def do_DELETE(self):
        self._forward()

    def log_message(self, fmt, *args):
        return


def _sign(payload: bytes) -> str:
    timestamp = int(time.time())
    signed = f"{timestamp}.{payload.decode()}"
    digest = hmac.new(WEBHOOK_SECRET.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _request(method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, dict | str]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{API_PORT}{path}",
        data=body,
        method=method,
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    text = raw.decode()
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


def _json_call(method: str, path: str, payload: dict | None = None, auth: bool = True) -> tuple[int, dict | str]:
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer local-test"
    body = json.dumps(payload).encode() if payload is not None else None
    return _request(method, path, body, headers)


def _post_event(event_id: str, event_type: str, obj: dict) -> tuple[int, dict | str]:
    payload = json.dumps({
        "id": event_id,
        "object": "event",
        "type": event_type,
        "data": {"object": obj},
    }, separators=(",", ":")).encode()
    return _request("POST", "/api/billing/stripe-webhook", payload, {
        "Content-Type": "application/json",
        "Stripe-Signature": _sign(payload),
    })


def _subscription(period_end: datetime, status: str, cancel: bool, price: str, plan: str) -> dict:
    stamp = int(period_end.timestamp())
    return {
        "id": "sub_e2e",
        "object": "subscription",
        "customer": "cus_e2e",
        "status": status,
        "current_period_end": stamp,
        "cancel_at_period_end": cancel,
        "metadata": {"plan": plan, "watchman_user_id": USER_ID},
        "items": {"data": [{"price": {"id": price}, "current_period_end": stamp}]},
    }


def _check(name: str, ok: bool, detail: str, results: list) -> None:
    results.append({"check": name, "ok": ok, "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    if not ok:
        print("--- recent stripe-mock calls ---")
        for row in RECORDED[-8:]:
            print(row["method"], row["path"], row["status"], row["body"][:500])
        raise SystemExit(1)


def _bodies(path: str) -> list[str]:
    return [unquote_plus(row["body"]) for row in RECORDED if row["path"] == path and row["method"] == "POST"]


def main() -> None:
    mock_proc = None
    if not _port_open(MOCK_PORT):
        binary = os.environ.get("STRIPE_MOCK_BIN", "/tmp/stripe-mock-bin/stripe-mock")
        if not Path(binary).exists():
            raise SystemExit(f"stripe-mock is not running and {binary} is missing")
        mock_proc = subprocess.Popen(
            [binary, "-http-port", str(MOCK_PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if _port_open(MOCK_PORT):
                break
            time.sleep(0.1)
        else:
            raise SystemExit("stripe-mock did not start")

    proxy = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), _Proxy)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()

    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import app
    from app.middleware.auth import get_current_user
    from app.routes import billing
    from tests.stripe_memory import MemoryBillingDB

    settings = get_settings()
    if settings.supabase_url or settings.stripe_secret_key != "sk_test_watchmanlocal":
        raise SystemExit("Refusing to run: settings are not the local Stripe test values")

    memory = MemoryBillingDB()
    memory.users[USER_ID] = {
        "id": USER_ID,
        "email": "ada@example.com",
        "name": "Ada",
        "tier": "free",
        "role": "user",
    }
    billing.Database = lambda use_admin=False: memory

    async def _user():
        return {"id": USER_ID, "email": "ada@example.com", "name": "Ada", "tier": "free", "role": "user"}

    app.dependency_overrides[get_current_user] = _user

    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=API_PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    for _ in range(50):
        if _port_open(API_PORT):
            break
        time.sleep(0.1)
    else:
        raise SystemExit("API did not start")

    results: list[dict] = []
    try:
        health_status, health = _json_call("GET", "/health", auth=False)
        _check("health", health_status == 200 and health.get("status") == "healthy", str(health), results)

        seats_status, seats = _json_call("GET", "/api/billing/founding", auth=False)
        _check(
            "founding seats open",
            seats_status == 200 and seats.get("founding_seats_left") == 100,
            str(seats),
            results,
        )

        checkout_status, checkout = _json_call("POST", "/api/billing/checkout", {"plan": "monthly"})
        checkout_bodies = _bodies("/v1/checkout/sessions")
        _check(
            "checkout monthly",
            checkout_status == 200
            and str(checkout.get("url", "")).startswith("https://")
            and any("mode=subscription" in body or '"mode":"subscription"' in body or '"mode": "subscription"' in body for body in checkout_bodies)
            and any(USER_ID in body for body in checkout_bodies)
            and any(PRICE_MONTH in body for body in checkout_bodies)
            and any("card" in body for body in checkout_bodies)
            and any("/settings?billing=success" in body for body in checkout_bodies)
            and any("/settings?billing=cancel" in body for body in checkout_bodies)
            and all("trial" not in body for body in checkout_bodies),
            json.dumps({"status": checkout_status, "body": checkout, "stripe": checkout_bodies[:1]}),
            results,
        )

        customers_after_first = len(_bodies("/v1/customers"))
        yearly_status, yearly = _json_call("POST", "/api/billing/checkout", {"plan": "yearly"})
        _check(
            "checkout reuses customer",
            yearly_status == 200 and len(_bodies("/v1/customers")) == customers_after_first == 1
            and any(PRICE_YEAR in body for body in _bodies("/v1/checkout/sessions")),
            str(yearly),
            results,
        )

        portal_status, portal = _json_call("POST", "/api/billing/portal")
        portal_config = _bodies("/v1/billing_portal/configurations")
        _check(
            "portal cancels at period end",
            portal_status == 200
            and str(portal.get("url", "")).startswith("https://")
            and any("at_period_end" in body for body in portal_config)
            and any("/settings" in body for body in _bodies("/v1/billing_portal/sessions")),
            json.dumps({"status": portal_status, "body": portal, "config": portal_config[:1]}),
            results,
        )

        period_end = datetime.now(timezone.utc) + timedelta(days=30)
        completed_status, completed = _post_event("evt_e2e_checkout", "checkout.session.completed", {
            "id": "cs_e2e",
            "object": "checkout.session",
            "mode": "subscription",
            "customer": "cus_e2e",
            "subscription": "sub_e2e",
            "client_reference_id": USER_ID,
            "metadata": {"plan": "monthly", "watchman_user_id": USER_ID},
        })
        retrieved = any(
            row["method"] == "GET" and row["path"] == "/v1/subscriptions/sub_e2e" for row in RECORDED
        )
        _check(
            "checkout.session.completed",
            completed_status == 200 and retrieved,
            json.dumps({"webhook": completed, "retrieved_subscription": retrieved}),
            results,
        )

        paid_status, paid = _post_event("evt_e2e_invoice", "invoice.paid", {
            "id": "in_e2e",
            "object": "invoice",
            "customer": "cus_e2e",
            "subscription": "sub_e2e",
            "status": "paid",
            "metadata": {"plan": "monthly", "watchman_user_id": USER_ID},
            "lines": {"data": [{
                "period": {"end": int(period_end.timestamp())},
                "price": {"id": PRICE_MONTH},
            }]},
        })
        user = memory.users[USER_ID]
        _check(
            "invoice.paid sets Pro",
            paid_status == 200 and user.get("tier") == "pro",
            json.dumps({"webhook": paid, "tier": user.get("tier"), "expires": user.get("pro_expires_at")}),
            results,
        )

        status_code, status_body = _json_call("GET", "/api/billing/status")
        _check(
            "status is Pro monthly",
            status_code == 200
            and status_body.get("tier") == "pro"
            and status_body.get("plan") == "monthly"
            and status_body.get("status") == "active"
            and status_body.get("cancel_at_period_end") is False
            and status_body.get("founding_seats_left") == 100
            and bool(status_body.get("current_period_end")),
            str(status_body),
            results,
        )

        cancel_status, cancel_body = _post_event(
            "evt_e2e_cancel",
            "customer.subscription.updated",
            _subscription(period_end, "active", True, PRICE_MONTH, "monthly"),
        )
        held = memory.users[USER_ID].get("pro_expires_at")
        cancel_view_code, cancel_view = _json_call("GET", "/api/billing/status")
        _check(
            "cancel at period end keeps Pro",
            cancel_status == 200
            and memory.users[USER_ID].get("tier") == "pro"
            and cancel_view_code == 200
            and cancel_view.get("cancel_at_period_end") is True
            and cancel_view.get("tier") == "pro",
            str(cancel_view),
            results,
        )

        replay_status, replay_body = _post_event(
            "evt_e2e_cancel",
            "customer.subscription.updated",
            _subscription(period_end, "active", True, PRICE_MONTH, "monthly"),
        )
        _check(
            "replayed event is idempotent",
            replay_status == 200
            and replay_body.get("duplicate") is True
            and memory.users[USER_ID].get("pro_expires_at") == held
            and sum(1 for row in memory.events if row == "evt_e2e_cancel") == 1,
            str(replay_body),
            results,
        )

        deleted_status, deleted_body = _post_event(
            "evt_e2e_deleted",
            "customer.subscription.deleted",
            _subscription(datetime.now(timezone.utc) - timedelta(days=1), "canceled", False, PRICE_MONTH, "monthly"),
        )
        free_code, free_body = _json_call("GET", "/api/billing/status")
        _check(
            "subscription.deleted sets Free",
            deleted_status == 200
            and memory.users[USER_ID].get("tier") == "free"
            and free_code == 200
            and free_body.get("tier") == "free"
            and free_body.get("status") == "canceled",
            json.dumps({"webhook": deleted_body, "status": free_body}),
            results,
        )

        for index in range(100):
            memory.seats[f"past-{index}"] = {"status": "canceled"}
        sessions_before = len(_bodies("/v1/checkout/sessions"))
        full_status, full_body = _json_call("POST", "/api/billing/checkout", {"plan": "founding"})
        _check(
            "founding cap returns 409",
            full_status == 409
            and full_body.get("detail") == "Founding 100 is full"
            and len(_bodies("/v1/checkout/sessions")) == sessions_before,
            str(full_body),
            results,
        )
        left_status, left_body = _json_call("GET", "/api/billing/founding", auth=False)
        _check(
            "founding seats left is 0",
            left_status == 200 and left_body.get("founding_seats_left") == 0,
            str(left_body),
            results,
        )

        bad_payload = b'{"id":"evt_bad","object":"event","type":"invoice.paid","data":{"object":{}}}'
        bad_status, bad_body = _request("POST", "/api/billing/stripe-webhook", bad_payload, {
            "Content-Type": "application/json",
            "Stripe-Signature": "t=1,v1=" + ("ab" * 32),
        })
        _check(
            "bad signature is 400",
            bad_status == 400 and bad_body.get("detail") == "Invalid signature" and "evt_bad" not in memory.events,
            str(bad_body),
            results,
        )

        print(json.dumps({"ok": True, "results": results}, indent=2))
    finally:
        server.should_exit = True
        proxy.shutdown()
        if mock_proc is not None:
            mock_proc.terminate()


if __name__ == "__main__":
    main()
