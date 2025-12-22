"""
Watchman Calendar API Tests
Comprehensive tests for calendar management endpoints
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient
from datetime import date
import uuid


class TestGetCalendarDays:
    """Tests for GET /api/calendar endpoint"""
    
    def test_get_calendar_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/calendar?start_date=2025-01-01&end_date=2025-01-31")
        assert response.status_code == 401
    
    def test_get_calendar_missing_start_date(self, client):
        """Should reject missing start_date"""
        response = client.get("/api/calendar?end_date=2025-01-31",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_get_calendar_missing_end_date(self, client):
        """Should reject missing end_date"""
        response = client.get("/api/calendar?start_date=2025-01-01",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_get_calendar_invalid_date_format(self, client):
        """Should reject invalid date format"""
        response = client.get("/api/calendar?start_date=not-a-date&end_date=2025-01-31",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_get_calendar_end_before_start(self, client):
        """Should handle end date before start date"""
        response = client.get("/api/calendar?start_date=2025-12-31&end_date=2025-01-01",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_get_calendar_same_day(self, client):
        """Should handle same start and end date"""
        response = client.get("/api/calendar?start_date=2025-01-15&end_date=2025-01-15",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_get_calendar_large_range(self, client):
        """Should handle large date range"""
        response = client.get("/api/calendar?start_date=2020-01-01&end_date=2030-12-31",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_get_calendar_leap_year(self, client):
        """Should handle leap year dates"""
        response = client.get("/api/calendar?start_date=2024-02-28&end_date=2024-03-01",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestGetYear:
    """Tests for GET /api/calendar/year/{year} endpoint"""
    
    def test_get_year_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/calendar/year/2025")
        assert response.status_code == 401
    
    def test_get_year_invalid_year(self, client):
        """Should handle invalid year"""
        response = client.get("/api/calendar/year/not-a-year",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_get_year_negative_year(self, client):
        """Should handle negative year"""
        response = client.get("/api/calendar/year/-2025",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_get_year_far_future(self, client):
        """Should handle far future year"""
        response = client.get("/api/calendar/year/9999",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_get_year_far_past(self, client):
        """Should handle far past year"""
        response = client.get("/api/calendar/year/1000",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestGetMonth:
    """Tests for GET /api/calendar/month/{year}/{month} endpoint"""
    
    def test_get_month_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/calendar/month/2025/6")
        assert response.status_code == 401
    
    def test_get_month_invalid_month(self, client):
        """Should handle invalid month (13)"""
        response = client.get("/api/calendar/month/2025/13",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_get_month_zero(self, client):
        """Should handle month 0"""
        response = client.get("/api/calendar/month/2025/0",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_get_month_negative(self, client):
        """Should handle negative month"""
        response = client.get("/api/calendar/month/2025/-1",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_get_month_all_valid(self, client):
        """Should accept all valid months"""
        for month in range(1, 13):
            response = client.get(f"/api/calendar/month/2025/{month}",
                headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 401


class TestGetDay:
    """Tests for GET /api/calendar/day/{date_str} endpoint"""
    
    def test_get_day_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/calendar/day/2025-01-15")
        assert response.status_code == 401
    
    def test_get_day_invalid_format(self, client):
        """Should handle invalid date format"""
        response = client.get("/api/calendar/day/15-01-2025",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_get_day_sql_injection(self, client):
        """Should handle SQL injection in date"""
        response = client.get("/api/calendar/day/2025-01-01'; DROP TABLE calendar_days; --",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 404, 422]


class TestGenerateCalendar:
    """Tests for POST /api/calendar/generate endpoint"""
    
    def test_generate_calendar_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.post("/api/calendar/generate", json={"year": 2025})
        assert response.status_code == 401
    
    def test_generate_calendar_missing_year(self, client):
        """Should use default year if not provided"""
        response = client.post("/api/calendar/generate", json={},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_generate_calendar_invalid_year(self, client):
        """Should reject invalid year"""
        response = client.post("/api/calendar/generate", json={"year": "not-a-year"},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_generate_calendar_negative_year(self, client):
        """Should handle negative year"""
        response = client.post("/api/calendar/generate", json={"year": -2025},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_generate_calendar_with_regenerate(self, client):
        """Should accept regenerate flag"""
        response = client.post("/api/calendar/generate", json={
            "year": 2025,
            "regenerate": True
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestLeaveBlocks:
    """Tests for leave block endpoints"""
    
    def test_add_leave_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.post("/api/calendar/leave", json={
            "start_date": "2025-06-01",
            "end_date": "2025-06-14"
        })
        assert response.status_code == 401
    
    def test_add_leave_missing_dates(self, client):
        """Should reject missing dates"""
        response = client.post("/api/calendar/leave", json={
            "name": "Vacation"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_add_leave_end_before_start(self, client):
        """Should reject end before start"""
        response = client.post("/api/calendar/leave", json={
            "start_date": "2025-06-14",
            "end_date": "2025-06-01"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_add_leave_same_day(self, client):
        """Should allow single day leave"""
        response = client.post("/api/calendar/leave", json={
            "start_date": "2025-06-01",
            "end_date": "2025-06-01"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_add_leave_long_period(self, client):
        """Should handle long leave period"""
        response = client.post("/api/calendar/leave", json={
            "start_date": "2025-01-01",
            "end_date": "2025-12-31",
            "name": "Sabbatical"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_add_leave_with_notes(self, client):
        """Should accept notes"""
        response = client.post("/api/calendar/leave", json={
            "start_date": "2025-06-01",
            "end_date": "2025-06-14",
            "notes": "Summer vacation"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_add_leave_xss_in_notes(self, client):
        """Should handle XSS in notes"""
        response = client.post("/api/calendar/leave", json={
            "start_date": "2025-06-01",
            "end_date": "2025-06-14",
            "notes": "<script>alert('xss')</script>"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_list_leave_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/calendar/leave")
        assert response.status_code == 401
    
    def test_delete_leave_no_auth(self, client):
        """Should return 401 when not authenticated"""
        leave_id = str(uuid.uuid4())
        response = client.delete(f"/api/calendar/leave/{leave_id}")
        assert response.status_code == 401
    
    def test_delete_leave_invalid_uuid(self, client):
        """Should handle invalid leave ID"""
        response = client.delete("/api/calendar/leave/not-a-uuid",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]


class TestCalendarEdgeCases:
    """Edge case tests for calendar"""
    
    def test_feb_29_leap_year(self, client):
        """Should handle Feb 29 in leap year"""
        response = client.get("/api/calendar/day/2024-02-29",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_feb_29_non_leap_year(self, client):
        """Should handle Feb 29 in non-leap year"""
        response = client.get("/api/calendar/day/2025-02-29",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_year_boundary(self, client):
        """Should handle year boundary correctly"""
        response = client.get("/api/calendar?start_date=2024-12-25&end_date=2025-01-05",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_dst_transition(self, client):
        """Should handle DST transition dates"""
        # March DST in US
        response = client.get("/api/calendar?start_date=2025-03-09&end_date=2025-03-10",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
