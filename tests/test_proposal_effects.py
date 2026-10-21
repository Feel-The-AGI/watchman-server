"""Human-readable proposal text. No database and no model call."""

from datetime import date

import pytest

from app.engines.proposal_effects import _as_date, card_command, describe_command, explanation_for

DENTIST = {
    "name": "Dentist appointment",
    "type": "personal",
    "start_date": "2026-10-27",
    "end_date": "2026-10-27",
    "time_start": "15:00",
    "time_end": "16:00",
    "days_of_week": ["tuesday"],
}


def test_october_27_2026_is_a_tuesday():
    assert date(2026, 10, 27).strftime("%A") == "Tuesday"


def test_empty_tool_text_describes_the_dentist_appointment():
    summary = "Add Dentist appointment every Tue, 27 Oct, 3\u20134pm"
    assert describe_command("add_commitment", DENTIST) == summary
    assert explanation_for("add_commitment", DENTIST, "") == summary
    assert explanation_for("add_commitment", DENTIST, "Executing add_commitment") == summary
    assert "Executing" not in summary


def test_a_model_sentence_does_not_replace_the_payload_dates():
    payload = {
        "name": "Dentist appointment",
        "type": "personal",
        "start_date": "2026-10-27",
        "end_date": "2026-10-27",
        "time_start": "10:00",
        "time_end": "11:00",
    }
    original = dict(payload)
    card = card_command({
        "action": "add_commitment",
        "payload": payload,
        "explanation": "I'll add the dentist on Fri 6 Nov.",
    })
    assert payload == original
    assert card["payload"] == original
    assert card["explanation"] == "Add Dentist appointment on Tue 27 Oct, 10\u201311am"
    assert "Fri" not in card["explanation"]
    assert "6 Nov" not in card["explanation"]
    assert card["model_text"] == "I'll add the dentist on Fri 6 Nov."


def test_multi_day_leave_card_uses_the_payload_range():
    card = card_command({
        "action": "add_leave",
        "payload": {
            "type": "annual",
            "start_date": "2026-11-24",
            "end_date": "2026-11-26",
        },
        "explanation": "I'll book Fri 6 Nov off.",
    })
    assert card["explanation"] == "Mark Tue 24 Nov to Thu 26 Nov as leave"
    assert card["payload"]["start_date"] == "2026-11-24"
    assert card["payload"]["end_date"] == "2026-11-26"
    assert card["model_text"] == "I'll book Fri 6 Nov off."


def test_a_commitment_card_shows_the_payload_times():
    card = card_command({
        "action": "add_commitment",
        "payload": DENTIST,
        "explanation": "Executing add_commitment",
    })
    assert card["explanation"] == "Add Dentist appointment every Tue, 27 Oct, 3\u20134pm"
    assert "model_text" not in card


@pytest.mark.parametrize("action,payload,expected", [
    ("override_days", {
        "start_date": "2026-11-02",
        "end_date": "2026-11-06",
        "work_type": "work_night",
    }, "Set Mon 2 Nov \u2013 Fri 6 Nov to night shift, keeping off days"),
    ("override_days", {
        "start_date": "2026-11-02",
        "end_date": "2026-11-06",
        "work_type": "work_night",
        "preserve_off_days": False,
    }, "Set Mon 2 Nov \u2013 Fri 6 Nov to night shift"),
    ("update_cycle", {
        "name": "14/14",
        "pattern": [
            {"label": "work_day", "duration": 14},
            {"label": "off", "duration": 14},
        ],
        "anchor_date": "2026-10-21",
        "anchor_cycle_day": 1,
    }, "Set rotation to 14 day shifts, 14 days off, anchored on Wed 21 Oct as day 1"),
    ("update_cycle", {
        "name": "14/14",
        "shift_by_days": -1,
    }, "Shift your rotation by -1 day"),
    ("create_daily_log", {
        "date": "2026-10-27",
        "note": "Training",
        "actual_hours": 10,
    }, "Log a note for Tue 27 Oct, 10 hours"),
    ("create_incident", {
        "date": "2026-10-27",
        "title": "Forced overtime",
        "type": "overtime",
    }, "Log incident: Forced overtime on Tue 27 Oct"),
    ("copy_incident", {
        "source_date": "2026-10-27",
        "target_date": "2026-10-28",
    }, "Copy the Tue 27 Oct incident to Wed 28 Oct"),
    ("undo", {}, "Undo your last change"),
    ("redo", {}, "Redo your last undone change"),
    ("remove_commitment", {"id": "commitment-1", "name": "Gym"}, "Remove Gym"),
    ("remove_leave", {
        "id": "leave-1",
        "name": "Annual leave",
        "start_date": "2026-11-24",
        "end_date": "2026-11-26",
    }, "Remove Annual leave, Tue 24 Nov to Thu 26 Nov"),
    ("update_constraint", {
        "id": "rule-1",
        "description": "No study on night shifts",
    }, "Update constraint: No study on night shifts"),
    ("remove_constraint", {
        "id": "rule-1",
        "description": "No study on night shifts",
    }, "Remove constraint: No study on night shifts"),
    ("add_leave", {"type": "other", "date": "2026-10-31"}, "Mark Sat 31 Oct as leave"),
    ("add_commitment", {
        "name": "Dentist appointment",
        "type": "personal",
        "start_date": "2026-10-27",
        "end_date": "2026-10-27",
        "time_start": "15:00",
        "time_end": "16:00",
    }, "Add Dentist appointment on Tue 27 Oct, 3\u20134pm"),
])
def test_each_proposed_action_names_the_payload(action, payload, expected):
    assert describe_command(action, payload) == expected
    assert payload.get("name") != expected


def test_a_weekly_commitment_is_not_described_as_every_day():
    assert describe_command("add_commitment", {
        "name": "Gym",
        "type": "personal",
        "days_of_week": ["tuesday"],
        "start_date": "2026-11-03",
        "end_date": "2026-12-15",
        "time_start": "18:00",
        "time_end": "19:00",
    }) == "Add Gym every Tue, 3 Nov \u2013 15 Dec, 6\u20137pm"


def test_a_bad_date_does_not_raise():
    assert _as_date("next tuesday") is None
    assert _as_date("2026-13-40") is None
    card = card_command({
        "action": "add_commitment",
        "payload": {
            "name": "Gym",
            "start_date": "next tuesday",
            "time_start": "18:00",
            "time_end": "19:00",
        },
        "explanation": "I'll add it on Friday",
    })
    assert card["explanation"] == "Add Gym, 6\u20137pm"
    assert "Friday" not in card["explanation"]
    assert card["payload"]["start_date"] == "next tuesday"


def test_leave_with_type_other_is_described_from_the_date():
    assert date(2026, 10, 31).strftime("%A") == "Saturday"
    assert describe_command("add_leave", {"type": "other", "date": "2026-10-31"}) == (
        "Mark Sat 31 Oct as leave"
    )
