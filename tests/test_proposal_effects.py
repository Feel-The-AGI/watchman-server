"""Human-readable proposal text. No database and no model call."""

from datetime import date

from app.engines.proposal_effects import card_command, describe_command, explanation_for

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
    summary = "Add Dentist appointment on Tue 27 Oct, 3\u20134pm"
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
    assert card["explanation"] == "Add Dentist appointment on Tue 27 Oct, 3\u20134pm"
    assert "model_text" not in card


def test_leave_with_type_other_is_described_from_the_date():
    assert date(2026, 10, 31).strftime("%A") == "Saturday"
    assert describe_command("add_leave", {"type": "other", "date": "2026-10-31"}) == (
        "Mark Sat 31 Oct as leave"
    )
