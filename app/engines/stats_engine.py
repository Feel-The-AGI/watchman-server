"""
Watchman Stats Engine
Computes statistics from calendar state.
Provides: work days, off days, leave days, study hours, load by month, peak weeks.
"""

from datetime import date, datetime, timedelta
from typing import List, Dict, Optional
from collections import defaultdict
from loguru import logger


class StatsEngine:
    """
    The Stats Engine provides derived views of calendar data.
    "Calendar is data. Statistics are views."
    """
    
    def __init__(self, user_id: str):
        self.user_id = user_id
    
    def compute_yearly_stats(
        self,
        calendar_days: List[Dict],
        year: int
    ) -> Dict:
        """
        Compute comprehensive statistics for a full year.
        
        Args:
            calendar_days: List of calendar day dictionaries
            year: The year to compute stats for
        
        Returns:
            Yearly statistics dictionary
        """
        # Filter to the specified year
        year_days = [
            d for d in calendar_days
            if self._get_year(d.get("date")) == year
        ]
        
        # Initialize counters
        total_work_days = 0
        total_work_nights = 0
        total_off_days = 0
        total_leave_days = 0
        total_study_hours = 0.0
        
        monthly_stats = defaultdict(lambda: {
            "work_days": 0,
            "work_nights": 0,
            "off_days": 0,
            "leave_days": 0,
            "study_hours": 0.0,
            "total_commitments": 0,
            "overload_days": 0
        })
        
        weekly_loads = defaultdict(float)
        overload_days = []
        
        for day in year_days:
            date_str = day.get("date", "")
            work_type = day.get("work_type", "off")
            state = day.get("state_json", {})
            
            # Get month key (e.g., "2026-01")
            month_key = date_str[:7] if isinstance(date_str, str) else date_str.strftime("%Y-%m")
            
            # Get week key
            week_key = self._get_week_key(date_str)
            
            # Count work types
            if work_type == "work_day":
                total_work_days += 1
                monthly_stats[month_key]["work_days"] += 1
            elif work_type == "work_night":
                total_work_nights += 1
                monthly_stats[month_key]["work_nights"] += 1
            elif work_type == "off":
                total_off_days += 1
                monthly_stats[month_key]["off_days"] += 1
            
            # Count leave days
            if state.get("is_leave", False):
                total_leave_days += 1
                monthly_stats[month_key]["leave_days"] += 1
            
            # Sum study hours
            commitments = state.get("commitments", [])
            day_study_hours = sum(
                c.get("hours", 0) for c in commitments
                if c.get("type") in ["study", "education"]
            )
            total_study_hours += day_study_hours
            monthly_stats[month_key]["study_hours"] += day_study_hours
            monthly_stats[month_key]["total_commitments"] += len(commitments)
            
            weekly_loads[week_key] += day_study_hours
            
            # Track overloaded days
            if state.get("is_overloaded", False):
                monthly_stats[month_key]["overload_days"] += 1
                overload_days.append(date_str)
        
        # Find peak weeks (top 5 by study hours)
        sorted_weeks = sorted(weekly_loads.items(), key=lambda x: x[1], reverse=True)
        peak_weeks = [w[0] for w in sorted_weeks[:5] if w[1] > 0]
        
        # Find zero-recovery spans (consecutive work days without study)
        zero_recovery_spans = self._find_zero_recovery_spans(year_days)
        
        # Format monthly breakdown
        monthly_breakdown = []
        for month in sorted(monthly_stats.keys()):
            stats = monthly_stats[month]
            monthly_breakdown.append({
                "month": month,
                **stats
            })
        
        return {
            "year": year,
            "total_work_days": total_work_days,
            "total_work_nights": total_work_nights,
            "total_off_days": total_off_days,
            "total_leave_days": total_leave_days,
            "total_study_hours": round(total_study_hours, 1),
            "total_days": len(year_days),
            "peak_weeks": peak_weeks,
            "zero_recovery_spans": zero_recovery_spans,
            "overload_days_count": len(overload_days),
            "monthly_breakdown": monthly_breakdown
        }
    
    def compute_monthly_stats(
        self,
        calendar_days: List[Dict],
        year: int,
        month: int
    ) -> Dict:
        """
        Compute statistics for a specific month.
        
        Args:
            calendar_days: List of calendar day dictionaries
            year: The year
            month: The month (1-12)
        
        Returns:
            Monthly statistics dictionary
        """
        month_prefix = f"{year}-{month:02d}"
        
        month_days = [
            d for d in calendar_days
            if self._get_month_prefix(d.get("date")) == month_prefix
        ]
        
        work_days = sum(1 for d in month_days if d.get("work_type") == "work_day")
        work_nights = sum(1 for d in month_days if d.get("work_type") == "work_night")
        off_days = sum(1 for d in month_days if d.get("work_type") == "off")
        leave_days = sum(1 for d in month_days if d.get("state_json", {}).get("is_leave"))
        
        study_hours = sum(
            sum(c.get("hours", 0) for c in d.get("state_json", {}).get("commitments", [])
                if c.get("type") in ["study", "education"])
            for d in month_days
        )
        
        total_commitments = sum(
            len(d.get("state_json", {}).get("commitments", []))
            for d in month_days
        )
        
        overload_days = sum(
            1 for d in month_days
            if d.get("state_json", {}).get("is_overloaded")
        )
        
        return {
            "month": month_prefix,
            "work_days": work_days,
            "work_nights": work_nights,
            "off_days": off_days,
            "leave_days": leave_days,
            "study_hours": round(study_hours, 1),
            "total_commitments": total_commitments,
            "overload_days": overload_days,
            "total_days": len(month_days)
        }
    
    def compute_dashboard_stats(
        self,
        calendar_days: List[Dict],
        commitments: List[Dict],
        mutations: List[Dict],
        leave_blocks: List[Dict]
    ) -> Dict:
        """
        Compute quick statistics for dashboard display.
        
        Args:
            calendar_days: Recent/upcoming calendar days
            commitments: User's commitments
            mutations: User's mutations (for pending count)
            leave_blocks: User's leave blocks
        
        Returns:
            Dashboard statistics dictionary
        """
        today = date.today()
        week_end = today + timedelta(days=7)
        
        # Filter to upcoming week
        upcoming_days = [
            d for d in calendar_days
            if self._date_in_range(d.get("date"), today, week_end)
        ]
        
        # Count upcoming work and off days
        upcoming_work = sum(
            1 for d in upcoming_days
            if d.get("work_type") in ["work_day", "work_night"]
        )
        upcoming_off = sum(
            1 for d in upcoming_days
            if d.get("work_type") == "off"
        )
        
        # Count active commitments
        active_commitments = sum(
            1 for c in commitments
            if c.get("status") == "active"
        )
        
        # Count pending proposals
        pending_proposals = sum(
            1 for m in mutations
            if m.get("status") == "proposed"
        )
        
        # Calculate this week's study hours
        this_week_hours = sum(
            sum(c.get("hours", 0) for c in d.get("state_json", {}).get("commitments", [])
                if c.get("type") in ["study", "education"])
            for d in upcoming_days
        )
        
        # Find next leave
        next_leave = None
        for leave in leave_blocks:
            start = leave.get("start_date")
            if isinstance(start, str):
                start_date = date.fromisoformat(start)
            else:
                start_date = start
            
            if start_date >= today:
                if next_leave is None or start_date < date.fromisoformat(next_leave["start_date"]):
                    next_leave = {
                        "name": leave.get("name", "Leave"),
                        "start_date": leave.get("start_date"),
                        "end_date": leave.get("end_date")
                    }
        
        return {
            "upcoming_work_days": upcoming_work,
            "upcoming_off_days": upcoming_off,
            "active_commitments": active_commitments,
            "pending_proposals": pending_proposals,
            "this_week_study_hours": round(this_week_hours, 1),
            "next_leave": next_leave
        }
    
    def compute_commitment_stats(
        self,
        commitments: List[Dict],
        calendar_days: List[Dict]
    ) -> List[Dict]:
        """
        Compute statistics for each commitment.
        
        Args:
            commitments: User's commitments
            calendar_days: Calendar days with commitment assignments
        
        Returns:
            List of commitment statistics
        """
        commitment_stats = []
        
        for commitment in commitments:
            commitment_id = commitment.get("id")
            
            # Count scheduled days
            scheduled_days = 0
            total_hours = 0.0
            
            for day in calendar_days:
                state = day.get("state_json", {})
                for c in state.get("commitments", []):
                    if c.get("commitment_id") == commitment_id:
                        scheduled_days += 1
                        total_hours += c.get("hours", 0)
            
            commitment_stats.append({
                "commitment_id": commitment_id,
                "name": commitment.get("name"),
                "type": commitment.get("type"),
                "status": commitment.get("status"),
                "scheduled_days": scheduled_days,
                "total_hours": round(total_hours, 1),
                "completed_sessions": commitment.get("completed_sessions", 0),
                "total_sessions": commitment.get("total_sessions")
            })
        
        return commitment_stats
    
    def compute_load_distribution(
        self,
        calendar_days: List[Dict]
    ) -> Dict:
        """
        Compute how load is distributed across day types.
        
        Args:
            calendar_days: Calendar days to analyze
        
        Returns:
            Load distribution dictionary
        """
        distribution = {
            "off_days": {"count": 0, "total_hours": 0.0, "avg_hours": 0.0},
            "work_day_evenings": {"count": 0, "total_hours": 0.0, "avg_hours": 0.0},
            "work_night": {"count": 0, "total_hours": 0.0, "avg_hours": 0.0}
        }
        
        for day in calendar_days:
            work_type = day.get("work_type", "off")
            state = day.get("state_json", {})
            used_hours = state.get("used_hours", 0)
            
            if work_type == "off":
                distribution["off_days"]["count"] += 1
                distribution["off_days"]["total_hours"] += used_hours
            elif work_type == "work_day":
                distribution["work_day_evenings"]["count"] += 1
                distribution["work_day_evenings"]["total_hours"] += used_hours
            elif work_type == "work_night":
                distribution["work_night"]["count"] += 1
                distribution["work_night"]["total_hours"] += used_hours
        
        # Calculate averages
        for key in distribution:
            count = distribution[key]["count"]
            if count > 0:
                distribution[key]["avg_hours"] = round(
                    distribution[key]["total_hours"] / count, 1
                )
        
        return distribution
    
    def _get_year(self, date_val) -> int:
        """Extract year from date string or date object"""
        if isinstance(date_val, str):
            return int(date_val[:4])
        elif isinstance(date_val, date):
            return date_val.year
        return 0
    
    def _get_month_prefix(self, date_val) -> str:
        """Extract month prefix (YYYY-MM) from date"""
        if isinstance(date_val, str):
            return date_val[:7]
        elif isinstance(date_val, date):
            return date_val.strftime("%Y-%m")
        return ""
    
    def _get_week_key(self, date_val) -> str:
        """Get week key (YYYY-Www) for a date"""
        if isinstance(date_val, str):
            date_obj = date.fromisoformat(date_val)
        else:
            date_obj = date_val
        
        iso_cal = date_obj.isocalendar()
        return f"{iso_cal[0]}-W{iso_cal[1]:02d}"
    
    def _date_in_range(self, date_val, start: date, end: date) -> bool:
        """Check if a date is in the given range"""
        if isinstance(date_val, str):
            date_obj = date.fromisoformat(date_val)
        else:
            date_obj = date_val
        
        return start <= date_obj <= end
    
    def _find_zero_recovery_spans(self, calendar_days: List[Dict]) -> List[Dict]:
        """Find spans of consecutive work days without study"""
        # Sort days by date
        sorted_days = sorted(calendar_days, key=lambda d: d.get("date", ""))
        
        spans = []
        current_span_start = None
        current_span_days = 0
        
        for day in sorted_days:
            work_type = day.get("work_type", "off")
            state = day.get("state_json", {})
            commitments = state.get("commitments", [])
            
            has_study = any(
                c.get("type") in ["study", "education"]
                for c in commitments
            )
            
            is_work = work_type in ["work_day", "work_night"]
            
            if is_work and not has_study:
                if current_span_start is None:
                    current_span_start = day.get("date")
                current_span_days += 1
            else:
                if current_span_days >= 5:  # Report spans of 5+ days
                    spans.append({
                        "start": current_span_start,
                        "days": current_span_days
                    })
                current_span_start = None
                current_span_days = 0
        
        # Don't forget the last span
        if current_span_days >= 5:
            spans.append({
                "start": current_span_start,
                "days": current_span_days
            })
        
        return spans


def create_stats_engine(user_id: str) -> StatsEngine:
    """Factory function to create a StatsEngine instance"""
    return StatsEngine(user_id)
