"""
Watchman Admin Routes
Admin-only endpoints for dashboard metrics and user management
"""

from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from app.config import get_settings
from app.database import Database
from app.middleware.auth import get_current_user

router = APIRouter()
settings = get_settings()


def require_admin(user: dict = Depends(get_current_user)):
    """Middleware to require admin tier"""
    if user.get("tier") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.get("/stats/overview")
async def get_admin_overview(user: dict = Depends(require_admin)):
    """
    Get comprehensive admin dashboard stats.
    Returns 30+ metrics organized by category.
    """
    db = Database(use_admin=True)
    
    try:
        # Get all users for calculations
        all_users_result = db.client.table("users").select("*").execute()
        all_users = all_users_result.data or []
        
        # Get all payments
        all_payments_result = db.client.table("payments").select("*").execute()
        all_payments = all_payments_result.data or []
        
        # Calculate time boundaries
        now = datetime.utcnow()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = today - timedelta(days=1)
        week_ago = today - timedelta(days=7)
        month_ago = today - timedelta(days=30)
        quarter_ago = today - timedelta(days=90)
        
        # ===== USER METRICS =====
        total_users = len(all_users)
        
        # Users by tier
        free_users = len([u for u in all_users if u.get("tier") == "free"])
        pro_users = len([u for u in all_users if u.get("tier") == "pro"])
        admin_users = len([u for u in all_users if u.get("tier") == "admin"])
        
        # Onboarding metrics
        onboarded_users = len([u for u in all_users if u.get("onboarding_completed")])
        not_onboarded = total_users - onboarded_users
        onboarding_rate = round((onboarded_users / total_users * 100), 1) if total_users > 0 else 0
        
        # Time-based signups
        def parse_date(date_str):
            if not date_str:
                return None
            try:
                if isinstance(date_str, datetime):
                    return date_str
                return datetime.fromisoformat(date_str.replace('Z', '+00:00').replace('+00:00', ''))
            except:
                return None
        
        signups_today = len([u for u in all_users if parse_date(u.get("created_at")) and parse_date(u.get("created_at")).date() >= today.date()])
        signups_yesterday = len([u for u in all_users if parse_date(u.get("created_at")) and yesterday.date() <= parse_date(u.get("created_at")).date() < today.date()])
        signups_this_week = len([u for u in all_users if parse_date(u.get("created_at")) and parse_date(u.get("created_at")) >= week_ago])
        signups_this_month = len([u for u in all_users if parse_date(u.get("created_at")) and parse_date(u.get("created_at")) >= month_ago])
        signups_this_quarter = len([u for u in all_users if parse_date(u.get("created_at")) and parse_date(u.get("created_at")) >= quarter_ago])
        
        # Activity metrics
        def parse_last_active(date_str):
            return parse_date(date_str)
        
        active_today = len([u for u in all_users if parse_last_active(u.get("last_active")) and parse_last_active(u.get("last_active")).date() >= today.date()])
        active_this_week = len([u for u in all_users if parse_last_active(u.get("last_active")) and parse_last_active(u.get("last_active")) >= week_ago])
        active_this_month = len([u for u in all_users if parse_last_active(u.get("last_active")) and parse_last_active(u.get("last_active")) >= month_ago])
        
        # Dormant users (no activity in 30 days)
        dormant_users = len([u for u in all_users if not parse_last_active(u.get("last_active")) or parse_last_active(u.get("last_active")) < month_ago])
        
        # ===== GEOGRAPHIC METRICS =====
        # Users by country
        countries = {}
        for u in all_users:
            country = u.get("country") or "Unknown"
            countries[country] = countries.get(country, 0) + 1
        
        top_countries = sorted(countries.items(), key=lambda x: x[1], reverse=True)[:10]
        
        # ===== REVENUE METRICS =====
        total_revenue_usd = sum(float(p.get("amount", 0)) for p in all_payments if p.get("status") == "paid")
        
        # Revenue by period
        revenue_this_month = sum(
            float(p.get("amount", 0)) 
            for p in all_payments 
            if p.get("status") == "paid" and parse_date(p.get("created_at")) and parse_date(p.get("created_at")) >= month_ago
        )
        
        revenue_this_quarter = sum(
            float(p.get("amount", 0)) 
            for p in all_payments 
            if p.get("status") == "paid" and parse_date(p.get("created_at")) and parse_date(p.get("created_at")) >= quarter_ago
        )
        
        # Average revenue per paying user (ARPPU)
        paying_count = pro_users + admin_users
        arppu = round(total_revenue_usd / paying_count, 2) if paying_count > 0 else 0
        
        # MRR (Monthly Recurring Revenue) - count pro AND admin (paid admins)
        paying_users = pro_users + admin_users  # Both pro and admin are paying
        mrr = paying_users * 12  # $12/month per paying user
        
        # ARR (Annual Recurring Revenue)
        arr = mrr * 12
        
        # ===== CONVERSION METRICS =====
        conversion_rate = round((pro_users / total_users * 100), 2) if total_users > 0 else 0
        
        # Conversion from onboarded users
        onboarded_and_paid = len([u for u in all_users if u.get("onboarding_completed") and u.get("tier") == "pro"])
        onboarded_conversion = round((onboarded_and_paid / onboarded_users * 100), 2) if onboarded_users > 0 else 0
        
        # ===== ENGAGEMENT METRICS =====
        # Get chat message count
        try:
            chat_result = db.client.table("chat_messages").select("id", count="exact").execute()
            total_chat_messages = chat_result.count or 0
        except:
            total_chat_messages = 0
        
        # Get commitments count
        try:
            commitments_result = db.client.table("master_settings").select("commitments").execute()
            total_commitments = sum(len(ms.get("commitments", [])) for ms in (commitments_result.data or []))
        except:
            total_commitments = 0
        
        # Get incidents count
        try:
            incidents_result = db.client.table("incidents").select("id", count="exact").execute()
            total_incidents = incidents_result.count or 0
        except:
            total_incidents = 0
        
        # ===== GROWTH METRICS =====
        # Week over week growth
        signups_prev_week = len([u for u in all_users if parse_date(u.get("created_at")) and week_ago - timedelta(days=7) <= parse_date(u.get("created_at")) < week_ago])
        wow_growth = round(((signups_this_week - signups_prev_week) / signups_prev_week * 100), 1) if signups_prev_week > 0 else 0
        
        # Month over month growth
        two_months_ago = today - timedelta(days=60)
        signups_prev_month = len([u for u in all_users if parse_date(u.get("created_at")) and two_months_ago <= parse_date(u.get("created_at")) < month_ago])
        mom_growth = round(((signups_this_month - signups_prev_month) / signups_prev_month * 100), 1) if signups_prev_month > 0 else 0
        
        # ===== RECENT USERS LIST =====
        recent_users = sorted(all_users, key=lambda x: x.get("created_at", ""), reverse=True)[:20]
        recent_users_formatted = [
            {
                "id": u.get("id"),
                "email": u.get("email"),
                "name": u.get("name"),
                "tier": u.get("tier"),
                "country": u.get("country"),
                "country_code": u.get("country_code"),
                "city": u.get("city"),
                "onboarding_completed": u.get("onboarding_completed"),
                "created_at": u.get("created_at"),
                "last_active": u.get("last_active"),
            }
            for u in recent_users
        ]
        
        # ===== RETURN ALL METRICS =====
        return {
            "generated_at": now.isoformat(),
            
            # User Overview
            "users": {
                "total": total_users,
                "free": free_users,
                "pro": pro_users,
                "admin": admin_users,
                "paying": pro_users + admin_users,  # Pro + Admin = Paying
                "onboarded": onboarded_users,
                "not_onboarded": not_onboarded,
                "onboarding_rate": onboarding_rate,
                "dormant": dormant_users,
            },
            
            # Signups
            "signups": {
                "today": signups_today,
                "yesterday": signups_yesterday,
                "this_week": signups_this_week,
                "this_month": signups_this_month,
                "this_quarter": signups_this_quarter,
            },
            
            # Activity
            "activity": {
                "active_today": active_today,
                "active_this_week": active_this_week,
                "active_this_month": active_this_month,
                "dau": active_today,  # Daily Active Users
                "wau": active_this_week,  # Weekly Active Users
                "mau": active_this_month,  # Monthly Active Users
            },
            
            # Revenue
            "revenue": {
                "total_usd": round(total_revenue_usd, 2),
                "this_month_usd": round(revenue_this_month, 2),
                "this_quarter_usd": round(revenue_this_quarter, 2),
                "mrr": mrr,
                "arr": arr,
                "arppu": arppu,
            },
            
            # Conversion
            "conversion": {
                "overall_rate": conversion_rate,
                "onboarded_rate": onboarded_conversion,
                "total_payments": len([p for p in all_payments if p.get("status") == "paid"]),
            },
            
            # Growth
            "growth": {
                "wow_percent": wow_growth,
                "mom_percent": mom_growth,
            },
            
            # Engagement
            "engagement": {
                "total_chat_messages": total_chat_messages,
                "total_commitments": total_commitments,
                "total_incidents": total_incidents,
                "avg_messages_per_user": round(total_chat_messages / total_users, 1) if total_users > 0 else 0,
            },
            
            # Geographic
            "geography": {
                "top_countries": [{"country": c[0], "count": c[1]} for c in top_countries],
                "unique_countries": len([c for c in countries if c != "Unknown"]),
            },
            
            # Recent Users
            "recent_users": recent_users_formatted,
        }
        
    except Exception as e:
        logger.error(f"[ADMIN] Error getting stats: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get admin stats: {str(e)}")


@router.get("/users")
async def get_all_users(
    user: dict = Depends(require_admin),
    limit: int = 100,
    offset: int = 0,
    tier: Optional[str] = None,
    country: Optional[str] = None,
):
    """Get paginated list of all users with filters"""
    db = Database(use_admin=True)
    
    try:
        query = db.client.table("users").select("*")
        
        if tier:
            query = query.eq("tier", tier)
        if country:
            query = query.eq("country", country)
        
        result = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
        
        # Get total count
        count_query = db.client.table("users").select("id", count="exact")
        if tier:
            count_query = count_query.eq("tier", tier)
        if country:
            count_query = count_query.eq("country", country)
        count_result = count_query.execute()
        
        return {
            "users": result.data or [],
            "total": count_result.count or 0,
            "limit": limit,
            "offset": offset,
        }
        
    except Exception as e:
        logger.error(f"[ADMIN] Error getting users: {e}")
        raise HTTPException(status_code=500, detail="Failed to get users")


@router.get("/users/{user_id}")
async def get_user_details(user_id: str, user: dict = Depends(require_admin)):
    """Get detailed info for a specific user"""
    db = Database(use_admin=True)
    
    try:
        # Get user
        user_result = db.client.table("users").select("*").eq("id", user_id).single().execute()
        if not user_result.data:
            raise HTTPException(status_code=404, detail="User not found")
        
        user_data = user_result.data
        
        # Get user's payments
        payments_result = db.client.table("payments").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
        
        # Get user's chat messages count
        try:
            chat_result = db.client.table("chat_messages").select("id", count="exact").eq("user_id", user_id).execute()
            chat_count = chat_result.count or 0
        except:
            chat_count = 0
        
        # Get user's master settings
        try:
            settings_result = db.client.table("master_settings").select("*").eq("user_id", user_id).single().execute()
            settings_data = settings_result.data
        except:
            settings_data = None
        
        return {
            "user": user_data,
            "payments": payments_result.data or [],
            "chat_messages_count": chat_count,
            "master_settings": settings_data,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ADMIN] Error getting user details: {e}")
        raise HTTPException(status_code=500, detail="Failed to get user details")


@router.post("/users/{user_id}/update-tier")
async def update_user_tier(user_id: str, tier: str, admin: dict = Depends(require_admin)):
    """Manually update a user's tier"""
    if tier not in ["free", "pro", "admin"]:
        raise HTTPException(status_code=400, detail="Invalid tier. Must be: free, pro, or admin")
    
    db = Database(use_admin=True)
    
    try:
        result = db.client.table("users").update({"tier": tier}).eq("id", user_id).execute()
        
        if not result.data:
            raise HTTPException(status_code=404, detail="User not found")
        
        logger.info(f"[ADMIN] Admin {admin['id']} updated user {user_id} tier to {tier}")
        
        return {"success": True, "user": result.data[0]}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ADMIN] Error updating user tier: {e}")
        raise HTTPException(status_code=500, detail="Failed to update user tier")


@router.get("/payments")
async def get_all_payments(
    user: dict = Depends(require_admin),
    limit: int = 100,
    offset: int = 0,
):
    """Get paginated list of all payments"""
    db = Database(use_admin=True)
    
    try:
        result = db.client.table("payments").select("*, users(email, name)").order("created_at", desc=True).range(offset, offset + limit - 1).execute()
        
        count_result = db.client.table("payments").select("id", count="exact").execute()
        
        return {
            "payments": result.data or [],
            "total": count_result.count or 0,
            "limit": limit,
            "offset": offset,
        }
        
    except Exception as e:
        logger.error(f"[ADMIN] Error getting payments: {e}")
        raise HTTPException(status_code=500, detail="Failed to get payments")
