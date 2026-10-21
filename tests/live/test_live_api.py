"""Security and contract checks against the live Watchman API."""

import base64
import hashlib
import hmac
import json
import re
import time
from decimal import Decimal, ROUND_HALF_UP

import pytest

APP_ORIGIN = "https://www.trywatchman.app"
EVIL_ORIGIN = "https://evil.example"

PUBLIC_EXACT = {
    ("GET", "/"),
    ("GET", "/health"),
    ("GET", "/docs"),
    ("GET", "/redoc"),
    ("GET", "/openapi.json"),
    ("GET", "/api/payments/pricing"),
    ("POST", "/api/payments/webhook"),
    ("GET", "/api/cron/health"),
}

LEAK_MARKERS = (
    "traceback",
    "sk_live",
    "sk_test",
    "service_role",
    "supabase_service",
    "paystack_secret",
    "anthropic_api_key",
    'file "',
)


def _b64(value) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _hs256(payload: dict, secret: str) -> str:
    header = _b64({"alg": "HS256", "typ": "JWT"})
    body = _b64(payload)
    signature = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{header}.{body}.{encoded}"


def _forged_token() -> str:
    return _hs256(
        {
            "sub": "00000000-0000-0000-0000-000000000000",
            "aud": "authenticated",
            "role": "authenticated",
            "exp": 4102444800,
        },
        "not-the-watchman-jwt-secret",
    )


def _expired_token() -> str:
    return _hs256(
        {
            "sub": "00000000-0000-0000-0000-000000000000",
            "aud": "authenticated",
            "role": "authenticated",
            "exp": 1,
        },
        "not-the-watchman-jwt-secret",
    )


def _alg_none_token() -> str:
    header = _b64({"alg": "none", "typ": "JWT"})
    body = _b64(
        {
            "sub": "00000000-0000-0000-0000-000000000000",
            "aud": "authenticated",
            "role": "authenticated",
            "exp": 4102444800,
        }
    )
    return f"{header}.{body}."


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", path)


def _is_public(method: str, path: str) -> bool:
    if (method, path) in PUBLIC_EXACT:
        return True
    return method == "GET" and path.startswith("/api/sharing/public/")


def _assert_no_leak(response):
    text = response.text.lower()
    for marker in LEAK_MARKERS:
        assert marker not in text, marker
    assert "traceback (most recent call last)" not in text


def _operations(spec: dict):
    found = []
    for path, methods in (spec.get("paths") or {}).items():
        for method, operation in methods.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                found.append((method.upper(), path, operation or {}))
    return found


@pytest.fixture(scope="module")
def openapi_spec(live_client):
    response = live_client.get("/openapi.json")
    assert response.status_code == 200
    _assert_no_leak(response)
    return response.json()


def test_health(live_client):
    started = time.perf_counter()
    response = live_client.get("/health")
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body.get("version")
    assert "application/json" in response.headers.get("content-type", "")
    assert response.headers.get("access-control-allow-origin") != "*"
    # Printed so the run log keeps the warm-or-cold sample.
    print(f"HEALTH_SECONDS {elapsed:.3f}")
    assert elapsed < 90


def test_root(live_client):
    response = live_client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "operational"
    _assert_no_leak(response)


def test_cors_preflight_allows_the_app_origin(live_client):
    response = live_client.options(
        "/api/auth/me",
        headers={
            "Origin": APP_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code in (200, 204)
    assert response.headers.get("access-control-allow-origin") == APP_ORIGIN
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_cors_preflight_rejects_an_evil_origin(live_client):
    response = live_client.options(
        "/api/auth/me",
        headers={
            "Origin": EVIL_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    allow = response.headers.get("access-control-allow-origin")
    assert allow != EVIL_ORIGIN
    assert allow != "*"
    vary = response.headers.get("vary", "")
    assert "Origin" in vary or allow is None


def test_cors_simple_request_does_not_reflect_an_evil_origin(live_client):
    response = live_client.get("/health", headers={"Origin": EVIL_ORIGIN})
    assert response.status_code == 200
    allow = response.headers.get("access-control-allow-origin")
    assert allow != EVIL_ORIGIN
    assert allow != "*"


def test_pricing_is_twelve_dollars_in_a_sane_ghs_amount(live_client):
    response = live_client.get("/api/payments/pricing")
    assert response.status_code == 200, response.text[:300]
    _assert_no_leak(response)
    body = response.json()
    assert body["usd_price"] == 12
    assert body["currency"] == "GHS"
    assert body.get("period_days") == 30
    rate = body["exchange_rate"]
    ghs = body["ghs_amount"]
    assert isinstance(rate, (int, float)) and 5 <= float(rate) <= 40
    assert isinstance(ghs, (int, float)) and float(ghs) > 0
    expected = (Decimal("12") * Decimal(str(rate))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert Decimal(str(ghs)) == expected
    print(f"PRICING usd=12 rate={rate} ghs={ghs}")


def test_paystack_webhook_rejects_a_missing_signature(live_client):
    response = live_client.post(
        "/api/payments/webhook",
        json={"event": "charge.success", "data": {"reference": "qa-live-invalid-signature"}},
    )
    assert response.status_code == 401
    _assert_no_leak(response)
    assert response.json().get("detail")


def test_paystack_webhook_rejects_a_bad_signature_twice(live_client):
    payload = {"event": "charge.success", "data": {"reference": "qa-live-invalid-signature"}}
    headers = {"x-paystack-signature": "0" * 128}
    first = live_client.post("/api/payments/webhook", json=payload, headers=headers)
    second = live_client.post("/api/payments/webhook", json=payload, headers=headers)
    assert first.status_code == 401
    assert second.status_code == 401
    assert first.json() == second.json()
    _assert_no_leak(first)
    assert "invalid signature" in first.text.lower()


def test_cron_summary_rejects_a_missing_or_wrong_secret(live_client):
    missing = live_client.post("/api/cron/weekly-summary")
    wrong = live_client.post(
        "/api/cron/weekly-summary",
        headers={"X-Cron-Secret": "definitely-not-the-cron-secret"},
    )
    assert missing.status_code in (401, 503)
    assert wrong.status_code in (401, 503)
    assert missing.status_code != 200
    assert wrong.status_code != 200
    assert "sent" not in missing.text.lower()
    _assert_no_leak(missing)
    _assert_no_leak(wrong)
    health = live_client.get("/api/cron/health")
    assert health.status_code == 200


def test_malformed_json_is_4xx(live_client):
    response = live_client.post(
        "/api/chat/message",
        content=b"{",
        headers={"Content-Type": "application/json"},
    )
    assert 400 <= response.status_code < 500
    _assert_no_leak(response)


def test_oversized_body_is_4xx(live_client):
    # Large enough to be abusive, small enough not to exhaust a free instance.
    body = b'{"content":"' + (b"a" * 300_000) + b'"}'
    response = live_client.post(
        "/api/chat/message",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert 400 <= response.status_code < 500
    assert response.status_code != 500
    _assert_no_leak(response)


@pytest.mark.parametrize("header", [None, "Bearer ", "Bearer not-a-jwt"])
def test_auth_me_rejects_missing_or_garbage_tokens(live_client, header):
    headers = {"Authorization": header} if header is not None else {}
    response = live_client.get("/api/auth/me", headers=headers)
    assert response.status_code in (401, 403)
    _assert_no_leak(response)


@pytest.mark.parametrize("token_factory", [_forged_token, _expired_token, _alg_none_token])
def test_auth_me_rejects_forged_expired_and_unsigned_tokens(live_client, token_factory):
    response = live_client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token_factory()}"},
    )
    assert response.status_code in (401, 403)
    _assert_no_leak(response)
    assert response.status_code != 200


def test_profile_patch_cannot_be_anonymous(live_client):
    response = live_client.patch(
        "/api/auth/me",
        json={"name": "qa", "tier": "admin", "role": "admin"},
    )
    assert response.status_code in (401, 403)
    _assert_no_leak(response)


def test_every_authenticated_operation_rejects_missing_and_forged_tokens(live_client, openapi_spec):
    failures = []
    validation_before_auth = []
    checked = 0
    tokens = {
        "missing": None,
        "forged": _forged_token(),
        "expired": _expired_token(),
        "alg_none": _alg_none_token(),
    }
    for method, path, _operation in _operations(openapi_spec):
        if _is_public(method, path):
            continue
        url = _fill(path)
        for name, token in tokens.items():
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            kwargs = {}
            if method in {"POST", "PUT", "PATCH"}:
                kwargs["json"] = {}
            response = live_client.request(method, url, headers=headers, **kwargs)
            checked += 1
            if response.status_code == 422:
                validation_before_auth.append(f"{name} {method} {path}")
            elif response.status_code not in (401, 403):
                failures.append(f"{name} {method} {path} -> {response.status_code} {response.text[:180]}")
            else:
                text = response.text.lower()
                if any(marker in text for marker in LEAK_MARKERS):
                    failures.append(f"leak {name} {method} {path}")
    print(f"AUTH_CHECKS {checked} VALIDATION_BEFORE_AUTH {len(validation_before_auth)}")
    if validation_before_auth:
        print("VALIDATION_PATHS " + ", ".join(sorted(set(validation_before_auth))[:40]))
    assert not failures, "\n".join(failures[:30])


def test_unknown_route_is_404_without_a_stack(live_client):
    response = live_client.get("/api/does-not-exist")
    assert response.status_code == 404
    _assert_no_leak(response)


def test_response_headers_do_not_enable_wildcard_credentials(live_client):
    response = live_client.get("/health", headers={"Origin": APP_ORIGIN})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") in (None, APP_ORIGIN)
    assert "server" not in response.headers or "python" not in response.headers.get("server", "").lower()
    interesting = {
        key: response.headers.get(key)
        for key in (
            "strict-transport-security",
            "x-content-type-options",
            "x-frame-options",
            "referrer-policy",
            "server",
            "x-render-origin-server",
        )
    }
    print(f"HEADERS {interesting}")
