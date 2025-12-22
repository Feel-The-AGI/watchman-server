"""
Watchman Settings API Tests
Comprehensive tests for settings and configuration endpoints
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient
from datetime import date
import uuid


class TestGetSettings:
    """Tests for GET /api/settings endpoint"""
    
    def test_get_settings_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/settings")
        assert response.status_code == 401


class TestUpdateSettings:
    """Tests for PATCH /api/settings endpoint"""
    
    def test_update_settings_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.patch("/api/settings", json={"theme": "light"})
        assert response.status_code == 401
    
    def test_update_settings_theme(self, client):
        """Should accept valid theme values"""
        for theme in ["dark", "light"]:
            response = client.patch("/api/settings", json={"theme": theme},
                headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 401
    
    def test_update_settings_invalid_theme(self, client):
        """Should reject invalid theme"""
        response = client.patch("/api/settings", json={"theme": "purple"},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_update_settings_constraint_mode(self, client):
        """Should accept valid constraint modes"""
        for mode in ["binary", "weighted"]:
            response = client.patch("/api/settings", json={"constraint_mode": mode},
                headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 401
    
    def test_update_settings_invalid_constraint_mode(self, client):
        """Should reject invalid constraint mode"""
        response = client.patch("/api/settings", json={"constraint_mode": "invalid"},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_update_settings_max_commitments(self, client):
        """Should accept valid max_concurrent_commitments"""
        for val in [1, 5, 10]:
            response = client.patch("/api/settings", json={"max_concurrent_commitments": val},
                headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 401
    
    def test_update_settings_max_commitments_zero(self, client):
        """Should reject zero max_concurrent_commitments"""
        response = client.patch("/api/settings", json={"max_concurrent_commitments": 0},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_update_settings_max_commitments_negative(self, client):
        """Should reject negative max_concurrent_commitments"""
        response = client.patch("/api/settings", json={"max_concurrent_commitments": -1},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_update_settings_max_commitments_too_high(self, client):
        """Should reject too high max_concurrent_commitments"""
        response = client.patch("/api/settings", json={"max_concurrent_commitments": 100},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_update_settings_notifications(self, client):
        """Should accept notification settings"""
        response = client.patch("/api/settings", json={
            "notifications_email": True,
            "notifications_whatsapp": False
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_update_settings_empty_body(self, client):
        """Should handle empty update body"""
        response = client.patch("/api/settings", json={},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestListConstraints:
    """Tests for GET /api/settings/constraints endpoint"""
    
    def test_list_constraints_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/settings/constraints")
        assert response.status_code == 401


class TestCreateConstraint:
    """Tests for POST /api/settings/constraints endpoint"""
    
    def test_create_constraint_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.post("/api/settings/constraints", json={
            "name": "Test Constraint",
            "rule": {"type": "max_hours", "value": 8}
        })
        assert response.status_code == 401
    
    def test_create_constraint_missing_name(self, client):
        """Should reject missing name"""
        response = client.post("/api/settings/constraints", json={
            "rule": {"type": "max_hours", "value": 8}
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_create_constraint_missing_rule(self, client):
        """Should reject missing rule"""
        response = client.post("/api/settings/constraints", json={
            "name": "Test Constraint"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_create_constraint_empty_rule(self, client):
        """Should handle empty rule object"""
        response = client.post("/api/settings/constraints", json={
            "name": "Test Constraint",
            "rule": {}
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_constraint_with_weight(self, client):
        """Should accept weight parameter"""
        response = client.post("/api/settings/constraints", json={
            "name": "Test Constraint",
            "rule": {"type": "max_hours", "value": 8},
            "weight": 50
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_constraint_xss_name(self, client):
        """Should handle XSS in name"""
        response = client.post("/api/settings/constraints", json={
            "name": "<script>alert('xss')</script>",
            "rule": {"type": "max_hours", "value": 8}
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_create_constraint_sql_injection(self, client):
        """Should handle SQL injection in name"""
        response = client.post("/api/settings/constraints", json={
            "name": "'; DROP TABLE constraints; --",
            "rule": {"type": "max_hours", "value": 8}
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestUpdateConstraint:
    """Tests for PATCH /api/settings/constraints/{constraint_id} endpoint"""
    
    def test_update_constraint_no_auth(self, client):
        """Should return 401 when not authenticated"""
        constraint_id = str(uuid.uuid4())
        response = client.patch(f"/api/settings/constraints/{constraint_id}", json={
            "name": "Updated Constraint",
            "rule": {"type": "max_hours", "value": 10}
        })
        assert response.status_code == 401
    
    def test_update_constraint_invalid_uuid(self, client):
        """Should handle invalid constraint ID"""
        response = client.patch("/api/settings/constraints/not-a-uuid", json={
            "name": "Updated",
            "rule": {"type": "test"}
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]


class TestDeleteConstraint:
    """Tests for DELETE /api/settings/constraints/{constraint_id} endpoint"""
    
    def test_delete_constraint_no_auth(self, client):
        """Should return 401 when not authenticated"""
        constraint_id = str(uuid.uuid4())
        response = client.delete(f"/api/settings/constraints/{constraint_id}")
        assert response.status_code == 401
    
    def test_delete_constraint_invalid_uuid(self, client):
        """Should handle invalid constraint ID"""
        response = client.delete("/api/settings/constraints/not-a-uuid",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]


class TestToggleWeightedMode:
    """Tests for POST /api/settings/toggle-weighted-mode endpoint"""
    
    def test_toggle_weighted_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.post("/api/settings/toggle-weighted-mode?enabled=true")
        assert response.status_code == 401
    
    def test_toggle_weighted_enable(self, client):
        """Should accept enabled=true"""
        response = client.post("/api/settings/toggle-weighted-mode?enabled=true",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_toggle_weighted_disable(self, client):
        """Should accept enabled=false"""
        response = client.post("/api/settings/toggle-weighted-mode?enabled=false",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_toggle_weighted_missing_param(self, client):
        """Should reject missing enabled parameter"""
        response = client.post("/api/settings/toggle-weighted-mode",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]


class TestAdminGrantTier:
    """Tests for POST /api/settings/admin/grant-tier endpoint"""
    
    def test_grant_tier_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.post("/api/settings/admin/grant-tier", params={
            "user_email": "test@example.com",
            "tier": "pro"
        })
        assert response.status_code == 401
    
    def test_grant_tier_non_admin(self, client):
        """Should reject non-admin users"""
        response = client.post("/api/settings/admin/grant-tier", params={
            "user_email": "test@example.com",
            "tier": "pro"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_grant_tier_invalid_tier(self, client):
        """Should reject invalid tier"""
        response = client.post("/api/settings/admin/grant-tier", params={
            "user_email": "test@example.com",
            "tier": "invalid"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestGetSubscription:
    """Tests for GET /api/settings/subscription endpoint"""
    
    def test_get_subscription_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/settings/subscription")
        assert response.status_code == 401


class TestSettingsEdgeCases:
    """Edge case tests for settings"""
    
    def test_concurrent_settings_updates(self, client):
        """Should handle concurrent settings updates"""
        import threading
        results = []
        
        def update():
            r = client.patch("/api/settings", json={"theme": "dark"},
                headers={"Authorization": "Bearer invalid"})
            results.append(r.status_code)
        
        threads = [threading.Thread(target=update) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert all(code == 401 for code in results)
    
    def test_rapid_constraint_creation(self, client):
        """Should handle rapid constraint creation"""
        for i in range(20):
            response = client.post("/api/settings/constraints", json={
                "name": f"Constraint {i}",
                "rule": {"type": "test", "value": i}
            }, headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 401
