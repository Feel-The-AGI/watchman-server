"""Didn't-work marks that local date Off, and says so on the card."""

from datetime import date

from app.engines.missed_shift import calendar_request
from app.engines.proposal_effects import describe_command, no_change_message

TODAY = date(2026, 10, 22)


def test_remove_oct_15_uses_2026_and_does_not_preserve_off_days():
    found = calendar_request("Remove Oct 15th, I didn't go to work that day", TODAY)
    assert found.payload == {
        "start_date": "2026-10-15",
        "end_date": "2026-10-15",
        "work_type": "off",
        "preserve_off_days": False,
    }
    assert describe_command("override_days", found.payload) == "Mark Thu 15 Oct 2026 as Off"
    assert "Executing" not in describe_command("override_days", found.payload)


def test_a_day_already_off_is_described_as_no_change():
    assert no_change_message(date(2025, 10, 15), date(2025, 10, 15), "off") == (
        "No change: Wed 15 Oct 2025 was already Off"
    )
