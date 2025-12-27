"""
Watchman Pydantic Models
Type-safe data structures for the entire application
"""

from datetime import date, datetime
from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, validator


# ==========================================
# ENUMS
# ==========================================

class UserTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ADMIN = "admin"


class UserRole(str, Enum):
    USER = "user"
    ADMIN = "admin"


class WorkType(str, Enum):
    WORK_DAY = "work_day"
    WORK_NIGHT = "work_night"
    OFF = "off"


class CommitmentType(str, Enum):
    WORK = "work"
    EDUCATION = "education"
    PERSONAL = "personal"
    LEAVE = "leave"
    STUDY = "study"
    SLEEP = "sleep"


class CommitmentStatus(str, Enum):
    ACTIVE = "active"
    QUEUED = "queued"
    COMPLETED = "completed"
    PAUSED = "paused"


class MutationStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ConstraintMode(str, Enum):
    BINARY = "binary"
    WEIGHTED = "weighted"


# ==========================================
# USER MODELS
# ==========================================

class UserSettings(BaseModel):
    """User-specific settings"""
    constraint_mode: ConstraintMode = ConstraintMode.BINARY
    weighted_mode_enabled: bool = False
    max_concurrent_commitments: int = 2
    notifications_email: bool = True
    notifications_whatsapp: bool = False
    theme: str = "dark"


class UserBase(BaseModel):
    """Base user model"""
    email: str
    name: str
    timezone: str = "UTC"


class UserCreate(UserBase):
    """User creation model"""
    auth_id: str


class User(UserBase):
    """Full user model"""
    id: str
    auth_id: str
    tier: UserTier = UserTier.FREE
    role: UserRole = UserRole.USER
    onboarding_completed: bool = False
    settings: UserSettings = Field(default_factory=UserSettings)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    """User update model"""
    name: Optional[str] = None
    timezone: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None


# ==========================================
# CYCLE MODELS
# ==========================================

class CycleBlock(BaseModel):
    """A single block in a rotation cycle"""
    label: WorkType
    duration: int = Field(gt=0, description="Duration in days")


class CycleAnchor(BaseModel):
    """Anchor point for cycle calculation"""
    date: date
    cycle_day: int = Field(ge=1, description="Which day of the cycle this date represents")


class CycleBase(BaseModel):
    """Base cycle model"""
    name: str = "Default Rotation"
    pattern: List[CycleBlock]
    anchor_date: date
    anchor_cycle_day: int = Field(ge=1)
    crew: Optional[str] = None
    description: Optional[str] = None
    
    @validator('anchor_cycle_day')
    def validate_anchor_cycle_day(cls, v, values):
        if 'pattern' in values:
            cycle_length = sum(block.duration for block in values['pattern'])
            if v > cycle_length:
                raise ValueError(f'anchor_cycle_day ({v}) cannot exceed cycle length ({cycle_length})')
        return v
    
    @property
    def cycle_length(self) -> int:
        return sum(block.duration for block in self.pattern)


class CycleCreate(CycleBase):
    """Cycle creation model"""
    user_id: str


class Cycle(CycleBase):
    """Full cycle model"""
    id: str
    user_id: str
    is_active: bool = True
    cycle_length: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CycleUpdate(BaseModel):
    """Cycle update model"""
    name: Optional[str] = None
    pattern: Optional[List[CycleBlock]] = None
    anchor_date: Optional[date] = None
    anchor_cycle_day: Optional[int] = None
    is_active: Optional[bool] = None
    crew: Optional[str] = None
    description: Optional[str] = None


# ==========================================
# CONSTRAINT MODELS
# ==========================================

class ConstraintRule(BaseModel):
    """Constraint rule definition"""
    type: str  # no_activity_on, max_concurrent, immutable, required_gap
    # Additional fields depend on type
    activity: Optional[str] = None
    work_types: Optional[List[str]] = None
    scope: Optional[str] = None
    value: Optional[int] = None
    after: Optional[str] = None
    hours: Optional[int] = None


class ConstraintBase(BaseModel):
    """Base constraint model"""
    name: str
    description: Optional[str] = None
    is_active: bool = True
    rule: Dict[str, Any]
    weight: int = 100


class ConstraintCreate(ConstraintBase):
    """Constraint creation model"""
    user_id: str
    is_system: bool = False


class Constraint(ConstraintBase):
    """Full constraint model"""
    id: str
    user_id: str
    is_system: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ConstraintUpdate(BaseModel):
    """Constraint update model"""
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    rule: Optional[Dict[str, Any]] = None
    weight: Optional[int] = None


# ==========================================
# COMMITMENT MODELS
# ==========================================

class CommitmentConstraints(BaseModel):
    """Constraints specific to a commitment"""
    study_on: Optional[List[str]] = None  # ["off", "work_day_evening"]
    exclude: Optional[List[str]] = None  # ["work_night"]
    frequency: Optional[str] = None  # "weekly", "daily", "bi-weekly"
    duration_hours: Optional[float] = None


class CommitmentRecurrence(BaseModel):
    """Recurrence pattern for a commitment"""
    type: str  # "weekly", "daily", "monthly"
    days: Optional[List[int]] = None  # Day of week (0=Monday)
    time: Optional[str] = None  # "18:00"


class CommitmentBase(BaseModel):
    """Base commitment model"""
    name: str
    type: CommitmentType
    priority: int = 1
    constraints_json: Optional[Dict[str, Any]] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    recurrence: Optional[Dict[str, Any]] = None
    total_sessions: Optional[int] = None
    color: Optional[str] = None
    icon: Optional[str] = None
    notes: Optional[str] = None


class CommitmentCreate(CommitmentBase):
    """Commitment creation model"""
    user_id: str
    status: CommitmentStatus = CommitmentStatus.ACTIVE
    source: str = "manual"
    source_text: Optional[str] = None


class Commitment(CommitmentBase):
    """Full commitment model"""
    id: str
    user_id: str
    status: CommitmentStatus = CommitmentStatus.ACTIVE
    completed_sessions: int = 0
    source: str = "manual"
    source_text: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CommitmentUpdate(BaseModel):
    """Commitment update model"""
    name: Optional[str] = None
    type: Optional[CommitmentType] = None
    status: Optional[CommitmentStatus] = None
    priority: Optional[int] = None
    constraints_json: Optional[Dict[str, Any]] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    recurrence: Optional[Dict[str, Any]] = None
    completed_sessions: Optional[int] = None
    color: Optional[str] = None
    notes: Optional[str] = None


# ==========================================
# LEAVE BLOCK MODELS
# ==========================================

class LeaveEffects(BaseModel):
    """Effects of a leave block on constraints"""
    work: str = "suspended"  # "suspended", "modified"
    available_time: str = "increased"  # "increased", "unchanged"


class LeaveBlockBase(BaseModel):
    """Base leave block model"""
    name: str = "Leave"
    start_date: date
    end_date: date
    effects: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None
    
    @validator('end_date')
    def validate_date_range(cls, v, values):
        if 'start_date' in values and v < values['start_date']:
            raise ValueError('end_date must be after start_date')
        return v


class LeaveBlockCreate(LeaveBlockBase):
    """Leave block creation model"""
    user_id: str


class LeaveBlock(LeaveBlockBase):
    """Full leave block model"""
    id: str
    user_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class LeaveBlockUpdate(BaseModel):
    """Leave block update model"""
    name: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    effects: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None


# ==========================================
# CALENDAR DAY MODELS
# ==========================================

class DayCommitment(BaseModel):
    """A commitment scheduled on a specific day"""
    commitment_id: str
    name: str
    type: CommitmentType
    hours: float = 0
    time_slot: Optional[str] = None  # "morning", "afternoon", "evening"
    is_preview: bool = False


class DayState(BaseModel):
    """State of a calendar day"""
    commitments: List[DayCommitment] = []
    available_hours: float = 0
    used_hours: float = 0
    is_overloaded: bool = False
    is_leave: bool = False
    tags: List[str] = []


class CalendarDayBase(BaseModel):
    """Base calendar day model"""
    date: date
    cycle_day: Optional[int] = None
    work_type: WorkType
    state_json: Optional[Dict[str, Any]] = None


class CalendarDayCreate(CalendarDayBase):
    """Calendar day creation model"""
    user_id: str
    cycle_id: Optional[str] = None


class CalendarDay(CalendarDayBase):
    """Full calendar day model"""
    id: str
    user_id: str
    cycle_id: Optional[str] = None
    is_work_day: bool
    is_off_day: bool
    is_night_shift: bool
    version: int = 1
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ==========================================
# MUTATION MODELS
# ==========================================

class MutationChange(BaseModel):
    """A single change in a mutation"""
    type: str  # "add_commitment", "remove_commitment", "update_commitment", "add_leave"
    target_id: Optional[str] = None
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None


class MutationDiff(BaseModel):
    """Diff of changes in a mutation"""
    changes: List[MutationChange] = []
    affected_dates: List[str] = []
    summary: str = ""


class ConstraintViolation(BaseModel):
    """A constraint violation"""
    constraint_id: str
    constraint_name: str
    reason: str
    severity: str = "error"  # "error", "warning"


class MutationAlternative(BaseModel):
    """An alternative proposal when mutation fails"""
    id: str
    description: str
    changes: List[MutationChange]
    is_valid: bool = True


class MutationBase(BaseModel):
    """Base mutation model"""
    intent: str
    scope_start: Optional[date] = None
    scope_end: Optional[date] = None
    proposed_diff: Dict[str, Any]
    explanation: Optional[str] = None


class MutationCreate(MutationBase):
    """Mutation creation model"""
    user_id: str
    is_alternative: bool = False
    parent_mutation_id: Optional[str] = None
    triggered_by: str = "user"
    source_text: Optional[str] = None


class Mutation(MutationBase):
    """Full mutation model"""
    id: str
    user_id: str
    status: MutationStatus = MutationStatus.PROPOSED
    is_alternative: bool = False
    parent_mutation_id: Optional[str] = None
    failure_reasons: Optional[Dict[str, Any]] = None
    alternatives: Optional[List[Dict[str, Any]]] = None
    violations: Optional[List[Dict[str, Any]]] = None
    previous_state_hash: Optional[str] = None
    new_state_hash: Optional[str] = None
    proposed_at: datetime
    reviewed_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None
    triggered_by: str = "user"
    source_text: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class MutationReview(BaseModel):
    """Model for reviewing a mutation"""
    action: str  # "approve", "reject"
    reason: Optional[str] = None
    selected_alternative_id: Optional[str] = None


# ==========================================
# DAILY LOG MODELS
# ==========================================

class DailyLogBase(BaseModel):
    """Base daily log model"""
    date: date
    note: str
    actual_hours: Optional[float] = None
    overtime_hours: Optional[float] = None


class DailyLogCreate(DailyLogBase):
    """Daily log creation model"""
    user_id: str


class DailyLog(DailyLogBase):
    """Full daily log model"""
    id: str
    user_id: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class DailyLogUpdate(BaseModel):
    """Daily log update model"""
    note: Optional[str] = None
    actual_hours: Optional[float] = None
    overtime_hours: Optional[float] = None


# ==========================================
# INCIDENT MODELS
# ==========================================

class IncidentType(str, Enum):
    OVERTIME = "overtime"
    SAFETY = "safety"
    EQUIPMENT = "equipment"
    HARASSMENT = "harassment"
    INJURY = "injury"
    POLICY_VIOLATION = "policy_violation"
    OTHER = "other"


class IncidentSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentBase(BaseModel):
    """Base incident model"""
    date: date
    type: IncidentType
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    title: str
    description: str
    reported_to: Optional[str] = None
    witnesses: Optional[str] = None
    outcome: Optional[str] = None


class IncidentCreate(IncidentBase):
    """Incident creation model"""
    user_id: str


class Incident(IncidentBase):
    """Full incident model"""
    id: str
    user_id: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class IncidentUpdate(BaseModel):
    """Incident update model"""
    type: Optional[IncidentType] = None
    severity: Optional[IncidentSeverity] = None
    title: Optional[str] = None
    description: Optional[str] = None
    reported_to: Optional[str] = None
    witnesses: Optional[str] = None
    outcome: Optional[str] = None


class IncidentStats(BaseModel):
    """Statistics for incidents"""
    total_count: int = 0
    by_type: Dict[str, int] = {}
    by_severity: Dict[str, int] = {}
    by_month: Dict[str, int] = {}


# ==========================================
# STATISTICS MODELS
# ==========================================

class MonthlyStats(BaseModel):
    """Statistics for a single month"""
    month: str  # "2026-01"
    work_days: int = 0
    work_nights: int = 0
    off_days: int = 0
    leave_days: int = 0
    study_hours: float = 0
    total_commitments: int = 0
    overload_days: int = 0


class YearlyStats(BaseModel):
    """Statistics for a full year"""
    year: int
    total_work_days: int = 0
    total_work_nights: int = 0
    total_off_days: int = 0
    total_leave_days: int = 0
    total_study_hours: float = 0
    peak_weeks: List[str] = []
    zero_recovery_spans: List[Dict[str, str]] = []
    monthly_breakdown: List[MonthlyStats] = []


class DashboardStats(BaseModel):
    """Quick stats for dashboard display"""
    upcoming_work_days: int = 0
    upcoming_off_days: int = 0
    active_commitments: int = 0
    pending_proposals: int = 0
    this_week_study_hours: float = 0
    next_leave: Optional[Dict[str, Any]] = None


# ==========================================
# PROPOSAL MODELS (LLM Integration)
# ==========================================

class ProposalRequest(BaseModel):
    """Request to parse unstructured input"""
    text: str
    context: Optional[str] = None  # Additional context about what user wants


class ParsedProposal(BaseModel):
    """Result of LLM parsing"""
    intent: str
    confidence: float
    extracted_data: Dict[str, Any]
    explanation: str
    suggested_changes: List[Dict[str, Any]]
    warnings: List[str] = []


class ProposalPreview(BaseModel):
    """Preview of what a proposal would do"""
    is_valid: bool
    mutation: Optional[MutationBase] = None
    violations: List[ConstraintViolation] = []
    alternatives: List[MutationAlternative] = []
    explanation: str
    affected_dates: List[str] = []
    stats_impact: Optional[Dict[str, Any]] = None


# ==========================================
# API RESPONSE MODELS
# ==========================================

class APIResponse(BaseModel):
    """Standard API response wrapper"""
    success: bool = True
    message: Optional[str] = None
    data: Optional[Any] = None
    errors: Optional[List[str]] = None


class PaginatedResponse(BaseModel):
    """Paginated response wrapper"""
    items: List[Any]
    total: int
    page: int
    page_size: int
    has_more: bool
