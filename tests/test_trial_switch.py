"""New signups are not in trial. Accounts from before the cutoff can finish theirs."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings, get_settings
from app.middleware.auth import get_effective_tier, is_in_trial
from app.routes.settings import get_settings as get_settings_route


def _switch(enabled: bool, cutoff: str, end_all: bool = False):
    return SimpleNamespace(
        trial_enabled=enabled,
        trial_signup_cutoff=cutoff,
        trial_end_all=end_all,
    )


class _Clock(datetime):
    current = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.current.replace(tzinfo=None)
        return cls.current.astimezone(tz)


def _user(created_at: str):
    return {
        "id": "user-1",
        "tier": "free",
        "role": "user",
        "created_at": created_at,
        "settings": {},
    }


def test_the_configured_switch_is_off():
    settings = get_settings()
    assert settings.trial_enabled is False
    assert settings.trial_signup_cutoff == "2026-10-22T00:00:00Z"
    assert settings.trial_end_all is False
    fresh = Settings()
    assert fresh.trial_enabled is False
    assert fresh.trial_signup_cutoff == "2026-10-22T00:00:00Z"
    assert fresh.trial_end_all is False


def test_an_account_from_before_the_cutoff_can_finish_its_trial(monkeypatch):
    now = datetime.now(timezone.utc)
    cutoff = (now + timedelta(days=1)).isoformat()
    monkeypatch.setattr("app.middleware.auth.get_settings", lambda: _switch(True, cutoff))
    created = (now - timedelta(hours=1)).isoformat()
    assert is_in_trial(_user(created)) is True
    assert get_effective_tier(_user(created)) == "trial"

    expired = (now - timedelta(days=4)).isoformat()
    assert is_in_trial(_user(expired)) is False


def test_an_account_at_or_after_the_cutoff_is_not_in_trial(monkeypatch):
    monkeypatch.setattr(
        "app.middleware.auth.get_settings",
        lambda: _switch(False, "2026-10-22T00:00:00Z"),
    )
    assert is_in_trial(_user("2026-10-22T00:00:00Z")) is False
    assert is_in_trial(_user("2026-10-22T00:00:00+00:00")) is False
    assert is_in_trial(_user("2026-10-23T12:00:00Z")) is False
    assert get_effective_tier(_user("2026-10-22T00:00:00Z")) == "free"


def test_the_switch_being_off_ends_trial(monkeypatch):
    """Off means no new trial. A pre-cutoff account keeps the days it has left."""
    _Clock.current = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.middleware.auth.datetime", _Clock)
    monkeypatch.setattr(
        "app.middleware.auth.get_settings",
        lambda: _switch(False, "2026-10-22T00:00:00Z"),
    )
    assert is_in_trial(_user("2026-10-21T18:00:00Z")) is True
    assert get_effective_tier(_user("2026-10-21T18:00:00Z")) == "trial"
    assert is_in_trial(_user("2026-10-22T00:00:00Z")) is False
    assert get_effective_tier(_user("2026-10-22T00:00:00Z")) == "free"


# Created 21 Oct, so each trial ends on 24 Oct at the same clock time.
_OPEN_TRIALS = (
    ("2026-10-21T19:40:00Z", datetime(2026, 10, 24, 19, 40, tzinfo=timezone.utc)),
    ("2026-10-21T21:20:00Z", datetime(2026, 10, 24, 21, 20, tzinfo=timezone.utc)),
    ("2026-10-21T23:17:00Z", datetime(2026, 10, 24, 23, 17, tzinfo=timezone.utc)),
)


def test_mid_trial_accounts_keep_their_trial_when_new_signups_do_not(monkeypatch):
    monkeypatch.setattr("app.middleware.auth.datetime", _Clock)
    monkeypatch.setattr(
        "app.middleware.auth.get_settings",
        lambda: _switch(False, "2026-10-22T00:00:00Z"),
    )
    for created, ends in _OPEN_TRIALS:
        _Clock.current = ends - timedelta(seconds=1)
        assert is_in_trial(_user(created)) is True
        assert get_effective_tier(_user(created)) == "trial"
        _Clock.current = ends
        assert is_in_trial(_user(created)) is False


def test_ending_every_trial_is_a_separate_switch(monkeypatch):
    _Clock.current = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.middleware.auth.datetime", _Clock)
    monkeypatch.setattr(
        "app.middleware.auth.get_settings",
        lambda: _switch(False, "2026-10-22T00:00:00Z", end_all=True),
    )
    assert is_in_trial(_user("2026-10-21T19:40:00Z")) is False
    assert get_effective_tier(_user("2026-10-21T19:40:00Z")) == "free"


@pytest.mark.asyncio
async def test_settings_returns_trial_null_for_a_new_account():
    user = _user(datetime.now(timezone.utc).isoformat())
    result = await get_settings_route(user)
    assert result["data"]["trial"] is None
    assert result["data"]["effective_tier"] == "free"


@pytest.mark.asyncio
async def test_settings_still_reports_a_trial_that_started_before_the_cutoff(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "app.middleware.auth.get_settings",
        lambda: _switch(True, (now + timedelta(days=1)).isoformat()),
    )
    user = _user((now - timedelta(hours=1)).isoformat())
    result = await get_settings_route(user)
    assert result["data"]["trial"]["active"] is True
    assert result["data"]["effective_tier"] == "trial"
