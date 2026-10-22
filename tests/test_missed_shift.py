"""Didn't-work marks that local date Off, and says so on the card."""

from datetime import date

import pytest

from app.engines.missed_shift import calendar_request
from app.engines.proposal_effects import describe_command, no_change_message
from app.engines.spoken_dates import parse_spoken_span

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


def test_past_monday_is_the_19th():
    assert parse_spoken_span("Monday", TODAY, "past").start == date(2026, 10, 19)
    found = calendar_request("I didn't work Monday", TODAY)
    assert found.payload["start_date"] == "2026-10-19"
    assert found.payload["end_date"] == "2026-10-19"


def test_past_25th_is_last_month_not_last_year():
    assert parse_spoken_span("the 25th", TODAY, "past").start == date(2026, 9, 25)
    found = calendar_request("I didn't work the 25th", TODAY)
    assert found.payload["start_date"] == "2026-09-25"


@pytest.mark.parametrize("text", [
    "Delete my account",
    "cancel my subscription",
    "I'm off to bed",
    "Remove the meeting on Friday",
])
def test_ordinary_sentences_go_to_the_model(text):
    assert calendar_request(text, TODAY) is None


def test_oct_20_and_21_asks():
    span = parse_spoken_span("Oct 20 and 21", TODAY, "past")
    assert span.problem == "ambiguous"
    assert span.detail.strip().endswith("?")
    asked = calendar_request("I didn't work Oct 20 and 21", TODAY)
    assert asked.payload is None
    assert asked.clarify.strip().endswith("?")
