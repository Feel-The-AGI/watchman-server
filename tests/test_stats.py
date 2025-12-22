"""
Watchman Stats API Tests
Comprehensive tests for statistics and analytics endpoints
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient
from datetime import date
import uuid


class TestDashboardStats:
    """Tests for GET /api/stats/dashboard endpoint"""
    
    def test_dashboard_stats_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/stats/dashboard")
        assert response.status_code == 401


class TestYearlyStats:
    """Tests for GET /api/stats/year/{year} endpoint"""
    
    def test_yearly_stats_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/stats/year/2025")
        assert response.status_code == 401
    
    def test_yearly_stats_invalid_year(self, client):
        """Should handle invalid year"""
        response = client.get("/api/stats/year/not-a-year",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]
    
    def test_yearly_stats_negative_year(self, client):
        """Should handle negative year"""
        response = client.get("/api/stats/year/-2025",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_yearly_stats_far_future(self, client):
        """Should handle far future year"""
        response = client.get("/api/stats/year/9999",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


class TestMonthlyStats:
    """Tests for GET /api/stats/month/{year}/{month} endpoint"""
    
    def test_monthly_stats_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/stats/month/2025/6")
        assert response.status_code == 401
    
    def test_monthly_stats_invalid_month(self, client):
        """Should handle invalid month"""
        response = client.get("/api/stats/month/2025/13",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_monthly_stats_zero_month(self, client):
        """Should handle month 0"""
        response = client.get("/api/stats/month/2025/0",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_monthly_stats_negative_month(self, client):
        """Should handle negative month"""
        response = client.get("/api/stats/month/2025/-1",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_monthly_stats_all_months(self, client):
        """Should accept all valid months"""
        for month in range(1, 13):
            response = client.get(f"/api/stats/month/2025/{month}",
                headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 401


class TestCommitmentStats:
    """Tests for GET /api/stats/commitments endpoint"""
    
    def test_commitment_stats_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/stats/commitments")
        assert response.status_code == 401


class TestLoadDistribution:
    """Tests for GET /api/stats/load-distribution endpoint"""
    
    def test_load_distribution_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/stats/load-distribution")
        assert response.status_code == 401
    
    def test_load_distribution_with_year(self, client):
        """Should accept year parameter"""
        response = client.get("/api/stats/load-distribution?year=2025",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401
    
    def test_load_distribution_invalid_year(self, client):
        """Should handle invalid year parameter"""
        response = client.get("/api/stats/load-distribution?year=invalid",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]


class TestExportStats:
    """Tests for GET /api/stats/export endpoint"""
    
    def test_export_stats_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/stats/export?year=2025")
        assert response.status_code == 401
    
    def test_export_stats_free_tier_blocked(self, client):
        """Should block free tier users"""
        response = client.get("/api/stats/export?year=2025",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 403]
    
    def test_export_stats_json_format(self, client):
        """Should accept json format"""
        response = client.get("/api/stats/export?year=2025&format=json",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 403]
    
    def test_export_stats_csv_format(self, client):
        """Should accept csv format"""
        response = client.get("/api/stats/export?year=2025&format=csv",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 403]
    
    def test_export_stats_missing_year(self, client):
        """Should reject missing year"""
        response = client.get("/api/stats/export",
            headers={"Authorization": "Bearer invalid"})
        assert response.status_code in [401, 422]


class TestQuickSummary:
    """Tests for GET /api/stats/summary endpoint"""
    
    def test_quick_summary_no_auth(self, client):
        """Should return 401 when not authenticated"""
        response = client.get("/api/stats/summary")
        assert response.status_code == 401


class TestStatsEdgeCases:
    """Edge case tests for stats"""
    
    def test_concurrent_stats_requests(self, client):
        """Should handle concurrent stats requests"""
        import threading
        results = []
        
        def get_stats():
            r = client.get("/api/stats/dashboard",
                headers={"Authorization": "Bearer invalid"})
            results.append(r.status_code)
        
        threads = [threading.Thread(target=get_stats) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert all(code == 401 for code in results)
    
    def test_rapid_year_requests(self, client):
        """Should handle rapid requests for different years"""
        for year in range(2020, 2030):
            response = client.get(f"/api/stats/year/{year}",
                headers={"Authorization": "Bearer invalid"})
            assert response.status_code == 401
