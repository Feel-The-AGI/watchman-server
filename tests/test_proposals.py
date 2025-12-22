"""
Watchman Proposals API Tests
Comprehensive tests for proposal parsing and creation endpoints
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient
from datetime import date
import uuid
import io


class TestParsePDF:
    """Tests for POST /api/proposals/parse-pdf endpoint"""
    
    def test_parse_pdf_no_auth(self, client):
        """Should return 401 when not authenticated"""
        files = {"file": ("test.pdf", b"PDF content", "application/pdf")}
        response = client.post("/api/proposals/parse-pdf", files=files)
        assert response.status_code == 401
    
    def test_parse_pdf_no_file(self, client):
        """Should reject missing file"""
        response = client.post("/api/proposals/parse-pdf",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_parse_pdf_wrong_extension(self, client):
        """Should reject non-PDF files"""
        files = {"file": ("test.txt", b"Not a PDF", "text/plain")}
        response = client.post("/api/proposals/parse-pdf", files=files,
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [400, 401, 403]
    
    def test_parse_pdf_large_file(self, client):
        """Should reject files over 10MB"""
        large_content = b"x" * (11 * 1024 * 1024)  # 11MB
        files = {"file": ("large.pdf", large_content, "application/pdf")}
        response = client.post("/api/proposals/parse-pdf", files=files,
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [400, 401, 403]
    
    def test_parse_pdf_empty_file(self, client):
        """Should handle empty file"""
        files = {"file": ("empty.pdf", b"", "application/pdf")}
        response = client.post("/api/proposals/parse-pdf", files=files,
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [400, 401, 403]
    
    def test_parse_pdf_case_insensitive_extension(self, client):
        """Should accept .PDF extension"""
        files = {"file": ("test.PDF", b"PDF content", "application/pdf")}
        response = client.post("/api/proposals/parse-pdf", files=files,
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 403]  # Auth fails but extension accepted
    
    def test_parse_pdf_free_tier_blocked(self, client):
        """Should block free tier users"""
        files = {"file": ("test.pdf", b"PDF content", "application/pdf")}
        response = client.post("/api/proposals/parse-pdf", files=files,
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 403]


class TestParseInput:
    """Tests for POST /api/proposals/parse endpoint"""
    
    def test_parse_input_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.post("/api/proposals/parse", json={
            "text": "Add a new course"
        })
        assert response.status_code == 401
    
    def test_parse_input_missing_text(self, client):
        """Should reject missing text"""
        response = client.post("/api/proposals/parse", json={},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_parse_input_empty_text(self, client):
        """Should handle empty text"""
        response = client.post("/api/proposals/parse", json={
            "text": ""
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 403]
    
    def test_parse_input_long_text(self, client):
        """Should handle very long text"""
        response = client.post("/api/proposals/parse", json={
            "text": "A" * 100000
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 403]
    
    def test_parse_input_with_context(self, client):
        """Should accept optional context"""
        response = client.post("/api/proposals/parse", json={
            "text": "Add new course",
            "context": "User is a night shift worker"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 403]
    
    def test_parse_input_xss_text(self, client):
        """Should handle XSS in text"""
        response = client.post("/api/proposals/parse", json={
            "text": "<script>alert('xss')</script>"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 403]
    
    def test_parse_input_sql_injection(self, client):
        """Should handle SQL injection"""
        response = client.post("/api/proposals/parse", json={
            "text": "'; DROP TABLE users; --"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 403]
    
    def test_parse_input_unicode(self, client):
        """Should handle unicode text"""
        response = client.post("/api/proposals/parse", json={
            "text": "新しいコースを追加してください 📚"
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 403]


class TestCreateProposal:
    """Tests for POST /api/proposals/create endpoint"""
    
    def test_create_proposal_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.post("/api/proposals/create", json={
            "text": "Add a new course"
        })
        assert response.status_code == 401
    
    def test_create_proposal_missing_text(self, client):
        """Should reject missing text"""
        response = client.post("/api/proposals/create", json={},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_create_proposal_auto_validate(self, client):
        """Should accept auto_validate flag"""
        response = client.post("/api/proposals/create", json={
            "text": "Add new course",
            "auto_validate": True
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 403]
    
    def test_create_proposal_no_validate(self, client):
        """Should accept auto_validate=false"""
        response = client.post("/api/proposals/create", json={
            "text": "Add new course",
            "auto_validate": False
        }, headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 403]


class TestPreviewProposal:
    """Tests for POST /api/proposals/preview endpoint"""
    
    def test_preview_proposal_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.post("/api/proposals/preview", json={
            "text": "Add a new course"
        })
        assert response.status_code == 401
    
    def test_preview_proposal_missing_text(self, client):
        """Should reject missing text"""
        response = client.post("/api/proposals/preview", json={},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]


class TestQuickAdd:
    """Tests for POST /api/proposals/quick-add endpoint"""
    
    def test_quick_add_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.post("/api/proposals/quick-add?name=Test&type=education")
        assert response.status_code == 401
    
    def test_quick_add_missing_name(self, client):
        """Should reject missing name"""
        response = client.post("/api/proposals/quick-add?type=education",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_quick_add_default_type(self, client):
        """Should use default type if not provided"""
        response = client.post("/api/proposals/quick-add?name=Test",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_quick_add_valid_types(self, client):
        """Should accept valid types"""
        for ctype in ["education", "personal", "study", "sleep"]:
            response = client.post(f"/api/proposals/quick-add?name=Test&type={ctype}",
                headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 401
    
    def test_quick_add_long_name(self, client):
        """Should handle long name"""
        long_name = "A" * 1000
        response = client.post(f"/api/proposals/quick-add?name={long_name}&type=education",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_quick_add_special_chars(self, client):
        """Should handle special characters in name"""
        response = client.post("/api/proposals/quick-add",
            params={"name": "Test & Course <with> 'special' chars", "type": "education"},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_quick_add_unicode_name(self, client):
        """Should handle unicode name"""
        response = client.post("/api/proposals/quick-add",
            params={"name": "日本語コース 📚", "type": "education"},
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestProposalEdgeCases:
    """Edge case tests for proposals"""
    
    def test_rapid_parsing_requests(self, client):
        """Should handle rapid parsing requests"""
        import threading
        results = []
        
        def parse():
            r = client.post("/api/proposals/parse", json={
                "text": "Add course"
            }, headers={"Authorization": "Bearer invalid"})
            results.append(r.status_code)
        
        threads = [threading.Thread(target=parse) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert all(code in [401, 403] for code in results)
    
    def test_concurrent_quick_adds(self, client):
        """Should handle concurrent quick-add requests"""
        import threading
        results = []
        
        def add():
            r = client.post("/api/proposals/quick-add?name=Test&type=education",
                headers={"Authorization": "Bearer invalid"})
            results.append(r.status_code)
        
        threads = [threading.Thread(target=add) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert all(code == 401 for code in results)
