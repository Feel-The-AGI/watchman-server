"""Black-box tests for a deployed Watchman API.

Skipped unless LIVE_BASE_URL is set. They do not use a user token, do not
create accounts, and do not send a valid Paystack signature.
"""

import os

import pytest


def pytest_collection_modifyitems(config, items):
    skip = None
    if not os.environ.get("LIVE_BASE_URL"):
        skip = pytest.mark.skip(reason="Set LIVE_BASE_URL to run black-box tests against a deployed API")
    live = pytest.mark.live
    for item in items:
        path = str(getattr(item, "path", item.fspath)).replace("\\", "/")
        if "/tests/live/" not in path:
            continue
        item.add_marker(live)
        if skip is not None:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def live_base_url():
    return os.environ["LIVE_BASE_URL"].rstrip("/")


@pytest.fixture(scope="session")
def live_client(live_base_url):
    import httpx

    with httpx.Client(base_url=live_base_url, timeout=45.0, follow_redirects=False) as client:
        yield client
