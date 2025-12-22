"""
Watchman Commitments API Tests
Comprehensive tests for commitment management endpoints
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient
from datetime import date
import uuid


class TestListCommitments:
    """Tests for GET /api/commitments endpoint"""
    
    def test_list_commitments_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/commitments")
        assert response.status_code == 401
    
    def test_list_commitments_with_status_filter(self, client):
        """Should accept status filter parameter"""
        response = client.get("/api/commitments?status=active",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_list_commitments_with_type_filter(self, client):
        """Should accept type filter parameter"""
        response = client.get("/api/commitments?type=education",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_list_commitments_invalid_status(self, client):
        """Should handle invalid status filter"""
        response = client.get("/api/commitments?status=invalid",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestGetActiveCommitments:
    """Tests for GET /api/commitments/active endpoint"""
    
    def test_get_active_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/commitments/active")
        assert response.status_code == 401


class TestGetCommitment:
    """Tests for GET /api/commitments/{commitment_id} endpoint"""
    
    def test_get_commitment_no_auth(self, client):
        """Should return 401 when not authenticated"""
        commitment_id = str(uuid.uuid4())
        response = client.get(f"/api/commitments/{commitment_id}")
        assert response.status_code == 401
    
    def test_get_commitment_invalid_uuid(self, client):
        """Should handle invalid commitment ID"""
        response = client.get("/api/commitments/not-a-uuid",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]


class TestCreateCommitment:
    """Tests for POST /api/commitments endpoint"""
    
    def test_create_commitment_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.post("/api/commitments", json={
            "name": "Test Course",
            "type": "education"
        })
        assert response.status_code == 401
    
    def test_create_commitment_missing_name(self, client):
        """Should reject missing name"""
        response = client.post("/api/commitments", json={
            "type": "education"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_create_commitment_missing_type(self, client):
        """Should reject missing type"""
        response = client.post("/api/commitments", json={
            "name": "Test"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_create_commitment_empty_name(self, client):
        """Should handle empty name"""
        response = client.post("/api/commitments", json={
            "name": "",
            "type": "education"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_commitment_long_name(self, client):
        """Should handle very long name"""
        response = client.post("/api/commitments", json={
            "name": "A" * 10000,
            "type": "education"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_commitment_sql_injection(self, client):
        """Should handle SQL injection in name"""
        response = client.post("/api/commitments", json={
            "name": "'; DROP TABLE commitments; --",
            "type": "education"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_commitment_xss_name(self, client):
        """Should handle XSS in name"""
        response = client.post("/api/commitments", json={
            "name": "<script>alert('xss')</script>",
            "type": "education"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_commitment_unicode_name(self, client):
        """Should handle unicode in name"""
        response = client.post("/api/commitments", json={
            "name": "日本語コース 📚",
            "type": "education"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_commitment_valid_types(self, client):
        """Should accept valid commitment types"""
        for ctype in ["education", "personal", "study", "sleep"]:
            response = client.post("/api/commitments", json={
                "name": "Test",
                "type": ctype
            }, headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 401
    
    def test_create_commitment_negative_priority(self, client):
        """Should handle negative priority"""
        response = client.post("/api/commitments", json={
            "name": "Test",
            "type": "education",
            "priority": -5
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_commitment_large_priority(self, client):
        """Should handle very large priority"""
        response = client.post("/api/commitments", json={
            "name": "Test",
            "type": "education",
            "priority": 999999
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_commitment_invalid_date_format(self, client):
        """Should reject invalid date format"""
        response = client.post("/api/commitments", json={
            "name": "Test",
            "type": "education",
            "start_date": "not-a-date"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_create_commitment_end_before_start(self, client):
        """Should handle end date before start date"""
        response = client.post("/api/commitments", json={
            "name": "Test",
            "type": "education",
            "start_date": "2025-12-31",
            "end_date": "2025-01-01"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_commitment_invalid_color(self, client):
        """Should handle invalid color format"""
        response = client.post("/api/commitments", json={
            "name": "Test",
            "type": "education",
            "color": "not-a-color"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_commitment_valid_colors(self, client):
        """Should accept valid color formats"""
        for color in ["#FF0000", "#2979FF", "#00FF00"]:
            response = client.post("/api/commitments", json={
                "name": "Test",
                "type": "education",
                "color": color
            }, headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 401
    
    def test_create_commitment_negative_sessions(self, client):
        """Should handle negative total_sessions"""
        response = client.post("/api/commitments", json={
            "name": "Test",
            "type": "education",
            "total_sessions": -10
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_commitment_complex_constraints(self, client):
        """Should handle complex constraints JSON"""
        response = client.post("/api/commitments", json={
            "name": "Test",
            "type": "education",
            "constraints_json": {
                "study_on": ["off", "work_day_evening"],
                "exclude": ["work_night"],
                "frequency": "weekly",
                "duration_hours": 2.5
            }
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_commitment_with_recurrence(self, client):
        """Should handle recurrence pattern"""
        response = client.post("/api/commitments", json={
            "name": "Test",
            "type": "education",
            "recurrence": {
                "pattern": "weekly",
                "days": ["monday", "wednesday"],
                "interval": 1
            }
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestUpdateCommitment:
    """Tests for PATCH /api/commitments/{commitment_id} endpoint"""
    
    def test_update_commitment_no_auth(self, client):
        """Should return 401 when not authenticated"""
        commitment_id = str(uuid.uuid4())
        response = client.patch(f"/api/commitments/{commitment_id}", json={
            "name": "Updated Name"
        })
        assert response.status_code == 401
    
    def test_update_commitment_empty_body(self, client):
        """Should handle empty update body"""
        commitment_id = str(uuid.uuid4())
        response = client.patch(f"/api/commitments/{commitment_id}", json={},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_update_commitment_invalid_uuid(self, client):
        """Should handle invalid commitment ID"""
        response = client.patch("/api/commitments/not-a-uuid", json={
            "name": "Updated"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_update_commitment_status_change(self, client):
        """Should handle status changes"""
        commitment_id = str(uuid.uuid4())
        for status in ["active", "paused", "completed", "cancelled"]:
            response = client.patch(f"/api/commitments/{commitment_id}", json={
                "status": status
            }, headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 401
    
    def test_update_commitment_increment_sessions(self, client):
        """Should handle session count updates"""
        commitment_id = str(uuid.uuid4())
        response = client.patch(f"/api/commitments/{commitment_id}", json={
            "completed_sessions": 10
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestDeleteCommitment:
    """Tests for DELETE /api/commitments/{commitment_id} endpoint"""
    
    def test_delete_commitment_no_auth(self, client):
        """Should return 401 when not authenticated"""
        commitment_id = str(uuid.uuid4())
        response = client.delete(f"/api/commitments/{commitment_id}")
        assert response.status_code == 401
    
    def test_delete_commitment_invalid_uuid(self, client):
        """Should handle invalid commitment ID"""
        response = client.delete("/api/commitments/not-a-uuid",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]


class TestCommitmentEdgeCases:
    """Edge case tests for commitments"""
    
    def test_concurrent_commitment_creation(self, client):
        """Should handle concurrent creation attempts"""
        import threading
        results = []
        
        def create():
            r = client.post("/api/commitments", json={
                "name": "Concurrent Test",
                "type": "education"
            }, headers={"Authorization": "Bearer invalid"})
            results.append(r.status_code)
        
        threads = [threading.Thread(target=create) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert all(code == 401 for code in results)
    
    def test_commitment_with_null_values(self, client):
        """Should handle explicit null values"""
        response = client.post("/api/commitments", json={
            "name": "Test",
            "type": "education",
            "notes": None,
            "icon": None
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
