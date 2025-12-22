"""
Watchman Test Configuration
Shared fixtures and mocks for all tests
"""

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import date, datetime
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport
import uuid

# Mock settings before importing app
@pytest.fixture(scope="session", autouse=True)
def mock_settings():
    """Mock settings for all tests"""
    with patch("app.config.get_settings") as mock:
        mock.return_value = MagicMock(
            app_env="test",
            debug=True,
            host="0.0.0.0",
            port=8000,
            supabase_url="https://test.supabase.co",
            supabase_anon_key="test-anon-key",
            supabase_service_key="test-service-key",
            supabase_jwt_secret="test-jwt-secret-at-least-32-characters",
            gemini_api_key="test-gemini-key",
            cors_origins="http://localhost:3000",
            cors_origins_list=["http://localhost:3000"]
        )
        yield mock


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ==========================================
# Mock User Fixtures
# ==========================================

@pytest.fixture
def mock_free_user():
    """Mock free tier user"""
    return {
        "id": str(uuid.uuid4()),
        "auth_id": str(uuid.uuid4()),
        "email": "free@example.com",
        "name": "Free User",
        "tier": "free",
        "role": "user",
        "timezone": "UTC",
        "onboarding_completed": True,
        "settings": {
            "max_concurrent_commitments": 2,
            "theme": "dark",
            "constraint_mode": "binary"
        }
    }


@pytest.fixture
def mock_pro_user():
    """Mock pro tier user"""
    return {
        "id": str(uuid.uuid4()),
        "auth_id": str(uuid.uuid4()),
        "email": "pro@example.com",
        "name": "Pro User",
        "tier": "pro",
        "role": "user",
        "timezone": "America/New_York",
        "onboarding_completed": True,
        "settings": {
            "max_concurrent_commitments": 5,
            "theme": "light",
            "constraint_mode": "weighted"
        }
    }


@pytest.fixture
def mock_admin_user():
    """Mock admin user"""
    return {
        "id": str(uuid.uuid4()),
        "auth_id": str(uuid.uuid4()),
        "email": "admin@example.com",
        "name": "Admin User",
        "tier": "admin",
        "role": "admin",
        "timezone": "UTC",
        "onboarding_completed": True,
        "settings": {}
    }


@pytest.fixture
def mock_new_user():
    """Mock new user who hasn't completed onboarding"""
    return {
        "id": str(uuid.uuid4()),
        "auth_id": str(uuid.uuid4()),
        "email": "new@example.com",
        "name": "New User",
        "tier": "free",
        "role": "user",
        "timezone": "UTC",
        "onboarding_completed": False,
        "settings": {}
    }


# ==========================================
# Mock Data Fixtures
# ==========================================

@pytest.fixture
def mock_cycle():
    """Mock rotation cycle"""
    return {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "name": "Test Rotation",
        "pattern": [
            {"label": "work_day", "duration": 7},
            {"label": "work_night", "duration": 7},
            {"label": "off", "duration": 14}
        ],
        "cycle_length": 28,
        "anchor_date": "2025-01-01",
        "anchor_cycle_day": 1,
        "is_active": True,
        "crew": "A",
        "description": "Test 28-day cycle"
    }


@pytest.fixture
def mock_commitment():
    """Mock commitment"""
    return {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "name": "Test Course",
        "type": "education",
        "status": "active",
        "priority": 1,
        "constraints_json": {
            "study_on": ["off", "work_day_evening"],
            "exclude": ["work_night"],
            "frequency": "weekly",
            "duration_hours": 2
        },
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
        "total_sessions": 52,
        "completed_sessions": 0,
        "color": "#2979FF",
        "source": "manual"
    }


@pytest.fixture
def mock_calendar_days():
    """Mock calendar days for a month"""
    days = []
    for i in range(1, 32):
        day_num = i % 28  # 28-day cycle
        if day_num <= 7:
            work_type = "work_day"
        elif day_num <= 14:
            work_type = "work_night"
        else:
            work_type = "off"
        
        days.append({
            "id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "date": f"2025-01-{i:02d}",
            "cycle_id": str(uuid.uuid4()),
            "cycle_day": day_num or 28,
            "work_type": work_type,
            "state_json": {
                "available_hours": 4.0 if work_type == "off" else 2.0,
                "commitments": [],
                "tags": []
            }
        })
    return days


@pytest.fixture
def mock_constraint():
    """Mock constraint"""
    return {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "name": "No study on night shifts",
        "description": "Study not allowed during night shifts",
        "is_active": True,
        "is_system": True,
        "rule": {
            "type": "no_activity_on",
            "activity": "study",
            "work_types": ["work_night"]
        },
        "weight": 100
    }


@pytest.fixture
def mock_mutation():
    """Mock mutation record"""
    return {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "status": "proposed",
        "intent": "add_commitment",
        "proposed_diff": {
            "changes": [{"type": "add_commitment", "commitment": {"name": "Test"}}],
            "summary": "Add test commitment"
        },
        "explanation": "This will add a new commitment",
        "proposed_at": datetime.utcnow().isoformat(),
        "triggered_by": "user"
    }


@pytest.fixture
def mock_leave_block():
    """Mock leave block"""
    return {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "name": "Annual Leave",
        "start_date": "2025-06-01",
        "end_date": "2025-06-14",
        "effects": {"work": "suspended", "available_time": "increased"},
        "notes": "Summer vacation"
    }


# ==========================================
# Database Mock Fixture
# ==========================================

@pytest.fixture
def mock_database():
    """Mock database with all methods"""
    db = MagicMock()
    
    # User methods
    db.get_user_by_auth_id = AsyncMock(return_value=None)
    db.get_user_by_id = AsyncMock(return_value=None)
    db.create_user = AsyncMock(return_value=None)
    db.update_user = AsyncMock(return_value=None)
    db.complete_onboarding = AsyncMock(return_value=None)
    
    # Cycle methods
    db.get_cycles = AsyncMock(return_value=[])
    db.get_active_cycle = AsyncMock(return_value=None)
    db.create_cycle = AsyncMock(return_value=None)
    db.update_cycle = AsyncMock(return_value=None)
    db.delete_cycle = AsyncMock(return_value=True)
    
    # Commitment methods
    db.get_commitments = AsyncMock(return_value=[])
    db.get_active_commitments = AsyncMock(return_value=[])
    db.get_commitment = AsyncMock(return_value=None)
    db.create_commitment = AsyncMock(return_value=None)
    db.update_commitment = AsyncMock(return_value=None)
    db.delete_commitment = AsyncMock(return_value=True)
    
    # Constraint methods
    db.get_constraints = AsyncMock(return_value=[])
    db.get_active_constraints = AsyncMock(return_value=[])
    db.create_constraint = AsyncMock(return_value=None)
    db.create_default_constraints = AsyncMock(return_value=[])
    db.update_constraint = AsyncMock(return_value=None)
    db.delete_constraint = AsyncMock(return_value=True)
    
    # Calendar methods
    db.get_calendar_days = AsyncMock(return_value=[])
    db.get_calendar_day = AsyncMock(return_value=None)
    db.upsert_calendar_days = AsyncMock(return_value=[])
    db.delete_calendar_days = AsyncMock(return_value=True)
    
    # Leave block methods
    db.get_leave_blocks = AsyncMock(return_value=[])
    db.create_leave_block = AsyncMock(return_value=None)
    db.delete_leave_block = AsyncMock(return_value=True)
    
    # Mutation methods
    db.get_mutations = AsyncMock(return_value=[])
    db.get_pending_mutations = AsyncMock(return_value=[])
    db.get_mutation = AsyncMock(return_value=None)
    db.create_mutation = AsyncMock(return_value=None)
    db.update_mutation = AsyncMock(return_value=None)
    
    # Snapshot methods
    db.create_snapshot = AsyncMock(return_value=None)
    db.get_snapshots = AsyncMock(return_value=[])
    db.get_snapshot_by_hash = AsyncMock(return_value=None)
    
    # Subscription methods
    db.get_subscription = AsyncMock(return_value=None)
    
    return db


# ==========================================
# App Test Client Fixtures
# ==========================================

@pytest.fixture
def app():
    """Get the FastAPI app"""
    from app.main import create_app
    return create_app()


@pytest.fixture
def client(app):
    """Sync test client"""
    return TestClient(app)


@pytest.fixture
async def async_client(app):
    """Async test client"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ==========================================
# Auth Mock Fixtures
# ==========================================

@pytest.fixture
def mock_auth_header():
    """Mock valid auth header"""
    return {"Authorization": "Bearer valid-test-token"}


@pytest.fixture
def mock_invalid_auth_header():
    """Mock invalid auth header"""
    return {"Authorization": "Bearer invalid-token"}


@pytest.fixture
def mock_expired_auth_header():
    """Mock expired auth header"""
    return {"Authorization": "Bearer expired-token"}
