"""Human-readable proposal text. No database and no model call."""

from datetime import date

from app.engines.proposal_effects import describe_command, explanation_for

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


def test_a_real_model_sentence_is_kept():
    assert explanation_for(
        "add_leave",
        {"name": "Annual", "start_date": "2026-11-01", "end_date": "2026-11-05"},
        "I'll add leave.",
    ) == "I'll add leave."


def test_leave_with_type_other_is_described_from_the_date():
    assert date(2026, 10, 31).strftime("%A") == "Saturday"
    assert describe_command("add_leave", {"type": "other", "date": "2026-10-31"}) == (
        "Mark Sat 31 Oct as leave"
    )
