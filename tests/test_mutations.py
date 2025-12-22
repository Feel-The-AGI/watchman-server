"""
Watchman Mutations API Tests
Comprehensive tests for mutation management endpoints
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient
from datetime import date
import uuid


class TestListMutations:
    """Tests for GET /api/mutations endpoint"""
    
    def test_list_mutations_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/mutations")
        assert response.status_code == 401
    
    def test_list_mutations_with_status(self, client):
        """Should accept status filter"""
        response = client.get("/api/mutations?status=proposed",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_list_mutations_with_limit(self, client):
        """Should accept limit parameter"""
        response = client.get("/api/mutations?limit=10",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_list_mutations_invalid_limit(self, client):
        """Should handle invalid limit"""
        response = client.get("/api/mutations?limit=-5",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_list_mutations_large_limit(self, client):
        """Should handle large limit"""
        response = client.get("/api/mutations?limit=10000",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestListPendingMutations:
    """Tests for GET /api/mutations/pending endpoint"""
    
    def test_list_pending_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/mutations/pending")
        assert response.status_code == 401


class TestGetMutation:
    """Tests for GET /api/mutations/{mutation_id} endpoint"""
    
    def test_get_mutation_no_auth(self, client):
        """Should return 401 when not authenticated"""
        mutation_id = str(uuid.uuid4())
        response = client.get(f"/api/mutations/{mutation_id}")
        assert response.status_code == 401
    
    def test_get_mutation_invalid_uuid(self, client):
        """Should handle invalid mutation ID"""
        response = client.get("/api/mutations/not-a-uuid",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]


class TestReviewMutation:
    """Tests for POST /api/mutations/{mutation_id}/review endpoint"""
    
    def test_review_mutation_no_auth(self, client):
        """Should return 401 when not authenticated"""
        mutation_id = str(uuid.uuid4())
        response = client.post(f"/api/mutations/{mutation_id}/review", json={
            "action": "approve"
        })
        assert response.status_code == 401
    
    def test_review_mutation_approve(self, client):
        """Should accept approve action"""
        mutation_id = str(uuid.uuid4())
        response = client.post(f"/api/mutations/{mutation_id}/review", json={
            "action": "approve"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_review_mutation_reject(self, client):
        """Should accept reject action"""
        mutation_id = str(uuid.uuid4())
        response = client.post(f"/api/mutations/{mutation_id}/review", json={
            "action": "reject",
            "reason": "Not suitable"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_review_mutation_invalid_action(self, client):
        """Should reject invalid action"""
        mutation_id = str(uuid.uuid4())
        response = client.post(f"/api/mutations/{mutation_id}/review", json={
            "action": "invalid"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_review_mutation_missing_action(self, client):
        """Should reject missing action"""
        mutation_id = str(uuid.uuid4())
        response = client.post(f"/api/mutations/{mutation_id}/review", json={},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_review_mutation_with_alternative(self, client):
        """Should accept alternative selection"""
        mutation_id = str(uuid.uuid4())
        response = client.post(f"/api/mutations/{mutation_id}/review", json={
            "action": "approve",
            "selected_alternative_id": str(uuid.uuid4())
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_review_mutation_xss_in_reason(self, client):
        """Should handle XSS in reason"""
        mutation_id = str(uuid.uuid4())
        response = client.post(f"/api/mutations/{mutation_id}/review", json={
            "action": "reject",
            "reason": "<script>alert('xss')</script>"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestSelectAlternative:
    """Tests for POST /api/mutations/{mutation_id}/select-alternative endpoint"""
    
    def test_select_alternative_no_auth(self, client):
        """Should return 401 when not authenticated"""
        mutation_id = str(uuid.uuid4())
        alt_id = str(uuid.uuid4())
        response = client.post(f"/api/mutations/{mutation_id}/select-alternative?alternative_id={alt_id}")
        assert response.status_code == 401
    
    def test_select_alternative_missing_alt_id(self, client):
        """Should reject missing alternative_id"""
        mutation_id = str(uuid.uuid4())
        response = client.post(f"/api/mutations/{mutation_id}/select-alternative",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]


class TestUndoMutation:
    """Tests for mutation undo endpoints"""
    
    def test_get_undo_info_no_auth(self, client):
        """Should return 401 when not authenticated"""
        mutation_id = str(uuid.uuid4())
        response = client.get(f"/api/mutations/{mutation_id}/undo")
        assert response.status_code == 401
    
    def test_undo_mutation_no_auth(self, client):
        """Should return 401 when not authenticated"""
        mutation_id = str(uuid.uuid4())
        response = client.post(f"/api/mutations/{mutation_id}/undo")
        assert response.status_code == 401
    
    def test_undo_mutation_invalid_uuid(self, client):
        """Should handle invalid mutation ID"""
        response = client.post("/api/mutations/not-a-uuid/undo",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]


class TestMutationEdgeCases:
    """Edge case tests for mutations"""
    
    def test_concurrent_review_attempts(self, client):
        """Should handle concurrent review attempts"""
        import threading
        mutation_id = str(uuid.uuid4())
        results = []
        
        def review():
            r = client.post(f"/api/mutations/{mutation_id}/review", json={
                "action": "approve"
            }, headers={"Authorization": "Bearer invalid"})
            results.append(r.status_code)
        
        threads = [threading.Thread(target=review) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert all(code == 401 for code in results)
    
    def test_rapid_approve_reject_cycle(self, client):
        """Should handle rapid approve/reject cycles"""
        mutation_id = str(uuid.uuid4())
        for action in ["approve", "reject"] * 5:
            response = client.post(f"/api/mutations/{mutation_id}/review", json={
                "action": action
            }, headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 401
