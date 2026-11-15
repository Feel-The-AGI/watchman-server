"""Bad input is a clean validation error, never a 500 or a stored junk value."""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routes.auth import UpdateProfileRequest
from app.routes.calendar import GenerateCalendarRequest, LeaveBlockRequest
from app.routes.commands import UndoRequest
from app.routes.commitments import CreateCommitmentRequest, UpdateCommitmentRequest
from app.routes.cycles import CreateCycleRequest, UpdateCycleRequest
from app.routes.daily_logs import DailyLogCreateRequest, HoursUpdateRequest, _real_day
from app.routes.incidents import IncidentCreateRequest
from app.routes.settings import ConstraintRequest, UpdateSettingsRequest

LOG = {"date": "2026-11-12", "note": "x"}
INCIDENT = {"date": "2026-11-12", "type": "safety", "title": "t", "description": "d"}
LEAVE = {"start_date": "2026-11-12", "end_date": "2026-11-13"}


@pytest.mark.parametrize("model, body", [
    (DailyLogCreateRequest, {**LOG, "date": "2026-02-30"}),
    (DailyLogCreateRequest, {**LOG, "date": "0001-01-01"}),
    (DailyLogCreateRequest, {**LOG, "date": "9999-12-31"}),
    (DailyLogCreateRequest, {**LOG, "actual_hours": float("nan")}),
    (DailyLogCreateRequest, {**LOG, "actual_hours": 1e308}),
    (DailyLogCreateRequest, {**LOG, "actual_hours": 999}),
    (DailyLogCreateRequest, {**LOG, "actual_hours": -5}),
    (DailyLogCreateRequest, {**LOG, "overtime_hours": -4}),
    (DailyLogCreateRequest, {**LOG, "note": "a\x00b"}),
    (DailyLogCreateRequest, {**LOG, "note": "x" * 100000}),
    (DailyLogCreateRequest, {**LOG, "note": "   "}),
    (HoursUpdateRequest, {"actual_hours": 25}),
    (IncidentCreateRequest, {**INCIDENT, "date": "2026-02-30"}),
    (IncidentCreateRequest, {**INCIDENT, "title": "x" * 100000}),
    (IncidentCreateRequest, {**INCIDENT, "title": "a\x00b"}),
    (CreateCommitmentRequest, {"name": "x", "type": "nope"}),
    (CreateCommitmentRequest, {"name": "", "type": "personal"}),
    (CreateCommitmentRequest, {"name": "x", "type": "personal", "priority": -5}),
    (CreateCommitmentRequest, {"name": "x", "type": "personal", "start_date": "2026-12-31", "end_date": "2026-12-01"}),
    (UpdateCommitmentRequest, {"status": "bogus"}),
    (UpdateCommitmentRequest, {"type": "nope"}),
    (UpdateCommitmentRequest, {"completed_sessions": -5}),
    (UpdateCommitmentRequest, {"name": ""}),
    (LeaveBlockRequest, {**LEAVE, "start_date": "0001-01-01", "end_date": "0001-01-01"}),
    (LeaveBlockRequest, {**LEAVE, "start_date": "9999-12-30", "end_date": "9999-12-31"}),
    (LeaveBlockRequest, {**LEAVE, "end_date": "2076-12-13"}),
    (LeaveBlockRequest, {**LEAVE, "type": "vacation"}),
    (LeaveBlockRequest, {**LEAVE, "name": "x" * 100000}),
    (GenerateCalendarRequest, {"year": -5}),
    (CreateCycleRequest, {"pattern": [{"label": "zzz", "duration": 4}], "anchor_date": "2026-11-01", "anchor_cycle_day": 1}),
    (CreateCycleRequest, {"pattern": [], "anchor_date": "2026-11-01", "anchor_cycle_day": 1}),
    (UpdateCycleRequest, {"pattern": [{"label": "zzz", "duration": 4}]}),
    (UpdateCycleRequest, {"anchor_date": "9999-01-01"}),
    (UpdateSettingsRequest, {"timezone": "Mars/Olympus"}),
    (UpdateSettingsRequest, {"theme": "neon"}),
    (UpdateProfileRequest, {"timezone": "Mars/Olympus"}),
    (UpdateProfileRequest, {"name": "x" * 500}),
    (ConstraintRequest, {"name": "x", "rule": {}, "weight": -999999}),
    (UndoRequest, {"command_id": "not-a-uuid"}),
])
def test_bad_input_is_refused(model, body):
    with pytest.raises(ValidationError):
        model(**body)


@pytest.mark.parametrize("model, body", [
    (DailyLogCreateRequest, {**LOG, "actual_hours": 7.5, "overtime_hours": 3}),
    (HoursUpdateRequest, {"actual_hours": 0}),
    (IncidentCreateRequest, INCIDENT),
    (CreateCommitmentRequest, {"name": "Gym", "type": "other"}),
    (CreateCommitmentRequest, {"name": "Class", "type": "education", "start_date": "2026-12-01", "end_date": "2026-12-01"}),
    (LeaveBlockRequest, {**LEAVE, "type": "sick"}),
    (LeaveBlockRequest, LEAVE),
    (CreateCycleRequest, {"pattern": [{"label": "work_day", "duration": 4}, {"label": "off", "duration": 4}], "anchor_date": "2026-11-01", "anchor_cycle_day": 1}),
    (UpdateSettingsRequest, {"timezone": "Africa/Accra", "theme": "light"}),
    (UpdateProfileRequest, {"name": "QA 🌙 Tester"}),
    (ConstraintRequest, {"name": "No study on night shifts", "rule": {"type": "no_study_on", "value": "work_night"}}),
])
def test_good_input_still_passes(model, body):
    model(**body)


def test_text_is_trimmed_and_html_is_kept_as_text():
    assert CreateCommitmentRequest(name="  Gym  ", type="personal").name == "Gym"
    assert UpdateProfileRequest(name="<img src=x onerror=alert(1)>").name == "<img src=x onerror=alert(1)>"


def test_leave_reversed_range_is_left_to_the_route_message():
    LeaveBlockRequest(start_date="2026-11-13", end_date="2026-11-12")


@pytest.mark.parametrize("value", ["2026-13-45", "nope", "0001-01-01"])
def test_date_in_the_url_must_be_real(value):
    with pytest.raises(HTTPException) as exc:
        _real_day(value)
    assert exc.value.status_code == 422


def test_real_day_passes():
    assert _real_day("2026-11-12") == "2026-11-12"


def test_other_commitments_are_stored_as_personal():
    from app.engines.proposal_effects import commitment_record

    row = commitment_record("user-1", {"name": "Court date", "type": "other", "date": "2026-12-09"}, "c-1")
    assert row["type"] == "personal"
    assert commitment_record("user-1", {"name": "Class", "type": "education", "date": "2026-12-09"}, "c-2")["type"] == "education"
